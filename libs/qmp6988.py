"""QMP6988 barometric pressure sensor (M5 ENV III), I2C 0x70.

Fixed-point math ported from M5Stack M5Unit-ENV C driver.
Output: temperature in °C, pressure in Pa.
"""
import time


def _s16(hi, lo):
    v = (hi << 8) | lo
    return v - 0x10000 if v >= 0x8000 else v


def _s20(hi, mid, nibble):
    v = (hi << 12) | (mid << 4) | (nibble & 0x0F)
    return v - 0x100000 if v >= 0x80000 else v


class QMP6988:
    def __init__(self, i2c, addr=0x70):
        self.i2c = i2c
        self.addr = addr
        cid = i2c.readfrom_mem(addr, 0xD1, 1)[0]
        if cid != 0x5C:
            raise OSError("QMP6988 wrong chip id 0x{:02x}".format(cid))
        # Soft reset
        i2c.writeto_mem(addr, 0xE0, b'\xE6')
        time.sleep_ms(20)
        self._read_calib()
        i2c.writeto_mem(addr, 0xF1, b'\x00')   # IIR off
        # ctrl = temp x4 (010<<5=0x40) | press x16 (101<<2=0x14) | normal mode (11) = 0x57
        i2c.writeto_mem(addr, 0xF4, b'\x57')
        time.sleep_ms(50)

    def _read_calib(self):
        a = self.i2c.readfrom_mem(self.addr, 0xA0, 25)
        a0  = _s20(a[18], a[19], a[24] & 0x0F)
        b00 = _s20(a[0],  a[1],  (a[24] & 0xF0) >> 4)
        a1  = _s16(a[20], a[21])
        a2  = _s16(a[22], a[23])
        bt1 = _s16(a[2],  a[3])
        bt2 = _s16(a[4],  a[5])
        bp1 = _s16(a[6],  a[7])
        b11 = _s16(a[8],  a[9])
        bp2 = _s16(a[10], a[11])
        b12 = _s16(a[12], a[13])
        b21 = _s16(a[14], a[15])
        bp3 = _s16(a[16], a[17])

        # Scale to internal fixed-point (Q-formats from M5 driver)
        self.a0  = a0
        self.b00 = b00
        self.a1  = 3608   * a1  - 1731677965
        self.a2  = 16889  * a2  - 87619360
        self.bt1 = 2982   * bt1 + 107370906
        self.bt2 = 329854 * bt2 + 108083093
        self.bp1 = 19923  * bp1 + 1133836764
        self.b11 = 2406   * b11 + 118215883
        self.bp2 = 3079   * bp2 - 181579595
        self.b12 = 6846   * b12 + 85590281
        self.b21 = 13836  * b21 + 79333336
        self.bp3 = 2915   * bp3 + 157155561

    def _conv_t(self, dt):
        wk1 = self.a1 * dt
        wk2 = ((self.a2 * dt) >> 14) * dt >> 10
        wk2 = ((wk1 + wk2) // 32767) >> 19
        return (self.a0 + wk2) >> 4    # 17Q0 — units are °C * 256 (see read())

    def _conv_p(self, dp, tx):
        wk1 = self.bt1 * tx
        wk2 = (self.bp1 * dp) >> 5
        wk1 += wk2

        wk2 = ((self.bt2 * tx) >> 1) * tx >> 8
        wk3 = wk2
        wk2 = (((self.b11 * tx) >> 4) * dp) >> 1
        wk3 += wk2
        wk2 = (((self.bp2 * dp) >> 13) * dp) >> 1
        wk3 += wk2
        wk1 += wk3 >> 14

        wk2 = ((self.b12 * tx) * tx >> 22) * dp >> 1
        wk3 = wk2
        wk2 = (((self.b21 * tx) >> 6) * dp >> 23) * dp >> 1
        wk3 += wk2
        wk2 = (((self.bp3 * dp) >> 12) * dp >> 23) * dp
        wk3 += wk2
        wk1 += wk3 >> 15

        wk1 //= 32767
        wk1 >>= 11
        wk1 += self.b00
        return wk1

    def read(self):
        """Returns (temperature_C, pressure_Pa)."""
        d = self.i2c.readfrom_mem(self.addr, 0xF7, 6)
        raw_p = ((d[0] << 16) | (d[1] << 8) | d[2]) - 0x800000
        raw_t = ((d[3] << 16) | (d[4] << 8) | d[5]) - 0x800000
        tx = self._conv_t(raw_t)
        p_int = self._conv_p(raw_p, tx)
        return tx / 256.0, p_int / 16.0
