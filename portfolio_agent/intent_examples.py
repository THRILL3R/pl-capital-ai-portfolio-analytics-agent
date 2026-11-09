"""
Prototype examples for the local semantic intent classifier.

Each intent is described by representative user messages. The classifier embeds them once and
compares incoming messages by cosine similarity, so paraphrases the list does not contain still
match. Add examples here (not rules in code) to teach the classifier new phrasings.

Hard negatives are deliberate: analytical questions that *sound* like help or small-talk requests
("What can you tell me about Growth Equity Fund holdings?") are listed under the analytical
intent so they are not mistaken for "what can you do?".

None of these strings are held out for calibration: see tests/intent_validation_set.py.
"""

from __future__ import annotations

from typing import Dict, List

GREETING: List[str] = [
    "hi", "hello", "hey", "hey there", "hello there", "hiya", "howdy", "yo",
    "good morning", "good afternoon", "good evening", "morning!", "hii", "helloo", "hey hey",
    "hi, how are you?", "hello, how are you doing today?", "greetings", "hi team", "sup",
    "hi assistant", "hello bot", "heya", "good day", "hey, anyone there?",
]

THANKS: List[str] = [
    "thanks", "thank you", "thank you so much", "thanks a lot", "thx", "ty", "many thanks",
    "cheers", "appreciate it", "much appreciated", "great, thanks", "perfect, thank you",
    "awesome, thanks for the help", "ok thanks", "got it, thanks", "that was helpful, thank you",
    "ok", "okay", "got it", "cool", "nice", "great", "perfect", "sounds good", "understood",
    "alright", "thanks, that helps", "that makes sense, thanks",
]

GOODBYE: List[str] = [
    "bye", "goodbye", "bye bye", "see you", "see you later", "see you soon", "take care",
    "good night", "have a nice day", "talk to you later", "catch you later", "that's all for now",
    "i'm done, bye", "cya", "farewell", "that will be all, goodbye", "ttyl",
    "have a great day", "have a good evening", "enjoy the rest of your day", "i'm off now",
    "i'll leave it here, bye", "signing off", "later!", "bye for today, thanks",
]

IDENTITY: List[str] = [
    "who are you?", "what is your name?", "what's your name?", "introduce yourself",
    "tell me about yourself", "what assistant am I speaking to?", "who am I talking to?",
    "are you a bot?", "are you a human or a machine?", "are you an AI?", "what are you?",
    "who built you?", "who made this assistant?", "what should I call you?",
    "can you introduce yourself?", "who is this?", "what is this assistant called?",
    "are you a real person?", "what kind of assistant are you?", "tell me who you are",
    "wat is ur name", "whos this", "who r u", "are you a chatbot?", "is this a bot or a human?",
    "am I talking to a robot?", "what is your role here?", "what are you called?",
    "tell me a bit about who you are", "are you ChatGPT?", "who developed you?",
]

CAPABILITIES: List[str] = [
    "what can you do?", "what things can you do?", "what things can you help me with?",
    "how can you help me?", "what can I explore here?", "tell me about your features",
    "what kind of portfolio analysis is available?", "how should I use this assistant?",
    "what sort of questions can this application answer?", "what questions can I ask?",
    "what can I ask you?", "help", "help me", "i need help", "how does this work?",
    "what are you capable of?", "what are your capabilities?", "what do you support?",
    "show me what you can do", "give me some example questions", "what should I ask?",
    "what kind of questions do you understand?", "how do I get started?", "what is this tool for?",
    "can you show me examples of what I can ask?", "what features do you have?",
    "what is this app for?", "what topics can you cover?", "what can u do", "wat can i ask u",
    "what's possible here?", "guide me on how to use this", "what analysis can you run?",
]

