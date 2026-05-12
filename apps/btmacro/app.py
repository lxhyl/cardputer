"""BLE HID keyboard for Cardputer-Adv.

GATT structure + characteristic values are taken verbatim from
Heerkog/MicroPythonBLEHID (Erik Andresen — the canonical low-level
MicroPython BLE HID reference). We can't import that library directly
because it depends on `hid_keystores` and bonding APIs this firmware
doesn't expose, so we inline the relevant pieces. The wire-level GATT
tree is identical to what every Heerkog user's macOS / iOS / Windows
machine successfully pairs with.

Source: https://github.com/Heerkog/MicroPythonBLEHID/blob/master/hid_services.py

Bonding limitation on this firmware: `BLE.config()` doesn't accept
`bond`/`mitm`/`io`/`le_secure`, so pairing is JustWorks and bonds do
not persist across reboots. After a Cardputer reboot, the host's
"forget device" + re-pair flow may be needed.
"""

import struct
import time

import M5
import bluetooth
from bluetooth import UUID
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# --- BLE flags (mirrors bluetooth.FLAG_* shorthand from Heerkog) ---
F_READ = bluetooth.FLAG_READ
F_WRITE = bluetooth.FLAG_WRITE
F_READ_NOTIFY = bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY
F_READ_WRITE_NORESPONSE = (bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
                           | bluetooth.FLAG_WRITE_NO_RESPONSE)
F_READ_WRITE_NOTIFY_NORESPONSE = (bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
                                  | bluetooth.FLAG_NOTIFY
                                  | bluetooth.FLAG_WRITE_NO_RESPONSE)
# Encryption-required permission flags. Not exported as named constants in
# this firmware but the integer values are accepted in the flags field
# (mirrors the canonical micropython/examples/bluetooth/ble_bonding_peripheral.py).
_FLAG_READ_ENCRYPTED  = 0x0200
_FLAG_WRITE_ENCRYPTED = 0x1000
# HID input/output reports MUST require encryption — without this, the host
# never initiates SMP pairing, the link stays unencrypted, and HID class
# drivers (macOS/iOS) silently ignore the report stream.
F_READ_NOTIFY_ENCRYPTED = F_READ_NOTIFY | _FLAG_READ_ENCRYPTED
F_READ_WRITE_NOTIFY_NORESPONSE_ENCRYPTED = (F_READ_WRITE_NOTIFY_NORESPONSE
                                            | _FLAG_READ_ENCRYPTED
                                            | _FLAG_WRITE_ENCRYPTED)

DSC_F_READ = 0x02  # descriptor "readable" flag (per Heerkog)

# IRQ codes
_IRQ_CENTRAL_CONNECT     = 1
_IRQ_CENTRAL_DISCONNECT  = 2
_IRQ_GATTS_WRITE         = 3
_IRQ_GATTS_READ_REQUEST  = 4
_IRQ_MTU_EXCHANGED       = 21
_IRQ_CONNECTION_UPDATE   = 27
_IRQ_ENCRYPTION_UPDATE   = 28
_IRQ_GET_SECRET          = 29
_IRQ_SET_SECRET          = 30
_IRQ_PASSKEY_ACTION      = 31

_IRQ_NAMES = {1: "CONNECT", 2: "DISCONN", 3: "WRITE", 4: "READ_REQ",
              21: "MTU", 27: "CONN_UPD", 28: "ENCRYPT",
              29: "GET_SEC", 30: "SET_SEC", 31: "PASSKEY"}

_SECRETS_FILE = "/flash/ble_bonds.json"

# Advertising LTV record types
_ADV_TYPE_FLAGS       = 0x01
_ADV_TYPE_NAME        = 0x09
_ADV_TYPE_UUID16_LIST = 0x03
_ADV_TYPE_APPEARANCE  = 0x19


