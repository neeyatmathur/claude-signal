#!/usr/bin/env python3
"""Floating "traffic light" indicator for Claude Code sessions.

Watches the per-session state files written by signal_hook.py and renders a
small always-on-top panel pinned to the top-right of the screen, with one
mini traffic light per active session:

    amber (steady)  -> Claude is working
    red   (blinks)  -> Claude needs permission / your attention
    green (blinks)  -> Claude finished, response is ready

The panel is a non-activating NSPanel, so it never steals focus from whatever
you are doing in other windows/tabs.

Requires pyobjc (see requirements.txt); run via start.command / the venv.
"""

import json
import os
import re
import subprocess
import time

import objc
from Cocoa import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSMakePoint,
    NSObject,
    NSPanel,
    NSPointInRect,
    NSScreen,
    NSStatusWindowLevel,
    NSString,
    NSTimer,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from PyObjCTools import AppHelper

STATE_DIR = os.path.expanduser("~/.claude/signal/sessions")
CONFIG_PATH = os.path.expanduser("~/.claude/signal/config.json")
STALE_SECONDS = 6 * 60 * 60  # drop sessions whose state hasn't updated in 6h
# A "just finished" green (Stop) settles into a steady "ready" (your turn) after
# this long untouched. Driven here, by file age, so steady-green works even when
# the host doesn't emit the idle "waiting for your input" Notification — the
# Claude Code desktop app, unlike the terminal CLI, does not fire that event.
GREEN_SETTLE_SECONDS = 60

# Layout (points) — each session is drawn as a vertical traffic-light casing.
PAD = 12
HEADER_H = 18
LAMP_D = 14          # lamp diameter
LAMP_GAP = 5         # vertical gap between lamps
HOUSE_PAD = 6        # padding between casing edge and lamps
HOUSE_W = LAMP_D + 2 * HOUSE_PAD
HOUSE_H = 3 * LAMP_D + 2 * LAMP_GAP + 2 * HOUSE_PAD
LABEL_H = 14         # folder label sits directly below each casing
LABEL_PAD = 3
ROW_GAP = 14         # vertical gap between sessions
ROW_H = HOUSE_H + LABEL_PAD + LABEL_H + ROW_GAP
WIDTH = 112

# Horizontal orientation: the casing is rotated so the 3 lamps sit in a row,
# making each session much shorter. Lamps stay in red→amber→green order (now
# left→right). Sessions still stack vertically, label centered below each pill.
HOUSE_W_H = 3 * LAMP_D + 2 * LAMP_GAP + 2 * HOUSE_PAD   # wide pill
HOUSE_H_H = LAMP_D + 2 * HOUSE_PAD                       # short pill
ROW_H_H = HOUSE_H_H + LABEL_PAD + LABEL_H + ROW_GAP
WIDTH_H = 132        # a touch wider to give folder labels room

COLLAPSED_H = HEADER_H + 2 * PAD
SCREEN_MARGIN = 12
BTN_D = 12           # app close / minimize button diameter
ROW_BTN_D = 11       # per-session dismiss (✕) button diameter

POLL_INTERVAL = 0.25
BLINK_INTERVAL = 0.5

# Colors
def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


RED = _rgb(0.93, 0.21, 0.21)
AMBER = _rgb(1.0, 0.68, 0.0)
GREEN = _rgb(0.22, 0.80, 0.36)
DIM = _rgb(1.0, 1.0, 1.0, 0.10)
BG = _rgb(0.10, 0.10, 0.12, 0.80)
HOUSING = _rgb(0.0, 0.0, 0.0, 0.92)       # the black traffic-light casing
BORDER = _rgb(1.0, 1.0, 1.0, 0.22)         # subtle casing edge
TEXT = _rgb(1.0, 1.0, 1.0, 0.92)
HEADER_TEXT = _rgb(1.0, 1.0, 1.0, 0.45)

