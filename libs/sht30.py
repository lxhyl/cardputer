"""SHT30 temperature + humidity sensor (M5 ENV II/III), I2C 0x44."""
import time


class SHT30:
    def __init__(self, i2c, addr=0x44):
        self.i2c = i2c
        self.addr = addr

    @staticmethod
    def _crc(data):
        crc = 0xFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = ((crc << 1) ^ 0x131) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        return crc

    def read(self):
        """Returns (temperature_C, humidity_percent)."""
        # Single-shot, high repeatability, clock-stretching disabled: 0x2400
        self.i2c.writeto(self.addr, b'\x24\x00')
        time.sleep_ms(20)
        d = self.i2c.readfrom(self.addr, 6)
        if self._crc(d[0:2]) != d[2] or self._crc(d[3:5]) != d[5]:
            raise OSError("SHT30 CRC error")
        raw_t = (d[0] << 8) | d[1]
        raw_h = (d[3] << 8) | d[4]
        return -45 + 175 * raw_t / 65535, 100 * raw_h / 65535