# --- HID Report Descriptor (verbatim Heerkog Keyboard report map) ---
# Report ID 1, 8-byte input (modifier + reserved + 6 keys), 1-byte LED output.
_HID_REPORT_MAP = bytes([
    0x05, 0x01,  # Usage Page (Generic Desktop)
    0x09, 0x06,  # Usage (Keyboard)
    0xA1, 0x01,  # Collection (Application)
    0x85, 0x01,  #   Report ID (1)
    0x75, 0x01,  #   Report Size (1)
    0x95, 0x08,  #   Report Count (8)
    0x05, 0x07,  #   Usage Page (Key Codes)
    0x19, 0xE0,  #   Usage Min (224)
    0x29, 0xE7,  #   Usage Max (231)
    0x15, 0x00,  #   Logical Min (0)
    0x25, 0x01,  #   Logical Max (1)
    0x81, 0x02,  #   Input (Data, Var, Abs) — modifier byte
    0x95, 0x01,  #   Report Count (1)
    0x75, 0x08,  #   Report Size (8)
    0x81, 0x01,  #   Input (Const) — reserved byte
    0x95, 0x05,  #   Report Count (5)
    0x75, 0x01,  #   Report Size (1)
    0x05, 0x08,  #   Usage Page (LEDs)
    0x19, 0x01,  #   Usage Min (Num Lock)
    0x29, 0x05,  #   Usage Max (Kana)
    0x91, 0x02,  #   Output (Data, Var, Abs) — LED report
    0x95, 0x01,  #   Report Count (1)
    0x75, 0x03,  #   Report Size (3)
    0x91, 0x01,  #   Output (Const) — LED padding
    0x95, 0x06,  #   Report Count (6)
    0x75, 0x08,  #   Report Size (8)
    0x15, 0x00,  #   Logical Min (0)
    0x25, 0x65,  #   Logical Max (101)
    0x05, 0x07,  #   Usage Page (Key Codes)
    0x19, 0x00,  #   Usage Min (0)
    0x29, 0x65,  #   Usage Max (101)
    0x81, 0x00,  #   Input (Data, Array) — 6-byte key array
    0xC0,        # End Collection
])


# --- Service tree: DIS, BAS, DID, HIDS — order matters (handle indexing) ---
_DIS = (
    UUID(0x180A),
    (
        (UUID(0x2A24), F_READ),  # Model number
        (UUID(0x2A25), F_READ),  # Serial number
        (UUID(0x2A26), F_READ),  # Firmware rev
        (UUID(0x2A27), F_READ),  # Hardware rev
        (UUID(0x2A28), F_READ),  # Software rev
        (UUID(0x2A29), F_READ),  # Manufacturer name
        (UUID(0x2A50), F_READ),  # PnP ID
    ),
)

_BAS = (
    UUID(0x180F),
    (
        (UUID(0x2A19), F_READ_NOTIFY, (
            (UUID(0x2904), DSC_F_READ),  # Characteristic Presentation Format
        )),
    ),
)

_DID = (
    UUID(0x1200),  # PnP Information (Device ID profile)
    (
        (UUID(0x0200), F_READ),  # SpecificationID
        (UUID(0x0201), F_READ),  # VendorID
        (UUID(0x0202), F_READ),  # ProductID
        (UUID(0x0203), F_READ),  # Version
        (UUID(0x0204), F_READ),  # PrimaryRecord
        (UUID(0x0205), F_READ),  # VendorIDSource
    ),
)

_HIDS = (
    UUID(0x1812),
    (
        (UUID(0x2A4A), F_READ),                                       # HID information
        (UUID(0x2A4B), F_READ),                                       # HID report map (must read pre-bond for discovery)
        (UUID(0x2A4C), F_READ_WRITE_NORESPONSE),                      # HID control point
        (UUID(0x2A4D), F_READ_NOTIFY_ENCRYPTED, (                     # HID input report (encrypted to force SMP pairing)
            (UUID(0x2908), DSC_F_READ),
        )),
        (UUID(0x2A4D), F_READ_WRITE_NOTIFY_NORESPONSE_ENCRYPTED, (    # HID output report (LED, encrypted)
            (UUID(0x2908), DSC_F_READ),
        )),
        (UUID(0x2A4E), F_READ_WRITE_NORESPONSE),                      # HID protocol mode
    ),
)


