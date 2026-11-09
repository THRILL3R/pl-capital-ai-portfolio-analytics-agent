"""
Unit test suite for PL Capital AI Portfolio Analytics Agent.

The automated suite is hermetic: it must never make live Gemini API calls, even
when a developer has GEMINI_API_KEY in their shell or in .env. Gemini behaviour
is tested with mocks; live verification lives in scripts/gemini_smoke_test.py.
"""

import os

os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)
# Inherited by CLI subprocess tests: prevents main.py from loading .env.
os.environ["PORTFOLIO_AGENT_SKIP_DOTENV"] = "1"