PORTFOLIO_ANALYTICS: List[str] = [
    "How many portfolios are available?",
    "How many portfolios do we have?",
    "What are the names of all active portfolios?",
    "List the portfolios that are currently active.",
    "Show the top holdings in Growth Equity Fund.",
    "Show me the top 5 holdings by cost basis in a portfolio.",
    "What is the total AUM?",
    "What is the total assets under management for portfolios with high risk?",
    "Which securities belong to Technology?",
    "Which securities are in the Healthcare sector?",
    "Compare portfolios by risk.",
    "Compare the portfolios by AUM.",
    "What is the average current price of securities in each sector?",
    "Which portfolios hold the most stocks?",
    "Find portfolios that have holdings in more than 5 different sectors.",
    "Show the diversification metrics for each portfolio.",
    "For each portfolio, what percentage of its value is in Technology?",
    "What is the total market value of the Balanced Portfolio?",
    "List all holdings in Tech Innovation Fund.",
    "Which portfolio has the highest cost basis?",
    "Show me every security with its sector and current price.",
    "How many holdings does the International Equity Fund have?",
    "What is the strategy type of each portfolio?",
    "Show the performance records for the Growth Equity Fund.",
    "Which portfolios have a conservative target risk level?",
    "how many portfolioz do we have in total",
    "top holdings growth equity fund cost basis",
    # Hard negatives: analytical questions that start like help / identity requests.
    "What can you tell me about Growth Equity Fund holdings?",
    "What can you show me about the securities in the Technology sector?",
    "Can you tell me how many portfolios we have?",
    "Can you show me which portfolios are active?",
    "Can you tell me the total AUM of the high risk portfolios?",
    "Tell me about the Balanced Portfolio holdings.",
    "What do you know about the Tech Innovation Fund holdings?",
    "Help me find the portfolio with the highest AUM.",
    "Who are the largest holders by cost basis in the Growth Equity Fund?",
    "What assets does the Conservative Income Fund hold?",
    "Hi, how many portfolios do we have?",
    "Hello, show me the active portfolios.",
    # Hypothetical stress / scenario requests (answered by the deterministic scenario tool).
    "Stress test the Balanced Portfolio under a 15% market decline.",
    "What happens to the Technology exposure if that sector falls 30%?",
    "Run a severe market stress scenario for the Growth Equity Fund.",
    "Apply an energy shock to the International Equity Fund.",
]

SECTOR_EXPOSURE: List[str] = [
    "Calculate sector exposure for Balanced Portfolio.",
    "What are the sector exposures for the Tech Innovation Fund?",
    "Show the sector exposure breakdown for the Growth Equity Fund.",
    "Calculate the sector exposure breakdown for international equity.",
    "What is the sector allocation of the Balanced Portfolio?",
    "Show sector weights for the ESG Sustainable Fund.",
    "How is the Dividend Aristocrats Fund distributed across sectors?",
    "What percentage is invested in each sector in the Small Cap Value Fund?",
    "Give me the sector mix of the Emerging Markets Fund.",
    "Exposure breakdown by sector for the Conservative Income Fund.",
    "Show exposure for Fixed Income Plus.",
    "Which sectors is the Tech Innovation Fund invested in, and how much?",
    "Sector distribution of Growth Equity Fund please.",
    "How much of the Balanced Portfolio is in technology stocks?",
    "calc sector exposure growth equity",
    # Mixed / help-shaped requests that contain a real calculation request.
    "Can you help me calculate Balanced Portfolio exposure?",
    "Help me work out the sector exposure of the Growth Equity Fund.",
    "Hi, show the sector exposure for Tech Innovation Fund.",
    "Please calculate the equity sector breakdown for portfolio 3.",
    # Hybrid requests: data plus exposure in one question.
    "Show the holdings and sector exposure breakdown for the Balanced Portfolio.",
    "List the securities and the sector exposure of the Tech Innovation Fund.",
    "Give me the holdings together with the sector allocation for the Growth Equity Fund.",
    "What are the holdings of the ESG Sustainable Fund and its sector exposures?",
]