def _adv_payload(name, services=(UUID(0x1812),), appearance=961):
    """Builds the advertising packet — Heerkog's `advertising_payload`
    inline. flags = LE General Discoverable (0x02) + BR/EDR Not Supported
    (0x04) = 0x06. Appearance 961 = Keyboard. Only the HID service UUID
    is advertised (macOS / iOS use that to recognise the device as a
    keyboard)."""
    p = bytearray()

    def _ltv(t, value):
        p.extend(struct.pack("BB", len(value) + 1, t))
        p.extend(value)

    _ltv(_ADV_TYPE_FLAGS, struct.pack("B", 0x02 | 0x04))
    _ltv(_ADV_TYPE_NAME, name.encode("utf-8"))
    for u in services:
        _ltv(_ADV_TYPE_UUID16_LIST, bytes(u))
    _ltv(_ADV_TYPE_APPEARANCE, struct.pack("<h", appearance))
    return bytes(p)


class BLEKeyboard:
    """Minimal Heerkog-style BLE HID keyboard. No bonding (firmware
    doesn't expose the config keys for it), no battery notifications
    (static 100% is fine for a USB-powered dev board)."""

    def __init__(self, name="Cardputer-Adv"):
        self._name = name
        self._conn = None
        self.event_log = []
        self._secrets = self._load_secrets()

        # Order matters — match the official MicroPython
        # examples/bluetooth/ble_bonding_peripheral.py sequence exactly:
        #   1) BLE() + IRQ handler set
        #   2) config(bond / le_secure / mitm / io)  ←  BEFORE active(True)
        #   3) active(True)
        #   4) config(addr_mode) AFTER active
        #   5) register services + advertise
        # Doing config(bond=True) after active(True) silently no-ops on
        # NimBLE — that's why secrets never got persisted in earlier tries.
        self._ble = bluetooth.BLE()
        self._ble.irq(self._irq)
        # io=3 = NoInputNoOutput → JustWorks pairing. mitm=False to keep
        # JustWorks (mitm=True would force passkey display which we can't
        # do with a NoInputNoOutput capability — would fail to bond).
        try:
            self._ble.config(bond=True)
            self._ble.config(le_secure=True)
            self._ble.config(mitm=False)
            self._ble.config(io=3)
        except Exception:
            pass
        self._ble.active(True)
        # NOTE: deliberately NOT setting addr_mode=2 (RPA). Most ESP32 BLE
        # keyboard projects (T-vK/ESP32-BLE-Keyboard, NimBLE-Arduino) use
        # the public BD_ADDR. If you advertise with a fresh random address
        # every boot, macOS treats each as a different device and the
        # discovery/pair flow gets ambiguous. Public address = stable
        # identity = Mac's BT settings UI behaves predictably.
        self._ble.config(gap_name=name)

        # Register services in order DIS, BAS, DID, HIDS.
        handles = self._ble.gatts_register_services((_DIS, _BAS, _DID, _HIDS))
        (h_mod, h_ser, h_fwr, h_hwr, h_swr, h_man, h_pnp) = handles[0]
        (self._h_bat, _h_bfmt) = handles[1]
        (h_sid, h_vid, h_pid, h_ver, h_rec, h_vs) = handles[2]
        (h_info, h_hid, h_ctrl,
         self._h_rep, h_d1,
         self._h_repout, h_d2,
         h_proto) = handles[3]

        # DIS values
        self._ble.gatts_write(h_mod, b"Cardputer-Adv")
        self._ble.gatts_write(h_ser, b"1")
        self._ble.gatts_write(h_fwr, b"1.0")
        self._ble.gatts_write(h_hwr, b"1.0")
        self._ble.gatts_write(h_swr, b"1.0")
        self._ble.gatts_write(h_man, b"M5Stack")
        # PnP ID: vendor_src=USB(2), VID=0x05AC, PID=0x820A, ver=0x0123 — big-endian
        self._ble.gatts_write(h_pnp,
                              struct.pack(">BHHH", 0x02, 0x05AC, 0x820A, 0x0123))

        # BAS
        self._ble.gatts_write(self._h_bat, b"\x64")  # 100%

        # DID
        self._ble.gatts_write(h_sid, struct.pack(">H", 0x0103))   # SpecID
        self._ble.gatts_write(h_vid, struct.pack(">H", 0x05AC))   # VendorID
        self._ble.gatts_write(h_pid, struct.pack(">H", 0x820A))   # ProductID
        self._ble.gatts_write(h_ver, struct.pack(">H", 0x0123))   # Version
        self._ble.gatts_write(h_rec, b"\x01")                     # Primary
        self._ble.gatts_write(h_vs,  b"\x01")                     # source = SIG

        # HIDS
        # HID Info: bcdHID=0x0111 (little-endian: 11 01), country=0, flags=00.
        # Heerkog's exact bytes — empirically what macOS accepts.
        self._ble.gatts_write(h_info, b"\x01\x01\x00\x00")
        self._ble.gatts_write(h_hid, _HID_REPORT_MAP)
        self._ble.gatts_write(h_ctrl, b"\x00")
        self._ble.gatts_write(h_d1, struct.pack("<BB", 1, 1))   # Report Ref: id=1, type=Input
        self._ble.gatts_write(h_d2, struct.pack("<BB", 1, 2))   # Report Ref: id=1, type=Output
        self._ble.gatts_write(h_proto, b"\x01")                  # Report Mode
        # Seed the report value so reads before any keypress return zeros
        self._ble.gatts_write(self._h_rep, b"\x00" * 8)
        self._ble.gatts_write(self._h_repout, b"\x00")

        self._adv_data = _adv_payload(name)

    def start_advertising(self):
        self._ble.gap_advertise(100_000, adv_data=self._adv_data)

    def stop_advertising(self):
        try:
            self._ble.gap_advertise(None)
        except Exception:
            pass

    def shutdown(self):
        """Stop advertising AND deactivate the BLE radio. Without the
        active(False) call, the chip keeps broadcasting after the app
        exits — Mac sees a ghost 'Nearby Device' that won't go away."""
        self.stop_advertising()
        try:
            self._ble.active(False)
        except Exception:
            pass

    def is_connected(self):
        return self._conn is not None

    # ---- bond key persistence — pattern from official MicroPython
    # examples/bluetooth/ble_bonding_peripheral.py. Secrets are keyed by
    # (sec_type:int, key:bytes) tuple. JSON-on-flash via base64.
    def _load_secrets(self):
        import binascii, json
        secrets = {}
        try:
            with open(_SECRETS_FILE) as f:
                for sec_type, key, value in json.load(f):
                    secrets[(sec_type, binascii.a2b_base64(key))] = \
                        binascii.a2b_base64(value)
        except Exception:
            pass
        return secrets

    def _save_secrets(self):
        import binascii, json
        try:
            entries = [
                (st, binascii.b2a_base64(k).decode().strip(),
                     binascii.b2a_base64(v).decode().strip())
                for (st, k), v in self._secrets.items()
            ]
            with open(_SECRETS_FILE, "w") as f:
                json.dump(entries, f)
        except Exception:
            pass

    def _irq(self, event, data):
        try:
            self.event_log.append((time.ticks_ms(),
                                   _IRQ_NAMES.get(event, str(event)),
                                   tuple(data) if isinstance(data, (list, tuple)) else data))
            if len(self.event_log) > 30:
                self.event_log.pop(0)
        except Exception:
            pass

        if event == _IRQ_CENTRAL_CONNECT:
            self._conn, _, _ = data
            # macOS doesn't auto-initiate SMP on encrypted-characteristic
            # discovery — it just gives up. Proactively request pairing
            # so the host gets the SMP Pairing Request from us.
            try:
                self._ble.gap_pair(self._conn)
            except Exception:
                pass
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn = None
            self._save_secrets()
            try: self.start_advertising()
            except Exception: pass
        elif event == _IRQ_SET_SECRET:
            sec_type, key, value = data
            key = (sec_type, bytes(key))
            if value is None:
                if key in self._secrets:
                    del self._secrets[key]
                    return True
                return False
            self._secrets[key] = bytes(value)
            return True
        elif event == _IRQ_GET_SECRET:
            sec_type, index, key = data
            if key is None:
                # Indexed iteration over saved secrets matching sec_type
                i = 0
                for (t, _k), v in self._secrets.items():
                    if t == sec_type:
                        if i == index:
                            return v
                        i += 1
                return None
            return self._secrets.get((sec_type, bytes(key)), None)
        elif event == _IRQ_PASSKEY_ACTION:
            # JustWorks (io=NoInputNoOutput, mitm=False) — nothing to do.
            pass

    def send_report(self, modifier, keys):
        """Notify an 8-byte HID input report (modifier, reserved, k0..k5)."""
        if self._conn is None:
            return False
        rep = bytearray(8)
        rep[0] = modifier & 0xFF
        for i, k in enumerate(keys[:6]):
            rep[2 + i] = k & 0xFF
        try:
            self._ble.gatts_notify(self._conn, self._h_rep, bytes(rep))
            return True
        except OSError:
            return False

    def send_release(self):
        return self.send_report(0, [])


