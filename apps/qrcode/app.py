# QR code generator app.
#
# Two modes:
#   - Preset picker: if /flash/qrcode.json has entries, the app starts here.
#     Up/Down to select, Enter to render full-screen, n to switch to type mode.
#   - Type mode: free-form text input with live (debounced) re-rendering.
#     Tab cycles error-correction level (L/M).
#
# Preset file format (device-local, gitignored):
#   {"presets": [{"name": "alipay", "content": "https://qr.alipay.com/fkx..."}]}

import json
import time

import M5
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

from apps.qrcode import qr

_PRESET_FILE = "/flash/qrcode.json"

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_OK = 0x00DD66
_ERR = 0xFF4444
_QR_BG = 0xFFFFFF
_QR_FG = 0x000000
_INPUT_BG = 0x1A1A1A
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_SEL_BG = 0xFFFFFF
_SEL_FG = 0x000000

_SMALL = M5.Lcd.FONTS.DejaVu12

_INPUT_H = 20
_HINT_H = 14
_HDR_H = 16
_LINE_H = 20
_VISIBLE_ROWS = 5  # (135 - 16 header - 14 hint) / 20 ≈ 5
_MAX_LEN = 400
_DEBOUNCE_MS = 140


def _load_presets():
    try:
        with open(_PRESET_FILE) as f:
            cfg = json.load(f)
    except Exception:
        return []
    items = cfg.get("presets", []) if isinstance(cfg, dict) else []
    out = []
    for p in items:
        if isinstance(p, dict) and p.get("name") and p.get("content"):
            out.append({"name": str(p["name"]), "content": str(p["content"])})
    return out


# ---- Drawing helpers ----------------------------------------------------

def _draw_qr(size, mat, area_w, area_h, ox, oy):
    quiet = 2
    scale = max(1, min(area_w, area_h) // (size + 2 * quiet))
    total = (size + 2 * quiet) * scale
    px = ox + (area_w - total) // 2
    py = oy + (area_h - total) // 2
    Lcd.fillRect(px, py, total, total, _QR_BG)
    inner_x = px + quiet * scale
    inner_y = py + quiet * scale
    for r in range(size):
        row_off = r * size
        c = 0
        while c < size:
            if mat[row_off + c]:
                start = c
                c += 1
                while c < size and mat[row_off + c]:
                    c += 1
                Lcd.fillRect(inner_x + start * scale, inner_y + r * scale,
                             (c - start) * scale, scale, _QR_FG)
            else:
                c += 1


def _draw_header(text, color=_HDR_FG):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _HDR_BG)
    Lcd.setCursor(4, 2)
    Lcd.print(text)


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, y + 1)
    Lcd.print(text)