# (state -> which of the three lamps is the active one)
ORDER = ["red", "amber", "green"]
LAMP_COLOR = {"red": RED, "amber": AMBER, "green": GREEN}
# Which lamp each state lights, and whether it's steady (vs blinking).
STATE_LAMP = {"red": "red", "amber": "amber", "green": "green", "ready": "green"}
STEADY_STATES = {"amber", "ready"}  # lit but not blinking; red/green blink
URGENCY = {"red": 0, "amber": 1, "green": 2, "ready": 3, "idle": 4}


def load_sessions():
    """Read all live session state files, newest-relevant first."""
    out = []
    now = time.time()
    # User-assigned names override the folder label (see _rename_session). The
    # hook rewrites `label` = folder on every event, so the custom name has to
    # live in config and be re-applied here, or it would get clobbered.
    custom_names = load_config().get("names")
    if not isinstance(custom_names, dict):
        custom_names = {}
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(STATE_DIR, name)
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        age = now - float(rec.get("ts", 0))
        if age > STALE_SECONDS:
            continue
        # Promote a long-settled "just finished" green to steady "ready".
        if rec.get("state") == "green" and age > GREEN_SETTLE_SECONDS:
            rec["state"] = "ready"
        rec["_file"] = path  # so a per-row close button can remove it
        rec["folder"] = rec.get("label")  # keep the original folder name
        sid = str(rec.get("session_id") or "")
        if custom_names.get(sid):
            rec["label"] = custom_names[sid]
        out.append(rec)
    out.sort(key=lambda r: (URGENCY.get(r.get("state"), 4), r.get("label", "")))
    return out


def load_config():
    """Read the persisted UI config (orientation, etc). Never raises."""
    try:
        with open(CONFIG_PATH) as fh:
            cfg = json.load(fh)
        if isinstance(cfg, dict):
            return cfg
    except (OSError, ValueError):
        pass
    return {}


def save_config(cfg):
    """Atomically persist the UI config. Errors are swallowed."""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cfg, fh)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


