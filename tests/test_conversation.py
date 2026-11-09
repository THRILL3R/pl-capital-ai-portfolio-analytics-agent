"""
Tests for conversational intents (greetings, thanks, goodbye) in PortfolioAgent.

Greetings are answered by the agent itself, before routing: no SQLQueryTool, Gemini or
ExposureCalculator call is made, in either --no-llm or Gemini-enabled mode.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as cli
from portfolio_agent.agent import CONVERSATION_EXAMPLE_QUESTIONS, PortfolioAgent
from portfolio_agent.database import PortfolioDatabase
from portfolio_agent.sql_generator import GeminiSQLGenerator
from portfolio_agent.tools import SQLQueryTool

REQUIRED_KEYS = {"status", "question", "selected_tool", "route_reason", "result", "error"}

GREETINGS = [
    "hi", "Hi!", "hello", "HELLO", "Hello!!", "hey", "Hey there", "hello there",
    "good morning", "Good afternoon", "good evening!", "Hi, how are you?", "  hi  ",
]
THANKS = ["thanks", "Thank you!", "thank you so much", "thx", "ok", "okay thanks", "got it", "great, thanks", "perfect"]
GOODBYES = ["bye", "Goodbye!", "see you later"]
NOT_CONVERSATION = [
    "What's the weather today?",
    "Who won the football match last night?",
    "What is the capital of France?",
    "hello world",
    "hi, what is the weather",
    "Tell me a joke",
    "hey, show me the top 5 holdings by cost basis in the Growth Equity Fund",
    "thanks for the sector exposure for Tech Innovation Fund",
    "What is the meaning of life?",
]


class TestConversationDoesNotTouchTools(unittest.TestCase):
    """Greetings must never reach SQLQueryTool, Gemini or ExposureCalculator."""

    def setUp(self):
        self.sql_tool = MagicMock()
        self.exposure_tool = MagicMock()
        self.agent = PortfolioAgent(db=MagicMock(), sql_tool=self.sql_tool, exposure_tool=self.exposure_tool)

    def test_greetings_return_conversation_response(self):
        for q in GREETINGS:
            with self.subTest(q=q):
                resp = self.agent.answer_question(q)
                self.assertEqual(resp["status"], "conversation")
                self.assertTrue(REQUIRED_KEYS.issubset(resp.keys()))
                self.assertIsNone(resp["selected_tool"])
                self.assertIsNone(resp["error"])
                self.assertEqual(resp["question"], q.strip())
                self.assertEqual(resp["result"]["tool"], "conversation")
                self.assertEqual(resp["result"]["intent"], "greeting")
                self.assertTrue(resp["result"]["message"])
        self.sql_tool.execute.assert_not_called()
        self.exposure_tool.calculate.assert_not_called()

    def test_greeting_offers_three_or_four_supported_example_questions(self):
        resp = self.agent.answer_question("hello")
        examples = resp["result"]["suggested_questions"]
        self.assertIn(len(examples), (3, 4))
        self.assertEqual(examples, CONVERSATION_EXAMPLE_QUESTIONS)

    def test_thanks_and_acknowledgements(self):
        for q in THANKS:
            with self.subTest(q=q):
                resp = self.agent.answer_question(q)
                self.assertEqual(resp["status"], "conversation")
                self.assertEqual(resp["result"]["intent"], "thanks")
                self.assertIsNone(resp["selected_tool"])
        self.sql_tool.execute.assert_not_called()
        self.exposure_tool.calculate.assert_not_called()

    def test_goodbye_has_no_example_questions(self):
        for q in GOODBYES:
            with self.subTest(q=q):
                resp = self.agent.answer_question(q)
                self.assertEqual(resp["status"], "conversation")
                self.assertEqual(resp["result"]["intent"], "goodbye")
                self.assertEqual(resp["result"]["suggested_questions"], [])

    def test_unrelated_questions_are_not_conversation(self):
        self.sql_tool.execute.return_value = {"status": "unsupported", "error": "x"}
        for q in NOT_CONVERSATION:
            with self.subTest(q=q):
                self.sql_tool.execute.reset_mock()
                self.exposure_tool.calculate.reset_mock()
                resp = self.agent.answer_question(q)
                self.assertNotEqual(resp["status"], "conversation")
                self.assertTrue(self.sql_tool.execute.called or self.exposure_tool.calculate.called)

    def test_empty_input_still_an_error(self):
        for q in ["", "   "]:
            self.assertEqual(self.agent.answer_question(q)["status"], "error")

    def test_example_questions_do_not_trigger_conversation(self):
        for q in CONVERSATION_EXAMPLE_QUESTIONS:
            self.assertIsNone(PortfolioAgent._detect_conversation_intent(q))


class TestConversationInBothModes(unittest.TestCase):
    """Real database; greeting handling is identical with and without Gemini, and never calls Gemini."""

    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_no_llm_mode(self):
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False))
        resp = agent.answer_question("hello")
        self.assertEqual(resp["status"], "conversation")
        self.assertIsNone(resp["selected_tool"])

    def test_gemini_enabled_mode_makes_no_gemini_call(self):
        client = MagicMock()
        gemini = GeminiSQLGenerator(client=client, db=self.db)
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, gemini_generator=gemini, use_gemini=True))
        for q in ["hi", "Good morning", "thanks"]:
            self.assertEqual(agent.answer_question(q)["status"], "conversation")
        client.models.generate_content.assert_not_called()

    def test_unsupported_question_handling_is_preserved(self):
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False))
        for q in ["What is the meaning of life?", "What's the weather today?"]:
            resp = agent.answer_question(q)
            self.assertEqual(resp["status"], "unsupported")
            self.assertEqual(resp["selected_tool"], "sql_query")
            self.assertIn("not recognized", resp["error"])

    def test_normal_portfolio_queries_still_route_normally(self):
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False))
        resp = agent.answer_question("How many portfolios do we have in total?")
        self.assertEqual((resp["status"], resp["selected_tool"]), ("success", "sql_query"))
        self.assertEqual(resp["result"]["rows"][0]["COUNT(*)"], 13)
        resp = agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")
        self.assertEqual((resp["status"], resp["selected_tool"]), ("success", "exposure_calculator"))
        # A greeting prefix with a real question is a real question.
        resp = agent.answer_question("hello, how many portfolios do we have in total?")
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "sql_query")


class TestConversationCli(unittest.TestCase):
    def test_pretty_output_shows_message_and_examples(self):
        resp = PortfolioAgent(db=MagicMock(), sql_tool=MagicMock(), exposure_tool=MagicMock()).answer_question("hi")
        text = cli._format_pretty(resp)
        self.assertIn("conversation", text)
        self.assertIn(resp["result"]["message"], text)
        for q in CONVERSATION_EXAMPLE_QUESTIONS:
            self.assertIn(q, text)

    def test_one_shot_greeting_exits_zero(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--question", "hello", "--no-llm", "--pretty"])
        self.assertEqual(code, 0)
        self.assertIn("conversation", out.getvalue())


if __name__ == "__main__":
    unittest.main()
