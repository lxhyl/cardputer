# Claude usage dashboard.
#
# Polls a JSON endpoint (configured in /flash/usage.json) for the rolling
# 5-hour session and 7-day weekly utilization. Layout is optimized for the
# 240x135 LCD: two side-by-side columns so each metric gets a hero %.
#
# Endpoint contract (HTTP GET → JSON):
#   {
#     "session": {"pct": 31, "resets_in_sec": 11031},
#     "weekly":  {"pct": 66, "resets_in_sec": 46431},
#     "active":  false,            // optional: query in progress
#     "status":  "allowed",        // optional
#     "source":  "anthropic"       // optional: "anthropic" | "mock" | ...
#   }
#
# Device-local config (gitignored):
#   /flash/usage.json → {"endpoint": "http://192.168.x.x:8765/usage", "auth": ""}

import gc
import json
import time

import M5
import requests
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

_CFG = "/flash/usage.json"
_POLL_MS = 30000
_BAKING_POLL_MS = 5000
_BAKE_FRAMES = ("*", "+", "x", "+")

_W = 240
_H = 135
_HALF = _W // 2

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x888888
_DIMMER = 0x444444
_DIVIDER = 0x1A1A1A
_ACCENT = 0xE07550        # Claude mascot orange
_GREEN = 0x7BB662
_AMBER = 0xE5A642
_RED = 0xE5483B
_BAR_BG = 0x262626
_ERR = 0xFF5544

_BIG = M5.Lcd.FONTS.DejaVu24
_MID = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12

# Vertical layout (must sum to <= _H = 135). Spread to fill the full screen
# so nothing is cramped against the title and nothing is wasted at the bottom.
_HEADER_H = 17
_LABEL_Y = 32         # "5H SESSION"   — 14px gap below header divider
_PCT_Y = 50           # big % number   — y=50..74
_BAR_Y = 92           # progress bar   — y=92..100
_BAR_H = 8
_RESET_Y = 118        # "resets 3h 03m" — sits at the bottom, ends y~130
# Transient status (error / baking / fetching) overlays the reset row.


# ---- Helpers ------------------------------------------------------------

def _fmt_reset(sec):
    if not sec or sec <= 0:
        return "soon"
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        h = sec // 3600
        m = (sec % 3600) // 60
        return "%dh %02dm" % (h, m)
    d = sec // 86400
    h = (sec % 86400) // 3600
    return "%dd %dh" % (d, h)


def _pct_color(pct):
    if pct >= 80:
        return _RED
    if pct >= 50:
        return _AMBER
    return _GREEN


def _load_cfg():
    try:
        with open(_CFG) as f:
            cfg = json.load(f)
        if isinstance(cfg, dict) and cfg.get("endpoint"):
            return cfg
    except Exception:
        pass
    return None


def _fetch(cfg):
    """Returns (data_dict_or_None, error_str_or_None)."""
    url = cfg.get("endpoint", "")
    r = None
    try:
        gc.collect()
        h = {}
        auth = cfg.get("auth")
        if auth:
            h["Authorization"] = "Bearer " + auth
        r = requests.get(url, headers=h, timeout=8)
        if r.status_code != 200:
            return None, "HTTP %d" % r.status_code
        return r.json(), None
    except Exception as e:
        return None, repr(e)[:26]
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


# ---- Drawing ------------------------------------------------------------

