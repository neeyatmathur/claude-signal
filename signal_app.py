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
        out.append(rec)
    out.sort(key=lambda r: (URGENCY.get(r.get("state"), 4), r.get("label", "")))
    return out


class SignalView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(SignalView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.sessions = []
        self.blink_on = True
        self.hover = False
        self.collapsed = False
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
        close_rect, mini_rect = self._button_rects()
        if NSPointInRect(loc, close_rect):
            NSApplication.sharedApplication().terminate_(None)
            return
        if NSPointInRect(loc, mini_rect):
            if self.controller is not None:
                self.controller.toggle_collapsed()
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
        return close_rect, mini_rect

    @objc.python_method
    def _draw_button(self, rect, kind, d=BTN_D):
        # subtle circular background
        _rgb(1.0, 1.0, 1.0, 0.16).set()
        NSBezierPath.bezierPathWithOvalInRect_(rect).fill()
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
    def _row_close_rects(self):
        """(rect, file_path) for each session's per-row dismiss button."""
        rects = []
        if self.collapsed or not self.sessions:
            return rects
        b = self.bounds()
        content_top = b.size.height - PAD - HEADER_H
        for i, rec in enumerate(self.sessions):
            house_top = content_top - i * ROW_H
            rx = b.size.width - PAD - ROW_BTN_D
            ry = house_top - ROW_BTN_D
            rects.append((NSMakeRect(rx, ry, ROW_BTN_D, ROW_BTN_D), rec.get("_file")))
        return rects

    @objc.python_method
    def _draw_light(self, house_x, house_top, state):
        house_rect = NSMakeRect(house_x, house_top - HOUSE_H, HOUSE_W, HOUSE_H)
        casing = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            house_rect, 6.0, 6.0
        )
        HOUSING.set()
        casing.fill()
        BORDER.set()
        casing.setLineWidth_(1.0)
        casing.stroke()

        active_lamp = STATE_LAMP.get(state)
        lamp_x = house_x + HOUSE_PAD
        for j, lamp in enumerate(ORDER):  # red, amber, green (top→bottom)
            cy = house_top - HOUSE_PAD - LAMP_D / 2.0 - j * (LAMP_D + LAMP_GAP)
            is_active = lamp == active_lamp
            if is_active:
                lit = True if state in STEADY_STATES else self.blink_on
            else:
                lit = False
            if lit:
                glow_d = LAMP_D + 8
                glow_rect = NSMakeRect(lamp_x - 4, cy - glow_d / 2.0, glow_d, glow_d)
                color = LAMP_COLOR[lamp]
                color.colorWithAlphaComponent_(0.30).set()
                NSBezierPath.bezierPathWithOvalInRect_(glow_rect).fill()
                color.set()
            else:
                DIM.set()
            circle = NSMakeRect(lamp_x, cy - LAMP_D / 2.0, LAMP_D, LAMP_D)
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
            close_rect, mini_rect = self._button_rects()
            self._draw_button(close_rect, "close")
            self._draw_button(mini_rect, "plus" if self.collapsed else "minus")

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

        house_x = (w - HOUSE_W) / 2.0
        content_top = h - PAD - HEADER_H
        for i, rec in enumerate(rows):
            state = rec.get("state", "idle")
            house_top = content_top - i * ROW_H
            self._draw_light(house_x, house_top, state)

            label = str(rec.get("label", "?"))
            if len(label) > 16:
                label = label[:15] + "…"
            label_cy = house_top - HOUSE_H - LABEL_PAD - 10
            self._draw_centered(
                label, label_cy, placeholder_attrs if placeholder else label_attrs
            )

        # Per-session dismiss buttons (only over real sessions, on hover).
        if self.hover and not placeholder:
            for brect, _f in self._row_close_rects():
                self._draw_button(brect, "close", ROW_BTN_D)


class Controller(NSObject):
    def setup(self):
        self.panel = None
        self.view = None
        self.cur_height = -1
        self.visible = False
        self.blink_on = True
        self.collapsed = False

        height = self._height_for(1)
        rect = NSMakeRect(0, 0, WIDTH, height)
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
    def _height_for(self, n):
        return PAD * 2 + HEADER_H + max(n, 1) * ROW_H

    @objc.python_method
    def _reposition(self, height):
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        vf = screen.visibleFrame()
        x = vf.origin.x + vf.size.width - WIDTH - SCREEN_MARGIN
        y = vf.origin.y + vf.size.height - height - SCREEN_MARGIN
        self.panel.setFrame_display_(NSMakeRect(x, y, WIDTH, height), True)

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
