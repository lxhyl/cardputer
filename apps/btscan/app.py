"""BLE scanner — discover and identify nearby Bluetooth Low Energy devices.

Continuously scans, lists devices sorted by signal strength, and tags
known protocols where it can:

  - Xiaomi Mi Body Composition Scale 2 (service data UUID 0x181B):
    decodes weight + impedance from the broadcast payload, no pairing.
  - Xiaomi Mi Smart Scale v1 (UUID 0x181D): decodes weight.
  - MiBeacon (UUID 0xFE95): tagged so you know it's a Mi/Aqara sensor or
    accessory; full decode needs the per-device bind key (see notes).
  - Apple / Microsoft / Samsung / Google / Bose / etc. by manufacturer ID.
  - Common BLE bulb / HID / audio names by substring match.

Useful for figuring out what protocol a no-name BLE device speaks before
deciding whether the cardputer can talk to it.

Controls:
  ↑ ↓ (or w / s, ; / .)   navigate list
  Enter                   open detail view (raw advertisement hex)
  R                       clear list & restart scan
  ESC                     exit

References (paste into a browser if curious):
  Mi Scale 2 ADV format: github.com/oliexdev/openScale/wiki/Xiaomi-Bluetooth-Mi-Scale
  MiBeacon protocol:     home-is-where-you-hang-your-hack.github.io/ble_monitor/MiBeacon_protocol
  MicroPython bluetooth: docs.micropython.org/en/latest/library/bluetooth.html
"""

import bluetooth
import time

import M5
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# IRQ event ids — see MicroPython bluetooth docs.
_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6

# AD type codes — Bluetooth Core Spec, Supplement to the Core Specification.
_AD_FLAGS = 0x01
_AD_INCOMP_16 = 0x02
_AD_COMP_16 = 0x03
_AD_INCOMP_128 = 0x06
_AD_COMP_128 = 0x07
_AD_SHORT_NAME = 0x08
_AD_COMPLETE_NAME = 0x09
_AD_TX_POWER = 0x0A
_AD_SERVICE_DATA_16 = 0x16
_AD_SERVICE_DATA_128 = 0x21
_AD_MANUFACTURER = 0xFF

# Manufacturer IDs from the Bluetooth SIG assigned-numbers list (subset).
_VENDORS = {
    0x004C: "Apple",
    0x0006: "Microsoft",
    0x0075: "Samsung",
    0x00E0: "Google",
    0x009E: "Bose",
    0x038F: "Xiaomi",
    0x0157: "Anker",
    0x0499: "Ruuvi",
    0x0059: "Nordic",
    0x02E5: "Espressif",
    0x004F: "Logitech",
    0x000F: "Broadcom",
    0x0087: "Garmin",
    0x019A: "TP-Link",
    0x0822: "TP-Link",         # newer assignment
    0x07A6: "Govee",
    0x0131: "Cypress / Infineon",
}

_W = 240
_H = 135
_HEADER_H = 16
_FOOTER_H = 14
_ROW_H = 14
_ROWS_VISIBLE = (_H - _HEADER_H - _FOOTER_H) // _ROW_H

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x888888
_SEL_BG = 0x224466
_ACCENT = 0x40C8FF
_GREEN = 0x7BB662
_AMBER = 0xE5A642
_RED = 0xE5483B
_DIVIDER = 0x202020

_SMALL = M5.Lcd.FONTS.DejaVu12
_MID = M5.Lcd.FONTS.DejaVu18


# ---- Advertisement parsing ---------------------------------------------

def _iter_ad(data):
    """Yield (ad_type, ad_payload_bytes) tuples from a raw adv payload."""
    i = 0
    n = len(data)
    while i + 1 < n:
        seg_len = data[i]
        if seg_len == 0 or i + seg_len >= n:
            return
        yield data[i + 1], bytes(data[i + 2:i + 1 + seg_len])
        i += 1 + seg_len


