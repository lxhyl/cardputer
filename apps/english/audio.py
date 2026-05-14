"""WAV playback for the english app.

Tries the M5 native `Speaker.playWavFile` first (if exposed in this
firmware build). Falls back to a manual I2S streamer using the *correct*
I2C peripheral (1) and pins for Cardputer-Adv — note that the shared
mp-scripts/wav_player.py is hard-coded to I2C(0), which would break the
TCA8418 matrix keyboard on this board.

Caller is responsible for `_spk_on()` before and `_spk_off()` after; this
module does NOT touch power state. Keeps things layered so app.py can do
one power-down at the end of a multi-key playback session.
"""

import struct
import time

import M5

_PIN_SCK = 41
_PIN_WS = 43
_PIN_SD = 42

_native_probed = False
_native_method = None  # callable(path) or None


def _probe_native():
    """Return a callable that plays a wav file via M5.Speaker, or None."""
    global _native_probed, _native_method
    if _native_probed:
        return _native_method
    _native_probed = True
    spk = M5.Speaker
    for name in ("playWavFile", "playWAVFile", "playWav", "playWAV"):
        fn = getattr(spk, name, None)
        if callable(fn):
            _native_method = fn
            return fn
    _native_method = None
    return None


def play(filepath, wait_ms_max=10000):
    """Play a WAV file. Returns True if started; blocks until playback ends
    (or wait_ms_max elapses)."""
    native = _probe_native()
    if native is not None:
        try:
            native(filepath)
        except TypeError:
            # Some bindings take (path, repeat) or similar; try plain path.
            try:
                native(filepath, 0)
            except Exception:
                return False
        except Exception:
            return False
        # Block until playback finishes.
        deadline = time.ticks_add(time.ticks_ms(), wait_ms_max)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                if not M5.Speaker.isPlaying():
                    return True
            except Exception:
                return True
            time.sleep_ms(20)
        return True
    return _play_manual(filepath)


def _play_manual(filepath):
    """Stream a 16-bit PCM WAV directly to the ES8311 codec via I2S.

    Uses I2C(1) for codec control (the bus the keyboard / IMU also live on
    — pins 8/9 are routed to peripheral 1 on this board). Caller has
    already powered the codec on via M5.Speaker.begin()."""
    from machine import I2C, Pin, I2S
    try:
        from es8311 import ES8311
    except ImportError:
        return False
    try:
        f = open(filepath, "rb")
    except OSError:
        return False
    try:
        riff = f.read(12)
        if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            return False
        sample_rate = bits = channels = None
        data_size = 0
        while True:
            head = f.read(8)
            if len(head) < 8:
                return False
            cid = head[:4]
            csz = struct.unpack("<I", head[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(csz)
                audio_fmt, channels, sample_rate, _br, _ba, bits = struct.unpack(
                    "<HHIIHH", fmt[:16])
                if audio_fmt != 1 or bits != 16:
                    return False
            elif cid == b"data":
                data_size = csz
                break
            else:
                f.seek(csz, 1)

        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
        codec = ES8311(i2c)
        codec.init(sample_rate=sample_rate, bits=16, mclk_from_pin=False)
        codec.set_volume(80)
        audio = I2S(0, sck=Pin(_PIN_SCK), ws=Pin(_PIN_WS), sd=Pin(_PIN_SD),
                    mode=I2S.TX, bits=16, format=I2S.STEREO,
                    rate=sample_rate, ibuf=16384)
        try:
            bytes_left = data_size
            chunk = 4096
            while bytes_left > 0:
                data = f.read(min(chunk, bytes_left))
                if not data:
                    break
                if channels == 1:
                    stereo = bytearray(len(data) * 2)
                    for i in range(0, len(data), 2):
                        stereo[i * 2] = data[i]
                        stereo[i * 2 + 1] = data[i + 1]
                        stereo[i * 2 + 2] = data[i]
                        stereo[i * 2 + 3] = data[i + 1]
                    audio.write(stereo)
                else:
                    audio.write(data)
                bytes_left -= len(data)
        finally:
            try:
                audio.deinit()
            except Exception:
                pass
        return True
    finally:
        f.close()
