"""Morse code beacon / listener — flash, audio TX, audio RX.

Modes (cycle with ← / →):
  - Flash TX  : fullscreen LCD flashes (white = mark)  — decoded by camera
  - Audio TX  : 700 Hz sidetone via built-in speaker   — decoded by mic
  - Audio RX  : live decoder using built-in microphone

(Cardputer-Adv Tab key types '+' rather than reporting KEYCODE_TAB,
 so we use the arrows for mode + WPM.)

ITU-R M.1677-1 PARIS timing:
  unit (U) = 1200 / WPM ms
  dot=1U on, dash=3U on, intra-char=1U off, inter-char=3U off, inter-word=7U off
"""

import time

import M5
import micropython
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# --- ITU international Morse table (letters, digits, common punct) ---
_MORSE = {
    "A": ".-",     "B": "-...",   "C": "-.-.",   "D": "-..",
    "E": ".",      "F": "..-.",   "G": "--.",    "H": "....",
    "I": "..",     "J": ".---",   "K": "-.-",    "L": ".-..",
    "M": "--",     "N": "-.",     "O": "---",    "P": ".--.",
    "Q": "--.-",   "R": ".-.",    "S": "...",    "T": "-",
    "U": "..-",    "V": "...-",   "W": ".--",    "X": "-..-",
    "Y": "-.--",   "Z": "--..",
    "0": "-----",  "1": ".----",  "2": "..---",  "3": "...--",
    "4": "....-",  "5": ".....",  "6": "-....",  "7": "--...",
    "8": "---..",  "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "'": ".----.",
    "!": "-.-.--", "/": "-..-.",  "(": "-.--.",  ")": "-.--.-",
    "&": ".-...",  ":": "---...", ";": "-.-.-.", "=": "-...-",
    "+": ".-.-.",  "-": "-....-", "_": "..--.-", "\"": ".-..-.",
    "$": "...-..-", "@": ".--.-.",
}
_REV = {v: k for k, v in _MORSE.items()}

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_OK = 0x00DD66
_ACCENT = 0x40A0FF
_RED = 0xFF6060

_FONT = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12
_PAD = 6
_HDR_H = 22
_HINT_H = 16

_DEFAULT_WPM = 10
_MIN_WPM = 5
_MAX_WPM = 25
_MAX_LEN = 80

# --- TX audio ---
_TX_FREQ = 700  # Hz, standard CW sidetone
_TX_VOL = 240   # 0-255; need this loud so a laptop mic across the room
                # can pick it up (1W speaker, but small driver = low SPL)

# --- RX audio ---
_RX_RATE = 8000
_RX_WIN_MS = 40                                  # sample window (40 ms = 320 samples)
_RX_SAMPLES = _RX_RATE * _RX_WIN_MS // 1000
_RX_HISTORY_MS = 3500                            # adaptive-threshold window

# Modes
_M_FLASH = 0
_M_AUDIO = 1
_M_LISTEN = 2
_M_NAMES = ("Flash TX", "Audio TX", "Audio RX")


# ---------------- core helpers ----------------

def _unit_ms(wpm):
    return 1200 // wpm


def _encode(text):
    """Yield ('on'|'off', units) timing events."""
    text = text.upper()
    word_break = False
    first = True
    for ch in text:
        if ch == " ":
            word_break = True
            continue
        code = _MORSE.get(ch)
        if code is None:
            continue
        if not first:
            yield ("off", 7 if word_break else 3)
        word_break = False
        first = False
        for i, sym in enumerate(code):
            if i > 0:
                yield ("off", 1)
            yield ("on", 1 if sym == "." else 3)


def _morse_preview(text):
    parts = []
    for word in text.upper().split(" "):
        wp = []
        for ch in word:
            c = _MORSE.get(ch)
            if c:
                wp.append(c)
        if wp:
            parts.append(" ".join(wp))
    return " / ".join(parts)


# ---------------- viper RMS-style energy ----------------
# Sum of |sample| over n signed-16-bit LE samples in p, divided by n.
@micropython.viper
def _mean_abs16(p: ptr8, n: int) -> int:  # noqa: F821
    total: int = 0
    i: int = 0
    while i < n:
        lo: int = int(p[i * 2])
        hi: int = int(p[i * 2 + 1])
        s: int = (hi << 8) | lo
        if s & 0x8000:
            s = s - 0x10000
        if s < 0:
            s = -s
        total += s
        i += 1
    if n == 0:
        return 0
    return total // n