class SignalView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(SignalView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.sessions = []
        self.blink_on = True
        self.hover = False
        self.collapsed = False
        self.orientation = "vertical"   # controller overrides from config
        self.controller = None
        return self

    def isOpaque(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True

    # --- hover tracking (reveals the close/minimize buttons) -------------
    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        opts = (
            NSTrackingMouseEnteredAndExited
            | NSTrackingActiveAlways
            | NSTrackingInVisibleRect
        )
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None
        )
        self.addTrackingArea_(area)

    def mouseEntered_(self, event):
        self.hover = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.hover = False
        self.setNeedsDisplay_(True)

    def mouseDown_(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        close_rect, mini_rect, orient_rect = self._button_rects()
        if NSPointInRect(loc, close_rect):
            NSApplication.sharedApplication().terminate_(None)
            return
        if NSPointInRect(loc, mini_rect):
            if self.controller is not None:
                self.controller.toggle_collapsed()
            return
        if NSPointInRect(loc, orient_rect):
            if self.controller is not None:
                self.controller.toggle_orientation()
            return
        # Per-session dismiss: remove just that session's light.
        for brect, fpath in self._row_close_rects():
            if fpath and NSPointInRect(loc, brect):
                try:
                    os.remove(fpath)
                except OSError:
                    pass
                if self.controller is not None:
                    self.controller.onPoll_(None)  # refresh immediately
                return
        # Per-session rename: pencil opens a dialog to relabel this light.
        for brect, rrec in self._row_edit_rects():
            if NSPointInRect(loc, brect):
                self._rename_session(rrec)
                return
        # Click on a session's light -> jump to that terminal session.
        for srect, rec in self._session_rects():
            if NSPointInRect(loc, srect):
                self._jump_to_session(rec)
                return
        if self.collapsed and self.controller is not None:
            self.controller.toggle_collapsed()
            return
        # Otherwise let the window handle background dragging.
        objc.super(SignalView, self).mouseDown_(event)

    @objc.python_method
    def _button_rects(self):
        h = self.bounds().size.height
        w = self.bounds().size.width
        y = h - PAD - BTN_D + 1
        close_rect = NSMakeRect(w - PAD - BTN_D, y, BTN_D, BTN_D)
        mini_rect = NSMakeRect(w - PAD - 2 * BTN_D - 6, y, BTN_D, BTN_D)
        orient_rect = NSMakeRect(w - PAD - 3 * BTN_D - 12, y, BTN_D, BTN_D)
        return close_rect, mini_rect, orient_rect

    @objc.python_method
    def _draw_button(self, rect, kind, d=BTN_D):
        # subtle circular background
        _rgb(1.0, 1.0, 1.0, 0.16).set()
        NSBezierPath.bezierPathWithOvalInRect_(rect).fill()
        if kind == "pencil":
            attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(d - 2),
                NSForegroundColorAttributeName: _rgb(1.0, 1.0, 1.0, 0.85),
            }
            glyph = NSString.stringWithString_("✎")
            sz = glyph.sizeWithAttributes_(attrs)
            px = rect.origin.x + (d - sz.width) / 2.0
            py = rect.origin.y + (d - sz.height) / 2.0
            glyph.drawAtPoint_withAttributes_(NSMakePoint(px, py), attrs)
            return
        if kind == "orient":
            # Three dots arranged in the orientation you'll switch TO.
            target_horizontal = self.orientation == "vertical"
            cx = rect.origin.x + d / 2.0
            cy = rect.origin.y + d / 2.0
            r = 1.3
            _rgb(1.0, 1.0, 1.0, 0.85).set()
            for o in (-3.5, 0.0, 3.5):
                px, py = (cx + o, cy) if target_horizontal else (cx, cy + o)
                dot = NSMakeRect(px - r, py - r, 2 * r, 2 * r)
                NSBezierPath.bezierPathWithOvalInRect_(dot).fill()
            return
        line = NSBezierPath.bezierPath()
        line.setLineWidth_(1.4)
        inset = 3.5
        x0, y0 = rect.origin.x + inset, rect.origin.y + inset
        x1, y1 = rect.origin.x + d - inset, rect.origin.y + d - inset
        mid_y = rect.origin.y + d / 2.0
        if kind == "close":
            line.moveToPoint_(NSMakePoint(x0, y0))
            line.lineToPoint_(NSMakePoint(x1, y1))
            line.moveToPoint_(NSMakePoint(x0, y1))
            line.lineToPoint_(NSMakePoint(x1, y0))
        elif kind == "minus":
            line.moveToPoint_(NSMakePoint(x0, mid_y))
            line.lineToPoint_(NSMakePoint(x1, mid_y))
        else:  # plus (expand)
            mid_x = rect.origin.x + d / 2.0
            line.moveToPoint_(NSMakePoint(x0, mid_y))
            line.lineToPoint_(NSMakePoint(x1, mid_y))
            line.moveToPoint_(NSMakePoint(mid_x, y0))
            line.lineToPoint_(NSMakePoint(mid_x, y1))
        _rgb(1.0, 1.0, 1.0, 0.85).set()
        line.stroke()

    @objc.python_method
    def _row_h(self):
        return ROW_H_H if self.orientation == "horizontal" else ROW_H

    @objc.python_method
    def _casing_rect(self, i):
        """Bounding rect of row i's traffic-light casing, for this orientation."""
        b = self.bounds()
        content_top = b.size.height - PAD - HEADER_H
        house_top = content_top - i * self._row_h()
        if self.orientation == "horizontal":
            house_x = (b.size.width - HOUSE_W_H) / 2.0
            return NSMakeRect(house_x, house_top - HOUSE_H_H, HOUSE_W_H, HOUSE_H_H)
        house_x = (b.size.width - HOUSE_W) / 2.0
        return NSMakeRect(house_x, house_top - HOUSE_H, HOUSE_W, HOUSE_H)

    @objc.python_method
    def _row_close_rects(self):
        """(rect, file_path) for each session's per-row dismiss button."""
        rects = []
        if self.collapsed or not self.sessions:
            return rects
        b = self.bounds()
        for i, rec in enumerate(self.sessions):
            casing = self._casing_rect(i)
            top = casing.origin.y + casing.size.height
            rx = b.size.width - PAD - ROW_BTN_D
            ry = top - ROW_BTN_D
            rects.append((NSMakeRect(rx, ry, ROW_BTN_D, ROW_BTN_D), rec.get("_file")))
        return rects

    @objc.python_method
    def _label_display(self, rec):
        """The label string as drawn (truncated), so hit-tests match the text."""
        label = str(rec.get("label", "?"))
        if len(label) > 16:
            label = label[:15] + "…"
        return label

    @objc.python_method
    def _row_edit_rects(self):
        """(rect, record) for each session's per-row rename (pencil) button.

        Sits just past the right end of the (center-aligned) label, vertically
        centered on it. Clamped to stay inside the panel for long labels.
        """
        rects = []
        if self.collapsed or not self.sessions:
            return rects
        attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(11)}
        w = self.bounds().size.width
        for i, rec in enumerate(self.sessions):
            casing = self._casing_rect(i)
            label = self._label_display(rec)
            size = NSString.stringWithString_(label).sizeWithAttributes_(attrs)
            label_right = (w + size.width) / 2.0
            label_cy = casing.origin.y - LABEL_PAD - 10
            bx = min(label_right + 3, w - ROW_BTN_D - 3)
            by = label_cy + (size.height - ROW_BTN_D) / 2.0
            rects.append((NSMakeRect(bx, by, ROW_BTN_D, ROW_BTN_D), rec))
        return rects

    @objc.python_method
    def _rename_session(self, rec):
        """Prompt for a custom label and persist it, keyed by session id.

        Uses an osascript dialog (same approach as click-to-jump) so we don't
        have to make this non-activating panel key just to capture text. An
        empty answer clears the override and reverts to the folder name.
        """
        sid = str(rec.get("session_id") or "")
        if not sid:
            return
        cur = str(rec.get("label") or "")
        esc = cur.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'display dialog "Rename this session" with title "Signal" '
            'default answer "%s" buttons {"Cancel", "Rename"} '
            'default button "Rename"' % esc
        )
        try:
            out = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True
            )
        except OSError:
            return
        if out.returncode != 0:
            return  # cancelled
        marker = "text returned:"
        idx = out.stdout.find(marker)
        new = out.stdout[idx + len(marker):].strip() if idx >= 0 else ""

        cfg = load_config()
        names = cfg.get("names")
        if not isinstance(names, dict):
            names = {}
        if new:
            names[sid] = new
        else:
            names.pop(sid, None)  # empty -> back to folder name
        # Prune names for sessions that no longer exist, so config stays small.
        live = {str(r.get("session_id") or "") for r in load_sessions()}
        live.add(sid)
        names = {k: v for k, v in names.items() if k in live}
        cfg["names"] = names
        save_config(cfg)
        if self.controller is not None:
            self.controller.onPoll_(None)  # refresh immediately

    @objc.python_method
    def _session_rects(self):
        """(casing_rect, record) for each live session, for click-to-jump."""
        if self.collapsed or not self.sessions:
            return []
        return [(self._casing_rect(i), rec) for i, rec in enumerate(self.sessions)]

    @objc.python_method
    def _jump_to_session(self, rec):
        """Focus the terminal tab running this session (best-effort)."""
        tty = str(rec.get("tty") or "")
        term = str(rec.get("term_program") or "")
        if term == "Apple_Terminal" and re.match(r"^/dev/ttys[0-9]+$", tty):
            script = (
                "on run argv\n"
                " set theTty to item 1 of argv\n"
                ' tell application "Terminal"\n'
                "  activate\n"
                "  repeat with w in windows\n"
                "   repeat with t in tabs of w\n"
                "    if tty of t is theTty then\n"
                "     set selected of t to true\n"
                "     set index of w to 1\n"
                "     return\n"
                "    end if\n"
                "   end repeat\n"
                "  end repeat\n"
                " end tell\n"
                "end run"
            )
            self._spawn(["osascript", "-e", script, tty])
            return
        # Fallback (no tty / non-Terminal host): reveal the project folder.
        cwd = str(rec.get("cwd") or "")
        if cwd:
            self._spawn(["open", cwd])

    @objc.python_method
    def _spawn(self, argv):
        try:
            subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError:
            pass

    @objc.python_method
    def _draw_light(self, rect, state):
        casing = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, 6.0, 6.0
        )
        HOUSING.set()
        casing.fill()
        BORDER.set()
        casing.setLineWidth_(1.0)
        casing.stroke()

        horizontal = self.orientation == "horizontal"
        active_lamp = STATE_LAMP.get(state)
        for j, lamp in enumerate(ORDER):  # red, amber, green (top→bottom / L→R)
            if horizontal:
                cx = rect.origin.x + HOUSE_PAD + LAMP_D / 2.0 + j * (LAMP_D + LAMP_GAP)
                cy = rect.origin.y + rect.size.height / 2.0
            else:
                cx = rect.origin.x + HOUSE_PAD + LAMP_D / 2.0
                cy = (
                    rect.origin.y + rect.size.height
                    - HOUSE_PAD - LAMP_D / 2.0 - j * (LAMP_D + LAMP_GAP)
                )
            if lamp == active_lamp:
                lit = True if state in STEADY_STATES else self.blink_on
            else:
                lit = False
            if lit:
                glow_d = LAMP_D + 8
                glow_rect = NSMakeRect(
                    cx - glow_d / 2.0, cy - glow_d / 2.0, glow_d, glow_d
                )
                color = LAMP_COLOR[lamp]
                color.colorWithAlphaComponent_(0.30).set()
                NSBezierPath.bezierPathWithOvalInRect_(glow_rect).fill()
                color.set()
            else:
                DIM.set()
            circle = NSMakeRect(cx - LAMP_D / 2.0, cy - LAMP_D / 2.0, LAMP_D, LAMP_D)
            NSBezierPath.bezierPathWithOvalInRect_(circle).fill()

    @objc.python_method
    def _draw_centered(self, text, cy, attrs):
        size = NSString.stringWithString_(text).sizeWithAttributes_(attrs)
        x = (self.bounds().size.width - size.width) / 2.0
        NSString.stringWithString_(text).drawAtPoint_withAttributes_(
            NSMakePoint(x, cy), attrs
        )

    def drawRect_(self, rect):
        bounds = self.bounds()
        h = bounds.size.height
        w = bounds.size.width

        bg_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, 12.0, 12.0
        )
        BG.set()
        bg_path.fill()

        header_attrs = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(10),
            NSForegroundColorAttributeName: HEADER_TEXT,
        }
        self._draw_centered("CLAUDE", h - PAD - 12, header_attrs)

        # Hover-revealed window controls in the top-right corner.
        if self.hover:
            close_rect, mini_rect, orient_rect = self._button_rects()
            self._draw_button(close_rect, "close")
            self._draw_button(mini_rect, "plus" if self.collapsed else "minus")
            self._draw_button(orient_rect, "orient")

        if self.collapsed:
            return

        label_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11),
            NSForegroundColorAttributeName: TEXT,
        }
        placeholder_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11),
            NSForegroundColorAttributeName: HEADER_TEXT,
        }

        placeholder = not self.sessions
        rows = self.sessions if self.sessions else [{"state": "idle", "label": "no sessions"}]

        for i, rec in enumerate(rows):
            state = rec.get("state", "idle")
            rect = self._casing_rect(i)
            self._draw_light(rect, state)

            label = self._label_display(rec)
            label_cy = rect.origin.y - LABEL_PAD - 10
            self._draw_centered(
                label, label_cy, placeholder_attrs if placeholder else label_attrs
            )

        # Per-session dismiss (✕) and rename (✎) buttons, on hover over real
        # sessions — placed on opposite top corners of each casing.
        if self.hover and not placeholder:
            for brect, _f in self._row_close_rects():
                self._draw_button(brect, "close", ROW_BTN_D)
            for brect, _r in self._row_edit_rects():
                self._draw_button(brect, "pencil", ROW_BTN_D)


