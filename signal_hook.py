#!/usr/bin/env python3
"""Claude Code hook entrypoint for the "signal" traffic-light indicator.

Reads the hook JSON payload from stdin and writes a tiny per-session state
file that the floating UI (signal_app.py) watches.

Usage (wired up via ~/.claude/settings.json hooks):
    /usr/bin/python3 signal_hook.py <state>

where <state> is one of: idle | amber | red | green | end

Design notes:
- Dependency-free (stdlib only) so it runs under the system /usr/bin/python3
  with zero setup — hooks must never need a venv.
- Never raises: any failure is swallowed and we always exit 0, so a broken
  indicator can never disrupt a Claude Code session.
- Writes are atomic (temp file + os.replace) so the UI never reads a
  half-written file.
"""

import json
import os
import sys
import time

# State files live outside the project repo, user-global and untracked.
STATE_DIR = os.path.expanduser("~/.claude/signal/sessions")


def main() -> None:
    state = sys.argv[1] if len(sys.argv) > 1 else "amber"

    # Read and parse the hook payload from stdin. Be lenient: if anything is
    # missing or malformed, fall back to sensible defaults rather than failing.
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        pass

    payload = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

    session_id = str(payload.get("session_id") or "unknown")
    cwd = str(payload.get("cwd") or os.getcwd())
    label = os.path.basename(cwd.rstrip("/")) or cwd

    # The Notification event fires both for permission prompts AND for the
    # "waiting for your input" idle prompt. Classify by message so an idle
    # session shows green (ready/your turn), not red (needs permission).
    event = str(payload.get("hook_event_name") or "")
    if event == "Notification" or state == "notify":
        msg = str(payload.get("message") or "").lower()
        needs_permission = any(
            kw in msg
            for kw in ("permission", "approve", "wants to", "allow", "grant", "needs your")
        )
        # permission -> red; idle/waiting-for-input -> steady green ("ready")
        state = "red" if needs_permission else "ready"

    os.makedirs(STATE_DIR, exist_ok=True)
    target = os.path.join(STATE_DIR, _safe_name(session_id) + ".json")

    # SessionEnd removes the row entirely.
    if state == "end":
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        return

    record = {
        "state": state,
        "cwd": cwd,
        "label": label,
        "session_id": session_id,
        "event": str(payload.get("hook_event_name") or ""),
        "ts": time.time(),
    }

    tmp = target + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(record, fh)
    os.replace(tmp, target)


def _safe_name(session_id: str) -> str:
    """Make a session id safe to use as a filename."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in session_id)[:128]


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never break the session. Stay silent, exit clean.
        pass
    sys.exit(0)
