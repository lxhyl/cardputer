"""BLE HID mouse — tilt the Cardputer to move the cursor.

Same BLE pairing/bonding stack as `btmacro` (SMP encryption forced via
FLAG_READ_ENCRYPTED on the input report, proactive `gap_pair()` from
the connect IRQ, JustWorks Just Works pairing) — those are the bits
that make macOS / iOS actually pair. See btmacro/app.py + the BLE HID
section of CLAUDE.md for the why.

Cursor motion comes from the BMI270 accelerometer:
  - "Neutral" pose is captured on app start (or via 'C' to recalibrate)
  - Tilt forward / back  → cursor moves up / down
  - Tilt left / right    → cursor moves left / right

Keys (forwarded as mouse buttons / scroll, not keystrokes):
  Space   left click
  Enter   right click
  M       middle click
  W / S   scroll up / down
  C       recalibrate neutral pose to current orientation
  ESC     quit (BLE radio fully off on exit)
"""

import struct
import time

import M5
import bluetooth
from bluetooth import UUID
from M5 import Lcd
from machine import I2C, Pin
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# --- BLE flags (mirrors btmacro) ---
F_READ = bluetooth.FLAG_READ
F_WRITE = bluetooth.FLAG_WRITE
F_READ_NOTIFY = bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY
F_READ_WRITE_NORESPONSE = (bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
                           | bluetooth.FLAG_WRITE_NO_RESPONSE)
_FLAG_READ_ENCRYPTED = 0x0200
F_READ_NOTIFY_ENCRYPTED = F_READ_NOTIFY | _FLAG_READ_ENCRYPTED
DSC_F_READ = 0x02

_IRQ_CENTRAL_CONNECT     = 1
_IRQ_CENTRAL_DISCONNECT  = 2
_IRQ_GATTS_WRITE         = 3
_IRQ_MTU_EXCHANGED       = 21
_IRQ_CONNECTION_UPDATE   = 27
_IRQ_ENCRYPTION_UPDATE   = 28
_IRQ_GET_SECRET          = 29
_IRQ_SET_SECRET          = 30
_IRQ_PASSKEY_ACTION      = 31

_SECRETS_FILE = "/flash/ble_mouse_bonds.json"

_ADV_TYPE_FLAGS       = 0x01
_ADV_TYPE_NAME        = 0x09
_ADV_TYPE_UUID16_LIST = 0x03
_ADV_TYPE_APPEARANCE  = 0x19

_UUID_HID_SVC      = UUID(0x1812)
_UUID_DEVINFO_SVC  = UUID(0x180A)
_UUID_HID_INFO     = UUID(0x2A4A)
_UUID_REPORT_MAP   = UUID(0x2A4B)
_UUID_HID_CTRL_PT  = UUID(0x2A4C)
_UUID_REPORT       = UUID(0x2A4D)
_UUID_PROTOCOL_MD  = UUID(0x2A4E)
_UUID_REPORT_REF   = UUID(0x2908)
_UUID_PNP_ID       = UUID(0x2A50)
_UUID_MFR          = UUID(0x2A29)
_UUID_MODEL        = UUID(0x2A24)


# Standard HID mouse report descriptor — buttons (3 + 5 padding) + dx + dy + wheel.
# Report ID 1; 4 bytes total (button_byte, dx, dy, wheel) per notification.
_HID_REPORT_MAP = bytes([
    0x05, 0x01,  # Usage Page (Generic Desktop)
    0x09, 0x02,  # Usage (Mouse)
    0xA1, 0x01,  # Collection (Application)
    0x85, 0x01,  #   Report ID (1)
    0x09, 0x01,  #   Usage (Pointer)
    0xA1, 0x00,  #   Collection (Physical)
    0x05, 0x09,  #     Usage Page (Buttons)
    0x19, 0x01,  #     Usage Min (1)
    0x29, 0x03,  #     Usage Max (3) — left, right, middle
    0x15, 0x00,  #     Logical Min (0)
    0x25, 0x01,  #     Logical Max (1)
    0x95, 0x03,  #     Report Count (3)
    0x75, 0x01,  #     Report Size (1)
    0x81, 0x02,  #     Input (Data, Var, Abs)
    0x95, 0x01,  #     Report Count (1)
    0x75, 0x05,  #     Report Size (5)
    0x81, 0x03,  #     Input (Const) — button-byte padding
    0x05, 0x01,  #     Usage Page (Generic Desktop)
    0x09, 0x30,  #     Usage (X)
    0x09, 0x31,  #     Usage (Y)
    0x09, 0x38,  #     Usage (Wheel)
    0x15, 0x81,  #     Logical Min (-127)
    0x25, 0x7F,  #     Logical Max (127)
    0x75, 0x08,  #     Report Size (8)
    0x95, 0x03,  #     Report Count (3)
    0x81, 0x06,  #     Input (Data, Var, Rel) — relative motion
    0xC0,        #   End Collection
    0xC0,        # End Collection
])