# ---------------- HID code mappings (US ANSI, mirrors USB app) ----------------

def _char_to_hid(ch):
    if "a" <= ch <= "z":
        return False, 0x04 + ord(ch) - ord("a")
    if "A" <= ch <= "Z":
        return True, 0x04 + ord(ch) - ord("A")
    if "1" <= ch <= "9":
        return False, 0x1E + ord(ch) - ord("1")
    if ch == "0":
        return False, 0x27
    table = {
        " ":  (False, 0x2C),
        "-":  (False, 0x2D), "_":  (True,  0x2D),
        "=":  (False, 0x2E), "+":  (True,  0x2E),
        "[":  (False, 0x2F), "{":  (True,  0x2F),
        "]":  (False, 0x30), "}":  (True,  0x30),
        "\\": (False, 0x31), "|":  (True,  0x31),
        ";":  (False, 0x33), ":":  (True,  0x33),
        "'":  (False, 0x34), "\"": (True,  0x34),
        "`":  (False, 0x35), "~":  (True,  0x35),
        ",":  (False, 0x36), "<":  (True,  0x36),
        ".":  (False, 0x37), ">":  (True,  0x37),
        "/":  (False, 0x38), "?":  (True,  0x38),
        "!":  (True,  0x1E), "@":  (True,  0x1F), "#":  (True,  0x20),
        "$":  (True,  0x21), "%":  (True,  0x22), "^":  (True,  0x23),
        "&":  (True,  0x24), "*":  (True,  0x25), "(":  (True,  0x26),
        ")":  (True,  0x27),
    }
    return table.get(ch, (False, 0))


