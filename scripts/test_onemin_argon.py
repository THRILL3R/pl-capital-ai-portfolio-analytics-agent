
import os
import sys
import json
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("ONEMIN_API_KEY")

if not api_key:
    sys.exit("ERROR: ONEMIN_API_KEY not found in .env")

print("1min.AI Gemini 4 Argon test")
print("API key: Present")
print("Model: gemini-4-argon")

try:
    response = requests.post(
        "https://api.1min.ai/api/chat-with-ai",
        headers={
            "API-KEY": api_key,
            "Content-Type": "application/json",
        },
        json={
            "type": "UNIFY_CHAT_WITH_AI",
            "model": "gemini-4-argon",
            "promptObject": {
                "prompt": (
                    "You are a financial AI assistant. "
                    "Explain how a portfolio analytics agent "
                    "calculates sector exposure in three sentences."
                )
            },
        },
        timeout=300,
    )

    if not response.ok:
        print("FAILED: HTTP", response.status_code)
        print(response.text[:1000])
        sys.exit(1)

    data = response.json()
    result = (
        data.get("aiRecord", {})
        .get("aiRecordDetail", {})
        .get("resultObject")
    )

    if not result:
        print("Request succeeded, but response format was unexpected.")
        print("Top-level response keys:", list(data.keys()))
        sys.exit(1)

    print("\nSUCCESS: Gemini 4 Argon responded!\n")
    print(result[0] if isinstance(result, list) else result)

except requests.RequestException as error:
    print("REQUEST ERROR:", str(error))
    sys.exit(1)
except (ValueError, KeyError, TypeError) as error:
    print("RESPONSE ERROR:", str(error))
    sys.exit(1)
