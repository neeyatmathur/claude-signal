# signal — a traffic light for Claude Code

**The problem:** when Claude Code is working, you have to keep watching the
terminal to know if it's still busy, stuck waiting for permission, or done. That
gets worse with several sessions at once.

**signal** shows each session's state as a color in the corner of your screen,
so you can work on something else and just glance over: amber = working, red =
needs you, green = done. You only switch back when a light says it's time.

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

Once installed, the indicator **starts itself**: the `SessionStart` hook
launches the floating panel automatically the first time you start any Claude
Code session, so there's nothing to run by hand. The launch is detached and
guarded — it never spawns a second copy and never blocks the session.

To opt out of auto-start, set `SIGNAL_NO_AUTOLAUNCH=1` in your environment.

You can also start it manually any time:

```bash
open start.command        # or double-click start.command in Finder
```

The app launches **detached**, so you can safely close the terminal/window it
opened — the indicator keeps running. To quit it, use the hover **✕** button on
the panel, double-click **`stop.command`**, or run `pkill -f signal_app.py`.
(If you quit it mid-session, it reappears on the next session start.)

Optional alias (add to `~/.zshrc`):

```bash
alias claude-signal='~/signal/.venv/bin/python ~/signal/signal_app.py >/dev/null 2>&1 &'
```

The window is draggable (click-and-drag the background) if you want to nudge it.
Hover to reveal, top-right, the app **✕** (quit), **–** (minimize), and a
**⋮⋮ orientation** toggle, plus a per-row **✕** to dismiss a single session's light.

### Orientation

By default each session is a **vertical** traffic light (lamps stacked). Click
the orientation toggle to switch to a **horizontal** layout — the three lamps sit
in a row, so each session is shorter and more fit in less space. The choice is
saved to `~/.claude/signal/config.json` and restored on the next launch.

### Click to jump to a session

Click a session's light to jump straight to the terminal it's running in. This
works for **Apple Terminal** (the indicator records each session's tty and uses
AppleScript to select that exact tab and raise its window). For other hosts —
iTerm2, VS Code, the Claude Code desktop app — it falls back to opening the
session's project folder in Finder.

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