class Controller(NSObject):
    def setup(self):
        self.panel = None
        self.view = None
        self.cur_height = -1
        self.visible = False
        self.blink_on = True
        self.collapsed = False
        orientation = load_config().get("orientation")
        self.orientation = orientation if orientation in ("vertical", "horizontal") else "vertical"

        height = self._height_for(1)
        rect = NSMakeRect(0, 0, self._width(), height)
        mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )

        view = SignalView.alloc().initWithFrame_(rect)
        view.controller = self
        view.orientation = self.orientation
        panel.setContentView_(view)
        self.panel = panel
        self.view = view

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_INTERVAL, self, "onPoll:", None, True
        )
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            BLINK_INTERVAL, self, "onBlink:", None, True
        )
        self.onPoll_(None)
        return self

    @objc.python_method
    def _width(self):
        return WIDTH_H if self.orientation == "horizontal" else WIDTH

    @objc.python_method
    def _row_h(self):
        return ROW_H_H if self.orientation == "horizontal" else ROW_H

    @objc.python_method
    def _height_for(self, n):
        return PAD * 2 + HEADER_H + max(n, 1) * self._row_h()

    @objc.python_method
    def _reposition(self, height):
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        width = self._width()
        vf = screen.visibleFrame()
        x = vf.origin.x + vf.size.width - width - SCREEN_MARGIN
        y = vf.origin.y + vf.size.height - height - SCREEN_MARGIN
        self.panel.setFrame_display_(NSMakeRect(x, y, width, height), True)

    @objc.python_method
    def _desired_height(self):
        if self.collapsed:
            return COLLAPSED_H
        return self._height_for(len(self.view.sessions))

    @objc.python_method
    def relayout(self):
        # Keep the top edge anchored so it grows downward, not from the corner.
        height = self._desired_height()
        if height != self.cur_height:
            self._reposition(height)
            self.cur_height = height

    @objc.python_method
    def toggle_collapsed(self):
        self.collapsed = not self.collapsed
        self.view.collapsed = self.collapsed
        self.relayout()
        self.view.setNeedsDisplay_(True)

    @objc.python_method
    def toggle_orientation(self):
        self.orientation = "horizontal" if self.orientation == "vertical" else "vertical"
        self.view.orientation = self.orientation
        cfg = load_config()
        cfg["orientation"] = self.orientation
        save_config(cfg)
        self.cur_height = -1  # width changes too, so force a reposition
        self.relayout()
        self.view.setNeedsDisplay_(True)

    def onPoll_(self, _timer):
        # Always stay visible: when there are no sessions we still show a
        # single dim "no sessions" row so the indicator never looks dead.
        self.view.sessions = load_sessions()
        self.relayout()
        if not self.visible:
            self.panel.orderFrontRegardless()
            self.visible = True
        self.view.setNeedsDisplay_(True)

    def onBlink_(self, _timer):
        self.blink_on = not self.blink_on
        if self.view is not None:
            self.view.blink_on = self.blink_on
            if self.visible:
                self.view.setNeedsDisplay_(True)


_CONTROLLER = None  # module-level reference so the controller/timers survive


def main():
    global _CONTROLLER
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _CONTROLLER = Controller.alloc().init()
    _CONTROLLER.setup()
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
