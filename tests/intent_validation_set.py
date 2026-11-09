"""
Labelled validation set for the local intent classifier.

None of these messages appear in portfolio_agent/intent_examples.py (a unit test enforces this), so
they measure generalisation to unseen wording. Categories:

  positive       a clean example of the intent
  hard_negative  looks like another intent (e.g. an analytical question phrased like a help request)
  mixed          conversational wrapper around a real analytical request
  ambiguous      too vague to act on: the expected outcome is UNKNOWN (ask for clarification)
  regression     the ten official ground-truth questions (Q1-Q10)

``expected`` is an Intent name. For analytical expectations either analytical intent is accepted
as correct by the calibration report, because the agent's keyword routing still decides between the
SQL tool and the exposure calculator (Q9/Q10 are checked end to end separately).
"""

from __future__ import annotations

from typing import List, Tuple

# (text, expected intent, category)
Case = Tuple[str, str, str]

VALIDATION: List[Case] = [
    # --- GREETING
    ("hallo there!", "GREETING", "positive"),
    ("Hey, good morning", "GREETING", "positive"),
    ("hi there, how's it going?", "GREETING", "positive"),
    ("Good evening team", "GREETING", "positive"),
    ("heyyy", "GREETING", "positive"),
    ("hello again", "GREETING", "positive"),
    # --- THANKS / acknowledgements
    ("thanks a ton", "THANKS", "positive"),
    ("thank you very much", "THANKS", "positive"),
    ("ok great, thank you", "THANKS", "positive"),
    ("brilliant, that's what I needed", "THANKS", "positive"),
    ("alright, makes sense", "THANKS", "positive"),
    ("super, thanks", "THANKS", "positive"),
    # --- GOODBYE
    ("bye for now", "GOODBYE", "positive"),
    ("goodbye and thanks", "GOODBYE", "positive"),
    ("see ya", "GOODBYE", "positive"),
    ("I have to go now, bye", "GOODBYE", "positive"),
    ("have a great evening", "GOODBYE", "positive"),
    # --- IDENTITY
    ("Who exactly are you?", "IDENTITY", "positive"),
    ("May I know your name?", "IDENTITY", "positive"),
    ("What do they call you?", "IDENTITY", "positive"),
    ("Which assistant is this?", "IDENTITY", "positive"),
    ("Is this a chatbot or a person?", "IDENTITY", "positive"),
    ("Say a little about yourself", "IDENTITY", "positive"),
    ("what's this assistant's name", "IDENTITY", "positive"),
    ("Who created you?", "IDENTITY", "positive"),
    # --- CAPABILITIES / HELP
    ("What all can you do?", "CAPABILITIES", "positive"),
    ("What can you help with?", "CAPABILITIES", "positive"),
    ("How can you assist me today?", "CAPABILITIES", "positive"),
    ("What kind of things can I ask here?", "CAPABILITIES", "positive"),
    ("Can you explain how to use this?", "CAPABILITIES", "positive"),
    ("What features are available?", "CAPABILITIES", "positive"),
    ("Show me what this application can answer", "CAPABILITIES", "positive"),
    ("I'm new here, where do I start?", "CAPABILITIES", "positive"),
    ("What are you able to help me with?", "CAPABILITIES", "positive"),
    ("what is possible with this tool", "CAPABILITIES", "positive"),
    ("Which questions do you support?", "CAPABILITIES", "positive"),
    ("can you help me", "CAPABILITIES", "positive"),
    ("what things can you do for me", "CAPABILITIES", "positive"),
    # --- PORTFOLIO_ANALYTICS
    ("How many portfolios exist in total?", "PORTFOLIO_ANALYTICS", "positive"),
    ("Which portfolios are currently active?", "PORTFOLIO_ANALYTICS", "positive"),
    ("Show the five biggest holdings of Growth Equity Fund by cost basis", "PORTFOLIO_ANALYTICS", "positive"),
    ("What is the combined AUM of high risk portfolios?", "PORTFOLIO_ANALYTICS", "positive"),
    ("Which stocks are classified under Technology?", "PORTFOLIO_ANALYTICS", "positive"),
    ("Average security price grouped by sector", "PORTFOLIO_ANALYTICS", "positive"),
    ("Rank portfolios by total AUM", "PORTFOLIO_ANALYTICS", "positive"),
    ("What does the Balanced Portfolio hold?", "PORTFOLIO_ANALYTICS", "positive"),
    ("Which portfolios have more than 5 sectors?", "PORTFOLIO_ANALYTICS", "positive"),
    ("What is the target risk level of the ESG Sustainable Fund?", "PORTFOLIO_ANALYTICS", "positive"),
    ("Show me the securities held in Dividend Aristocrats Fund", "PORTFOLIO_ANALYTICS", "positive"),
    ("What are the risk metrics for the Growth Equity Fund?", "PORTFOLIO_ANALYTICS", "positive"),
    ("What is the total value of all holdings?", "PORTFOLIO_ANALYTICS", "positive"),
    # --- SECTOR_EXPOSURE
    ("What is the sector breakdown of the Growth Equity Fund?", "SECTOR_EXPOSURE", "positive"),
    ("Show me the equity exposure by sector for Balanced Portfolio", "SECTOR_EXPOSURE", "positive"),
    ("How much is the Tech Innovation Fund exposed to each sector?", "SECTOR_EXPOSURE", "positive"),
    ("Calculate exposure by sector for the Small Cap Value Fund", "SECTOR_EXPOSURE", "positive"),
    ("Sector weights of the Emerging Markets Fund", "SECTOR_EXPOSURE", "positive"),
    ("Which sectors dominate the ESG Sustainable Fund?", "SECTOR_EXPOSURE", "positive"),
    # --- UNSUPPORTED
    ("What time is it in Tokyo?", "UNSUPPORTED", "positive"),
    ("Who is the best footballer of all time?", "UNSUPPORTED", "positive"),
    ("Write a haiku about autumn", "UNSUPPORTED", "positive"),
    ("What is the tallest building in the world?", "UNSUPPORTED", "positive"),
    ("How do I fix a flat bicycle tyre?", "UNSUPPORTED", "positive"),
    ("Which cryptocurrency should I buy right now?", "UNSUPPORTED", "positive"),
    ("What's the temperature outside?", "UNSUPPORTED", "positive"),
    ("Tell me about the history of Rome", "UNSUPPORTED", "positive"),
    ("Who won the Oscar for best picture?", "UNSUPPORTED", "positive"),
    ("What is 15 percent of 240?", "UNSUPPORTED", "positive"),
    ("Remove the Apple holding from the Growth Equity Fund", "UNSUPPORTED", "positive"),
    # --- Hard negatives: analytical questions phrased like help / identity / small talk
    ("What can you tell me about the Tech Innovation Fund holdings?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("What can you say about the securities in Healthcare?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Could you tell me how many portfolios are active?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Can you show the top holdings of the International Equity Fund?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Help me understand the AUM of the high risk portfolios", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Who are the biggest holders in the Growth Equity Fund?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("What kind of securities does the Balanced Portfolio contain?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("What sort of risk does the Small Cap Value Fund carry?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("What things are in the Conservative Income Fund?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Can you help me calculate the exposure of Growth Equity Fund?", "SECTOR_EXPOSURE", "hard_negative"),
    ("What can you tell me about the sector exposure of Balanced Portfolio?", "SECTOR_EXPOSURE", "hard_negative"),
    ("Thanks, now show me the active portfolios", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Who are you comparing the portfolios against?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    # --- Mixed: conversational wrapper + analytical request (must reach the analytical pipeline)
    ("Hi! How many portfolios are there?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Hello! Which portfolios are active?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Hey, what is the total AUM for high risk portfolios?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Good morning, show the top 5 holdings in Growth Equity Fund", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Hi there, calculate sector exposure for Tech Innovation Fund", "SECTOR_EXPOSURE", "mixed"),
    ("Could you help me work out the Tech Innovation Fund exposure?", "SECTOR_EXPOSURE", "mixed"),
    ("What can you tell me about the Balanced Portfolio holdings?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Who are you and how many portfolios do we have?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("What can you do? Also list the active portfolios.", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Thank you. Which securities are in Technology?", "PORTFOLIO_ANALYTICS", "mixed"),
    # --- Ambiguous / too vague: the right answer is "ask for clarification"
    ("portfolio", "UNKNOWN", "ambiguous"),
    ("tell me more", "UNKNOWN", "ambiguous"),
    ("and the other one?", "UNKNOWN", "ambiguous"),
    ("what about it", "UNKNOWN", "ambiguous"),
    ("the fund", "UNKNOWN", "ambiguous"),
    ("explain", "UNKNOWN", "ambiguous"),
    ("numbers please", "UNKNOWN", "ambiguous"),
    ("compare them", "UNKNOWN", "ambiguous"),
    # --- Regression: official ground-truth questions Q1-Q10
    ("How many portfolios do we have in total?", "PORTFOLIO_ANALYTICS", "regression"),
    ("What are the names of all active portfolios?", "PORTFOLIO_ANALYTICS", "regression"),
    ("Which securities are in the Technology sector?", "PORTFOLIO_ANALYTICS", "regression"),
    ("What is the total Assets Under Management (AUM) for portfolios with high target risk level?", "PORTFOLIO_ANALYTICS", "regression"),
    ("Show me the top 5 holdings by cost basis in the Growth Equity Fund", "PORTFOLIO_ANALYTICS", "regression"),
    ("What is the average current price of securities in each sector?", "PORTFOLIO_ANALYTICS", "regression"),
    ("For each portfolio, show the total value of Technology sector holdings and what percentage it represents of the total portfolio value", "PORTFOLIO_ANALYTICS", "regression"),
    ("Find portfolios that have holdings in more than 5 different sectors and show their diversification metrics", "PORTFOLIO_ANALYTICS", "regression"),
    ("What are the sector exposures for the Tech Innovation Fund?", "SECTOR_EXPOSURE", "regression"),
    ("Calculate the sector exposure breakdown for international equity", "SECTOR_EXPOSURE", "regression"),
]


# ---------------------------------------------------------------------------------------------
# HOLDOUT: written after thresholds were chosen on VALIDATION and evaluated once, so it is an
# unbiased estimate. Do not tune thresholds or prototypes on it.
# ---------------------------------------------------------------------------------------------
HOLDOUT: List[Case] = [
    ("hey buddy", "GREETING", "positive"),
    ("Good afternoon everyone", "GREETING", "positive"),
    ("hello, hope you're well", "GREETING", "positive"),
    ("hi hi", "GREETING", "positive"),
    ("Thank you kindly", "THANKS", "positive"),
    ("cheers mate, that's great", "THANKS", "positive"),
    ("ok understood, appreciated", "THANKS", "positive"),
    ("thx a lot!", "THANKS", "positive"),
    ("Okay, bye", "GOODBYE", "positive"),
    ("Talk soon, goodbye", "GOODBYE", "positive"),
    ("until next time", "GOODBYE", "positive"),
    ("Please introduce yourself", "IDENTITY", "positive"),
    ("what should I address you as", "IDENTITY", "positive"),
    ("Are you some kind of AI model?", "IDENTITY", "positive"),
    ("who am i chatting with", "IDENTITY", "positive"),
    ("What is your purpose?", "CAPABILITIES", "positive"),
    ("how do I use you", "CAPABILITIES", "positive"),
    ("What functions do you offer?", "CAPABILITIES", "positive"),
    ("Could you walk me through what this app does?", "CAPABILITIES", "positive"),
    ("what type of portfolio questions are supported", "CAPABILITIES", "positive"),
    ("What can I do with this assistant?", "CAPABILITIES", "positive"),
    ("tell me what you are good at", "CAPABILITIES", "positive"),
    ("how many funds do we manage", "PORTFOLIO_ANALYTICS", "positive"),
    ("Which portfolios carry high risk?", "PORTFOLIO_ANALYTICS", "positive"),
    ("What is the AUM of the Balanced Portfolio?", "PORTFOLIO_ANALYTICS", "positive"),
    ("Show securities in the Energy sector", "PORTFOLIO_ANALYTICS", "positive"),
    ("What are the largest positions in Tech Innovation Fund?", "PORTFOLIO_ANALYTICS", "positive"),
    ("list every portfolio and its strategy", "PORTFOLIO_ANALYTICS", "positive"),
    ("What is the cheapest security by current price?", "PORTFOLIO_ANALYTICS", "positive"),
    ("How many holdings are there in total?", "PORTFOLIO_ANALYTICS", "positive"),
    ("Sector exposure of the Dividend Aristocrats Fund, please", "SECTOR_EXPOSURE", "positive"),
    ("Break down the Conservative Income Fund by sector", "SECTOR_EXPOSURE", "positive"),
    ("What share of the Fixed Income Plus fund sits in each sector?", "SECTOR_EXPOSURE", "positive"),
    ("What is the Capital of Japan?", "UNSUPPORTED", "positive"),
    ("Will it snow this weekend?", "UNSUPPORTED", "positive"),
    ("Who scored the winning goal in the final?", "UNSUPPORTED", "positive"),
    ("Give me a recipe for biryani", "UNSUPPORTED", "positive"),
    ("What's the best laptop under 1000 dollars?", "UNSUPPORTED", "positive"),
    ("Write a short story about a dragon", "UNSUPPORTED", "positive"),
    ("How many moons does Jupiter have?", "UNSUPPORTED", "positive"),
    ("Should I put all my savings into Tesla?", "UNSUPPORTED", "positive"),
    ("Can you tell me what the Capital of Japan is?", "UNSUPPORTED", "hard_negative"),
    ("What can you tell me about the weather in Paris?", "UNSUPPORTED", "hard_negative"),
    ("Who are you voting for?", "UNSUPPORTED", "hard_negative"),
    ("What can you tell me about the Growth Equity Fund's securities?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Can you list the active portfolios for me?", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Tell me which securities the Balanced Portfolio holds", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Help me see how much AUM sits in high-risk portfolios", "PORTFOLIO_ANALYTICS", "hard_negative"),
    ("Hello, could you show the average price by sector?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Hey! How many portfolios are active?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Hi, what's the exposure breakdown for Growth Equity Fund?", "SECTOR_EXPOSURE", "mixed"),
    ("Thanks! Next, show the securities in Technology", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Can you help me list the top holdings in Growth Equity Fund?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("Who are you? Also what is the total AUM?", "PORTFOLIO_ANALYTICS", "mixed"),
    ("ok then", "THANKS", "positive"),
    ("What do you think?", "UNKNOWN", "ambiguous"),
    ("that thing from before", "UNKNOWN", "ambiguous"),
    ("any update?", "UNKNOWN", "ambiguous"),
    ("which one is better", "UNKNOWN", "ambiguous"),
    ("show it", "UNKNOWN", "ambiguous"),
    ("give me the figures", "UNKNOWN", "ambiguous"),
    ("and for them?", "UNKNOWN", "ambiguous"),
]