def _draw_placeholder(top, area_h, msg, color):
    Lcd.fillRect(0, top, Lcd.width(), area_h, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _BG)
    tw = Lcd.textWidth(msg, _SMALL)
    Lcd.setCursor((Lcd.width() - tw) // 2, top + area_h // 2 - 6)
    Lcd.print(msg)


# ---- Preset list view --------------------------------------------------

def _draw_list(presets, idx, scroll):
    Lcd.clear(_BG)
    _draw_header("Saved QR codes")
    _draw_hint("Enter=show  n=new  ESC=quit")
    Lcd.setFont(_SMALL)
    list_y = _HDR_H + 4
    for i in range(_VISIBLE_ROWS):
        pi = scroll + i
        if pi >= len(presets):
            break
        p = presets[pi]
        y = list_y + i * _LINE_H
        if pi == idx:
            Lcd.fillRect(0, y - 2, Lcd.width(), _LINE_H, _SEL_BG)
            Lcd.setTextColor(_SEL_FG, _SEL_BG)
        else:
            Lcd.setTextColor(_FG, _BG)
        name = p["name"]
        if len(name) > 32:
            name = name[:31] + "."
        Lcd.setCursor(8, y + 2)
        Lcd.print(name)


def _list_screen(kb, presets):
    """Returns ('preset', preset) | ('new', None) | ('quit', None)."""
    idx = 0
    scroll = 0
    _draw_list(presets, idx, scroll)
    while True:
        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return ("quit", None)
        if k == KeyCode.KEYCODE_ENTER:
            return ("preset", presets[idx])
        if k == ord("n") or k == ord("N"):
            return ("new", None)
        if k == KeyCode.KEYCODE_UP or k == ord("w") or k == ord(";"):
            idx = (idx - 1) % len(presets)
        elif k == KeyCode.KEYCODE_DOWN or k == ord("s") or k == ord("."):
            idx = (idx + 1) % len(presets)
        else:
            continue
        if idx < scroll:
            scroll = idx
        elif idx >= scroll + _VISIBLE_ROWS:
            scroll = idx - _VISIBLE_ROWS + 1
        _draw_list(presets, idx, scroll)


# ---- Render-a-preset view ----------------------------------------------

def _render_preset_screen(kb, preset):
    Lcd.clear(_BG)
    name = preset["name"]
    if len(name) > 34:
        name = name[:33] + "."
    _draw_header(name)
    _draw_hint("ESC = back")
    qr_h = Lcd.height() - _HDR_H - _HINT_H
    try:
        sz, mat = qr.encode(preset["content"], qr.LEVEL_L)
        _draw_qr(sz, mat, Lcd.width(), qr_h, 0, _HDR_H)
    except Exception:
        _draw_placeholder(_HDR_H, qr_h, "too long for v15", _ERR)
    while True:
        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return


# ---- Type mode ---------------------------------------------------------

def _draw_input(buf):
    iy = Lcd.height() - _INPUT_H - _HINT_H
    Lcd.fillRect(0, iy, Lcd.width(), _INPUT_H, _INPUT_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_OK, _INPUT_BG)
    shown = buf
    if len(shown) > 36:
        shown = "..." + shown[-33:]
    Lcd.setCursor(4, iy + 4)
    Lcd.print("> " + shown + "_")


def _draw_hint_type(version, level, char_count):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, y + 1)
    lvl = "L" if level == qr.LEVEL_L else "M"
    if version:
        msg = "v%d %s  %dch  Tab=ec  ESC=back" % (version, lvl, char_count)
    else:
        msg = "ec=%s  Tab=switch  ESC=back" % lvl
    Lcd.print(msg)


def _type_screen(kb):
    Lcd.clear(_BG)
    qr_h = Lcd.height() - _INPUT_H - _HINT_H
    buf = ""
    rendered_for = None
    pending_since = None
    level = qr.LEVEL_L
    version = None

    _draw_input(buf)
    _draw_hint_type(None, level, 0)
    _draw_placeholder(0, qr_h, "Type to encode", _DIM)

    while True:
        k = kb.get_key()
        now = time.ticks_ms()

        if (pending_since is not None and
                time.ticks_diff(now, pending_since) >= _DEBOUNCE_MS):
            state = (buf, level)
            if state != rendered_for:
                if not buf:
                    _draw_placeholder(0, qr_h, "Type to encode", _DIM)
                    version = None
                else:
                    try:
                        sz, mat = qr.encode(buf, level)
                        Lcd.fillRect(0, 0, Lcd.width(), qr_h, _BG)
                        _draw_qr(sz, mat, Lcd.width(), qr_h, 0, 0)
                        version = (sz - 17) // 4
                    except Exception:
                        _draw_placeholder(0, qr_h, "too long for v15", _ERR)
                        version = None
                _draw_hint_type(version, level, len(buf))
                rendered_for = state
            pending_since = None

        if k is None:
            time.sleep_ms(20)
            continue

        if k == KeyCode.KEYCODE_ESC:
            return
        if k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
            if buf:
                buf = buf[:-1]
                pending_since = now
                _draw_input(buf)
            continue
        if k == KeyCode.KEYCODE_TAB:
            level = qr.LEVEL_M if level == qr.LEVEL_L else qr.LEVEL_L
            pending_since = now
            _draw_hint_type(version, level, len(buf))
            continue
        if isinstance(k, int) and 32 <= k <= 126 and len(buf) < _MAX_LEN:
            buf += chr(k)
            pending_since = now
            _draw_input(buf)


# ---- Top level ---------------------------------------------------------

def run():
    kb = MatrixKeyboard()
    presets = _load_presets()
    if not presets:
        _type_screen(kb)
        return
    while True:
        action, item = _list_screen(kb, presets)
        if action == "quit":
            return
        if action == "preset":
            _render_preset_screen(kb, item)
        elif action == "new":
            _type_screen(kb)