def _parse_adv(data):
    """Return dict with name, mfr_id, mfr_data, services, service_data."""
    info = {
        "name": "",
        "mfr_id": None,
        "mfr_data": b"",
        "services_16": [],
        "services_128": [],
        "service_data": [],   # list of (uuid16, payload_bytes)
        "flags": None,
        "tx_power": None,
    }
    for t, v in _iter_ad(data):
        if t == _AD_FLAGS and v:
            info["flags"] = v[0]
        elif t in (_AD_SHORT_NAME, _AD_COMPLETE_NAME):
            try:
                info["name"] = v.decode("utf-8", "replace")
            except Exception:
                info["name"] = repr(v)[2:-1]
        elif t in (_AD_INCOMP_16, _AD_COMP_16):
            for j in range(0, len(v), 2):
                if j + 1 < len(v):
                    info["services_16"].append(v[j] | (v[j + 1] << 8))
        elif t in (_AD_INCOMP_128, _AD_COMP_128):
            for j in range(0, len(v), 16):
                if j + 16 <= len(v):
                    info["services_128"].append(bytes(v[j:j + 16]))
        elif t == _AD_SERVICE_DATA_16 and len(v) >= 2:
            uuid = v[0] | (v[1] << 8)
            info["service_data"].append((uuid, bytes(v[2:])))
        elif t == _AD_MANUFACTURER and len(v) >= 2:
            info["mfr_id"] = v[0] | (v[1] << 8)
            info["mfr_data"] = bytes(v[2:])
        elif t == _AD_TX_POWER and v:
            info["tx_power"] = v[0] - 256 if v[0] > 127 else v[0]
    return info


# ---- Protocol-specific decoders ---------------------------------------

def _decode_mi_scale_v2(payload):
    """Mi Body Composition Scale 2, service data UUID 0x181B, 13 bytes.

    Layout (openScale wiki):
      [0]  ctrl1: bit0=lbs, bit4=stabilized, bit5=has_impedance, bit7=weight_removed
      [1]  ctrl2 (rarely used)
      [2..3]  year (LE)
      [4]  month
      [5]  day
      [6]  hour
      [7]  minute
      [8]  second
      [9..10]  impedance (LE)
      [11..12] weight (LE) — divide by 200 (kg) or 100 (lb)
    """
    if len(payload) < 13:
        return None
    ctrl = payload[0]
    raw_w = payload[11] | (payload[12] << 8)
    raw_z = payload[9] | (payload[10] << 8)
    is_lb = bool(ctrl & 0x01)
    stabilized = bool(ctrl & 0x20)
    has_z = bool(ctrl & 0x02)
    weight_kg = raw_w / (100.0 if is_lb else 200.0)
    if is_lb:
        weight_kg = weight_kg * 0.45359237
    return {
        "weight_kg": weight_kg,
        "impedance": raw_z if has_z and raw_z != 0 else None,
        "stabilized": stabilized,
        "is_lb": is_lb,
    }


def _decode_mi_scale_v1(payload):
    """Mi Smart Scale v1, service data UUID 0x181D, ~10 bytes.

    [0] ctrl: bit0=lbs, bit4=stabilized, bit7=weight_removed
    [1..2] weight (LE)
    """
    if len(payload) < 3:
        return None
    ctrl = payload[0]
    raw_w = payload[1] | (payload[2] << 8)
    is_lb = bool(ctrl & 0x01)
    weight_kg = raw_w / (100.0 if is_lb else 200.0)
    if is_lb:
        weight_kg = weight_kg * 0.45359237
    return {"weight_kg": weight_kg, "is_lb": is_lb,
            "stabilized": bool(ctrl & 0x20)}


def _classify(info):
    """Return (tag_string, color, special) where special is decoded info
    or None. Tag is at most ~14 chars to fit the list row."""
    name = info["name"] or ""
    name_lc = name.lower()

    # Mi Scale 2 — strongest signal: service data UUID 0x181B
    for uuid, payload in info["service_data"]:
        if uuid == 0x181B:
            d = _decode_mi_scale_v2(payload)
            if d is not None:
                return "Mi Scale 2", _GREEN, d
            return "Mi Scale 2?", _DIM, None
        if uuid == 0x181D:
            d = _decode_mi_scale_v1(payload)
            if d is not None:
                return "Mi Scale 1", _GREEN, d
            return "Mi Scale 1?", _DIM, None
        if uuid == 0xFE95:
            return "MiBeacon", _AMBER, None  # Mi sensors/locks/accessories

    # Manufacturer ID lookup
    if info["mfr_id"] is not None:
        v = _VENDORS.get(info["mfr_id"])
        if v:
            return v, _ACCENT, None
        return "mfr 0x%04X" % info["mfr_id"], _DIM, None

    # Name heuristics for unbranded BLE bulbs
    for kw, tag in (
        ("yeelight", "Yeelight"),
        ("mjdp", "Mi bulb"),
        ("mjlamp", "Mi lamp"),
        ("govee", "Govee"),
        ("triones", "BLE bulb"),
        ("ledble", "BLE bulb"),
        ("mesh", "BLE Mesh"),
        ("airpods", "AirPods"),
        ("magic", "Magic Home"),
        ("ihoment", "Govee"),
        ("hue", "Hue (BT)"),
        ("ble-bulb", "BLE bulb"),
    ):
        if kw in name_lc:
            return tag, _ACCENT, None

    if 0x180F in info["services_16"]:
        return "Battery", _DIM, None
    if 0x1812 in info["services_16"]:
        return "HID", _ACCENT, None
    if 0x180D in info["services_16"]:
        return "HRate", _ACCENT, None

    return "", _DIM, None


