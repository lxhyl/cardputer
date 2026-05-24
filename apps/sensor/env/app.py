"""ENV + CO2 foreground UI — pure consumer of the launcher's sensor
service.

This app DOES NOT touch the Grove I2C bus, does NOT instantiate SHT30 /
QMP6988 / SCD40, and does NOT POST to the LAN server. All of that is
owned by the launcher process:
  - `sensors` module owns the bus + sensors and publishes a snapshot
    (see launcher/sensors.py)
  - `env_daemon` module uploads readings to the LAN server with offline
    buffering (see launcher/env_daemon.py)

Why: before this split, both this app and env_daemon opened their own
SoftI2C on the same Grove pins. Two SoftI2C masters on the same physical
bus from two threads race mid-transaction (the GIL releases between
bytecodes during bit-banging) → both reads fail with CRC errors. They
also both POST, causing duplicate rows in the Mac SQLite. Refactoring
the app into a pure consumer removes the conflict structurally.
"""

import time

import M5
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

import sensors
import env_daemon

_BG = 0x000000
_DIM = 0x666666
_TEMP = 0xFF8C64
_HUM = 0x64C8FF
_PRES = 0xB4FF8C
_CO2 = 0xFFD040
_OK = 0x00DD66
_WAIT = 0xFFD040
_ERR = 0xFF4444
_OFF = 0x444444
_HEADER_BG = 0x232332
_HEADER_FG = 0xFFD040

_LABEL_FONT = M5.Lcd.FONTS.DejaVu18
_VAL_FONT = M5.Lcd.FONTS.DejaVu24
_SMALL = M5.Lcd.FONTS.DejaVu12

_PAD = 6
_HEADER_H = 22
_ROW_H = 27
_ROW_TOP = 24

# How often we re-render. The sensor service refreshes the snapshot at
# 2 Hz (every 500 ms) so polling at 5 Hz gives a smooth-feeling UI
# without over-rendering. Doesn't drive any I/O — just LCD redraws.
_RENDER_PERIOD_MS = 200


def _draw_header(sensor_ok, up_color):
    """sensor_ok: bool. up_color: integer LCD color for the upload-status
    dot, or None to hide it (no /flash/env_upload.json → daemon off)."""
    Lcd.fillRect(0, 0, Lcd.width(), _HEADER_H, _HEADER_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_HEADER_FG, _HEADER_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print("ENV + CO2")
    # Sensor dot (rightmost) shows whether the launcher's sensor thread
    # is producing fresh readings.
    Lcd.fillCircle(Lcd.width() - 10, _HEADER_H // 2, 4,
                   _OK if sensor_ok else _ERR)
    if up_color is not None:
        # Upload dot left of the sensor dot — daemon's LAN posting state.
        Lcd.fillCircle(Lcd.width() - 22, _HEADER_H // 2, 3, up_color)


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


def _arrow(prev, cur, threshold):
    """Trend marker shown after each reading."""
    if prev is None or cur is None:
        return ""
    d = cur - prev
    if d > threshold:
        return " ^"
    if d < -threshold:
        return " v"
    return "  "


def _upload_dot_color(daemon_state):
    """Translate the daemon's state into the small upload-status dot.
    None hides the dot entirely (daemon hasn't been configured)."""
    if daemon_state == "off":
        return None
    if daemon_state == "ok":
        return _OK
    if daemon_state == "err":
        return _ERR
    # "init" / "stale" both shown as warming/in-progress.
    return _WAIT


def run():
    Lcd.clear(_BG)
    _draw_header(False, _upload_dot_color(env_daemon.state()))
    _draw_row(0, "T", "--", _TEMP)
    _draw_row(1, "H", "--", _HUM)
    _draw_row(2, "P", "--", _PRES)
    _draw_row(3, "CO2", "--", _CO2)

    kb = MatrixKeyboard()

    last_render = -10_000
    prev_t = prev_h = prev_p = prev_c = None

    try:
        while True:
            if kb.get_key() == KeyCode.KEYCODE_ESC:
                return

            now = time.ticks_ms()
            if time.ticks_diff(now, last_render) < _RENDER_PERIOD_MS:
                time.sleep_ms(20)
                continue
            last_render = now

            snap = sensors.latest()
            sensor_ok = (sensors.state() == "ok")
            up_color = _upload_dot_color(env_daemon.state())
            _draw_header(sensor_ok, up_color)

            t = snap.get("temp_c") if snap else None
            h = snap.get("humidity") if snap else None
            p = snap.get("pressure_pa") if snap else None
            c = snap.get("co2_ppm") if snap else None

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
            _draw_row(3, "CO2",
                      ("{:4d}{} ppm".format(c, _arrow(prev_c, c, 1))
                       if c is not None else "warming..."),
                      _CO2)
            prev_t, prev_h, prev_p, prev_c = t, h, p, c
    finally:
        # No teardown: sensors and daemon belong to the launcher, not
        # this app. Nothing for us to clean up.
        pass
