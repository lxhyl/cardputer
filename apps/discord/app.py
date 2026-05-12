import gc
import json
import time

import M5
import requests
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# Endpoint we POST to with {"content": "..."}. Override at runtime by
# dropping a /flash/discord.json file containing {"webhook": "http://..."}.
# Common setups:
#   - Direct Discord webhook URL (https://discord.com/api/webhooks/...)
#   - A LAN relay you control (e.g. http://192.168.x.y:8089/) when the
#     device is on a network that can't reach Discord directly.
_DEFAULT_WEBHOOK = ""
_CFG = "/flash/discord.json"

# UI
_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_OK = 0x00DD66
_HOT = 0xFF6464
_WAIT = 0xFFD040
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_INPUT_BG = 0x111122
_CURSOR = 0x64A0FF

_FONT_S = M5.Lcd.FONTS.DejaVu12
_PAD = 6
_HEADER_H = 18
_STATUS_H = 16
_HINT_H = 14
_LINE_H = 14
_VISIBLE_LINES = 5
_CHARS_PER_LINE = 28
_MAX_TEXT = 1500


def _load_webhook():
    try:
        with open(_CFG) as f:
            return json.load(f).get("webhook", _DEFAULT_WEBHOOK)
    except Exception:
        return _DEFAULT_WEBHOOK


def _wrap(text):
    """Word-wrap into lines that fit on screen. Honors explicit '\n'."""
    lines = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        cur += ch
        if len(cur) >= _CHARS_PER_LINE:
            # Try to break on a space within the last third of the line.
            split = cur.rfind(" ")
            if split > _CHARS_PER_LINE * 2 // 3:
                lines.append(cur[:split])
                cur = cur[split + 1:]
            else:
                lines.append(cur)
                cur = ""
    lines.append(cur)
    return lines


def _draw_header():
    Lcd.fillRect(0, 0, Lcd.width(), _HEADER_H, _HDR_BG)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_HDR_FG, _HDR_BG)
    Lcd.setCursor(_PAD, 4)
    Lcd.print("Discord")


def _draw_text(text):
    y_top = _HEADER_H + 2
    text_h = Lcd.height() - _HEADER_H - 2 - _STATUS_H - _HINT_H
    Lcd.fillRect(0, y_top, Lcd.width(), text_h, _INPUT_BG)

    lines = _wrap(text)
    visible = lines[-_VISIBLE_LINES:]
    Lcd.setFont(_FONT_S)
    cursor_y = y_top + 2
    for i, line in enumerate(visible):
        y = y_top + 2 + i * _LINE_H
        Lcd.setTextColor(_FG, _INPUT_BG)
        Lcd.setCursor(_PAD, y)
        Lcd.print(line)
        if i == len(visible) - 1:
            cursor_y = y
            cursor_x = _PAD + Lcd.textWidth(line, _FONT_S)
    # Blinking-style cursor (always visible solid block)
    Lcd.fillRect(cursor_x, cursor_y, 2, 12, _CURSOR)

    # Char count in the bottom-right of the input area
    cnt = "{}/{}".format(len(text), _MAX_TEXT)
    cw = Lcd.textWidth(cnt, _FONT_S)
    Lcd.setTextColor(_DIM, _INPUT_BG)
    Lcd.setCursor(Lcd.width() - cw - _PAD, y_top + text_h - 13)
    Lcd.print(cnt)


def _draw_status(state, msg):
    color = {"ok": _OK, "err": _HOT, "wait": _WAIT}.get(state, _DIM)
    y = Lcd.height() - _HINT_H - _STATUS_H + 2
    Lcd.fillRect(0, y, Lcd.width(), _STATUS_H, _BG)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(color, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(msg[:38])


def _draw_hint():
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 1)
    Lcd.print("ENTER send  BKSP del  ESC back")


def _post(webhook, content):
    r = None
    try:
        gc.collect()
        body = json.dumps({"content": content})
        r = requests.post(
            webhook,
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code in (200, 204):
            return True, "Sent! ({})".format(r.status_code)
        snippet = ""
        try:
            snippet = r.text[:30]
        except Exception:
            pass
        return False, "HTTP {} {}".format(r.status_code, snippet)
    except Exception as e:
        return False, repr(e)[:36]
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def run():
    kb = MatrixKeyboard()
    webhook = _load_webhook()

    Lcd.clear(_BG)
    _draw_header()
    _draw_hint()
    _draw_status("idle", "Type your message...")
    text = ""
    _draw_text(text)

    while True:
        k = kb.get_key()
        if k is None:
            time.sleep_ms(20)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return
        if k == KeyCode.KEYCODE_ENTER:
            stripped = text.strip()
            if not stripped:
                _draw_status("err", "Empty — type something first")
                continue
            _draw_status("wait", "Sending to Discord...")
            ok, msg = _post(webhook, stripped)
            if ok:
                _draw_status("ok", msg)
                text = ""
                _draw_text(text)
            else:
                _draw_status("err", msg)
            continue
        if k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
            if text:
                text = text[:-1]
                _draw_text(text)
                _draw_status("idle", "Type your message...")
            continue
        if k == KeyCode.KEYCODE_TAB:
            # Tab inserts a space — handier than the spacebar mid-typing
            if len(text) < _MAX_TEXT:
                text += " "
                _draw_text(text)
            continue
        if isinstance(k, int) and 32 <= k <= 126 and len(text) < _MAX_TEXT:
            text += chr(k)
            _draw_text(text)
            _draw_status("idle", "Type your message...")