# ---------------- drawing ----------------

def _draw_header(text, color=_HDR_FG):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print(text)


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(text)


def _fit_left(s, font, max_w):
    while s and Lcd.textWidth(s, font) > max_w:
        s = s[1:]
    return s


def _draw_compose(buf, wpm, mode, status=None):
    Lcd.fillRect(0, _HDR_H, Lcd.width(),
                 Lcd.height() - _HDR_H - _HINT_H, _BG)
    if status:
        _draw_header(status, color=_OK)
    else:
        _draw_header("{}  {} WPM".format(_M_NAMES[mode], wpm))
    _draw_hint("Enter=send <>=mode ^v=WPM ESC=quit")

    avail = Lcd.width() - 2 * _PAD

    Lcd.setFont(_FONT)
    Lcd.setTextColor(_FG, _BG)
    shown = _fit_left((buf if buf else "") + "_", _FONT, avail)
    Lcd.setCursor(_PAD, _HDR_H + 6)
    Lcd.print(shown)

    code = _morse_preview(buf)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_ACCENT, _BG)
    code_shown = _fit_left(code, _SMALL, avail)
    Lcd.setCursor(_PAD, _HDR_H + 36)
    Lcd.print(code_shown)

    units = 0
    chars = 0
    for ev, n in _encode(buf):
        units += n
    for c in buf:
        if c == " " or c.upper() in _MORSE:
            chars += 1
    secs = units * _unit_ms(wpm) / 1000.0
    info = "{} chars  ~{:.1f}s".format(chars, secs)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 60)
    Lcd.print(info)


# ---------------- TX ----------------

def _flash(state):
    color = _FG if state == "on" else _BG
    Lcd.fillRect(0, 0, Lcd.width(), Lcd.height(), color)


def _wait_with_esc(ms, kb):
    """Sleep ms, polling for ESC. Returns False if ESC pressed."""
    end = time.ticks_add(time.ticks_ms(), ms)
    while True:
        remaining = time.ticks_diff(end, time.ticks_ms())
        if remaining <= 0:
            return True
        if kb.get_key() == KeyCode.KEYCODE_ESC:
            return False
        time.sleep_ms(20 if remaining > 25 else remaining)


def _transmit_flash(buf, wpm, kb):
    u = _unit_ms(wpm)
    _flash("off")
    if not _wait_with_esc(500, kb):
        return False
    for ev, n in _encode(buf):
        if kb.get_key() == KeyCode.KEYCODE_ESC:
            _flash("off")
            return False
        _flash(ev)
        if not _wait_with_esc(u * n, kb):
            _flash("off")
            return False
    _flash("off")
    time.sleep_ms(200)
    return True


_es8311_i2c = None
def _es8311():
    global _es8311_i2c
    if _es8311_i2c is None:
        from machine import I2C, Pin
        # Same I2C(1) bus the keyboard / IMU sit on — 0x18 is the codec.
        _es8311_i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400000)
    return _es8311_i2c


def _spk_off():
    """Cleanly power down ES8311 after Audio TX.

    Values from Espressif's ESP-ADF ES8311 driver — 0x12=0x02 (just the
    PDN_DAC bit), NOT 0xFF (which leaves bias generators at MAX and the
    DAC output stage drifting, making the amp hiss and pick up RF
    coupling from any active radio nearby like WiFi scans).
    """
    try: M5.Speaker.stop()
    except Exception: pass
    try: M5.Speaker.end()
    except Exception: pass
    try:
        i2c = _es8311()
        for reg, val in (
            (0x32, 0x00),  # DAC_VOLUME = mute
            (0x12, 0x02),  # SYSTEM12 = PDN_DAC
            (0x13, 0x10),  # SYSTEM13 = ESP-ADF default
            (0x0E, 0xFF),  # SYSTEM0E = close ADC modulator
            (0x14, 0x00),  # ADC14 = disable PGA
            (0x0D, 0xFA),  # SYSTEM0D = power down analog
            (0x37, 0x08),  # DAC37 = bypass equalizer
            (0x00, 0x00),  # CSM off
        ):
            i2c.writeto_mem(0x18, reg, bytes([val]))
    except Exception:
        pass


