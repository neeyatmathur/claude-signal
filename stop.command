#!/usr/bin/env bash
# Optional: double-click to quit the signal indicator.
# (You can also use the hover ✕ button on the panel.)
if pkill -f "signal_app.py"; then
  echo "signal indicator stopped."
else
  echo "signal indicator was not running."
fi
