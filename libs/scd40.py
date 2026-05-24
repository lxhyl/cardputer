"""Sensirion SCD40 CO2 + T + RH sensor driver (I2C).

Constants and timing are taken verbatim from Sensirion's own driver
`embedded-i2c-scd4x` (scd4x_i2c.c, model 2.0). The CRC parameters are
Sensirion's standard: poly 0x31, init 0xFF, no reflection. In periodic
mode the sensor updates every 5 s, so a poller must check
`data_ready()` instead of blindly reading.
"""

import time


class SCD40:
    ADDR = 0x62

    _CMD_START_PERIODIC = 0x21B1
    _CMD_STOP_PERIODIC = 0x3F86  # datasheet: 500 ms before next cmd
    _CMD_READ_MEASUREMENT = 0xEC05
    _CMD_DATA_READY = 0xE4B8
    _CMD_GET_SERIAL = 0x3682
    _CMD_REINIT = 0x3646  # 30 ms

    def __init__(self, i2c, addr=ADDR):
        self.i2c = i2c
        self.addr = addr
        # Sensor may already be mid-periodic from a previous run that
        # didn't exit cleanly. Stop it so subsequent commands ACK.
        try:
            self._send(self._CMD_STOP_PERIODIC)
            time.sleep_ms(500)
        except OSError:
            pass

    # --- low level ---

    @staticmethod
    def _crc8(b0, b1):
        c = 0xFF
        for b in (b0, b1):
            c ^= b
            for _ in range(8):
                c = ((c << 1) ^ 0x31) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        return c

    def _send(self, cmd):
        self.i2c.writeto(self.addr, bytes((cmd >> 8, cmd & 0xFF)))

    def _read_words(self, n):
        raw = self.i2c.readfrom(self.addr, n * 3)
        out = []
        for i in range(n):
            b0, b1, crc = raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]
            if self._crc8(b0, b1) != crc:
                raise OSError("SCD40 CRC")
            out.append((b0 << 8) | b1)
        return out

    # --- public ---

    def start_periodic(self):
        self._send(self._CMD_START_PERIODIC)

    def stop_periodic(self):
        self._send(self._CMD_STOP_PERIODIC)
        time.sleep_ms(500)

    def data_ready(self):
        self._send(self._CMD_DATA_READY)
        time.sleep_ms(1)
        # Bits[10:0] zero → no fresh sample yet.
        return (self._read_words(1)[0] & 0x07FF) != 0

    def read(self):
        """Return (co2_ppm:int, temp_c:float, rh_pct:float). Caller
        should gate on data_ready()."""
        self._send(self._CMD_READ_MEASUREMENT)
        time.sleep_ms(1)
        co2, t_raw, rh_raw = self._read_words(3)
        t = -45.0 + 175.0 * t_raw / 65535.0
        rh = 100.0 * rh_raw / 65535.0
        return co2, t, rh

    def serial(self):
        self._send(self._CMD_GET_SERIAL)
        time.sleep_ms(1)
        w0, w1, w2 = self._read_words(3)
        return (w0 << 32) | (w1 << 16) | w2