# ---- Scanner ------------------------------------------------------------

class Scanner:
    """Wraps bluetooth.BLE() with a per-MAC device dict.

    IRQ runs in interrupt context so we keep it allocation-free; all
    parsing happens later from the main loop.
    """

    def __init__(self):
        self._ble = bluetooth.BLE()
        self._devs = {}      # key: bytes(addr) -> dict
        self._order = []     # addrs in first-seen order — stable list ordering
        self._raw_queue = []  # (addr, addr_type, rssi, bytes(adv_data))

    def start(self):
        try:
            self._ble.active(False)
        except Exception:
            pass
        self._ble.active(True)
        self._ble.irq(self._on_irq)
        # Active scan picks up SCAN_RSP (often where the device name lives).
        # Wide window + interval = catch as many adv as possible.
        self._ble.gap_scan(0, 50_000, 50_000, True)

    def stop(self):
        try:
            self._ble.gap_scan(None)
        except Exception:
            pass
        try:
            self._ble.irq(None)
        except Exception:
            pass
        try:
            self._ble.active(False)
        except Exception:
            pass

    def _on_irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            addr_type, addr, _adv_type, rssi, adv_data = data
            # Allocation in IRQ is risky; keep it minimal.
            try:
                self._raw_queue.append((bytes(addr), addr_type,
                                        rssi, bytes(adv_data)))
            except Exception:
                pass

    def drain(self):
        """Process queued raw adv records into self._devs. Returns number
        of devices touched."""
        n = 0
        q = self._raw_queue
        self._raw_queue = []
        now = time.ticks_ms()
        for addr, addr_type, rssi, adv in q:
            n += 1
            d = self._devs.get(addr)
            if d is None:
                d = {
                    "addr": addr,
                    "addr_type": addr_type,
                    "rssi": rssi,
                    "rssi_ema": float(rssi),
                    "last_seen": now,
                    "info": _parse_adv(adv),
                    "raw": adv,
                    "count": 1,
                }
                self._devs[addr] = d
                self._order.append(addr)
            else:
                d["rssi"] = rssi
                # EWMA smoothing so the displayed value stops jittering by a
                # few dB every redraw. Time constant ≈ 4 samples.
                d["rssi_ema"] = d["rssi_ema"] * 0.7 + rssi * 0.3
                d["last_seen"] = now
                d["count"] += 1
                # Merge in fields that we didn't have before (a SCAN_RSP
                # commonly arrives separately from the ADV_IND).
                new_info = _parse_adv(adv)
                old = d["info"]
                if not old["name"] and new_info["name"]:
                    old["name"] = new_info["name"]
                if old["mfr_id"] is None and new_info["mfr_id"] is not None:
                    old["mfr_id"] = new_info["mfr_id"]
                    old["mfr_data"] = new_info["mfr_data"]
                for u in new_info["services_16"]:
                    if u not in old["services_16"]:
                        old["services_16"].append(u)
                for u in new_info["services_128"]:
                    if u not in old["services_128"]:
                        old["services_128"].append(u)
                for sd in new_info["service_data"]:
                    # replace any existing entry for the same UUID
                    old["service_data"] = [
                        x for x in old["service_data"] if x[0] != sd[0]
                    ]
                    old["service_data"].append(sd)
                if new_info["tx_power"] is not None:
                    old["tx_power"] = new_info["tx_power"]
                d["raw"] = adv
        return n

    def device_list(self):
        """Devices in first-seen order — stable, so the user can lock onto
        a row and select it without it dancing around. RSSI updates in
        place inside the row."""
        out = []
        for a in self._order:
            d = self._devs.get(a)
            if d is not None:
                out.append(d)
        return out

    def reset(self):
        self._devs = {}
        self._order = []
        self._raw_queue = []


# ---- Drawing ------------------------------------------------------------

def _fmt_addr(addr):
    return ":".join("%02X" % b for b in addr)