_HID_INFO_VAL = b"\x01\x01\x00\x00"  # Heerkog default — known-good for macOS
_PNP_ID_VAL   = struct.pack(">BHHH", 0x02, 0x05AC, 0x820B, 0x0123)  # Apple-ish vendor


_DIS = (
    _UUID_DEVINFO_SVC,
    (
        (_UUID_MFR,    F_READ),
        (_UUID_MODEL,  F_READ),
        (_UUID_PNP_ID, F_READ),
    ),
)

_HIDS = (
    _UUID_HID_SVC,
    (
        (_UUID_HID_INFO,    F_READ),
        (_UUID_REPORT_MAP,  F_READ),
        (_UUID_HID_CTRL_PT, F_READ_WRITE_NORESPONSE),
        (_UUID_REPORT,      F_READ_NOTIFY_ENCRYPTED, (
            (_UUID_REPORT_REF, DSC_F_READ),
        )),
        (_UUID_PROTOCOL_MD, F_READ_WRITE_NORESPONSE),
    ),
)


def _adv_payload(name):
    p = bytearray()
    p += b"\x02\x01\x06"
    p += b"\x03\x19" + struct.pack("<H", 962)  # 962 = HID Mouse
    p += b"\x03\x03" + struct.pack("<H", 0x1812)
    n = name.encode()
    p += struct.pack("BB", 1 + len(n), 0x09) + n
    return bytes(p)


# --- BLE mouse server ---

class BLEMouse:
    def __init__(self, name="Cardputer-Mouse"):
        self._name = name
        self._conn = None
        self._secrets = self._load_secrets()

        self._ble = bluetooth.BLE()
        self._ble.irq(self._irq)
        # Same JustWorks bonding setup as btmacro — see CLAUDE.md.
        try:
            self._ble.config(bond=True)
            self._ble.config(le_secure=True)
            self._ble.config(mitm=False)
            self._ble.config(io=3)
        except Exception:
            pass
        self._ble.active(True)
        self._ble.config(gap_name=name)

        handles = self._ble.gatts_register_services((_DIS, _HIDS))
        (h_mfr, h_model, h_pnp) = handles[0]
        (h_info, h_hid, h_ctrl, self._h_rep, h_ref, h_proto) = handles[1]

        self._ble.gatts_write(h_mfr,   b"M5Stack")
        self._ble.gatts_write(h_model, b"Cardputer-Adv")
        self._ble.gatts_write(h_pnp,   _PNP_ID_VAL)
        self._ble.gatts_write(h_info,  _HID_INFO_VAL)
        self._ble.gatts_write(h_hid,   _HID_REPORT_MAP)
        self._ble.gatts_write(h_ctrl,  b"\x00")
        # Report Reference: report_id=1, type=Input(1)
        self._ble.gatts_write(h_ref,   struct.pack("<BB", 1, 1))
        self._ble.gatts_write(h_proto, b"\x01")
        self._ble.gatts_write(self._h_rep, b"\x00" * 4)

        self._adv_data = _adv_payload(name)

    def start_advertising(self):
        self._ble.gap_advertise(100_000, adv_data=self._adv_data)

    def shutdown(self):
        try: self._ble.gap_advertise(None)
        except Exception: pass
        try: self._ble.active(False)
        except Exception: pass

    def is_connected(self):
        return self._conn is not None

    # ---- bond persistence (same pattern as btmacro) ----
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
        if event == _IRQ_CENTRAL_CONNECT:
            self._conn, _, _ = data
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
                i = 0
                for (t, _k), v in self._secrets.items():
                    if t == sec_type:
                        if i == index:
                            return v
                        i += 1
                return None
            return self._secrets.get((sec_type, bytes(key)), None)

    def send_report(self, buttons, dx, dy, wheel):
        if self._conn is None:
            return False
        # Clamp dx/dy/wheel to int8 range; pack as signed bytes.
        def _s8(v):
            v = int(v)
            if v > 127: v = 127
            if v < -127: v = -127
            return v & 0xFF
        rep = bytes((buttons & 0x07, _s8(dx), _s8(dy), _s8(wheel)))
        try:
            self._ble.gatts_write(self._h_rep, rep)
            self._ble.gatts_notify(self._conn, self._h_rep)
            return True
        except OSError:
            return False