UNSUPPORTED: List[str] = [
    # weather / time / location
    "What's the weather today?", "What is the weather like in Mumbai?", "Will it rain tomorrow?",
    "How hot is it in London right now?", "What is the date today?", "What's the time in New York?",
    "Where is the nearest petrol station?", "Give me directions to the airport.",
    # sports / entertainment
    "Who won the football match last night?", "Who won the cricket world cup?", "What's the latest sports score?",
    "Who is the greatest tennis player ever?", "When is the next Formula 1 race?", "Recommend a good movie.",
    "Who sang this song?", "What should I watch on Netflix tonight?", "Which team won the NBA finals?",
    # general knowledge / education
    "What is the capital of France?", "Who was the first president of the United States?",
    "What is the meaning of life?", "How tall is Mount Everest?", "Who is the prime minister of India?",
    "Who painted the Mona Lisa?", "How many continents are there?", "What is photosynthesis?",
    "Explain the theory of relativity.", "When did World War 2 end?", "What is the speed of light?",
    "Who discovered penicillin?", "Why is the sky blue?", "What is the population of China?",
    "Describe the French revolution.", "What is the longest river in the world?",
    # creative / chit-chat
    "Tell me a joke.", "Write me a poem about the sea.", "Tell me a story.", "Sing me a song.",
    "Write an essay about climate change.", "Compose a limerick about cats.", "Make up a riddle.",
    "What is your favourite colour?", "Do you like pizza?", "Are you happy today?",
    # everyday help
    "How do I bake a chocolate cake?", "Give me a recipe for pasta.", "What is the best restaurant near me?",
    "How do I lose weight fast?", "How do I change a car tyre?", "How can I learn to play the guitar?",
    "What should I cook for dinner?", "Suggest a name for my dog.", "Plan a holiday in Goa for me.",
    "Book me a flight to Delhi.", "Order me a pizza.", "Set an alarm for 7 am.", "Send an email to my boss.",
    "Translate this sentence into Spanish.", "How do you say thank you in Japanese?",
    # maths / code (not portfolio analytics)
    "What is the square root of 144?", "What is 12 times 13?", "Solve x squared equals 49.",
    "Write Python code to reverse a string.", "How do I center a div in CSS?", "Explain recursion.",
    # news / tech / health
    "What is the latest news today?", "Who is the CEO of Tesla?", "How does a vaccine work?",
    "What are the symptoms of the flu?", "Which smartphone should I buy?", "What is blockchain?",
    # investment advice / predictions / other finance not answerable from this dataset
    "Should I buy Apple stock tomorrow?", "Which stock will double next month?", "Give me a hot stock tip.",
    "Predict the S&P 500 for next year.", "Is now a good time to invest in gold?",
    "What is the exchange rate between dollars and euros today?", "What will Bitcoin be worth next year?",
    "Tell me today's Nifty closing price.", "How do I file my income tax return?",
    "Which mutual fund will give the best returns?",
    # data modification / destructive requests (not supported: the agent is read-only)
    "Delete all the holdings from the database.", "Update the price of Apple to 500.",
    "Drop the portfolios table.", "Add a new portfolio called Alpha Fund.", "Change the status of the Growth Equity Fund to inactive.",
    "Insert a new security into the database.", "Edit the AUM of the Balanced Portfolio.",
    # gibberish
    "hello world", "asdf qwerty zxcv", "lorem ipsum dolor sit amet", "blah blah blah", "???",
]

# Vague or context-dependent messages: nothing actionable on their own, so the agent asks the user to be specific.
UNKNOWN: List[str] = [
    "what about the other one?", "and that one?", "more", "go on", "continue", "tell me more about that",
    "what do you mean?", "can you clarify?", "why?", "how?", "really?", "and then?", "what else?",
    "show me", "do it again", "same but different", "that one", "the previous one", "compare those",
    "portfolios", "the funds", "a fund", "my portfolio", "numbers", "details",
    "i want information", "give me information", "i have a question", "can I ask something?", "um",
    "hmm", "what is this about", "which one", "so?", "what next?", "any ideas?", "anything else?",
]

INTENT_EXAMPLES: Dict[str, List[str]] = {
    "GREETING": GREETING,
    "THANKS": THANKS,
    "GOODBYE": GOODBYE,
    "IDENTITY": IDENTITY,
    "CAPABILITIES": CAPABILITIES,
    "PORTFOLIO_ANALYTICS": PORTFOLIO_ANALYTICS,
    "SECTOR_EXPOSURE": SECTOR_EXPOSURE,
    "UNSUPPORTED": UNSUPPORTED,
    "UNKNOWN": UNKNOWN,
}
