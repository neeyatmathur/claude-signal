#!/usr/bin/env bash
#
# Installer for the "signal" Claude Code traffic-light indicator.
#
#   1. creates the runtime state dir (~/.claude/signal/sessions)
#   2. builds a venv and installs pyobjc (for the GUI only)
#   3. merges the hooks into ~/.claude/settings.json WITHOUT clobbering any
#      existing hooks (e.g. the rtk PreToolUse/Bash hook). Backs up first.
#
# Re-runnable: the hook merge is idempotent and self-updating.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
STATE_DIR="$HOME/.claude/signal/sessions"

echo "==> signal installer"
echo "    project: $PROJECT_DIR"

# 1. State directory ---------------------------------------------------------
mkdir -p "$STATE_DIR"
echo "==> state dir ready: $STATE_DIR"

# 2. venv + pyobjc (GUI only; the hook uses system /usr/bin/python3) ---------
if [ ! -d "$PROJECT_DIR/.venv" ]; then
  echo "==> creating venv"
  python3 -m venv "$PROJECT_DIR/.venv"
fi
echo "==> installing pyobjc (this can take a moment)"
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# 3. Merge hooks into ~/.claude/settings.json --------------------------------
chmod +x "$PROJECT_DIR/signal_hook.py" "$PROJECT_DIR/start.command" 2>/dev/null || true

PROJECT_DIR="$PROJECT_DIR" SETTINGS="$SETTINGS" /usr/bin/python3 <<'PYEOF'
import json
import os
import shutil
import time

project_dir = os.environ["PROJECT_DIR"]
settings_path = os.environ["SETTINGS"]
base_cmd = "/usr/bin/python3 {}/signal_hook.py".format(project_dir)

# event -> (matcher, state)
EVENTS = {
    "SessionStart":     ("",  "idle"),
    "UserPromptSubmit": ("",  "amber"),
    "PreToolUse":       ("*", "amber"),
    "PostToolUse":      ("*", "amber"),
    "Notification":     ("",  "notify"),
    "Stop":             ("",  "green"),
    "SessionEnd":       ("",  "end"),
}

# Load (or initialise) settings.
if os.path.exists(settings_path):
    with open(settings_path) as fh:
        settings = json.load(fh)
    backup = "{}.bak.{}".format(settings_path, int(time.time()))
    shutil.copy2(settings_path, backup)
    print("==> backed up settings to {}".format(backup))
else:
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = {}

hooks = settings.setdefault("hooks", {})

for event, (matcher, state) in EVENTS.items():
    arr = hooks.get(event, [])
    if not isinstance(arr, list):
        arr = []

    # Strip any previously-installed signal groups (so re-runs update cleanly),
    # while preserving every other hook (e.g. rtk's Bash hook).
    cleaned = []
    for group in arr:
        ghooks = [
            h for h in group.get("hooks", [])
            if "signal_hook.py" not in (h.get("command") or "")
        ]
        if ghooks:
            group["hooks"] = ghooks
            cleaned.append(group)

    cleaned.append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": "{} {}".format(base_cmd, state)}],
    })
    hooks[event] = cleaned

with open(settings_path, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")

print("==> hooks merged into {}".format(settings_path))
PYEOF

echo ""
echo "==> Done. Start the indicator with:"
echo "      open \"$PROJECT_DIR/start.command\"     (or double-click it in Finder)"
echo ""
echo "    Optional alias (add to ~/.zshrc):"
echo "      alias claude-signal='$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/signal_app.py >/dev/null 2>&1 &'"
echo ""
echo "    Hooks apply to NEW Claude Code sessions started after this point."
