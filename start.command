#!/usr/bin/env bash
# Launcher for the signal traffic-light indicator.
#
# Detaches the app from this terminal so it keeps running after you close
# the window (double-clicking a .command opens a Terminal window; without
# this the app would die when that window closes).
cd "$(dirname "$0")" || exit 1

if pgrep -f "signal_app.py" >/dev/null 2>&1; then
  echo "signal indicator is already running."
  exit 0
fi

nohup .venv/bin/python signal_app.py >/tmp/claude-signal.log 2>&1 &
disown
echo "signal indicator started — you can close this window safely."
