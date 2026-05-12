import gc
import struct
import time

import M5
from M5 import Lcd
from machine import I2C, Pin
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# --- BMI270 raw I/O ---------------------------------------------------------
_BMI270_ADDR = 0x69
_REG_CHIP_ID = 0x00
_REG_INTERNAL_STATUS = 0x21
_REG_ACC_X_LSB = 0x0C
_REG_ACC_RANGE = 0x41
_G = 9.80665
_LSB_PER_G = (16384, 8192, 4096, 2048)
_acc_scale = _G / 16384.0


def _bmi_init(i2c):
    global _acc_scale
    chip = i2c.readfrom_mem(_BMI270_ADDR, _REG_CHIP_ID, 1)[0]
    if chip != 0x24:
        raise OSError("BMI270 chip_id 0x{:02x}, expected 0x24".format(chip))
    status = i2c.readfrom_mem(_BMI270_ADDR, _REG_INTERNAL_STATUS, 1)[0]
    if (status & 0x01) == 0:
        from micropython_bmi270 import bmi270
        bmi270.BMI270(i2c, address=_BMI270_ADDR)
    rng = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_RANGE, 1)[0] & 0x03
    _acc_scale = _G / _LSB_PER_G[rng]


def _read_accel(i2c):
    raw = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_X_LSB, 6)
    ax, ay, az = struct.unpack("<hhh", raw)
    return ax * _acc_scale, ay * _acc_scale, az * _acc_scale


# --- UI ---------------------------------------------------------------------
_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_OK = 0x00DD66
_WAIT = 0xFFD040
_ERR = 0xFF4444
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_X_C = 0xFF6464
_Y_C = 0x64FF64
_Z_C = 0x64A0FF

_FONT = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12

_PAD = 6
_HEADER_H = 18
_ROW_H = 24
_DRAW_INTERVAL_MS = 50
_LOOP_SLEEP_MS = 10


def _draw_chrome():
    Lcd.clear(_BG)
    Lcd.fillRect(0, 0, Lcd.width(), _HEADER_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_HDR_FG, _HDR_BG)
    Lcd.setCursor(_PAD, 4)
    Lcd.print("IMU  (BMI270)")
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, Lcd.height() - 14)
    Lcd.print("ESC = back")


def _draw_status(state, msg):
    color = _OK if state == "ok" else (_WAIT if state == "wait" else _ERR)
    y = Lcd.height() // 2 - 8
    Lcd.fillRect(0, y, Lcd.width(), 18, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(msg[:36])


def _draw_row(idx, label, value, color):
    y = _HEADER_H + 6 + idx * _ROW_H
    Lcd.fillRect(0, y, Lcd.width() - 60, _ROW_H, _BG)
    Lcd.setFont(_FONT)
    Lcd.setTextColor(color, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(label)
    Lcd.setTextColor(_FG, _BG)
    txt = "{:6.2f}".format(value)
    tw = Lcd.textWidth(txt, _FONT)
    Lcd.setCursor(60 + (60 - tw), y + 2)
    Lcd.print(txt)


def _draw_tilt_box(ax, ay):
    box = 60
    bx = Lcd.width() - box - _PAD
    by = _HEADER_H + 8
    Lcd.drawRect(bx, by, box, box, _DIM)
    Lcd.fillRect(bx + 1, by + 1, box - 2, box - 2, _BG)
    Lcd.drawLine(bx + box // 2, by + 2, bx + box // 2, by + box - 3, 0x303040)
    Lcd.drawLine(bx + 2, by + box // 2, bx + box - 3, by + box // 2, 0x303040)
    G = 9.8
    rng = box // 2 - 4
    nx = max(-1.0, min(1.0, ax / G))
    ny = max(-1.0, min(1.0, -ay / G))
    cx = bx + box // 2 + int(nx * rng)
    cy = by + box // 2 + int(ny * rng)
    Lcd.fillCircle(cx, cy, 4, _OK)


def run():
    _draw_chrome()
    _draw_status("wait", "Initializing IMU...")

    gc.collect()
    # IMPORTANT: the keyboard chip lives on I2C peripheral 1 (sda=8, scl=9,
    # 400 kHz) — see m5stack/libs/hardware/matrix_keyboard.py in the M5
    # firmware. Using I2C(0) on the same physical pins reroutes the IO mux
    # away from peripheral 1 and breaks the keyboard. Sharing peripheral 1
    # lets the keyboard's IRQ handler keep working while we read the BMI270.
    i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
    try:
        _bmi_init(i2c)
    except Exception as e:
        _draw_status("err", "IMU: " + repr(e)[:30])
        kb = MatrixKeyboard()
        while True:
            if kb.get_key() == KeyCode.KEYCODE_ESC:
                return
            time.sleep_ms(50)

    # Clear the "wait" message
    Lcd.fillRect(0, Lcd.height() // 2 - 8, Lcd.width(), 18, _BG)

    kb = MatrixKeyboard()
    EMA_A = 0.4
    fax = fay = 0.0
    faz = 9.8
    last_draw = 0

    while True:
        # Drain queued keys; ESC wins.
        while True:
            k = kb.get_key()
            if k is None:
                break
            if k == KeyCode.KEYCODE_ESC:
                return

        now = time.ticks_ms()
        if time.ticks_diff(now, last_draw) >= _DRAW_INTERVAL_MS:
            try:
                ax, ay, az = _read_accel(i2c)
            except Exception:
                ax = ay = az = 0.0
            fax = EMA_A * ax + (1 - EMA_A) * fax
            fay = EMA_A * ay + (1 - EMA_A) * fay
            faz = EMA_A * az + (1 - EMA_A) * faz

            _draw_row(0, "X", fax, _X_C)
            _draw_row(1, "Y", fay, _Y_C)
            _draw_row(2, "Z", faz, _Z_C)
            _draw_tilt_box(fax, fay)
            last_draw = now

        time.sleep_ms(_LOOP_SLEEP_MS)