_SPECIAL_HID = {
    KeyCode.KEYCODE_ENTER:     0x28,
    KeyCode.KEYCODE_BACKSPACE: 0x2A,
    KeyCode.KEYCODE_DEL:       0x4C,
    KeyCode.KEYCODE_TAB:       0x2B,
    KeyCode.KEYCODE_SPACE:     0x2C,
    KeyCode.KEYCODE_UP:        0x52,
    KeyCode.KEYCODE_DOWN:      0x51,
    KeyCode.KEYCODE_LEFT:      0x50,
    KeyCode.KEYCODE_RIGHT:     0x4F,
}

_MOD_LCTRL  = 0x01
_MOD_LSHIFT = 0x02
_MOD_LGUI   = 0x08


# ---------------- UI ----------------

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_OK = 0x00DD66
_RED = 0xFF6060
_BLUE = 0x40A0FF
_SEL_BG = 0xFFFFFF
_SEL_FG = 0x000000

_FONT = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12
_PAD = 6
_HDR_H = 22
_HINT_H = 16
_LIST_TOP = 28
_LINE_H = 20
_VISIBLE = 4

_LIVE_TYPING = "_LIVE_"

_MACROS = (
    ("> Live typing mode", _LIVE_TYPING),
    ("Lock screen (Cmd+Ctrl+Q)",
     lambda kbd: _shortcut(kbd, _MOD_LGUI | _MOD_LCTRL, [0x14])),
)


def _shortcut(kbd, mods, keys):
    kbd.send_report(mods, keys)
    time.sleep_ms(20)
    kbd.send_release()


def _truncate(s, n):
    return s if len(s) <= n else s[:n - 1] + "."