def _transmit_audio(buf, wpm, kb):
    u = _unit_ms(wpm)
    try:
        M5.Speaker.begin()
        M5.Speaker.setPA(True)
        M5.Speaker.setVolume(_TX_VOL)
    except Exception:
        pass

    if not _wait_with_esc(500, kb):
        _spk_off()
        return False

    for ev, n in _encode(buf):
        if kb.get_key() == KeyCode.KEYCODE_ESC:
            _spk_off()
            return False
        ms = u * n
        if ev == "on":
            try: M5.Speaker.tone(_TX_FREQ, ms)
            except Exception: pass
        if not _wait_with_esc(ms, kb):
            _spk_off()
            return False

    # Wait for the last queued tone to finish before cutting the PA, otherwise
    # the final dit/dah gets clipped or pops.
    spin_end = time.ticks_add(time.ticks_ms(), 200)
    while time.ticks_diff(spin_end, time.ticks_ms()) > 0:
        try:
            if not M5.Speaker.isPlaying():
                break
        except Exception:
            break
        time.sleep_ms(5)
    _spk_off()
    return True


# ---------------- RX (audio listen) ----------------

class _Decoder:
    """Audio-Morse FSM. Mirrors decoder.html — feed energy + timestamps,
    let it figure out unit_ms and emit decoded chars into .text."""

    def __init__(self):
        self.history = []      # [(t_ms, energy)]
        self.on_pulses = []
        self.cur_state = "off"
        self.state_start = time.ticks_ms()
        self.unit_ms = None
        self.buf = ""          # current letter
        self.text = ""         # decoded
        self.flushed = True
        self.last_thr_span = 0

    def reset(self):
        now = time.ticks_ms()
        self.history = []
        self.on_pulses = []
        self.cur_state = "off"
        self.state_start = now
        self.unit_ms = None
        self.buf = ""
        self.text = ""
        self.flushed = True
        self.last_thr_span = 0

    def _adaptive(self, now):
        cutoff = time.ticks_add(now, -_RX_HISTORY_MS)
        mn = None
        mx = 0
        for t, e in self.history:
            if time.ticks_diff(t, cutoff) >= 0:
                if mn is None or e < mn:
                    mn = e
                if e > mx:
                    mx = e
        if mn is None:
            return None
        span = mx - mn
        self.last_thr_span = span
        # Need real signal: span has to clear noise floor by a margin.
        # 80 covers roughly the codec self-noise floor; the *0.20 ratio
        # protects against constant loud noise (kitchen fan, etc.) where
        # span/mx is small.
        if span < 80 or span < mx * 0.18:
            return None
        mid = (mn + mx) / 2
        hyst = span * 0.12
        return mid, hyst

    def _estimate_unit(self):
        if not self.on_pulses:
            return None
        s = sorted(self.on_pulses)
        mn = s[0]
        mx = s[-1]
        if mx / max(mn, 1) < 2.0:
            # single cluster — assume dots so the 2U threshold below catches
            # future dashes
            return sum(s) / len(s)
        split = (mn + mx) / 2
        dots = [d for d in s if d <= split]
        if not dots:
            dots = s
        return sum(dots) / len(dots)

    def _decode_buf(self):
        if not self.buf:
            return
        self.text += _REV.get(self.buf, "?")
        self.buf = ""
        if len(self.text) > 160:
            self.text = self.text[-160:]

    def _pulse_ended(self, state, dur):
        if not self.unit_ms:
            return
        if state == "on":
            sym = "." if dur < 2 * self.unit_ms else "-"
            self.buf += sym
        else:
            if dur < 2 * self.unit_ms:
                pass  # intra-character
            elif dur < 5 * self.unit_ms:
                self._decode_buf()
            else:
                self._decode_buf()
                if self.text and not self.text.endswith(" "):
                    self.text += " "

    def feed(self, energy, now):
        self.history.append((now, energy))
        cutoff = time.ticks_add(now, -(_RX_HISTORY_MS + 500))
        # drop expired samples (history is mostly small — linear is fine)
        i = 0
        for t, _ in self.history:
            if time.ticks_diff(t, cutoff) >= 0:
                break
            i += 1
        if i:
            self.history = self.history[i:]

        thr = self._adaptive(now)
        if not thr:
            return
        mid, hyst = thr
        nxt = self.cur_state
        if self.cur_state == "off" and energy > mid + hyst:
            nxt = "on"
        elif self.cur_state == "on" and energy < mid - hyst:
            nxt = "off"

        if nxt != self.cur_state:
            dur = time.ticks_diff(now, self.state_start)
            # Discard the very first OFF (could be hours long pre-startup)
            seed_idle = (self.cur_state == "off"
                         and self.unit_ms is None
                         and not self.on_pulses)
            if not seed_idle:
                if self.cur_state == "on":
                    self.on_pulses.append(dur)
                    if len(self.on_pulses) > 24:
                        self.on_pulses.pop(0)
                    self.unit_ms = self._estimate_unit()
                self._pulse_ended(self.cur_state, dur)
            self.cur_state = nxt
            self.state_start = now
            self.flushed = False
        elif self.cur_state == "off" and self.unit_ms and not self.flushed:
            # Long silence after a letter — flush; the transmitter never
            # emits an inter-char gap after the final character.
            idle = time.ticks_diff(now, self.state_start)
            if idle > 5 * self.unit_ms and self.buf:
                self._decode_buf()
                self.flushed = True
            elif idle > 9 * self.unit_ms:
                self.flushed = True


