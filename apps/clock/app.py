import time

import M5
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

_BJ_OFFSET = 8 * 3600
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_ACCENT = 0xFFD040
_WAIT = 0xFFD040

_BIG = M5.Lcd.FONTS.DejaVu40
_MID = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12


def _bj_now():
    return time.localtime(time.time() + _BJ_OFFSET)


def _draw_hint():
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    hint = "ESC to exit"
    hw = Lcd.textWidth(hint, _SMALL)
    Lcd.setCursor((Lcd.width() - hw) // 2, Lcd.height() - 14)
    Lcd.print(hint)


def _draw_unsynced():
    Lcd.fillRect(0, 0, Lcd.width(), Lcd.height() - 16, _BG)
    Lcd.setFont(_MID)
    Lcd.setTextColor(_WAIT, _BG)
    msg = "Waiting for NTP..."
    mw = Lcd.textWidth(msg, _MID)
    Lcd.setCursor((Lcd.width() - mw) // 2, Lcd.height() // 2 - 10)
    Lcd.print(msg)


def run():
    Lcd.clear(_BG)
    _draw_hint()
    kb = MatrixKeyboard()

    last_text = ""
    last_date = ""
    last_synced = None

    big_h = Lcd.fontHeight(_BIG)
    mid_h = Lcd.fontHeight(_MID)
    big_y = (Lcd.height() - big_h - mid_h - 8 - 16) // 2 + 4
    date_y = big_y + big_h + 6

    while True:
        if kb.get_key() == KeyCode.KEYCODE_ESC:
            return

        t = _bj_now()
        synced = t[0] >= 2024
        if synced != last_synced:
            # Wipe content area on state transition
            Lcd.fillRect(0, 0, Lcd.width(), Lcd.height() - 16, _BG)
            last_text = ""
            last_date = ""
            last_synced = synced

        if not synced:
            _draw_unsynced()
            time.sleep_ms(500)
            continue

        text = "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5])
        date = "{:04d}-{:02d}-{:02d}  {}".format(
            t[0], t[1], t[2], _DAYS[t[6]])

        if text != last_text:
            Lcd.setFont(_BIG)
            tw = Lcd.textWidth(text, _BIG)
            x = (Lcd.width() - tw) // 2
            Lcd.fillRect(0, big_y, Lcd.width(), big_h, _BG)
            Lcd.setTextColor(_FG, _BG)
            Lcd.setCursor(x, big_y)
            Lcd.print(text)
            last_text = text

        if date != last_date:
            Lcd.setFont(_MID)
            dw = Lcd.textWidth(date, _MID)
            x = (Lcd.width() - dw) // 2
            Lcd.fillRect(0, date_y, Lcd.width(), mid_h, _BG)
            Lcd.setTextColor(_ACCENT, _BG)
            Lcd.setCursor(x, date_y)
            Lcd.print(date)
            last_date = date

        time.sleep_ms(100)