def _spk_off():
    """Same ES8311 power-down as launcher / morse (ESP-ADF values)."""
    try:
        from machine import I2C, Pin
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400000)
        for reg, val in (
            (0x32, 0x00), (0x12, 0x02), (0x13, 0x10),
            (0x0E, 0xFF), (0x14, 0x00), (0x0D, 0xFA),
            (0x37, 0x08), (0x00, 0x00),
        ):
            i2c.writeto_mem(0x18, reg, bytes([val]))
    except Exception:
        pass


def _draw_header(text, color=_HDR_FG):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print(_truncate(text, 38))


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(text)


def _draw_list(idx, scroll, header):
    Lcd.fillRect(0, _HDR_H + 1, Lcd.width(),
                 Lcd.height() - _HDR_H - 1 - _HINT_H, _BG)
    _draw_header(header)
    Lcd.setFont(_FONT)
    visible = _MACROS[scroll:scroll + _VISIBLE]
    for i, (name, _fn) in enumerate(visible):
        actual = scroll + i
        y = _LIST_TOP + i * _LINE_H
        is_sel = actual == idx
        if is_sel:
            Lcd.fillRect(0, y - 2, Lcd.width(), _LINE_H, _SEL_BG)
            Lcd.setTextColor(_SEL_FG, _SEL_BG)
        else:
            Lcd.setTextColor(_FG, _BG)
        Lcd.setCursor(_PAD, y)
        Lcd.print(_truncate(name, 26))


def _live_typing(kb_in, ble_kbd):
    Lcd.clear(_BG)
    _draw_header("LIVE typing → BLE host", color=_OK)
    _draw_hint("ESC = exit  every key forwards")
    Lcd.setFont(_FONT)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 8)
    Lcd.print("type away...")
    tail = ""

    def repaint():
        Lcd.fillRect(0, _HDR_H + 30, Lcd.width(),
                     Lcd.height() - _HDR_H - 30 - _HINT_H, _BG)
        Lcd.setFont(_FONT)
        Lcd.setTextColor(_OK, _BG)
        show = tail[-16:]
        pretty = ""
        for c in show:
            if c == "\n": pretty += "_"
            elif c == "\b": pretty += "<"
            elif c == "\t": pretty += ">"
            else: pretty += c
        Lcd.setCursor(_PAD, _HDR_H + 32)
        Lcd.print(pretty)

    while True:
        k = kb_in.get_key()
        if k is None:
            time.sleep_ms(15)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return
        if not ble_kbd.is_connected():
            Lcd.setFont(_SMALL)
            Lcd.setTextColor(_RED, _BG)
            Lcd.setCursor(_PAD, _HDR_H + 70)
            Lcd.print("not connected — pair host first")
            continue

        try:
            if k in _SPECIAL_HID:
                hid = _SPECIAL_HID[k]
                ble_kbd.send_report(0, [hid])
                time.sleep_ms(8)
                ble_kbd.send_release()
                if k == KeyCode.KEYCODE_ENTER:
                    # Clear the on-device preview after Enter — matches the
                    # mental model of a terminal/chat input where Enter
                    # "submits" and the input line resets.
                    tail = ""
                elif k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
                    tail = tail[:-1] if tail else ""
                elif k == KeyCode.KEYCODE_SPACE:
                    tail += " "
                elif k == KeyCode.KEYCODE_TAB:
                    tail += "\t"
            elif isinstance(k, int) and 32 <= k <= 126:
                ch = chr(k)
                shift, hid = _char_to_hid(ch)
                if hid:
                    mod = _MOD_LSHIFT if shift else 0
                    ble_kbd.send_report(mod, [hid])
                    time.sleep_ms(8)
                    ble_kbd.send_release()
                    tail += ch
            else:
                continue
            repaint()
        except Exception as e:
            Lcd.setFont(_SMALL)
            Lcd.setTextColor(_RED, _BG)
            Lcd.setCursor(_PAD, _HDR_H + 70)
            Lcd.print(_truncate(repr(e), 40))


_status_msg = None
_status_color = _OK
_status_until = 0


def _set_status(text, color):
    global _status_msg, _status_color, _status_until
    _status_msg = text
    _status_color = color
    _status_until = time.ticks_add(time.ticks_ms(), 1500)


