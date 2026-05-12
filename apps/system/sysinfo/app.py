"""System information browser.

Compact info screen — CPU, memory, storage, battery, WiFi/BLE state,
firmware. Static rows stay put; dynamic ones (uptime / RAM / temp /
battery / RSSI) refresh every second.
"""

import gc
import os
import sys
import time

import M5
import machine
import network
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_LABEL = 0x40A0FF
_VAL = 0xFFFFFF
_OK = 0x00DD66

_SMALL = M5.Lcd.FONTS.DejaVu12
_PAD = 6
_HDR_H = 22
_HINT_H = 16
_LINE_H = 13


def _draw_header(text):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_HDR_FG, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print(text)


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(text)


def _format_uptime():
    s = time.ticks_ms() // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "{}h{:02d}m{:02d}s".format(h, m, sec)
    return "{}m{:02d}s".format(m, sec)


def _format_kb(b):
    return "{:.1f}KB".format(b / 1024)


def _format_mb(b):
    return "{:.2f}MB".format(b / (1024 * 1024))


def _format_mac(b):
    return ":".join("{:02X}".format(x) for x in b)


def _mcu_temp():
    try:
        import esp32
        return esp32.mcu_temperature()
    except Exception:
        return None


def _battery_mv():
    try:
        return M5.Power.getBatteryVoltage()
    except Exception:
        return None


def _ble_mac():
    try:
        import bluetooth
        ble = bluetooth.BLE()
        # config(mac) returns (addr_type, addr_bytes); addr_type 0=public
        _at, addr = ble.config("mac")
        return addr
    except Exception:
        return None


def _gather():
    """One snapshot of all values — cheap to call repeatedly."""
    rows = []

    rows.append(("Uptime", _format_uptime()))

    rows.append(("CPU", "{}MHz".format(machine.freq() // 1_000_000)))
    t = _mcu_temp()
    if t is not None:
        rows.append(("MCU temp", "{}C".format(t)))

    free = gc.mem_free()
    used = gc.mem_alloc()
    rows.append(("RAM free", _format_kb(free)))
    rows.append(("RAM used", _format_kb(used)))

    # /flash filesystem usage (the LittleFS partition you can write to)
    try:
        st = os.statvfs("/flash")
        bsz = st[0]
        total = st[2] * bsz
        free_b = st[3] * bsz
        rows.append(("/flash used", "{} / {}".format(
            _format_mb(total - free_b), _format_mb(total))))
    except Exception:
        pass

    # Full 8MB NOR flash partition layout — this is why /flash is much
    # smaller than the chip's nominal capacity.
    try:
        import esp32
        chip_total = 0
        # Walk both APP (type=0) and DATA (type=1) partitions
        all_parts = list(esp32.Partition.find(type=0)) + \
                    list(esp32.Partition.find(type=1))
        for p in all_parts:
            t, st_, off, size, name, _enc = p.info()
            chip_total = max(chip_total, off + size)
            rows.append(("part:" + name, _format_mb(size)))
        # Add bootloader + partition-table region (everything below the
        # first partition is pre-app stuff)
        first_off = min((p.info()[2] for p in all_parts), default=0)
        if first_off > 0:
            rows.append(("part:boot+pt", "{:.1f}KB".format(first_off / 1024)))
        rows.append(("Flash chip", _format_mb(chip_total)))
    except Exception:
        pass

    mv = _battery_mv()
    if mv is not None:
        rows.append(("Battery", "{:.2f}V".format(mv / 1000.0)))

    try:
        w = network.WLAN(network.STA_IF)
        if w.isconnected():
            try:
                rows.append(("WiFi", w.config("essid")))
            except Exception:
                pass
            try:
                rows.append(("IP", w.ifconfig()[0]))
            except Exception:
                pass
            try:
                rows.append(("RSSI", "{}dBm".format(w.status("rssi"))))
            except Exception:
                pass
        else:
            rows.append(("WiFi", "(not connected)"))
        try:
            rows.append(("WiFi MAC", _format_mac(w.config("mac"))))
        except Exception:
            pass
    except Exception:
        pass

    bm = _ble_mac()
    if bm is not None:
        rows.append(("BLE MAC", _format_mac(bm)))

    impl = sys.implementation
    rows.append(("MPY", "{}.{}.{}".format(*impl.version[:3])))
    try:
        build = impl._build
        if build:
            rows.append(("Build", build))
    except AttributeError:
        pass
    rows.append(("Platform", sys.platform))

    return rows


def _draw_rows(rows, scroll, max_rows):
    Lcd.fillRect(0, _HDR_H + 1, Lcd.width(),
                 Lcd.height() - _HDR_H - 1 - _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    visible = rows[scroll:scroll + max_rows]
    label_x = _PAD
    val_x = 90
    for i, (label, value) in enumerate(visible):
        y = _HDR_H + 4 + i * _LINE_H
        Lcd.setTextColor(_LABEL, _BG)
        Lcd.setCursor(label_x, y)
        Lcd.print(label)
        Lcd.setTextColor(_VAL, _BG)
        Lcd.setCursor(val_x, y)
        # Truncate value if too wide
        s = str(value)
        avail = Lcd.width() - val_x - _PAD
        while s and Lcd.textWidth(s, _SMALL) > avail:
            s = s[:-1]
        Lcd.print(s)


def run():
    Lcd.clear(_BG)
    kb = MatrixKeyboard()

    body_h = Lcd.height() - _HDR_H - _HINT_H - 4
    max_rows = max(1, body_h // _LINE_H)

    scroll = 0
    last_refresh = -10_000
    rows = _gather()

    _draw_header("System info")
    _draw_hint("^v scroll  R refresh  ESC")
    _draw_rows(rows, scroll, max_rows)

    while True:
        k = kb.get_key()
        if k == KeyCode.KEYCODE_ESC:
            return
        elif k == KeyCode.KEYCODE_UP:
            if scroll > 0:
                scroll -= 1
                _draw_rows(rows, scroll, max_rows)
        elif k == KeyCode.KEYCODE_DOWN:
            if scroll + max_rows < len(rows):
                scroll += 1
                _draw_rows(rows, scroll, max_rows)
        elif isinstance(k, int) and (k == ord("r") or k == ord("R")):
            rows = _gather()
            _draw_rows(rows, scroll, max_rows)

        # Auto-refresh dynamic values once per second
        now = time.ticks_ms()
        if time.ticks_diff(now, last_refresh) >= 1000:
            last_refresh = now
            new_rows = _gather()
            # Only repaint if anything changed (avoid flicker)
            if new_rows != rows:
                rows = new_rows
                _draw_rows(rows, scroll, max_rows)

        time.sleep_ms(40)
