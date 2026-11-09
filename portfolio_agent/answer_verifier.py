"""
Final-answer verification: does the user-facing response faithfully represent the structured tool results?

What exists to verify (established by inspecting the code, not assumed)
-----------------------------------------------------------------------
The application does **not** generate free-form analytical final answers. Gemini only ever returns JSON
``{sql, parameters, description}``. Every user-facing analytical response is therefore either

* a direct tool result (rows from ``SQLQueryTool``, figures from ``ExposureCalculator`` already checked by
  ``FinancialVerifier``, or the combination from ``HybridAnalysisTool``), or
* deterministic text built from those results (hybrid summary, error messages, conversation replies), or
* the single model-written sentence ``query_description`` (Gemini's description of its own SQL).

So this module does structured evidence-consistency checking. It does not add a second Gemini call, and it does not
try to prove natural-language claims.

What it checks (all structural; no arithmetic on financial figures, no database, no model)
------------------------------------------------------------------------------------------
SQL result      status valid and equal to the response status; ``rows`` / ``columns`` well formed; ``row_count`` equals
                the number of rows; every row has the declared columns; a successful result has SQL text; a failed one
                carries no rows; fallback metadata is coherent.
Exposure        a displayed result carries a ``verified`` financial verification with no mismatches, whose figure
                digest matches the figures about to be shown (so nothing changed after verification); identity,
                count and status fields are coherent; a failed or unverified result shows no figures (Phase 4.1).
Hybrid          each component passes its own checks; both refer to the same canonical portfolio; component statuses
                match ``component_status``; the overall status, ``complete`` flag, ``issues``, ``error`` and ``summary``
                agree with what the components actually achieved. ``complete`` is true only if both succeeded.

What it cannot verify (and says so)
-----------------------------------
Free text. ``query_description`` is written by the model; it is reported under ``unverified_claims`` and the UI
labels it as unverified. The headline shown to the user is a deterministic summary derived from the tool result
instead. Whether Gemini's SQL answers the user's *intent* is likewise not judged here (only that the response is
consistent with what the SQL returned).

Outcomes: ``verified`` | ``failed`` (an inconsistency) | ``unverified`` (could not be completed, or a required
piece of evidence is absent) | ``not_applicable`` (no analytical claim is presented). A failed answer (or a verifier
crash) has the affected data withheld, never presented as trusted; a hybrid with one good component stays ``partial``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .financial_verifier import figures_digest
from .scenario_verifier import scenario_digest

logger = logging.getLogger(__name__)

VERIFIED = "verified"
FAILED = "failed"
UNVERIFIED = "unverified"
NOT_APPLICABLE = "not_applicable"

SQL_TOOL = "sql_query"
EXPOSURE_TOOL = "exposure_calculator"
HYBRID_TOOL = "hybrid_analysis"
SCENARIO_TOOL = "scenario_analysis"
_INF = 1e309  # float infinity (written as a literal: this module does no arithmetic)
_RANGE = 100  # shocks are percentages within [-100, +100]

METHOD = (
    "Structural consistency checks between the final response and the structured tool results (statuses, row counts, "
    "column/row shape, portfolio identity, verification provenance). No financial calculation, no database access, "
    "no model call."
)
SCOPE = "Model-written text (query_description) cannot be reliably verified and is reported as an unverified claim."
MAX_MISMATCHES = 25
_SEVERITY = {"success": 0, "no_equity_holdings": 0, "partial": 1, "error": 2}
_EXPOSURE_KEYS = ("sector", "exposure_pct", "weight_fraction", "holding_count")
_COUNT_KEYS = ("total_holdings_count", "equity_holdings_count", "excluded_bond_holdings", "unmapped_equity_holdings")


class _Run:
    """Collects the outcome of one verification pass."""

    def __init__(self) -> None:
        self.checks = 0
        self.mismatches: List[Dict[str, Any]] = []
        self.skipped: List[str] = []
        self.unverified_labelled: List[str] = []   # unverified but already withheld upstream (Phase 4.1): label only
        self.unverified_withhold: List[str] = []   # the check itself could not complete: data withheld
        self.unverified_claims: List[str] = []
        self.claims = 0                            # analytical datasets presented in this response
        self.component: Optional[str] = None

    def check(self, ok: bool, code: str, message: str) -> bool:
        self.checks += 1
        if not ok:
            entry: Dict[str, Any] = {"code": code, "message": message}
            if self.component:
                entry["component"] = self.component
            self.mismatches.append(entry)
        return ok

    def failed_components(self) -> set:
        return {m["component"] for m in self.mismatches if m.get("component")}


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY.get(a, 2) >= _SEVERITY.get(b, 2) else b


class AnswerVerifier:
    """Verifies that a PortfolioAgent response is consistent with its structured evidence."""

    # ------------------------------------------------------------------ public

    def verify_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """
        Return ``response`` with ``result['answer_verification']`` added (analytical tool results only) and, when an
        inconsistency is found, the affected data withheld. Never raises.
        """
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict) or result.get("tool") not in (SQL_TOOL, EXPOSURE_TOOL, HYBRID_TOOL, SCENARIO_TOOL):
            return response  # conversation, unsupported, plain errors: no analytical claim to verify
        try:
            run = self._run(response, result)
            status = self._overall(run)
            return self._apply(response, result, run, status)
        except Exception as exc:  # a bug here must neither crash the request nor let unchecked data through
            logger.error("Answer verification failed unexpectedly: %s: %s", type(exc).__name__, exc)
            run = _Run()
            run.unverified_withhold.append(f"Answer verification could not be completed ({type(exc).__name__}).")
            try:
                return self._apply(response, result, run, UNVERIFIED)
            except Exception:  # last resort: a plain error, no data
                return {**response, "status": "error", "result": None,
                        "error": "Answer verification could not be completed, so the result was withheld."}

    # ------------------------------------------------------------------ checks

    def _run(self, response: Dict[str, Any], result: Dict[str, Any]) -> _Run:
        run = _Run()
        tool = result["tool"]
        run.check(response.get("selected_tool") == tool, "tool_mismatch",
                  f"selected_tool is {response.get('selected_tool')!r} but the result is from {tool!r}.")
        if tool == SQL_TOOL:
            self._sql(run, result, response.get("status"), "sql")
        elif tool == EXPOSURE_TOOL:
            self._exposure(run, result, response.get("status"), "exposure")
        elif tool == SCENARIO_TOOL:
            self._scenario(run, result, response.get("status"))
        else:
            self._hybrid(run, result, response.get("status"))
        run.component = None
        return run

    def _sql(self, run: _Run, r: Dict[str, Any], response_status: Optional[str], comp: str) -> None:
        run.component = comp
        status = r.get("status")
        run.check(status in ("success", "unsupported", "error"), "sql_status_invalid", f"SQL status {status!r} is not recognised.")
        if response_status is not None:
            run.check(response_status == status, "status_mismatch", f"Response status {response_status!r} differs from the SQL result status {status!r}.")
        rows, columns, row_count = r.get("rows"), r.get("columns"), r.get("row_count")
        if not run.check(isinstance(rows, list) and isinstance(columns, list) and all(isinstance(c, str) for c in columns),
                         "sql_shape_invalid", "rows and columns must be lists (columns of text)."):
            return
        run.check(_is_int(row_count) and row_count == len(rows), "row_count_mismatch",
                  f"row_count is {row_count!r} but {len(rows)} rows were returned.")
        unique = len(set(columns)) == len(columns)  # duplicate column names collapse in a dict row: only require a subset then
        bad = [i for i, row in enumerate(rows) if not isinstance(row, dict) or (set(row) != set(columns) if unique else not set(row) <= set(columns))]
        run.check(not bad, "row_structure_mismatch", f"Rows {bad[:5]} do not match the declared columns {columns}.")
        if status == "success":
            sql, params = r.get("generated_sql"), r.get("query_parameters")
            run.check(isinstance(sql, str) and bool(sql.strip()), "missing_sql", "A successful SQL result must include the SQL that produced it.")
            run.check(params is None or isinstance(params, list), "parameters_invalid", "query_parameters must be a list or null.")
            run.claims += 1
        else:
            run.check(not rows and (row_count in (0, None)), "data_on_failed_result", f"A {status!r} SQL result must not carry rows.")
        used, reason, method = r.get("fallback_used"), r.get("fallback_reason"), r.get("generation_method")
        run.check(isinstance(used, bool) or used is None, "fallback_flag_invalid", "fallback_used must be true or false.")
        if used is True:
            run.check(isinstance(reason, str) and bool(reason.strip()), "fallback_unexplained", "A fallback must state its reason.")
        if method == "gemini":
            run.check(used is not True, "fallback_label_mismatch", "A result labelled Gemini cannot also be a fallback.")
        if r.get("query_description"):
            run.unverified_claims.append(f"{comp}.query_description" if comp != "sql" else "query_description")
        run.component = None

    def _exposure(self, run: _Run, r: Dict[str, Any], response_status: Optional[str], comp: str) -> None:
        run.component = comp
        status = r.get("status")
        run.check(status in ("success", "no_equity_holdings", "error"), "exposure_status_invalid", f"Exposure status {status!r} is not recognised.")
        if response_status is not None:
            run.check(response_status == status, "status_mismatch", f"Response status {response_status!r} differs from the exposure result status {status!r}.")
        verification = r.get("verification") if isinstance(r.get("verification"), dict) else None
        exposures = r.get("exposures")
        if status in ("success", "no_equity_holdings"):
            run.claims += 1
            if verification is None:
                # Defence in depth: figures with no financial verification block must not be displayed.
                run.skipped.append("financial verification evidence is absent")
                run.unverified_withhold.append("The exposure figures carry no independent financial verification.")
            else:
                run.check(verification.get("status") == "verified" and verification.get("verified") is True, "exposure_not_verified",
                          f"Exposure figures are shown but their verification status is {verification.get('status')!r}.")
                run.check(verification.get("mismatch_count") == 0, "verification_mismatches", "A verified exposure result reports verification mismatches.")
                digest = verification.get("figures_digest")
                run.check(isinstance(digest, str) and digest == figures_digest(r), "figures_changed_after_verification",
                          "The figures about to be displayed are not the figures that were verified.")
            run.check(_is_int(r.get("portfolio_id")) and isinstance(r.get("portfolio_name"), str) and bool(r.get("portfolio_name")), "identity_missing",
                      "An exposure result must identify its portfolio (id and name).")
            run.check(all(_is_int(r.get(k)) and r.get(k) >= 0 for k in _COUNT_KEYS), "counts_invalid", "Holding counts must be non-negative integers.")
            ok_list = isinstance(exposures, list) and all(isinstance(e, dict) and all(k in e for k in _EXPOSURE_KEYS) for e in exposures)
            if run.check(ok_list, "exposures_malformed", "exposures must be a list of sector entries with sector, exposure_pct, weight_fraction and holding_count."):
                if _is_int(r.get("equity_holdings_count")):
                    run.check(len(exposures) <= r["equity_holdings_count"], "sector_count_exceeds_holdings", "More sectors are shown than there are equity holdings.")
                    run.check((status == "no_equity_holdings") == (r["equity_holdings_count"] == 0 and not exposures), "status_inconsistent_with_counts",
                              "The status does not match the presence of equity holdings.")
        else:  # an error result: the figures must be absent
            fin = (verification or {}).get("status")
            figures_shown = bool(exposures) or bool(r.get("total_equity_weight_pct"))
            if fin in ("failed", "unverified"):
                run.check(not figures_shown, "withheld_figures_shown", f"Exposure verification is {fin!r} but figures are still present.")
                if fin == "failed":
                    run.check(False, "financial_verification_failed", "Independent financial verification disproved the exposure figures; they were withheld.")
                else:
                    run.unverified_labelled.append("Independent financial verification could not be completed; the figures were withheld.")
            else:
                run.check(not figures_shown, "data_on_failed_result", "An error result must not carry exposure figures.")
        run.component = None

    @staticmethod
    def _finite(x: Any) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and x not in (_INF, -_INF)

    def _scenario(self, run: _Run, r: Dict[str, Any], response_status: Optional[str]) -> None:
        """
        Structural consistency of a hypothetical scenario result. The arithmetic (contributions, total, coverage, currency
        impact) was already recomputed independently by ScenarioVerifier: this method trusts that verdict, requires its
        digest to match the figures about to be shown (so any later change is caught) and checks shape, identity, flags,
        signs and statuses only.
        """
        run.component = "scenario"
        status = r.get("status")
        fin = self._finite
        run.check(status in ("success", "no_equity_holdings", "error"), "scenario_status_invalid", f"Scenario status {status!r} is not recognised.")
        if response_status is not None:
            run.check(response_status == status, "status_mismatch", f"Response status {response_status!r} differs from the scenario status {status!r}.")
        verification = r.get("verification") if isinstance(r.get("verification"), dict) else None
        impacts, starting = r.get("sector_impacts"), r.get("starting_exposure")
        total, value, coverage = r.get("estimated_portfolio_impact_pct"), r.get("estimated_value_impact"), r.get("coverage")
        if status in ("success", "no_equity_holdings"):
            run.claims += 1
            if verification is None:
                run.skipped.append("scenario verification evidence is absent")
                run.unverified_withhold.append("The scenario figures carry no independent verification.")
            else:
                run.check(verification.get("status") == "verified" and verification.get("verified") is True, "scenario_not_verified",
                          f"Scenario figures are shown but their verification status is {verification.get('status')!r}.")
                run.check(verification.get("mismatch_count") == 0, "verification_mismatches", "A verified scenario reports verification mismatches.")
                digest = verification.get("figures_digest")
                run.check(isinstance(digest, str) and digest == scenario_digest(r), "figures_changed_after_verification",
                          "The scenario figures about to be displayed are not the figures that were verified.")
            run.check(_is_int(r.get("portfolio_id")) and isinstance(r.get("portfolio_name"), str) and bool(r.get("portfolio_name")), "identity_missing",
                      "A scenario result must identify its portfolio (id and name).")
            sc = r.get("scenario") if isinstance(r.get("scenario"), dict) else {}
            kind, sid = sc.get("type"), sc.get("id")
            run.check(isinstance(sid, str) and bool(sid) and isinstance(sc.get("name"), str) and bool(sc.get("name")) and isinstance(sc.get("description"), str)
                      and kind in ("predefined", "custom") and ((kind == "custom") == (sid == "custom")), "scenario_identity_invalid",
                      "The scenario needs an id, name, description and a type that matches its id.")
            run.check(sc.get("is_hypothetical") is True and sc.get("is_forecast") is False, "hypothetical_flags", "The scenario must be flagged hypothetical and not a forecast.")
            shocks = sc.get("sector_shocks") if isinstance(sc.get("sector_shocks"), dict) else None
            run.check(fin(sc.get("market_shock_pct")) and -_RANGE <= sc["market_shock_pct"] <= _RANGE and shocks is not None
                      and all(fin(v) and -_RANGE <= v <= _RANGE for v in shocks.values()), "shock_assumptions_invalid",
                      "Shock assumptions must be finite percentages within -100% and +100%.")
            disclaimer = r.get("disclaimer")
            run.check(isinstance(disclaimer, str) and "hypothetical" in disclaimer.lower() and "not a forecast" in disclaimer.lower(), "disclaimer_missing",
                      "The hypothetical-scenario disclaimer is missing.")
            rows_ok = isinstance(impacts, list) and isinstance(starting, list) and all(
                isinstance(x, dict) for x in impacts + starting)
            if run.check(rows_ok, "sector_structure_invalid", "sector_impacts and starting_exposure must be lists of objects."):
                keys = ("sector", "exposure_pct", "shock_pct", "contribution_pp", "shocked")
                shaped = all(all(k in x for k in keys) and all(fin(x[k]) for k in keys[1:4]) and isinstance(x["shocked"], bool) and isinstance(x["sector"], str) for x in impacts)
                if run.check(shaped, "sector_structure_invalid", "Every sector impact needs a sector, numeric exposure, shock and contribution, and a shocked flag."):
                    names = [x["sector"] for x in impacts]
                    run.check(len(names) == len(set(names)), "sector_duplicate", "A sector appears more than once in sector_impacts.")
                    run.check(set(names) == {x.get("sector") for x in starting}, "sector_set_mismatch", "starting_exposure and sector_impacts cover different sectors.")
                    run.check(all(-_RANGE <= x["shock_pct"] <= _RANGE for x in impacts), "shock_assumptions_invalid", "A sector shock is outside -100% to +100%.")
                    run.check(all(x["shocked"] is (x["shock_pct"] != 0) for x in impacts), "shocked_flag_mismatch", "A 'shocked' flag disagrees with its shock.")
                    run.check(all((x["shock_pct"] == 0 and x["contribution_pp"] == 0) or (x["shock_pct"] < 0 and x["contribution_pp"] <= 0)
                                  or (x["shock_pct"] > 0 and x["contribution_pp"] >= 0) for x in impacts), "contribution_sign_mismatch",
                              "A contribution has the wrong sign for its shock (an unshocked sector must contribute zero).")
                    unheld = r.get("unheld_sector_shocks")
                    run.check(isinstance(unheld, list) and all(isinstance(u, dict) and u.get("sector") not in names for u in unheld), "unheld_shocks_invalid",
                              "Shocks on unheld sectors must be a list of sectors the portfolio does not hold.")
                    if fin(total):
                        c = [x["contribution_pp"] for x in impacts]
                        sign_ok = (not all(v == 0 for v in c) or total == 0) and (not all(v <= 0 for v in c) or total <= 0) and (not all(v >= 0 for v in c) or total >= 0)
                        run.check(sign_ok, "total_sign_mismatch", "The reported total impact is inconsistent in sign with the sector contributions.")
                    if _is_int(coverage.get("equity_holdings_count") if isinstance(coverage, dict) else None):
                        run.check(len(impacts) <= coverage["equity_holdings_count"], "sector_count_exceeds_holdings", "More sectors are shown than there are equity holdings.")
                    run.check((status == "no_equity_holdings") == (not impacts), "status_inconsistent_with_sectors", "The status does not match the presence of sector impacts.")
            run.check(fin(total), "total_missing", "A displayed scenario must report its estimated portfolio impact.")
            run.check(isinstance(coverage, dict) and all(fin(coverage.get(k)) for k in (
                "equity_exposure_pct", "shocked_weight_pct", "unshocked_mapped_weight_pct", "unmapped_equity_weight_pct", "excluded_bond_weight_pct",
                "recorded_weight_accounted_pct", "recorded_weight_unaccounted_pct")) and all(_is_int(coverage.get(k)) and coverage[k] >= 0 for k in (
                "equity_holdings_count", "unmapped_equity_holdings", "excluded_bond_holdings")) and coverage.get("renormalised") is False,
                      "coverage_invalid", "Coverage metadata is incomplete, has invalid counts, or is flagged renormalised.")
            if value is None:
                run.check(isinstance(r.get("value_impact_note"), str) and bool(r.get("value_impact_note")), "value_impact_unexplained",
                          "When no currency impact is shown the reason must be stated.")
            else:
                ok = (isinstance(value, dict) and fin(value.get("amount")) and fin(value.get("aum")) and value["aum"] > 0
                      and isinstance(value.get("currency"), str) and bool(value.get("currency")) and fin(total)
                      and (total != 0 or value["amount"] == 0) and (total >= 0 or value["amount"] <= 0) and (total <= 0 or value["amount"] >= 0))
                run.check(ok, "value_impact_invalid", "The currency impact needs a positive AUM and must agree in sign with the portfolio impact.")
        else:  # an error result: the figures must be absent
            fin_status = (verification or {}).get("status")
            figures_shown = bool(impacts) or bool(starting) or total is not None or value is not None or coverage is not None
            if fin_status in ("failed", "unverified"):
                run.check(not figures_shown, "withheld_figures_shown", f"Scenario verification is {fin_status!r} but figures are still present.")
                if fin_status == "failed":
                    run.check(False, "scenario_verification_failed", "Independent scenario verification disproved the figures; they were withheld.")
                else:
                    run.unverified_labelled.append("Independent scenario verification could not be completed; the figures were withheld.")
            else:
                run.check(not figures_shown, "data_on_failed_result", "An error result must not carry scenario figures.")
        run.component = None

    def _hybrid(self, run: _Run, r: Dict[str, Any], response_status: Optional[str]) -> None:
        comps = r.get("components")
        if not run.check(isinstance(comps, dict), "components_missing", "A hybrid result must contain a components object."):
            return
        sql, exposure = comps.get("sql"), comps.get("exposure")
        cs = r.get("component_status") if isinstance(r.get("component_status"), dict) else {}
        for name, part in (("sql", sql), ("exposure", exposure)):
            expected_status = part.get("status") if isinstance(part, dict) else "not_run"
            run.check(cs.get(name) == expected_status, "component_status_mismatch",
                      f"component_status.{name} is {cs.get(name)!r} but the {name} component status is {expected_status!r}.")
        if isinstance(sql, dict):
            self._sql(run, sql, None, "sql")
        if isinstance(exposure, dict):
            self._exposure(run, exposure, None, "exposure")
        bad = run.failed_components()
        sql_ok = isinstance(sql, dict) and sql.get("status") == "success" and "sql" not in bad
        exp_ok = (isinstance(exposure, dict) and exposure.get("status") in ("success", "no_equity_holdings")
                  and (exposure.get("verification") or {}).get("status") == "verified" and "exposure" not in bad)
        expected = "success" if (sql_ok and exp_ok) else "partial" if (sql_ok or exp_ok) else "error"

        run.component = "hybrid"
        status = r.get("status")
        run.check(status == expected, "overall_status_mismatch", f"Overall status is {status!r} but the components imply {expected!r}.")
        if response_status is not None:
            run.check(response_status == status, "status_mismatch", f"Response status {response_status!r} differs from the hybrid result status {status!r}.")
        complete = r.get("complete")
        run.check(isinstance(complete, bool) and complete == (expected == "success"),
                  "complete_flag_mismatch", f"complete is {r.get('complete')!r} but may be true only when both components succeed (expected {expected == 'success'}).")
        issues, error, summary = r.get("issues"), r.get("error"), r.get("summary")
        run.check(isinstance(issues, list) and (len(issues) == 0) == (expected == "success"), "issues_mismatch",
                  "issues must be empty exactly when both components succeed.")
        run.check((error is None) == (expected == "success"), "error_field_mismatch", "error must be null exactly when both components succeed.")
        prefix = {"success": "Both parts completed", "partial": "Partial result", "error": "Neither part"}[expected]
        run.check(isinstance(summary, str) and summary.startswith(prefix), "summary_mismatch", f"The summary should begin {prefix!r} for a {expected!r} result.")
        if isinstance(issues, list) and expected != "success":
            if isinstance(sql, dict) and not sql_ok:  # (a component that never ran is explained by the resolution issue instead)
                run.check(any(isinstance(i, str) and i.startswith("SQL component") for i in issues), "summary_omits_failure", "The SQL failure is not described in the issues.")
            if isinstance(exposure, dict) and not exp_ok:
                run.check(any(isinstance(i, str) and i.startswith("Exposure component") for i in issues), "summary_omits_failure", "The exposure failure is not described in the issues.")

        run.component = "hybrid_identity"
        portfolio = r.get("portfolio")
        if isinstance(exposure, dict) and exposure.get("status") in ("success", "no_equity_holdings") and isinstance(portfolio, dict):
            run.check(portfolio.get("portfolio_id") == exposure.get("portfolio_id") and portfolio.get("portfolio_name") == exposure.get("portfolio_name"),
                      "portfolio_identity_mismatch", "The hybrid portfolio differs from the portfolio of the exposure component.")
        if isinstance(sql, dict) and isinstance(portfolio, dict) and portfolio.get("portfolio_name"):
            canonical = str(portfolio["portfolio_name"]).lower()
            question = sql.get("question")
            run.check(isinstance(question, str) and canonical in question.lower(), "sql_portfolio_mismatch", "The SQL component was not asked about the hybrid portfolio.")
            rows = sql.get("rows") if isinstance(sql.get("rows"), list) else []
            for column in ("portfolio_name", "portfolio_id"):
                values = {row.get(column) for row in rows if isinstance(row, dict) and column in row}
                if values:
                    want = portfolio.get(column)
                    run.check(all(str(v).lower() == str(want).lower() for v in values), "sql_rows_portfolio_mismatch",
                              f"SQL rows contain {column} values other than the hybrid portfolio's.")
        run.component = None

    # ------------------------------------------------------------------ outcome and policy

    @staticmethod
    def _overall(run: _Run) -> str:
        if run.mismatches:
            return FAILED
        if run.unverified_withhold or run.unverified_labelled:
            return UNVERIFIED
        return VERIFIED if run.claims else NOT_APPLICABLE

    @staticmethod
    def _summary(tool: str, result: Dict[str, Any]) -> Optional[str]:
        """Deterministic one-line description derived only from trusted fields of the tool result (counts, names)."""
        if tool == SQL_TOOL and result.get("status") == "success" and isinstance(result.get("rows"), list):
            n = len(result["rows"])
            c = len(result.get("columns") or [])
            return f"{n} {'row' if n == 1 else 'rows'} returned" + (f" ({c} {'column' if c == 1 else 'columns'})." if c else ".")
        if tool == EXPOSURE_TOOL and result.get("status") == "success" and isinstance(result.get("exposures"), list):
            n = len(result["exposures"])
            return f"Equity sector exposure for {result.get('portfolio_name')}: {n} {'sector' if n == 1 else 'sectors'}, bonds excluded."
        if tool == EXPOSURE_TOOL and result.get("status") == "no_equity_holdings":
            return f"{result.get('portfolio_name')} has no equity holdings mapped to a sector."
        if tool == SCENARIO_TOOL and result.get("status") in ("success", "no_equity_holdings") and isinstance(result.get("sector_impacts"), list):
            name = (result.get("scenario") or {}).get("name")
            n = len(result["sector_impacts"])
            if n == 0:
                return f"Hypothetical scenario \u201c{name}\u201d: {result.get('portfolio_name')} has no equity holdings mapped to a sector, so there is nothing to shock."
            return (f"Hypothetical scenario \u201c{name}\u201d for {result.get('portfolio_name')}: {n} {'sector' if n == 1 else 'sectors'}, "
                    "bonds excluded, weights not renormalised.")
        if tool == HYBRID_TOOL:
            return result.get("summary")
        return None

    def _block(self, status: str, run: _Run, tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": status,
            "verified": status == VERIFIED,
            "method": METHOD,
            "scope": SCOPE,
            "checks_run": run.checks,
            "mismatch_count": len(run.mismatches),
            "mismatches": run.mismatches[:MAX_MISMATCHES],
            "checks_skipped": list(run.skipped),
            "unverified_claims": list(run.unverified_claims),
            "summary": self._summary(tool, result),
            "error": "; ".join(run.unverified_withhold + run.unverified_labelled) or None,
        }

    def _apply(self, response: Dict[str, Any], result: Dict[str, Any], run: _Run, status: str) -> Dict[str, Any]:
        tool = result["tool"]
        block = self._block(status, run, tool, result)
        withhold = status == FAILED or bool(run.unverified_withhold)
        if not withhold:
            return {**response, "result": {**result, "answer_verification": block}}

        reason = (run.mismatches[0]["message"] if run.mismatches else (run.unverified_withhold[0] if run.unverified_withhold else "verification incomplete"))
        rejected: Dict[str, Any] = {}
        if tool == HYBRID_TOOL:
            new_result = self._withhold_hybrid(result, run, reason, rejected, response.get("status"))
        else:
            new_result = self._withhold_single(tool, result, reason, rejected)
        block["rejected_result"] = rejected
        block["summary"] = self._summary(tool, new_result) if tool != HYBRID_TOOL else new_result.get("summary")
        new_result = {**new_result, "answer_verification": block}
        out = {**response, "result": new_result}
        if tool == HYBRID_TOOL:
            out["status"], out["error"] = new_result["status"], new_result.get("error")
        elif new_result.get("status") != result.get("status") or new_result.get("error") != result.get("error"):
            out["status"], out["error"] = new_result["status"], new_result.get("error")
        return out

    @staticmethod
    def _message(what: str, reason: str) -> str:
        return f"The {what} failed answer verification and was withheld: {reason}"

    def _withhold_single(self, tool: str, result: Dict[str, Any], reason: str, rejected: Dict[str, Any]) -> Dict[str, Any]:
        return self._withhold_part(tool, result, reason, rejected, "result")

    def _withhold_part(self, tool: str, part: Dict[str, Any], reason: str, rejected: Dict[str, Any], key: str) -> Dict[str, Any]:
        message = self._message({EXPOSURE_TOOL: "exposure result", SCENARIO_TOOL: "scenario result"}.get(tool, "SQL result"), reason)
        if tool == SCENARIO_TOOL:
            shown = (part.get("sector_impacts"), part.get("starting_exposure"), part.get("estimated_portfolio_impact_pct"), part.get("estimated_value_impact"), part.get("coverage"))
            if any(v not in (None, [], {}) for v in shown):
                rejected[key] = {"status": part.get("status"), "sector_impacts": shown[0], "starting_exposure": shown[1],
                                 "estimated_portfolio_impact_pct": shown[2], "estimated_value_impact": shown[3], "coverage": shown[4]}
            return {**part, "status": "error", "sector_impacts": [], "starting_exposure": [], "unheld_sector_shocks": [], "estimated_portfolio_impact_pct": None,
                    "estimated_value_impact": None, "coverage": None, "error": message if part.get("status") in ("success", "no_equity_holdings") else part.get("error") or message}
        if tool == SQL_TOOL:
            if part.get("status") != "success":  # a failed result must carry no rows
                return {**part, "rows": [], "columns": [], "row_count": 0}
            rows = part.get("rows")
            rejected[key] = {"status": part.get("status"), "row_count_reported": part.get("row_count"),
                             "rows_returned": len(rows) if isinstance(rows, list) else None, "columns": part.get("columns"),
                             "generated_sql": part.get("generated_sql")}
            return {**part, "status": "error", "rows": [], "columns": [], "row_count": 0, "generated_sql": None, "error": message}
        if part.get("status") not in ("success", "no_equity_holdings"):  # already an error: make sure no figures remain
            if part.get("exposures") or part.get("total_equity_weight_pct"):
                rejected[key] = {"status": part.get("status"), "exposures": part.get("exposures"), "total_equity_weight_pct": part.get("total_equity_weight_pct")}
            return {**part, "exposures": [], "total_equity_weight_pct": 0.0}
        rejected[key] = {"status": part.get("status"), "exposures": part.get("exposures"), "total_equity_weight_pct": part.get("total_equity_weight_pct")}
        return {**part, "status": "error", "exposures": [], "total_equity_weight_pct": 0.0, "error": message}

    def _withhold_hybrid(self, r: Dict[str, Any], run: _Run, reason: str, rejected: Dict[str, Any], reported: Optional[str]) -> Dict[str, Any]:
        comps = dict(r.get("components") or {})
        failed = run.failed_components()
        everything = bool(run.unverified_withhold) or "hybrid_identity" in failed
        for name, tool in (("sql", SQL_TOOL), ("exposure", EXPOSURE_TOOL)):
            part = comps.get(name)
            if isinstance(part, dict) and (everything or name in failed):
                comps[name] = self._withhold_part(tool, part, reason, rejected, name)
        sql, exposure = comps.get("sql"), comps.get("exposure")
        sql_ok = isinstance(sql, dict) and sql.get("status") == "success"
        exp_ok = isinstance(exposure, dict) and exposure.get("status") in ("success", "no_equity_holdings") and (exposure.get("verification") or {}).get("status") == "verified"
        computed = "success" if (sql_ok and exp_ok) else "partial" if (sql_ok or exp_ok) else "error"
        final = _worse(str(r.get("status")), computed)  # never more successful than reported or than the evidence
        issues = [i for i in (r.get("issues") or []) if isinstance(i, str)]
        for name, label in (("sql", "SQL"), ("exposure", "Exposure")):
            if name in rejected or (everything and isinstance(comps.get(name), dict)):
                issues.append(f"{label} component failed answer verification; its results were withheld.")
        if final != "success" and not issues:
            issues.append("The response was inconsistent with its components.")
        summary = {"success": r.get("summary") or "Both parts completed.",
                   "partial": "Partial result: only part of the request could be completed. " + " ".join(issues),
                   "error": "Neither part of the request could be completed. " + " ".join(issues)}[final]
        return {
            **r,
            "components": comps,
            "component_status": {n: (comps[n].get("status") if isinstance(comps.get(n), dict) else "not_run") for n in ("sql", "exposure")},
            "status": final,
            "complete": final == "success" and r.get("complete") is True,
            "issues": issues if final != "success" else [],
            "summary": summary.strip(),
            "error": None if final == "success" else " ".join(issues),
        }
