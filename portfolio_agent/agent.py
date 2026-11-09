"""
Portfolio Agent orchestration layer for PL Capital AI Portfolio Analytics Agent.
Routes natural-language questions to the appropriate analytical tool
(SQLQueryTool or ExposureCalculator) and returns structured responses.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Union

from .database import PortfolioDatabase
from .answer_verifier import AnswerVerifier
from .financial_verifier import FinancialVerifier, apply_verification, unavailable_verification
from .hybrid_tool import HybridAnalysisTool, TOOL_NAME as HYBRID_TOOL, is_hybrid_question
from .scenario_tool import TOOL_NAME as SCENARIO_TOOL, ScenarioTool, is_scenario_question, parse_scenario_request
from .scenario_verifier import ScenarioVerifier
from .intent_classifier import (
    Intent,
    IntentClassifierConfig,
    IntentClassifierUnavailable,
    LocalIntentClassifier,
    build_default_classifier,
)
from .tools import SQLQueryTool, ExposureCalculator

logger = logging.getLogger(__name__)

# Keywords that signal a sector-exposure question
_EXPOSURE_KEYWORDS: List[str] = [
    "sector exposure",
    "exposure breakdown",
    "sector allocation",
    "sector distribution",
    "sector mix",
    "sector weight",
    "exposure for",
    "show exposure",
    "calculate exposure",
    "calculate the sector exposure",
    "sector exposures",
    "percentage is invested in",
    "percentage invested in",
]

# Phrases that should NOT be routed to exposure. These signal the Q7-style
# cross-portfolio market-value calculation (quantity * current_price), which is a
# different metric from weight-based equity sector exposure and belongs to the SQL tool.
_EXPOSURE_EXCLUSIONS: List[str] = [
    "for each portfolio",
    "each portfolio",
    "by portfolio",
    "per portfolio",
    "across portfolios",
    "across all portfolios",
    "total portfolio value",
    "market value",
    "tech_percentage",
]

# --- Conversational intents (handled before any tool routing) -------------------------
# Only a message that is *entirely* a greeting / acknowledgement / goodbye matches (after
# lower-casing and stripping punctuation). "hi, show me the top holdings" or "hello world"
# do not match, so they keep their normal routing and unsupported-question handling.
_GREETING_RE = re.compile(
    r"^(?:(?:hi+|hello+|hey+|hiya|howdy|greetings|good (?:morning|afternoon|evening))"
    r"(?: (?:there|team|everyone|all))?(?: how are you(?: doing)?(?: today)?)?"
    r"|how are you(?: doing)?(?: today)?)$"
)
_THANKS_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|great|cool|nice|perfect|awesome|got it|understood|sounds good|alright)(?: (?:thanks|thank you))?"
    r"|(?:thanks|thank you|thx|ty|many thanks|cheers)(?: (?:a lot|so much|very much|a ton))?(?: (?:again|mate))?"
    r"|(?:great|perfect|awesome|cool|nice) (?:thanks|thank you))$"
)
_GOODBYE_RE = re.compile(r"^(?:bye+|goodbye|good ?night|see you(?: later| soon)?|take care)$")

CONVERSATION_EXAMPLE_QUESTIONS: List[str] = [
    "How many portfolios do we have in total?",
    "What are the names of all active portfolios?",
    "Show me the top 5 holdings by cost basis in the Growth Equity Fund",
    "What are the sector exposures for the Tech Innovation Fund?",
]

_CONVERSATION_MESSAGES: Dict[str, str] = {
    "greeting": (
        "Hello! I'm the PL Capital portfolio analytics assistant. I can answer questions about "
        "portfolios, holdings, securities and sectors, and calculate sector exposure. "
        "Try one of these:"
    ),
    "thanks": "You're welcome! Ask me another portfolio question whenever you're ready, for example:",
    "goodbye": "Goodbye! Come back any time you have a question about your portfolios.",
    "identity": (
        "I'm the PL Capital Portfolio Analytics Agent, a prototype assistant for exploring a sample portfolio "
        "dataset. I turn your questions into read-only database queries and calculate equity sector exposure "
        "with a deterministic calculator. I'm not a person, and nothing I say is investment advice."
    ),
    "capabilities": "Here is what I can do with the sample portfolio dataset, and a few questions to start with:",
    "clarification": (
        "I'm not sure what you'd like to know. Could you name the portfolio, sector or measure you mean? "
        "For example:"
    ),
    "unsupported": (
        "That's outside what I can help with. I answer questions about the sample portfolios, their holdings, "
        "securities and sectors, and I can calculate sector exposure. For example:"
    ),
}

# Actual implemented features, shown for "what can you do?". Keep in step with the tools.
_CAPABILITY_DETAILS: List[str] = [
    "Portfolio facts: how many portfolios there are, which are active, and total AUM for a target risk level.",
    "Holdings and securities: top holdings by cost basis for a portfolio, securities in a sector, and the average "
    "price per sector.",
    "Diversification: portfolios holding more than five sectors, and the Technology share of each portfolio's value.",
    "Sector exposure: a deterministic, equity-only sector breakdown for a named portfolio, with charts.",
    "Every database answer shows the read-only SQL that ran and its bound parameters. I cannot change data, "
    "and I do not give investment advice.",
]
_CAPABILITY_MODE_GEMINI = (
    "Questions are translated to SQL by Gemini when it is available, with a safe rule-based fallback."
)
_CAPABILITY_MODE_RULES = (
    "This instance uses the rule-based SQL generator only, so database questions work best when they match the "
    "topics above."
)

# Historical / descriptive questions about STORED performance and risk data (portfolio_performance, risk_metrics). They are
# answered from the database by the SQL tool; a classifier hint towards "sector exposure" must not divert them.
_HISTORICAL_RISK_RE = re.compile(
    r"\b(?:max(?:imum)?\s+drawdowns?|drawdowns?|volatilit(?:y|ies)|sharpe|sortino|c?var|value[\s-]at[\s-]risk|tracking\s+error|information\s+ratio|"
    r"beta|correlation|returns?|performance|nav|historical(?:ly)?|history|year[\s-]to[\s-]date|ytd|since\s+inception|"
    r"(?:last|previous|prior|past)\s+(?:\d+\s+)?(?:years?|quarters?|months?|weeks?|days?)|in\s+(?:19|20)\d{2})\b",
    re.IGNORECASE,
)

_UNSET: Any = object()  # sentinel: "read the intent-classifier setting from the environment"

_OUT_OF_SCOPE_MESSAGE = (
    "That's outside what I can help with. I answer questions about the sample portfolios, their holdings, "
    "securities and sectors, and I can calculate sector exposure."
)


class PortfolioAgent:
    """
    Orchestration layer for the PL Capital AI Portfolio Analytics Agent.

    Responsibilities:
    1. Accept natural-language questions.
    2. Validate user input.
    3. Detect sector-exposure intent.
    4. Extract or resolve portfolio identifiers/names.
    5. Route exposure questions to ExposureCalculator.
    6. Route general portfolio questions to SQLQueryTool.
    7. Return a consistent structured response.
    8. Handle tool failures gracefully.
    9. Handle unsupported questions.
    10. Preserve tool execution metadata.
    """

    def __init__(
        self,
        db: Optional[PortfolioDatabase] = None,
        sql_tool: Optional[SQLQueryTool] = None,
        exposure_tool: Optional[ExposureCalculator] = None,
        intent_classifier: Any = _UNSET,
        financial_verifier: Any = _UNSET,
        answer_verifier: Any = _UNSET,
        scenario_verifier: Any = _UNSET,
    ) -> None:
        """
        ``intent_classifier``: a LocalIntentClassifier to use, ``None`` to disable semantic intent
        classification (legacy routing), or omitted to follow the environment
        (PORTFOLIO_INTENT_CLASSIFIER=on|off; on by default). Nothing is loaded until first use.
        """
        self.db = db or PortfolioDatabase()
        self.sql_tool = sql_tool or SQLQueryTool(db=self.db)
        self.exposure_tool = exposure_tool or ExposureCalculator(db=self.db)
        # Independent recomputation of every exposure result (portfolio_agent/financial_verifier.py). None means no
        # verifier is available: exposure figures are then withheld (status 'unverified'), never shown unchecked.
        self.financial_verifier: Optional[FinancialVerifier] = (
            FinancialVerifier(self.db) if financial_verifier is _UNSET else financial_verifier
        )
        # Final-response verification (portfolio_agent/answer_verifier.py): consistency of the response with its
        # structured tool evidence. None disables it (debugging / tests that use fake tool results).
        self.answer_verifier: Optional[AnswerVerifier] = AnswerVerifier() if answer_verifier is _UNSET else answer_verifier
        # Independent verification of scenario / stress-test results (portfolio_agent/scenario_verifier.py). None means
        # no verifier is available: scenario figures are then withheld (status 'unverified'), never shown unchecked.
        self.scenario_verifier: Optional[ScenarioVerifier] = ScenarioVerifier(self.db) if scenario_verifier is _UNSET else scenario_verifier
        self.intent_classifier: Optional[LocalIntentClassifier] = (
            build_default_classifier() if intent_classifier is _UNSET else intent_classifier
        )
        self._classifier_warned = False

    def prepare_intent_classifier(self) -> Optional[str]:
        """
        Load the intent model now (so the first question is fast) and return a human-readable problem
        description, or None when it is ready or intentionally disabled. Never raises.
        """
        if self.intent_classifier is None:
            return None
        try:
            self.intent_classifier.warm_up()
            return None
        except IntentClassifierUnavailable as exc:
            return str(exc)

    def answer_question(self, question: str) -> Dict[str, Any]:
        """
        Process a question and return the structured response, with analytical results checked by the AnswerVerifier
        (``result['answer_verification']``; inconsistent data is withheld). See ``_answer_question_unverified``.
        """
        response = self._answer_question_unverified(question)
        return response if self.answer_verifier is None else self.answer_verifier.verify_response(response)

    def _answer_question_unverified(self, question: str) -> Dict[str, Any]:
        """
        Process a natural-language question and return a structured response.

        Parameters:
            question: The user's natural-language question.

        Returns:
            A JSON-compatible dictionary:
            {
                "status": "success" | "unsupported" | "error" | "no_equity_holdings" | "conversation" | "partial",
                "question": str,
                "selected_tool": "sql_query" | "exposure_calculator" | "hybrid_analysis" | "scenario_analysis" | None,
                "route_reason": str,
                "result": Dict[str, Any] | None,
                "error": Optional[str]
            }
        """
        # --- Input validation ---
        if not question or not isinstance(question, str) or not question.strip():
            return self._make_response(
                status="error",
                question=question if isinstance(question, str) else str(question),
                selected_tool=None,
                route_reason="Input validation failed.",
                result=None,
                error="Question cannot be empty or whitespace.",
            )

        question = question.strip()

        # --- Conversational intents: answered directly, no tool or LLM is invoked ---
        intent = self._detect_conversation_intent(question)
        if intent is not None:
            return self._handle_conversation(question, intent)

        # --- Local semantic intent classification (no Gemini, no network) ---
        if self.intent_classifier is None:
            return self._route_analytical(question)  # classifier disabled: legacy routing, unchanged
        try:
            verdict = self.intent_classifier.classify(question)
        except IntentClassifierUnavailable as exc:
            # Fail safe: answer with the deterministic path only. Gemini is never used just because
            # classification was impossible (it could be spent on greetings or off-topic text).
            if not self._classifier_warned:
                self._classifier_warned = True
                logger.warning("Intent classifier unavailable; using deterministic routing only. %s", exc)
            return self._route_analytical(
                question,
                allow_gemini=False,
                gemini_skip_reason="Gemini skipped: the local intent classifier is unavailable (see setup instructions).",
            )
        logger.debug("Intent %s (%s) for %r", verdict.intent.value, verdict.reason, question)

        # Precedence: a historical / descriptive question about a portfolio's stored performance or risk data goes to the SQL
        # path whatever the classifier leaned towards (sector exposure, unsupported, unclear). Conversational verdicts are kept.
        if (not verdict.is_conversational and verdict.intent != Intent.PORTFOLIO_ANALYTICS and self._is_historical_risk_question(question)
                and not self._is_exposure_question(question) and self._mentions_portfolio(question)):
            return self._route_analytical(question, hint=Intent.PORTFOLIO_ANALYTICS, allow_gemini=True)
        if verdict.is_conversational:
            return self._handle_conversation(question, verdict.intent.value.lower(), local_semantic=True)
        if verdict.is_analytical:
            # Original question, unchanged: analytical routing and Gemini/rule-based SQL are exactly as before.
            return self._route_analytical(question, hint=verdict.intent)
        if verdict.intent == Intent.UNSUPPORTED:
            return self._handle_out_of_scope(question)
        return self._handle_conversation(question, "clarification", local_semantic=True)  # UNKNOWN

    def _route_analytical(
        self,
        question: str,
        hint: Optional[Intent] = None,
        allow_gemini: bool = True,
        gemini_skip_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Existing tool routing. ``hint`` is the classifier's view and can only add, never remove, exposure routing."""
        # Hypothetical stress / what-if questions with a stated shock go to the deterministic scenario tool (no Gemini).
        # Historical and descriptive questions never match, so existing SQL / exposure / hybrid routing is unchanged.
        if is_scenario_question(question):
            return self._handle_scenario(question)
        # Hybrid: an exposure request AND a data request in one question. Plain SQL-only and exposure-only questions
        # never reach this branch, so their routing is unchanged.
        historical = self._is_historical_risk_question(question)
        exposure_signal = self._is_exposure_question(question) or (hint == Intent.SECTOR_EXPOSURE and not self._has_exposure_exclusion(question) and not historical)
        if exposure_signal and is_hybrid_question(question, True):
            return self._handle_hybrid(question, allow_gemini=allow_gemini, gemini_skip_reason=gemini_skip_reason)
        if self._is_exposure_question(question):
            return self._handle_exposure(question)
        if hint == Intent.SECTOR_EXPOSURE and not self._has_exposure_exclusion(question) and not historical:
            return self._handle_exposure(question, prefer_known_name=True)
        return self._handle_sql(question, allow_gemini=allow_gemini, gemini_skip_reason=gemini_skip_reason)

    @staticmethod
    def _is_historical_risk_question(question: str) -> bool:
        """Wording about stored historical returns, performance, drawdown, volatility, VaR, Sharpe, tracking error or a past period."""
        return bool(_HISTORICAL_RISK_RE.search(question))

    def _mentions_portfolio(self, question: str) -> bool:
        return bool(re.search(r"\b(?:portfolios?|funds?)\b", question, re.IGNORECASE)) or self._known_portfolio_name(question) is not None

    @staticmethod
    def _has_exposure_exclusion(question: str) -> bool:
        norm = question.lower()
        return any(excl in norm for excl in _EXPOSURE_EXCLUSIONS)

    @staticmethod
    def _detect_conversation_intent(question: str) -> Optional[str]:
        """Return 'greeting', 'thanks' or 'goodbye' if the whole message is one, else None."""
        norm = re.sub(r"[^a-z0-9 ]+", " ", question.lower())
        norm = " ".join(norm.split())
        if not norm or len(norm) > 40:
            return None
        if _GREETING_RE.match(norm):
            return "greeting"
        if _THANKS_RE.match(norm):
            return "thanks"
        if _GOODBYE_RE.match(norm):
            return "goodbye"
        return None

    def _handle_conversation(self, question: str, intent: str, local_semantic: bool = False) -> Dict[str, Any]:
        """
        Friendly reply for greetings, thanks, goodbyes, identity, capability and clarification requests.
        Never touches SQLQueryTool, Gemini or ExposureCalculator.
        """
        if intent == "clarification":
            reason = "The message was too vague to act on (local semantic classifier); asked for clarification without invoking any tool."
        else:
            how = " (local semantic classifier)" if local_semantic else ""
            reason = f"Recognised a conversational {intent}{how}; answered directly without invoking any tool."
        result: Dict[str, Any] = {
            "tool": "conversation",
            "intent": intent,
            "message": _CONVERSATION_MESSAGES[intent],
            "suggested_questions": [] if intent == "goodbye" else list(CONVERSATION_EXAMPLE_QUESTIONS),
        }
        if intent == "capabilities":
            mode = _CAPABILITY_MODE_GEMINI if getattr(self.sql_tool, "use_gemini", False) is True else _CAPABILITY_MODE_RULES
            result["details"] = [*_CAPABILITY_DETAILS, mode]
        return self._make_response(
            status="conversation",
            question=question,
            selected_tool=None,
            route_reason=reason,
            result=result,
            error=None,
        )

    def _handle_out_of_scope(self, question: str) -> Dict[str, Any]:
        """Friendly local rejection of an unrelated question. No tool and no Gemini call."""
        return self._make_response(
            status="unsupported",
            question=question,
            selected_tool=None,
            route_reason="Recognised an out-of-scope question (local semantic classifier); declined without invoking any tool.",
            result=None,
            error=_OUT_OF_SCOPE_MESSAGE,
        )

    def _known_portfolio_name(self, question: str) -> Optional[str]:
        """Longest portfolio name from the database that appears in the question (case-insensitive)."""
        try:
            rows = self.db.execute_read_only("SELECT portfolio_name FROM portfolios")["rows"]
            names = [r["portfolio_name"] for r in rows if isinstance(r.get("portfolio_name"), str)]
        except Exception:
            return None
        lowered = question.lower()
        matches = [n for n in names if n.lower() in lowered]
        return max(matches, key=len) if matches else None

    def _is_exposure_question(self, question: str) -> bool:
        """
        Determine whether the question is asking for sector exposure.

        Uses keyword matching against exposure-specific phrases,
        while excluding questions that reference per-portfolio
        percentage breakdowns handled by the SQL tool (Q7).
        """
        norm = question.lower()

        # Check exclusions first — Q7 talks about "percentage" and "each portfolio"
        for excl in _EXPOSURE_EXCLUSIONS:
            if excl in norm:
                return False

        # Check for exposure keywords
        for kw in _EXPOSURE_KEYWORDS:
            if kw in norm:
                return True

        return False

    def _extract_portfolio_name(self, question: str) -> Optional[str]:
        """
        Extract a portfolio identifier from a sector-exposure question.

        Strategy:
        1. Match 'for <name>' pattern after exposure keywords.
        2. Match 'for the <name>' pattern.
        3. Match quoted portfolio names.
        4. Match 'portfolio <id>' pattern.
        5. Fallback: extract the noun phrase following the last exposure keyword.
        """
        norm = question.strip()

        # 1. Quoted portfolio name: "Tech Innovation Fund" or 'Tech Innovation Fund'
        quoted_match = re.search(r"""["']([^"']+)["']""", norm)
        if quoted_match:
            return quoted_match.group(1).strip()

        # 2. Pattern: "portfolio <id>" (numeric) — check before generic "for <name>"
        pid_match = re.search(r"\bportfolio\s+(\d+)\b", norm, re.IGNORECASE)
        if pid_match:
            return pid_match.group(1)

        # 3. Pattern: "for the <name>" or "for <name>" — capture until end or punctuation
        for_match = re.search(
            r"\bfor\s+(?:the\s+)?(.+?)(?:\?|$)",
            norm,
            re.IGNORECASE,
        )
        if for_match:
            candidate = for_match.group(1).strip().rstrip("?. ")
            # Filter out generic phrases that aren't portfolio names
            if candidate and candidate.lower() not in (
                "portfolios",
                "portfolio",
                "each portfolio",
                "all portfolios",
                "the portfolio",
            ):
                return candidate

        # 4. Pattern: "... in <Sector> in <Portfolio>" (e.g., "in Technology in Tech Innovation Fund")
        double_in = re.search(
            r"\bin\s+[a-zA-Z]+\s+in\s+(?:the\s+)?(.+?)(?:\?|$)",
            norm,
            re.IGNORECASE,
        )
        if double_in:
            candidate = double_in.group(1).strip().rstrip("?. ")
            if candidate and candidate.lower() not in (
                "portfolios",
                "portfolio",
                "each portfolio",
                "all portfolios",
                "the portfolio",
            ):
                return candidate

        # 5. Pattern: "in <name>" (e.g., "in Tech Innovation Fund")
        in_match = re.search(
            r"\bin\s+(?:the\s+)?(.+?)(?:\?|$)",
            norm,
            re.IGNORECASE,
        )
        if in_match:
            candidate = in_match.group(1).strip().rstrip("?. ")
            if candidate and candidate.lower() not in (
                "portfolios",
                "portfolio",
                "each portfolio",
                "all portfolios",
                "the portfolio",
            ):
                return candidate

        # 6. Fallback: text after last exposure keyword occurrence
        lower = norm.lower()
        last_pos = -1
        matched_kw = ""
        for kw in _EXPOSURE_KEYWORDS:
            pos = lower.rfind(kw)
            # On a tie prefer the longer keyword, so "sector exposures" is not cut to "sector exposure" + "s".
            if pos > last_pos or (pos == last_pos and pos >= 0 and len(kw) > len(matched_kw)):
                last_pos = pos
                matched_kw = kw

        if last_pos >= 0:
            remainder = norm[last_pos + len(matched_kw) :].strip().rstrip("?. ")
            # Remove leading prepositions
            remainder = re.sub(r"^(?:for|of|in)\s+(?:the\s+)?", "", remainder, flags=re.IGNORECASE).strip()
            if remainder:
                return remainder

        return None

    def _handle_exposure(self, question: str, prefer_known_name: bool = False) -> Dict[str, Any]:
        """
        Route a sector-exposure question to ExposureCalculator.

        ``prefer_known_name`` is used when the *classifier* (not the keyword rules) identified the
        intent: the phrasing may not follow the "exposure for <name>" patterns, so a portfolio name
        that exists in the database is looked for first.
        """
        portfolio_name = (self._known_portfolio_name(question) if prefer_known_name else None) or self._extract_portfolio_name(question)

        if not portfolio_name:
            return self._make_response(
                status="error",
                question=question,
                selected_tool="exposure_calculator",
                route_reason="Detected sector-exposure intent but could not extract a portfolio name.",
                result=None,
                error=(
                    "Could not determine which portfolio to analyse. "
                    "Please specify a portfolio name, e.g. "
                    "'Show sector exposure for Tech Innovation Fund'."
                ),
            )

        try:
            result = self.exposure_tool.calculate(portfolio_name)
            # Exposure figures are only ever shown after independent verification. Failed, unverified (e.g. a database
            # problem) and "no verifier configured" all withhold the figures; they differ in verification.status.
            verification = self.financial_verifier.verify(result) if self.financial_verifier is not None else unavailable_verification(result)
            result = apply_verification(result, verification)
            return self._make_response(
                status=result.get("status", "error"),
                question=question,
                selected_tool="exposure_calculator",
                route_reason=f"Question matched sector-exposure intent for portfolio '{portfolio_name}'.",
                result=result,
                error=result.get("error"),
            )
        except Exception as exc:
            logger.error("ExposureCalculator raised an exception: %s", exc)
            return self._make_response(
                status="error",
                question=question,
                selected_tool="exposure_calculator",
                route_reason=f"Detected sector-exposure intent for portfolio '{portfolio_name}'.",
                result=None,
                error=f"Exposure calculation failed: {exc}",
            )

    def _sector_names(self) -> List[str]:
        try:
            return [r["sector_name"] for r in self.db.execute_read_only("SELECT sector_name FROM sectors")["rows"]]
        except Exception:
            return []

    @staticmethod
    def _scenario_portfolio_text(question: str) -> Optional[str]:
        m = re.search(r"\b(?:for|of|on|in)\s+(?:the\s+)?(.+?)(?:\s+(?:under|if|when|with|after|following|given|assuming|using)\b|[?.!]*$)", question, re.IGNORECASE)
        candidate = m.group(1).strip() if m else ""
        if not candidate or re.match(r"(?:that|this|these|those|a|an|each|all|every|any)\b", candidate, re.IGNORECASE) or re.search(r"\bsectors?\b", candidate, re.IGNORECASE):
            return None
        return candidate

    def _handle_scenario(self, question: str) -> Dict[str, Any]:
        """Hypothetical scenario / stress test (see scenario_tool.py). Deterministic; Gemini is never involved."""
        reason = "Question asks for a hypothetical scenario or stress test; routed to the deterministic scenario tool (no Gemini)."
        spec = parse_scenario_request(question, self._sector_names())
        if not spec:
            return self._make_response(
                status="error", question=question, selected_tool=SCENARIO_TOOL, route_reason=reason, result=None,
                error="Please state the scenario: a predefined one (broad market downturn, severe market stress, technology sell-off, "
                      "energy shock) or a shock, e.g. 'Stress test the International Equity Fund under a 20% market decline'.",
            )
        portfolio_text = self._known_portfolio_name(question) or self._scenario_portfolio_text(question)
        if not portfolio_text:
            return self._make_response(
                status="error", question=question, selected_tool=SCENARIO_TOOL, route_reason=reason, result=None,
                error="Please name the portfolio to stress, e.g. 'What happens to the Tech Innovation Fund if Technology falls 25%?'.",
            )
        tool = ScenarioTool(self.db, self.exposure_tool, self.scenario_verifier)
        result = tool.run(portfolio_text, spec.get("scenario_id"), spec.get("market_shock_pct"), spec.get("sector_shocks"))
        return self._make_response(status=result.get("status", "error"), question=question, selected_tool=SCENARIO_TOOL,
                                   route_reason=reason, result=result, error=result.get("error"))

    def _handle_hybrid(self, question: str, allow_gemini: bool = True, gemini_skip_reason: Optional[str] = None) -> Dict[str, Any]:
        """Questions that need both the SQL tool and the verified exposure calculation (see hybrid_tool.py)."""
        portfolio_text = self._known_portfolio_name(question) or self._extract_portfolio_name(question)
        reason = "Question asks for both portfolio data and sector exposure; routed to hybrid analysis (SQL Query Tool + Exposure Calculator, exposure independently verified)."
        if not portfolio_text:
            return self._make_response(
                status="error",
                question=question,
                selected_tool=HYBRID_TOOL,
                route_reason=reason,
                result=None,
                error="Could not determine which portfolio to analyse. Please name one, e.g. 'Show the holdings and sector exposure for the Tech Innovation Fund'.",
            )
        tool = HybridAnalysisTool(self.sql_tool, self.exposure_tool, self.financial_verifier)
        result = tool.execute(question, portfolio_text, allow_gemini=allow_gemini, gemini_skip_reason=gemini_skip_reason)
        return self._make_response(
            status=result["status"],
            question=question,
            selected_tool=HYBRID_TOOL,
            route_reason=reason,
            result=result,
            error=result.get("error"),
        )

    def _handle_sql(
        self,
        question: str,
        allow_gemini: bool = True,
        gemini_skip_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Route a general analytical question to SQLQueryTool."""
        try:
            if allow_gemini:
                result = self.sql_tool.execute(question)
            else:  # classifier unavailable: deterministic generator only
                result = self.sql_tool.execute(question, allow_gemini=False, gemini_skip_reason=gemini_skip_reason)
            return self._make_response(
                status=result.get("status", "error"),
                question=question,
                selected_tool="sql_query",
                route_reason="Question routed to SQL query tool for database analysis.",
                result=result,
                error=result.get("error"),
            )
        except Exception as exc:
            logger.error("SQLQueryTool raised an exception: %s", exc)
            return self._make_response(
                status="error",
                question=question,
                selected_tool="sql_query",
                route_reason="Question routed to SQL query tool for database analysis.",
                result=None,
                error=f"SQL query tool failed: {exc}",
            )

    @staticmethod
    def _make_response(
        status: str,
        question: str,
        selected_tool: Optional[str],
        route_reason: str,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> Dict[str, Any]:
        """Build a standardised agent response dictionary."""
        return {
            "status": status,
            "question": question,
            "selected_tool": selected_tool,
            "route_reason": route_reason,
            "result": result,
            "error": error,
        }