def _rssi_color(rssi):
    if rssi >= -60:
        return _GREEN
    if rssi >= -80:
        return _AMBER
    return _RED


def _draw_header(count, scanning):
    Lcd.fillRect(0, 0, _W, _HEADER_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_FG, _BG)
    Lcd.setCursor(4, 2)
    Lcd.print("BTSCAN")
    msg = "%d dev" % count
    Lcd.setTextColor(_DIM, _BG)
    tw = Lcd.textWidth(msg, _SMALL)
    Lcd.setCursor(_W - tw - 4, 2)
    Lcd.print(msg)
    if scanning:
        Lcd.setTextColor(_ACCENT, _BG)
        Lcd.setCursor(_W // 2 - 16, 2)
        Lcd.print("scan...")
    Lcd.drawLine(0, _HEADER_H - 1, _W, _HEADER_H - 1, _DIVIDER)


def _draw_footer(text):
    y = _H - _FOOTER_H
    Lcd.fillRect(0, y, _W, _FOOTER_H, _BG)
    Lcd.drawLine(0, y, _W, y, _DIVIDER)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, y + 1)
    Lcd.print(text)


def _truncate(s, max_chars):
    if len(s) <= max_chars:
        return s
    return s[:max_chars - 1] + "."


def _draw_list(scanner, sel_idx, scroll):
    items = scanner.device_list()
    _draw_header(len(items), True)
    Lcd.fillRect(0, _HEADER_H, _W, _H - _HEADER_H - _FOOTER_H, _BG)

    if not items:
        Lcd.setFont(_SMALL)
        Lcd.setTextColor(_DIM, _BG)
        msg = "no devices yet"
        tw = Lcd.textWidth(msg, _SMALL)
        Lcd.setCursor((_W - tw) // 2, _HEADER_H + 36)
        Lcd.print(msg)
        _draw_footer("ESC quit  R reset")
        return items

    # Clamp selection
    if sel_idx >= len(items):
        sel_idx = len(items) - 1
    if sel_idx < 0:
        sel_idx = 0
    # Adjust scroll
    if sel_idx < scroll:
        scroll = sel_idx
    elif sel_idx >= scroll + _ROWS_VISIBLE:
        scroll = sel_idx - _ROWS_VISIBLE + 1

    Lcd.setFont(_SMALL)
    for row in range(_ROWS_VISIBLE):
        i = scroll + row
        if i >= len(items):
            break
        d = items[i]
        info = d["info"]
        tag, tag_color, _ = _classify(info)
        y = _HEADER_H + row * _ROW_H
        if i == sel_idx:
            Lcd.fillRect(0, y, _W, _ROW_H, _SEL_BG)
            row_bg = _SEL_BG
        else:
            row_bg = _BG
        # RSSI on the left (smoothed so the value doesn't twitch each frame).
        rssi = int(d.get("rssi_ema", d["rssi"]))
        Lcd.setTextColor(_rssi_color(rssi), row_bg)
        Lcd.setCursor(2, y + 1)
        Lcd.print("%4d" % rssi)
        # Name in the middle
        name = info["name"] or _fmt_addr(d["addr"])
        # Reserve room for the tag.
        max_name = 18
        if tag:
            max_name = 14
        Lcd.setTextColor(_FG, row_bg)
        Lcd.setCursor(34, y + 1)
        Lcd.print(_truncate(name, max_name))
        # Tag on the right
        if tag:
            Lcd.setTextColor(tag_color, row_bg)
            tw = Lcd.textWidth(tag, _SMALL)
            Lcd.setCursor(_W - tw - 4, y + 1)
            Lcd.print(tag)

    _draw_footer("\x18\x19 nav  Enter \x1b back  R reset")
    return items


def _draw_detail(d):
    Lcd.clear(_BG)
    info = d["info"]
    tag, tag_color, special = _classify(info)

    Lcd.setFont(_MID)
    Lcd.setTextColor(_FG, _BG)
    name = info["name"] or "(no name)"
    Lcd.setCursor(4, 0)
    Lcd.print(_truncate(name, 22))

    Lcd.setFont(_SMALL)
    y = 22
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, y)
    Lcd.print(_fmt_addr(d["addr"]))
    Lcd.setTextColor(_rssi_color(d["rssi"]), _BG)
    rmsg = "%d dBm" % d["rssi"]
    rw = Lcd.textWidth(rmsg, _SMALL)
    Lcd.setCursor(_W - rw - 4, y)
    Lcd.print(rmsg)
    y += 14

    # Tag + vendor
    if tag:
        Lcd.setTextColor(tag_color, _BG)
        Lcd.setCursor(4, y)
        Lcd.print(tag)
        y += 14

    # Mi Scale decoded value, big
    if special and "weight_kg" in special:
        Lcd.setFont(_MID)
        Lcd.setTextColor(_GREEN if special.get("stabilized") else _AMBER, _BG)
        msg = "%.2f kg" % special["weight_kg"]
        if special.get("impedance"):
            msg += "  Z=%d" % special["impedance"]
        Lcd.setCursor(4, y)
        Lcd.print(msg)
        Lcd.setFont(_SMALL)
        y += 22
        if not special.get("stabilized"):
            Lcd.setTextColor(_DIM, _BG)
            Lcd.setCursor(4, y)
            Lcd.print("(stabilizing...)")
            y += 12

    # Service UUIDs
    if info["services_16"]:
        Lcd.setTextColor(_DIM, _BG)
        Lcd.setCursor(4, y)
        s = "svc16: " + ", ".join("%04X" % u for u in info["services_16"][:6])
        Lcd.print(_truncate(s, 36))
        y += 12

    # Raw advertisement hex (truncated)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, y)
    Lcd.print("adv:")
    y += 12
    raw = d["raw"]
    hex_chars_per_line = 24  # 12 bytes per line at 2 chars each, no spaces
    bytes_per_line = 12
    Lcd.setTextColor(_FG, _BG)
    for i in range(0, min(len(raw), bytes_per_line * 3), bytes_per_line):
        line = "".join("%02X" % b for b in raw[i:i + bytes_per_line])
        Lcd.setCursor(4, y)
        Lcd.print(line)
        y += 12
        if y > _H - _FOOTER_H - 12:
            break

    _draw_footer("ESC back   seen %dx" % d["count"])


