import time

import M5
from machine import Pin, SoftI2C
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

from sht30 import SHT30
from qmp6988 import QMP6988

_BG = 0x000000
_DIM = 0x666666
_TEMP = 0xFF8C64
_HUM = 0x64C8FF
_PRES = 0xB4FF8C
_OK = 0x00DD66
_ERR = 0xFF4444
_HEADER_BG = 0x232332
_HEADER_FG = 0xFFD040

_LABEL_FONT = M5.Lcd.FONTS.DejaVu18
_VAL_FONT = M5.Lcd.FONTS.DejaVu24
_SMALL = M5.Lcd.FONTS.DejaVu12

_PAD = 6
_HEADER_H = 22
_ROW_H = 30
_ROW_TOP = 28


def _draw_header(ok):
    Lcd.fillRect(0, 0, Lcd.width(), _HEADER_H, _HEADER_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_HEADER_FG, _HEADER_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print("ENV III")
    Lcd.fillCircle(Lcd.width() - 10, _HEADER_H // 2, 4, _OK if ok else _ERR)


def _draw_row(idx, label, value_text, color):
    y = _ROW_TOP + idx * _ROW_H
    Lcd.fillRect(0, y, Lcd.width(), _ROW_H, _BG)

    Lcd.setFont(_LABEL_FONT)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 4)
    Lcd.print(label)

    Lcd.setFont(_VAL_FONT)
    Lcd.setTextColor(color, _BG)
    tw = Lcd.textWidth(value_text, _VAL_FONT)
    Lcd.setCursor(Lcd.width() - tw - _PAD, y)
    Lcd.print(value_text)


def _draw_hint():
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    hint = "ESC to exit"
    hw = Lcd.textWidth(hint, _SMALL)
    Lcd.setCursor(Lcd.width() - hw - _PAD, Lcd.height() - 14)
    Lcd.print(hint)


_READ_PERIOD_MS = 200  # 5 Hz — granular enough to see the SHT30 thermal
                       # decay curve in real time. The sensor's own thermal
                       # time constant is the actual bottleneck (~10-30 s),
                       # so faster polling just reveals the curve, doesn't
                       # speed up convergence.


def _arrow(prev, cur, threshold):
    """Trend marker shown after each reading. Helps the user see the value
    is actively moving even when the magnitude barely changes per sample."""
    if prev is None or cur is None:
        return ""
    d = cur - prev
    if d > threshold:
        return " ^"
    if d < -threshold:
        return " v"
    return "  "


def run():
    Lcd.clear(_BG)
    _draw_header(True)
    _draw_row(0, "T", "--", _TEMP)
    _draw_row(1, "H", "--", _HUM)
    _draw_row(2, "P", "--", _PRES)
    _draw_hint()

    kb = MatrixKeyboard()
    i2c = SoftI2C(sda=Pin(2), scl=Pin(1), freq=100_000)
    sht = SHT30(i2c)
    baro = QMP6988(i2c)

    last_read = -10_000
    prev_t = prev_h = prev_p = None
    while True:
        if kb.get_key() == KeyCode.KEYCODE_ESC:
            return

        now = time.ticks_ms()
        if time.ticks_diff(now, last_read) >= _READ_PERIOD_MS:
            t = h = p = None
            ok = True
            try:
                t, h = sht.read()
            except Exception:
                ok = False
            try:
                _bt, p = baro.read()
            except Exception:
                ok = False
            last_read = now

            _draw_header(ok)
            _draw_row(0, "T",
                      ("{:5.2f}{} C".format(t, _arrow(prev_t, t, 0.02))
                       if t is not None else "--"),
                      _TEMP)
            _draw_row(1, "H",
                      ("{:5.2f}{} %".format(h, _arrow(prev_h, h, 0.05))
                       if h is not None else "--"),
                      _HUM)
            _draw_row(2, "P",
                      ("{:7.2f}{} hPa".format(p / 100.0,
                                              _arrow(prev_p, p, 1))
                       if p is not None else "--"),
                      _PRES)
            _draw_hint()
            prev_t, prev_h, prev_p = t, h, p

        time.sleep_ms(20)
