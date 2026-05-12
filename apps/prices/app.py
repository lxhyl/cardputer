import gc
import time

import M5
import requests
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# Binance Vision is reachable on networks where api.binance.com is DNS-blocked.
_BASE = "https://data-api.binance.vision/api/v3/ticker/24hr"
_REFRESH_MS = 30000
_DEFAULTS = ["BTC", "ETH", "PENDLE"]
_QUOTE = "USDT"
_MAX_INPUT = 12

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_UP = 0x00DD66
_DOWN = 0xFF4444
_WAIT = 0xFFD040
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_INPUT_BG = 0x111122
_SYM_FG = 0xCCDDFF

_FONT = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12

_PAD = 6
_HEADER_H = 18
_INPUT_H = 20
_ROW_H = 24


def _fmt_price(p):
    if p is None:
        return "--"
    if p >= 1000:
        int_part, frac = "{:.2f}".format(p).split(".")
        groups = ""
        while len(int_part) > 3:
            groups = "," + int_part[-3:] + groups
            int_part = int_part[:-3]
        return int_part + groups + "." + frac
    if p >= 1:
        return "{:.4f}".format(p).rstrip("0").rstrip(".")
    return "{:.6f}".format(p).rstrip("0").rstrip(".")


def _fmt_pct(c):
    if c is None:
        return ""
    return "{:+.2f}%".format(c)


def _fetch_one(symbol):
    """symbol like 'BTC'. Returns (price, pct, error_str)."""
    pair = symbol + _QUOTE
    r = None
    try:
        gc.collect()
        r = requests.get(_BASE + "?symbol=" + pair, timeout=15)
        if r.status_code != 200:
            return None, None, "HTTP " + str(r.status_code)
        d = r.json()
        return float(d["lastPrice"]), float(d["priceChangePercent"]), None
    except Exception as e:
        return None, None, repr(e)[:24]
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _draw_header(state):
    Lcd.fillRect(0, 0, Lcd.width(), _HEADER_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_HDR_FG, _HDR_BG)
    Lcd.setCursor(_PAD, 4)
    Lcd.print("Crypto / USDT")
    color = _UP if state == "ok" else (_WAIT if state == "wait" else _DOWN)
    Lcd.fillCircle(Lcd.width() - 10, _HEADER_H // 2, 4, color)


def _draw_row(idx, sym, price, pct, error):
    y = _HEADER_H + idx * _ROW_H + 2
    Lcd.fillRect(0, y, Lcd.width(), _ROW_H, _BG)

    # Symbol on left
    Lcd.setFont(_FONT)
    Lcd.setTextColor(_SYM_FG, _BG)
    Lcd.setCursor(_PAD, y)
    Lcd.print(sym[:8])

    if error:
        Lcd.setFont(_SMALL)
        Lcd.setTextColor(_DOWN, _BG)
        ew = Lcd.textWidth(error, _SMALL)
        Lcd.setCursor(Lcd.width() - ew - _PAD, y + 6)
        Lcd.print(error)
        return

    # Right: pct (small, colored), then price (large, white) right-aligned to its left
    right = Lcd.width() - _PAD
    if pct is not None:
        ptxt_pct = _fmt_pct(pct)
        Lcd.setFont(_SMALL)
        pct_w = Lcd.textWidth(ptxt_pct, _SMALL)
        Lcd.setTextColor(_UP if pct >= 0 else _DOWN, _BG)
        Lcd.setCursor(right - pct_w, y + 6)
        Lcd.print(ptxt_pct)
        right = right - pct_w - 6

    if price is not None:
        ptxt = _fmt_price(price)
        Lcd.setFont(_FONT)
        pw = Lcd.textWidth(ptxt, _FONT)
        Lcd.setTextColor(_FG, _BG)
        Lcd.setCursor(right - pw, y)
        Lcd.print(ptxt)


def _draw_input(buf):
    y = Lcd.height() - _INPUT_H
    Lcd.fillRect(0, y, Lcd.width(), _INPUT_H, _INPUT_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _INPUT_BG)
    Lcd.setCursor(_PAD, y + 4)
    Lcd.print(">")
    Lcd.setTextColor(_FG, _INPUT_BG)
    Lcd.setCursor(_PAD + 12, y + 4)
    Lcd.print((buf if buf else "") + "_")

    Lcd.setTextColor(_DIM, _INPUT_BG)
    h = "ESC=back"
    hw = Lcd.textWidth(h, _SMALL)
    Lcd.setCursor(Lcd.width() - hw - _PAD, y + 4)
    Lcd.print(h)


def run():
    kb = MatrixKeyboard()

    symbols = list(_DEFAULTS)         # current visible list
    user_slot = None                  # index of the user-typed slot (or None)
    state = {s: (None, None, None) for s in symbols}

    buf = ""
    last_fetch = -10 ** 8             # force first fetch

    Lcd.clear(_BG)
    _draw_header("wait")
    for i, s in enumerate(symbols):
        _draw_row(i, s, None, None, None)
    _draw_input(buf)

    def fetch_all():
        any_ok = False
        any_err = False
        for s in symbols:
            p, c, e = _fetch_one(s)
            state[s] = (p, c, e)
            if e is None:
                any_ok = True
            else:
                any_err = True
        if any_ok:
            return "ok"
        return "err" if any_err else "wait"

    def redraw_rows():
        for i, s in enumerate(symbols):
            p, c, e = state[s]
            _draw_row(i, s, p, c, e)

    while True:
        now = time.ticks_ms()
        if time.ticks_diff(now, last_fetch) >= _REFRESH_MS:
            _draw_header("wait")
            res = fetch_all()
            last_fetch = time.ticks_ms()
            redraw_rows()
            _draw_header(res)

        k = kb.get_key()
        if k is None:
            time.sleep_ms(40)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return
        if k == KeyCode.KEYCODE_ENTER:
            sym = buf.strip().upper()
            if sym:
                if user_slot is None:
                    # First custom query: append a new row (only if there's space)
                    if len(symbols) < 4:
                        symbols.append(sym)
                        state[sym] = (None, None, None)
                        user_slot = len(symbols) - 1
                    else:
                        # No room — overwrite last slot
                        old = symbols[-1]
                        if old not in _DEFAULTS and old in state:
                            del state[old]
                        symbols[-1] = sym
                        state[sym] = (None, None, None)
                        user_slot = len(symbols) - 1
                else:
                    old = symbols[user_slot]
                    if old not in _DEFAULTS and old in state:
                        del state[old]
                    symbols[user_slot] = sym
                    state[sym] = (None, None, None)
                buf = ""
                last_fetch = -10 ** 8   # force immediate refetch
            _draw_input(buf)
            continue
        if k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
            if buf:
                buf = buf[:-1]
                _draw_input(buf)
            continue
        if isinstance(k, int) and 32 <= k <= 126 and len(buf) < _MAX_INPUT:
            ch = chr(k)
            if ch.isalpha() or ch.isdigit():
                buf += ch.upper()
                _draw_input(buf)