def _draw_header():
    Lcd.fillRect(0, 0, _W, _HEADER_H, _BG)
    Lcd.setFont(_MID)
    Lcd.setTextColor(_FG, _BG)
    title = "Claude Usage"
    tw = Lcd.textWidth(title, _MID)
    Lcd.setCursor((_W - tw) // 2, -1)
    Lcd.print(title)
    # Hairline divider under header.
    Lcd.drawLine(8, _HEADER_H, _W - 8, _HEADER_H, _DIVIDER)


def _draw_column(col, label, pct, reset_sec, error=False):
    """col: 0=left, 1=right. Repaints the full column area below the header."""
    x0 = col * _HALF
    Lcd.fillRect(x0, _HEADER_H + 1, _HALF, _H - _HEADER_H - 1, _BG)

    cx = x0 + _HALF // 2

    # Section label, all-caps, dim.
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    lw = Lcd.textWidth(label, _SMALL)
    Lcd.setCursor(cx - lw // 2, _LABEL_Y)
    Lcd.print(label)

    # Big percentage — the hero element. Single font so digits and "%"
    # share the same baseline.
    Lcd.setFont(_BIG)
    if error:
        pct_text = "--"
        pct_color = _DIMMER
    else:
        pct_text = "%d%%" % pct
        pct_color = _pct_color(pct)
    pw = Lcd.textWidth(pct_text, _BIG)
    Lcd.setTextColor(pct_color, _BG)
    Lcd.setCursor(cx - pw // 2, _PCT_Y)
    Lcd.print(pct_text)

    # Progress bar — leave ~10px gutter each side of the column.
    bar_x = x0 + 10
    bar_w = _HALF - 20
    Lcd.fillRoundRect(bar_x, _BAR_Y, bar_w, _BAR_H, 2, _BAR_BG)
    if not error and pct > 0:
        fill_w = max(2, min(bar_w, (bar_w * pct) // 100))
        Lcd.fillRoundRect(bar_x, _BAR_Y, fill_w, _BAR_H, 2, pct_color)

    # Reset countdown.
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    if error:
        msg = "offline"
    else:
        msg = "resets " + _fmt_reset(reset_sec)
    mw = Lcd.textWidth(msg, _SMALL)
    Lcd.setCursor(cx - mw // 2, _RESET_Y)
    Lcd.print(msg)


def _draw_separator():
    """Faint vertical divider between the two columns."""
    Lcd.drawLine(_HALF, _LABEL_Y + 2, _HALF, _RESET_Y - 4, _DIVIDER)


def _draw_status(state, err_text=None, frame=0):
    """Overlay a centered status message on the reset row.

    `state == "idle"` is a no-op; the per-column reset countdowns drawn by
    `_draw_column` remain visible.
    """
    if state == "idle":
        return
    Lcd.fillRect(0, _RESET_Y - 2, _W, _H - (_RESET_Y - 2), _BG)
    Lcd.setFont(_SMALL)
    if state == "baking":
        msg = _BAKE_FRAMES[frame % len(_BAKE_FRAMES)] + "  Baking ..."
        color = _ACCENT
    elif state == "fetching":
        msg = "updating ..."
        color = _DIM
    elif state == "error":
        msg = "! " + (err_text or "error")
        color = _ERR
    elif state == "no-config":
        msg = "edit /flash/usage.json"
        color = _DIM
    else:
        return
    tw = Lcd.textWidth(msg, _SMALL)
    Lcd.setTextColor(color, _BG)
    Lcd.setCursor((_W - tw) // 2, _RESET_Y)
    Lcd.print(msg)


def _redraw(data, err, elapsed_s=0):
    if data:
        s = data.get("session") or {}
        w = data.get("weekly") or {}
        s_rst = int(s.get("resets_in_sec", 0) or 0) - elapsed_s
        w_rst = int(w.get("resets_in_sec", 0) or 0) - elapsed_s
        _draw_column(0, "5H SESSION", int(s.get("pct", 0) or 0), s_rst)
        _draw_column(1, "7D WEEKLY",  int(w.get("pct", 0) or 0), w_rst)
    else:
        _draw_column(0, "5H SESSION", 0, 0, error=True)
        _draw_column(1, "7D WEEKLY", 0, 0, error=True)
    _draw_separator()


# ---- Main loop ----------------------------------------------------------

def run():
    kb = MatrixKeyboard()
    Lcd.clear(_BG)
    _draw_header()

    cfg = _load_cfg()
    if not cfg:
        _redraw(None, "no config")
        _draw_status("no-config")
        while True:
            k = kb.get_key()
            if k == KeyCode.KEYCODE_ESC:
                return
            time.sleep_ms(40)

    data = None
    err = None
    last_fetch = None
    fetch_anchor = None
    baking = False
    baking_frame = 0
    last_anim = 0
    last_tick_redraw = 0

    _redraw(data, err, 0)
    _draw_status("fetching")

    while True:
        now = time.ticks_ms()
        interval = _BAKING_POLL_MS if baking else _POLL_MS
        if (last_fetch is None or
                time.ticks_diff(now, last_fetch) >= interval):
            new_data, new_err = _fetch(cfg)
            if new_data is not None:
                data = new_data
                err = None
                fetch_anchor = time.ticks_ms()
                baking = bool(data.get("active") or data.get("baking"))
            else:
                err = new_err
            last_fetch = time.ticks_ms()
            last_tick_redraw = last_fetch
            _redraw(data, err, 0)
            if err:
                _draw_status("error", err)
            elif baking:
                _draw_status("baking", frame=baking_frame)
            else:
                _draw_status("idle")
        else:
            # 1Hz countdown tick: subtract elapsed since fetch. Columns
            # redraw the reset row, so re-apply any active status overlay.
            if (data is not None and fetch_anchor is not None and
                    time.ticks_diff(now, last_tick_redraw) >= 1000):
                last_tick_redraw = now
                elapsed_s = time.ticks_diff(now, fetch_anchor) // 1000
                _redraw(data, err, elapsed_s)
                if err:
                    _draw_status("error", err)
                elif baking:
                    _draw_status("baking", frame=baking_frame)
            if baking and time.ticks_diff(now, last_anim) >= 400:
                last_anim = now
                baking_frame += 1
                _draw_status("baking", frame=baking_frame)

        k = kb.get_key()
        if k is None:
            time.sleep_ms(40)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return
        if k == ord("r") or k == ord("R") or k == KeyCode.KEYCODE_ENTER:
            last_fetch = None
