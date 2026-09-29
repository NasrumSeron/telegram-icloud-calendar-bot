"""
The simplest possible Gemini API call -- no schema, no pydantic, nothing
beyond "send one line of text, get one line back". This isolates whether
the problem is the API key/account/basic connectivity, or something
specific to our structured-output (response_schema) setup in
gemini_parse.py.

Run: python tests/live/test_minimal_gemini.py   (or the venv's python.exe on Windows)
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))

if not _os.environ.get("RUN_LIVE_TESTS"):
    print("SKIPPED: live test (real network / Gemini / iCloud). Set RUN_LIVE_TESTS=1 and fill .env to run it.")
    raise SystemExit(0)

import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

key = os.environ["GEMINI_API_KEY"]
print(f"Loaded GEMINI_API_KEY: starts with '{key[:6]}...', length {len(key)}", flush=True)

client = genai.Client(
    api_key=key,
    http_options=types.HttpOptions(timeout=30_000),
)

print("Sending minimal request (no schema)...", flush=True)
start = time.time()
try:
    response = client.models.generate_content(
        model="gemini-3.7-flash",
        contents="Say hello in exactly one word.",
    )
    print(f"Got response in {time.time() - start:.1f}s: {response.text!r}", flush=True)
except Exception as exc:  # noqa: BLE001
    print(f"FAILED after {time.time() - start:.1f}s: {exc!r}", flush=True)