def _refresh_status():
    global _status_msg
    if _status_msg is None:
        return False
    if time.ticks_diff(_status_until, time.ticks_ms()) > 0:
        Lcd.fillRect(0, _HDR_H + 1, Lcd.width(), 16, _BG)
        Lcd.setFont(_SMALL)
        Lcd.setTextColor(_status_color, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 4)
        Lcd.print(_truncate(_status_msg, 40))
        return True
    Lcd.fillRect(0, _HDR_H + 1, Lcd.width(), 16, _BG)
    _status_msg = None
    return True


def run():
    Lcd.clear(_BG)
    kb_in = MatrixKeyboard()
    _spk_off()  # BLE init touches I2C(1); make sure codec stays down

    _draw_header("BLE keyboard  starting...")
    _draw_hint("waiting for host to pair")

    try:
        ble_kbd = BLEKeyboard(name="Cardputer-Adv")
        ble_kbd.start_advertising()
    except Exception as e:
        _draw_header("BLE init error", color=_RED)
        Lcd.setFont(_SMALL)
        Lcd.setTextColor(_RED, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 6)
        Lcd.print(_truncate(repr(e), 40))
        while kb_in.get_key() != KeyCode.KEYCODE_ESC:
            time.sleep_ms(40)
        return

    idx = 0
    scroll = 0

    def header_text():
        return ("BLE: connected" if ble_kbd.is_connected()
                else "BLE: advertising 'Cardputer-Adv'")

    last_conn = ble_kbd.is_connected()
    _draw_list(idx, scroll, header_text())
    _draw_hint("Enter=send L=log ESC=quit")

    show_log = False
    last_log_len = 0

    while True:
        cur_conn = ble_kbd.is_connected()
        if cur_conn != last_conn:
            last_conn = cur_conn
            _draw_header(header_text(),
                         color=_OK if cur_conn else _BLUE)

        if show_log and len(ble_kbd.event_log) != last_log_len:
            last_log_len = len(ble_kbd.event_log)
            Lcd.fillRect(0, _HDR_H + 1, Lcd.width(),
                         Lcd.height() - _HDR_H - 1 - _HINT_H, _BG)
            Lcd.setFont(_SMALL)
            tail = ble_kbd.event_log[-7:]
            for i, (t, name, data) in enumerate(tail):
                Lcd.setTextColor(_FG, _BG)
                Lcd.setCursor(_PAD, _HDR_H + 2 + i * 14)
                Lcd.print(_truncate("{} {}".format(name, data), 38))

        if _refresh_status():
            pass

        k = kb_in.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if k == KeyCode.KEYCODE_ESC:
            ble_kbd.shutdown()  # full radio off, not just stop advertising
            return
        if isinstance(k, int) and (k == ord("l") or k == ord("L")):
            show_log = not show_log
            last_log_len = -1
            if not show_log:
                _draw_list(idx, scroll, header_text())
            continue
        if k == KeyCode.KEYCODE_UP:
            idx = (idx - 1) % len(_MACROS)
            if idx < scroll:
                scroll = idx
            if idx >= scroll + _VISIBLE:
                scroll = idx - _VISIBLE + 1
            _draw_list(idx, scroll, header_text())
        elif k == KeyCode.KEYCODE_DOWN:
            idx = (idx + 1) % len(_MACROS)
            if idx < scroll:
                scroll = idx
            if idx >= scroll + _VISIBLE:
                scroll = idx - _VISIBLE + 1
            _draw_list(idx, scroll, header_text())
        elif k == KeyCode.KEYCODE_ENTER:
            name, fn = _MACROS[idx]
            if fn is _LIVE_TYPING:
                if not ble_kbd.is_connected():
                    _set_status("not connected — pair first", _RED)
                    continue
                _live_typing(kb_in, ble_kbd)
                Lcd.clear(_BG)
                _draw_list(idx, scroll, header_text())
                _draw_hint("Enter=send L=log ESC=quit")
            else:
                if not ble_kbd.is_connected():
                    _set_status("not connected — pair first", _RED)
                    continue
                try:
                    fn(ble_kbd)
                    _set_status("sent: " + _truncate(name, 30), _OK)
                except Exception as e:
                    _set_status(_truncate(repr(e), 40), _RED)