# --- BMI270 raw I/O (mirrors apps/sensor/imu/app.py) ---
_BMI270_ADDR = 0x69
_REG_CHIP_ID = 0x00
_REG_INTERNAL_STATUS = 0x21
_REG_ACC_X_LSB = 0x0C
_REG_ACC_RANGE = 0x41
_G = 9.80665
_LSB_PER_G = (16384, 8192, 4096, 2048)


def _bmi_init(i2c):
    chip = i2c.readfrom_mem(_BMI270_ADDR, _REG_CHIP_ID, 1)[0]
    if chip != 0x24:
        raise OSError("BMI270 chip_id 0x{:02x}, expected 0x24".format(chip))
    status = i2c.readfrom_mem(_BMI270_ADDR, _REG_INTERNAL_STATUS, 1)[0]
    if (status & 0x01) == 0:
        from micropython_bmi270 import bmi270
        bmi270.BMI270(i2c, address=_BMI270_ADDR)
    rng = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_RANGE, 1)[0] & 0x03
    return _G / _LSB_PER_G[rng]


def _read_accel(i2c, scale):
    raw = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_X_LSB, 6)
    ax, ay, az = struct.unpack("<hhh", raw)
    return ax * scale, ay * scale, az * scale


# --- Tilt → cursor delta mapping ---
# Deadzone in m/s² — tilts smaller than this don't move the cursor at all.
_DEADZONE = 0.6
# How aggressive the cursor moves per "g" of tilt past the deadzone.
# Tuned for: gentle tilt = slow drift, hard tilt = fast sweep.
_GAIN = 9.0
# Maximum dx/dy per report (the HID spec caps at 127 anyway).
_MAX_STEP = 30


def _tilt_to_delta(ax, ay, neutral_ax, neutral_ay):
    """Map (ax, ay) accelerometer reading minus neutral pose to (dx, dy).
    Cardputer is held flat with screen up; tilting RIGHT lifts left edge,
    pushing accel.x positive. Tilting AWAY (top edge down) pushes accel.y
    negative. Match cursor coordinates: +X = right, +Y = down."""
    rx = ax - neutral_ax
    ry = ay - neutral_ay

    def _project(v):
        # Symmetric deadzone, then linear.
        if abs(v) < _DEADZONE:
            return 0
        v = v - _DEADZONE if v > 0 else v + _DEADZONE
        return v

    dx = int(_project(rx) * _GAIN)
    dy = int(-_project(ry) * _GAIN)  # invert Y so tilt-away moves cursor up

    if dx > _MAX_STEP: dx = _MAX_STEP
    if dx < -_MAX_STEP: dx = -_MAX_STEP
    if dy > _MAX_STEP: dy = _MAX_STEP
    if dy < -_MAX_STEP: dy = -_MAX_STEP
    return dx, dy


# --- UI ---
_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_OK = 0x00DD66
_BLUE = 0x40A0FF
_RED = 0xFF6060

_FONT = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12
_PAD = 6
_HDR_H = 22
_HINT_H = 16


def _draw_header(text, color=_HDR_FG):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print(text[:38])


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(text)


def _draw_body(neutral, ax, ay, dx, dy, buttons):
    Lcd.fillRect(0, _HDR_H + 1, Lcd.width(),
                 Lcd.height() - _HDR_H - 1 - _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_BLUE, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 4)
    Lcd.print("tilt  ax {:+.2f}  ay {:+.2f}".format(ax, ay))
    Lcd.setCursor(_PAD, _HDR_H + 18)
    Lcd.print("zero  ax {:+.2f}  ay {:+.2f}".format(*neutral))
    Lcd.setTextColor(_OK if (dx or dy) else _DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 32)
    Lcd.print("delta dx {:+3d}  dy {:+3d}".format(dx, dy))
    # Button indicators
    Lcd.setFont(_FONT)
    Lcd.setTextColor(_RED if buttons & 1 else _DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 50)
    Lcd.print("L")
    Lcd.setTextColor(_RED if buttons & 4 else _DIM, _BG)
    Lcd.setCursor(_PAD + 22, _HDR_H + 50)
    Lcd.print("M")
    Lcd.setTextColor(_RED if buttons & 2 else _DIM, _BG)
    Lcd.setCursor(_PAD + 44, _HDR_H + 50)
    Lcd.print("R")