def _draw_listen_static():
    Lcd.fillRect(0, _HDR_H, Lcd.width(),
                 Lcd.height() - _HDR_H - _HINT_H, _BG)
    _draw_hint("<>=mode  R=reset  ESC=quit")


def _draw_listen_dyn(dec, mic_dead):
    avail = Lcd.width() - 2 * _PAD
    body_y = _HDR_H

    if mic_dead:
        _draw_header("MIC FIRMWARE BUG", color=_RED)
    else:
        title = "Audio RX"
        if dec.unit_ms:
            title += "  ~{:.1f} WPM".format(1200.0 / dec.unit_ms)
        if dec.last_thr_span:
            title += "  span {}".format(int(dec.last_thr_span))
        _draw_header(title)

    # Wipe body area (but keep hint)
    Lcd.fillRect(0, body_y, Lcd.width(),
                 Lcd.height() - _HDR_H - _HINT_H, _BG)

    Lcd.setFont(_FONT)
    Lcd.setTextColor(_OK, _BG)
    Lcd.setCursor(_PAD, body_y + 6)
    Lcd.print(_fit_left(dec.text or "", _FONT, avail))

    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_ACCENT, _BG)
    Lcd.setCursor(_PAD, body_y + 36)
    Lcd.print("~ " + (dec.buf or "_"))

    Lcd.setFont(_SMALL)
    if mic_dead:
        Lcd.setTextColor(_RED, _BG)
        status = "mic returns flat DC; use web TX→web RX instead"
    elif dec.cur_state == "on":
        Lcd.setTextColor(_OK, _BG)
        status = "TONE"
    elif dec.unit_ms:
        Lcd.setTextColor(_DIM, _BG)
        status = "listening"
    else:
        Lcd.setTextColor(_DIM, _BG)
        status = "calibrating"
    Lcd.setCursor(_PAD, body_y + 60)
    Lcd.print(status)


