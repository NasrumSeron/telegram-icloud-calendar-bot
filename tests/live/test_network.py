"""
Quick, dependency-light network check to isolate WHERE a hang is coming from:
DNS resolution, raw HTTPS connectivity, or the Gemini API call itself.

Run: python tests/live/test_network.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))

if not _os.environ.get("RUN_LIVE_TESTS"):
    print("SKIPPED: live test (real network / Gemini / iCloud). Set RUN_LIVE_TESTS=1 and fill .env to run it.")
    raise SystemExit(0)

import socket
import time
import urllib.request

HOST = "generativelanguage.googleapis.com"


def check_dns() -> None:
    print(f"[1/2] Resolving DNS for {HOST} ...", flush=True)
    start = time.time()
    try:
        ip = socket.gethostbyname(HOST)
        print(f"      OK: {HOST} -> {ip} ({time.time() - start:.1f}s)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"      FAILED after {time.time() - start:.1f}s: {exc}", flush=True)
        print("      -> DNS resolution itself is broken/blocked in this container.", flush=True)


def check_https() -> None:
    print(f"[2/2] Making a raw HTTPS request to https://{HOST}/ (10s timeout) ...", flush=True)
    start = time.time()
    try:
        resp = urllib.request.urlopen(f"https://{HOST}/", timeout=10)
        print(f"      OK: got HTTP {resp.status} ({time.time() - start:.1f}s)", flush=True)
        print("      -> Outbound HTTPS works. If the Gemini test still hangs, it's API/key-specific, not network.", flush=True)
    except urllib.error.HTTPError as exc:
        # An HTTP error (even 403/404) still means we reached Google's servers.
        print(f"      Got HTTP {exc.code} ({time.time() - start:.1f}s) -- this is FINE, it means the connection worked.", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"      FAILED after {time.time() - start:.1f}s: {exc}", flush=True)
        print("      -> Outbound HTTPS from this container is blocked or extremely slow (check the host firewall/router).", flush=True)


if __name__ == "__main__":
    check_dns()
    check_https()