def run():
    Lcd.clear(_BG)
    kb = MatrixKeyboard()

    # Boot the IMU before BLE so a sensor failure shows a clean message.
    _draw_header("BLE Mouse  init IMU...")
    _draw_hint("BMI270 wakeup ~10ms config blob")
    try:
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
        scale = _bmi_init(i2c)
    except Exception as e:
        _draw_header("IMU init failed", color=_RED)
        Lcd.setFont(_SMALL); Lcd.setTextColor(_RED, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 6); Lcd.print(repr(e)[:38])
        while kb.get_key() != KeyCode.KEYCODE_ESC:
            time.sleep_ms(40)
        return

    _draw_header("BLE Mouse  starting BLE...")
    try:
        mouse = BLEMouse("Cardputer-Mouse")
        mouse.start_advertising()
    except Exception as e:
        _draw_header("BLE init failed", color=_RED)
        Lcd.setFont(_SMALL); Lcd.setTextColor(_RED, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 6); Lcd.print(repr(e)[:38])
        while kb.get_key() != KeyCode.KEYCODE_ESC:
            time.sleep_ms(40)
        return

    _draw_hint("Spc=L Ent=R M=mid W/S=scroll C=cal")

    # Average the first ~10 reads as the neutral pose. The user can recal
    # any time with 'C' to redefine "level".
    sx = sy = 0.0
    for _ in range(10):
        ax, ay, _az = _read_accel(i2c, scale)
        sx += ax; sy += ay
        time.sleep_ms(20)
    neutral = (sx / 10, sy / 10)

    last_conn = mouse.is_connected()
    last_draw = 0
    buttons = 0
    # Send-rate cap: ~33 Hz (BLE GATT_NOTIFY can do faster but Mac driver
    # gets choppy if we flood it; 30 Hz is typical for BLE mice).
    next_send = 0
    SEND_INTERVAL_MS = 30

    while True:
        # Update header on connect-state change
        cur_conn = mouse.is_connected()
        if cur_conn != last_conn:
            last_conn = cur_conn
            if cur_conn:
                _draw_header("BLE Mouse  CONNECTED", color=_OK)
            else:
                _draw_header("BLE Mouse  advertising 'Cardputer-Mouse'",
                             color=_BLUE)

        # Handle keys (clicks / scroll / quit / recalibrate)
        scroll = 0
        wheel_step = 1
        click_event = False  # whether button state changed this iter
        k = kb.get_key()
        if k is not None:
            click_event = True
            if k == KeyCode.KEYCODE_ESC:
                mouse.shutdown()
                return
            elif k == KeyCode.KEYCODE_SPACE:
                buttons ^= 0x01  # toggle left
            elif k == KeyCode.KEYCODE_ENTER:
                buttons ^= 0x02  # toggle right
            elif isinstance(k, int) and (k == ord("m") or k == ord("M")):
                buttons ^= 0x04  # toggle middle
            elif isinstance(k, int) and (k == ord("c") or k == ord("C")):
                # Recalibrate: take 5 quick reads as new neutral
                sx = sy = 0.0
                for _ in range(5):
                    ax, ay, _ = _read_accel(i2c, scale)
                    sx += ax; sy += ay
                    time.sleep_ms(10)
                neutral = (sx / 5, sy / 5)
                click_event = False
            elif isinstance(k, int) and (k == ord("w") or k == ord("W")):
                scroll = wheel_step
                click_event = False
            elif isinstance(k, int) and (k == ord("s") or k == ord("S")):
                scroll = -wheel_step
                click_event = False
            else:
                click_event = False

        # Read tilt + map to cursor delta
        try:
            ax, ay, _az = _read_accel(i2c, scale)
        except Exception:
            ax = ay = 0.0
        dx, dy = _tilt_to_delta(ax, ay, neutral[0], neutral[1])

        # Send a notification if it's time AND we have meaningful change
        now = time.ticks_ms()
        if cur_conn and time.ticks_diff(now, next_send) >= 0:
            if dx or dy or scroll or click_event:
                mouse.send_report(buttons, dx, dy, scroll)
                next_send = time.ticks_add(now, SEND_INTERVAL_MS)
            elif click_event is False and (buttons & 0x07):
                # Button held but no motion — keep state alive infrequently
                pass

        # UI refresh ~10Hz
        if time.ticks_diff(now, last_draw) > 100:
            _draw_body(neutral, ax, ay, dx, dy, buttons)
            last_draw = now

        time.sleep_ms(10)