def _listen(kb):
    """Live mic decoder. Returns 'quit' for ESC, 'next'/'prev' for arrows."""
    dec = _Decoder()
    _draw_listen_static()
    _draw_header("Audio RX  starting mic...")
    # Speaker amp shares the codec — kill it completely, otherwise the
    # NS4150B hisses (no signal but PA enabled) and bleeds into the mic.
    _spk_off()
    try:
        M5.Mic.begin()
    except Exception:
        _draw_header("mic init failed", color=_RED)
        _wait_with_esc(1500, kb)
        return "next"

    rec_buf = bytearray(_RX_SAMPLES * 2)
    last_draw = 0
    # Stuck-mic detection: if every window's energy is the same DC value
    # for >3s, the firmware mic path is broken and we tell the user.
    started_at = time.ticks_ms()
    last_energy = -1
    stale_since = started_at
    mic_dead = False

    try:
        while True:
            k = kb.get_key()
            if k == KeyCode.KEYCODE_ESC:
                return "quit"
            if k == KeyCode.KEYCODE_LEFT:
                return "prev"
            if k == KeyCode.KEYCODE_RIGHT:
                return "next"
            if isinstance(k, int) and (k == ord('r') or k == ord('R')):
                dec.reset()
                last_draw = 0
                mic_dead = False
                stale_since = time.ticks_ms()

            try:
                M5.Mic.record(rec_buf, _RX_RATE, False)
            except Exception:
                time.sleep_ms(50)
                continue

            deadline = time.ticks_add(time.ticks_ms(), _RX_WIN_MS + 60)
            while True:
                try:
                    busy = M5.Mic.isRecording()
                except Exception:
                    busy = 0
                if not busy:
                    break
                if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                    break
                time.sleep_ms(2)

            energy = _mean_abs16(rec_buf, _RX_SAMPLES)
            now = time.ticks_ms()
            dec.feed(energy, now)

            if energy != last_energy:
                stale_since = now
                last_energy = energy
            elif (not mic_dead
                  and time.ticks_diff(now, stale_since) > 3000
                  and time.ticks_diff(now, started_at) > 3500):
                mic_dead = True
                last_draw = 0  # force a redraw with the warning

            if time.ticks_diff(now, last_draw) > 180:
                _draw_listen_dyn(dec, mic_dead)
                last_draw = now
    finally:
        try: M5.Mic.end()
        except Exception: pass


# ---------------- top-level ----------------

def run():
    Lcd.clear(_BG)
    kb = MatrixKeyboard()
    # In case the launcher (or a previous app) left the speaker PA enabled —
    # otherwise we'd hiss continuously even before any TX.
    _spk_off()
    buf = ""
    wpm = _DEFAULT_WPM
    mode = _M_FLASH
    last_status = None
    _draw_compose(buf, wpm, mode)

    while True:
        if mode == _M_LISTEN:
            r = _listen(kb)
            if r == "quit":
                return
            # cycle Right: Listen → Flash;  Left: Listen → Audio TX
            mode = _M_FLASH if r == "next" else _M_AUDIO
            last_status = None
            _draw_compose(buf, wpm, mode)
            continue

        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue

        if k == KeyCode.KEYCODE_ESC:
            return

        if k == KeyCode.KEYCODE_LEFT:
            mode = (mode - 1) % 3
            last_status = None
            if mode != _M_LISTEN:
                _draw_compose(buf, wpm, mode)
            continue

        if k == KeyCode.KEYCODE_RIGHT:
            mode = (mode + 1) % 3
            last_status = None
            if mode != _M_LISTEN:
                _draw_compose(buf, wpm, mode)
            continue

        if k == KeyCode.KEYCODE_ENTER:
            if not any(c == " " or c.upper() in _MORSE for c in buf):
                continue
            if mode == _M_FLASH:
                ok = _transmit_flash(buf, wpm, kb)
            else:
                ok = _transmit_audio(buf, wpm, kb)
            Lcd.clear(_BG)
            last_status = "Sent" if ok else "Aborted"
            _draw_compose(buf, wpm, mode, status=last_status)
            continue

        if last_status is not None:
            last_status = None

        if k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
            if buf:
                buf = buf[:-1]
                _draw_compose(buf, wpm, mode)
        elif k == KeyCode.KEYCODE_UP:
            if wpm < _MAX_WPM:
                wpm += 1
                _draw_compose(buf, wpm, mode)
        elif k == KeyCode.KEYCODE_DOWN:
            if wpm > _MIN_WPM:
                wpm -= 1
                _draw_compose(buf, wpm, mode)
        elif isinstance(k, int) and 32 <= k <= 126:
            ch = chr(k)
            if (ch == " " or ch.upper() in _MORSE) and len(buf) < _MAX_LEN:
                buf += ch
                _draw_compose(buf, wpm, mode)
