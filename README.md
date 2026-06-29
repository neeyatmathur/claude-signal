# signal — a traffic light for Claude Code

A small always-on-top indicator pinned to the top-right of your Mac screen that
shows, **per Claude Code session**, what each one is doing right now:

| Light | Meaning |
|-------|---------|
| 🟡 **amber** (steady) | Claude is working / processing |
| 🔴 **red** (blinking) | Claude needs permission |
| 🟢 **green** (blinking) | Claude just finished — response is ready |
| 🟢 **green** (steady) | Idle, waiting for your next input |

Each running session gets its own row, labeled by its project folder, so when
you're juggling several sessions (or working in another tab) you can glance at
the corner and see which one needs you. The panel never steals focus.

> **Requirements:** macOS · Python 3 (the system `/usr/bin/python3` is fine — `install.sh` builds the venv).

## How it works

Claude Code [hooks](https://code.claude.com/docs/en/hooks) fire on lifecycle
events. A tiny hook script writes a per-session state file; a floating PyObjC
panel watches those files and draws the lights.

```
Claude session ─(hook)→ signal_hook.py → ~/.claude/signal/sessions/<id>.json
                                                  │ (polled 4×/sec)
                                                  ▼
                                          signal_app.py (floating panel)
```

| Hook event | Light |
|---|---|
| `SessionStart` | dim (idle) |
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | amber |
| `Notification` (permission / idle) | red |
| `Stop` | green |
| `SessionEnd` | row removed |

## Install

```bash
bash install.sh
```

This creates `~/.claude/signal/sessions/`, builds a `.venv` with pyobjc, and
**merges** the hooks into `~/.claude/settings.json` (existing hooks such as the
`rtk` Bash hook are preserved; a timestamped backup is made first).

Hooks take effect for **new** Claude Code sessions started afterward.

## Run

```bash
open start.command        # or double-click start.command in Finder
```

The app launches **detached**, so you can safely close the terminal/window it
opened — the indicator keeps running. To quit it, use the hover **✕** button on
the panel, double-click **`stop.command`**, or run `pkill -f signal_app.py`.

Optional alias (add to `~/.zshrc`):

```bash
alias claude-signal='~/signal/.venv/bin/python ~/signal/signal_app.py >/dev/null 2>&1 &'
```

The window is draggable (click-and-drag anywhere on it) if you want to nudge it.
Hover to reveal the app **✕**/**–** controls and a per-row **✕** to dismiss a
single session's light.

## Test without a real session

```bash
echo '{"session_id":"t1","cwd":"/Users/me/api-gw","hook_event_name":"Stop"}' \
  | /usr/bin/python3 signal_hook.py green
```

Repeat with `amber` (steady), `red` (blinks), and a second `session_id` to see
stacked rows. Run with `end` to remove a row.

## Uninstall

Restore the most recent `~/.claude/settings.json.bak.*` backup (or delete the
`signal_hook.py` hook entries), then `rm -rf ~/.claude/signal` and this folder.

## Files

- `signal_hook.py` — dependency-free hook entrypoint (system python3)
- `signal_app.py` — floating PyObjC traffic-light UI
- `install.sh` — venv + idempotent hook merge
- `start.command` — manual launcher