# ---- Main loop ----------------------------------------------------------

def run():
    kb = MatrixKeyboard()
    Lcd.clear(_BG)
    _draw_header(0, True)
    _draw_footer("starting BLE...")

    sc = Scanner()
    try:
        sc.start()
    except Exception as e:
        Lcd.setFont(_SMALL)
        Lcd.setTextColor(_RED, _BG)
        Lcd.setCursor(4, 40)
        Lcd.print("BLE start failed:")
        Lcd.setCursor(4, 56)
        Lcd.print(repr(e)[:36])
        while kb.get_key() != KeyCode.KEYCODE_ESC:
            time.sleep_ms(60)
        return

    sel_idx = 0
    scroll = 0
    detail = None
    last_draw = 0

    try:
        while True:
            sc.drain()
            now = time.ticks_ms()
            if detail is None:
                if time.ticks_diff(now, last_draw) >= 700:
                    last_draw = now
                    items = _draw_list(sc, sel_idx, scroll)
                    # rebind scroll after redraw applied clamp
                    if items:
                        if sel_idx >= len(items):
                            sel_idx = len(items) - 1
                        if sel_idx < scroll:
                            scroll = sel_idx
                        elif sel_idx >= scroll + _ROWS_VISIBLE:
                            scroll = sel_idx - _ROWS_VISIBLE + 1
            else:
                # Refresh detail view periodically (Mi Scale stabilizes).
                if time.ticks_diff(now, last_draw) >= 700:
                    last_draw = now
                    addr = detail["addr"]
                    fresh = sc._devs.get(addr)
                    if fresh:
                        detail = fresh
                    _draw_detail(detail)

            k = kb.get_key()
            if k is None:
                time.sleep_ms(40)
                continue
            if k == KeyCode.KEYCODE_ESC:
                if detail is not None:
                    detail = None
                    last_draw = 0  # force redraw
                else:
                    break
            elif k == KeyCode.KEYCODE_ENTER:
                if detail is None:
                    items = sc.device_list()
                    if items and 0 <= sel_idx < len(items):
                        detail = items[sel_idx]
                        last_draw = 0
            elif k == KeyCode.KEYCODE_UP or k == ord("w") or k == ord(";"):
                if detail is None and sel_idx > 0:
                    sel_idx -= 1
                    last_draw = 0
            elif k == KeyCode.KEYCODE_DOWN or k == ord("s") or k == ord("."):
                if detail is None:
                    sel_idx += 1
                    last_draw = 0
            elif k == ord("r") or k == ord("R"):
                sc.reset()
                sel_idx = 0
                scroll = 0
                detail = None
                last_draw = 0
    finally:
        sc.stop()
