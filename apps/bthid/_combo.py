"""Combo mode for bthid — kept as a separate module so importing app.py
doesn't compile this big function with its nested closures every time.

A previous all-in-one version of `app.py` reliably crashed (ESP32 hard
fault) on `BLE().active(True)` *only* when this combo function was
present in the same module. Removing the function fixed the crash.
Putting it in its own module that's only imported on demand sidesteps
the issue without losing the feature.

Pass the parent app module as `app_mod` so we can borrow its UI
constants and helper functions without duplicating them.
"""

import time

from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode


def run(kb_in, hid, i2c, accel_scale, app_mod):
    """Type-and-mouse-at-the-same-time mode.

    - Tilt the device → cursor moves (continuously).
    - Printable keys → keyboard events.
    - Arrow cluster → mouse clicks / scroll:
        ←=L click  →=R click  Tab=middle  ↑=scroll up  ↓=scroll down
    - ESC quits back to the menu.
    """
    Lcd.clear(app_mod._BG)
    app_mod._draw_header("Combo  calibrating...", color=app_mod._BLUE)
    app_mod._draw_hint("hold flat for 1 second")
    neutral = app_mod._capture_neutral(i2c, accel_scale, samples=50)

    app_mod._draw_header("Combo  type + tilt mouse", color=app_mod._OK)
    app_mod._draw_hint("<-=L  ->=R  ^v=scroll  Tab=mid  ESC=back")

    Lcd.setFont(app_mod._SMALL)
    Lcd.setTextColor(app_mod._DIM, app_mod._BG)
    Lcd.setCursor(app_mod._PAD, app_mod._HDR_H + 6)
    Lcd.print("typing forwards as keyboard")
    Lcd.setCursor(app_mod._PAD, app_mod._HDR_H + 22)
    Lcd.print("tilt forwards as mouse motion")

    accum = [0.0, 0.0]
    still_win = []
    still_since = -1
    last_mouse_send = 0
    SEND_MS = 30
    tail = [""]  # boxed in a list so inner helpers can mutate without nonlocal

    def _click(bits):
        if hid.is_connected():
            hid.send_mouse_report(bits, 0, 0, 0)
            time.sleep_ms(15)
            hid.send_mouse_report(0, 0, 0, 0)

    def _type_kb(hid_code, mod, label):
        hid.send_kb_report(mod, [hid_code])
        time.sleep_ms(8)
        hid.send_kb_release()
        if label:
            tail[0] = (tail[0] + label)[-16:]

    def _repaint_tail():
        Lcd.fillRect(0, app_mod._HDR_H + 38, Lcd.width(),
                     Lcd.height() - app_mod._HDR_H - 38 - app_mod._HINT_H,
                     app_mod._BG)
        Lcd.setFont(app_mod._FONT)
        Lcd.setTextColor(app_mod._OK, app_mod._BG)
        Lcd.setCursor(app_mod._PAD, app_mod._HDR_H + 40)
        Lcd.print(tail[0])

    while True:
        # Mouse motion (always)
        try:
            ax, ay, _az = app_mod._read_accel(i2c, accel_scale)
        except Exception:
            ax = ay = 0.0
        now = time.ticks_ms()

        # Continuous still-pose recalibration
        still_win.append((ax, ay))
        if len(still_win) > app_mod._STILL_WIN:
            still_win.pop(0)
        if len(still_win) == app_mod._STILL_WIN:
            xmin = ymin = 1e9
            xmax = ymax = -1e9
            for sx, sy in still_win:
                if sx < xmin: xmin = sx
                if sx > xmax: xmax = sx
                if sy < ymin: ymin = sy
                if sy > ymax: ymax = sy
            if (xmax - xmin) < app_mod._STILL_RANGE \
                    and (ymax - ymin) < app_mod._STILL_RANGE:
                if still_since < 0:
                    still_since = now
                elif now - still_since >= app_mod._STILL_HOLD_MS:
                    sx_sum = sy_sum = 0.0
                    for sx, sy in still_win:
                        sx_sum += sx; sy_sum += sy
                    neutral = (sx_sum / app_mod._STILL_WIN,
                               sy_sum / app_mod._STILL_WIN)
                    accum[0] = accum[1] = 0.0
                    still_since = now
            else:
                still_since = -1

        dx, dy = app_mod._tilt_to_cursor(ax, ay, neutral[0], neutral[1], accum)
        if (hid.is_connected() and (dx or dy)
                and time.ticks_diff(now, last_mouse_send) >= SEND_MS):
            hid.send_mouse_report(0, dx, dy, 0)
            last_mouse_send = now

        # Keyboard / clicks
        k = kb_in.get_key()
        if k is not None:
            if k == KeyCode.KEYCODE_ESC:
                return
            elif k == KeyCode.KEYCODE_LEFT:
                _click(0x01)
            elif k == KeyCode.KEYCODE_RIGHT:
                _click(0x02)
            elif k == KeyCode.KEYCODE_TAB:
                _click(0x04)
            elif k == KeyCode.KEYCODE_UP:
                if hid.is_connected():
                    hid.send_mouse_report(0, 0, 0, 1)
                    time.sleep_ms(8)
                    hid.send_mouse_report(0, 0, 0, 0)
            elif k == KeyCode.KEYCODE_DOWN:
                if hid.is_connected():
                    hid.send_mouse_report(0, 0, 0, -1)
                    time.sleep_ms(8)
                    hid.send_mouse_report(0, 0, 0, 0)
            elif k == KeyCode.KEYCODE_ENTER:
                _type_kb(app_mod._SPECIAL_HID[k], 0, "_")
                tail[0] = ""
                _repaint_tail()
            elif k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
                _type_kb(app_mod._SPECIAL_HID[k], 0, "")
                if tail[0]:
                    tail[0] = tail[0][:-1]
                _repaint_tail()
            elif k == KeyCode.KEYCODE_SPACE:
                _type_kb(app_mod._SPECIAL_HID[k], 0, " ")
                _repaint_tail()
            elif isinstance(k, int) and 32 <= k <= 126:
                ch = chr(k)
                shift, code = app_mod._char_to_hid(ch)
                if code:
                    mod = app_mod._MOD_LSHIFT if shift else 0
                    _type_kb(code, mod, ch)
                    _repaint_tail()

        time.sleep_ms(5)
