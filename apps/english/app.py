"""English vocabulary trainer.

Each session displays a small batch of words pulled from a Mac companion
app over the LAN. The user reviews words with <- -> ; ^ v scrolls the
definition; SPACE plays the recorded pronunciation; ESC exits and uploads
the session's view durations to the Mac for analytics + next-batch
selection.

Per-device config at /flash/english.json (NOT in source — see README).
"""

import os
import time

import M5
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

from . import sync
from . import render as _render

_VOL = 255  # 0-255; max. The Cardputer-Adv speaker + ES8311 codec is
            # genuinely quiet at moderate volumes — push it all the way.

# Mock data used when there is no /flash/english.json yet. Lets the user
# verify the app works visually before they configure the Mac side.
_MOCK_WORDS = [
    {"id": -1, "headword": "ephemeral", "pos": "adj",
     "ipa": "/i'fem.e.rel/",
     "def_en": "lasting for only a short time",
     "example": "a brief, ephemeral moment of joy",
     "pinyin": "duan zan de; zhuan shun ji shi de"},
    {"id": -2, "headword": "ubiquitous", "pos": "adj",
     "ipa": "/juu'bik.wi.tes/",
     "def_en": "seeming to be everywhere or in several places at the same time",
     "example": "mobile phones are now ubiquitous in modern life",
     "pinyin": "wu chu bu zai de"},
    {"id": -3, "headword": "candid", "pos": "adj",
     "ipa": "/'kaen.did/",
     "def_en": "truthful and straightforward; frank",
     "example": "a candid assessment of his performance",
     "pinyin": "tan shuai de; zhi yan bu hui de"},
]


class WordState:
    def __init__(self, batch_id, words, online=False):
        self.batch_id = batch_id
        self.words = words or []
        self.index = 0
        self.online = online

    def current(self):
        if not self.words:
            return None
        return self.words[self.index % len(self.words)]

    def next(self):
        if not self.words:
            return False
        self.index = (self.index + 1) % len(self.words)
        return True

    def prev(self):
        if not self.words:
            return False
        self.index = (self.index - 1) % len(self.words)
        return True


def _load_session():
    """Returns (cfg, state). cfg may be None if /flash/english.json is
    missing — in that case we show mock words for a visual smoke test."""
    cfg = sync.load_config()
    if cfg is None:
        return None, WordState(None, _MOCK_WORDS, online=False)

    def _status(txt):
        try:
            _renderer.status(txt)  # noqa: F821 — set later
        except Exception:
            pass

    result = sync.ensure_synced(cfg, status_cb=None)
    return cfg, WordState(result["batch_id"], result["words"],
                          online=result["online"])


def _spk_on():
    """Force-init the speaker. Mirrors the morse app pattern: end() before
    begin() forces M5Unified to re-run the codec init callback.

    After Speaker.setVolume (which only adjusts software gain), we also
    bump the ES8311 DAC_VOLUME register (0x32) to its maximum — that's
    the codec's own analog gain stage and gives the biggest perceived
    loudness boost on this otherwise-quiet built-in speaker."""
    try: M5.Speaker.end()
    except Exception: pass
    try: M5.Speaker.begin()
    except Exception: pass
    try: M5.Speaker.setVolume(_VOL)
    except Exception: pass
    # Keep ES8311 DACVOLUME at default 0 dB (0xBF). NS4150B's input
    # saturation is ~1 Vrms which is exactly what ES8311 produces at 0 dB
    # — any extra codec gain overdrives the PA, producing the audible
    # distortion. Loudness should be achieved by source-side LUFS
    # normalization (loudnorm in TTSGenerator), not codec boost.
    try:
        from machine import I2C, Pin
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
        i2c.writeto_mem(0x18, 0x32, b"\xBF")  # 0 dB (default)
    except Exception:
        pass


def _spk_off():
    """Power down ES8311 + amp. Values from ESP-ADF (see CLAUDE.md)."""
    try: M5.Speaker.stop()
    except Exception: pass
    try: M5.Speaker.end()
    except Exception: pass
    try:
        from machine import I2C, Pin
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
        for reg, val in (
            (0x32, 0x00),
            (0x12, 0x02),
            (0x13, 0x10),
            (0x0E, 0xFF),
            (0x14, 0x00),
            (0x0D, 0xFA),
            (0x37, 0x08),
            (0x00, 0x00),
        ):
            i2c.writeto_mem(0x18, reg, bytes([val]))
    except Exception:
        pass


def _audio_exists(word_id, kind="word"):
    if word_id is None or word_id < 0:
        return False
    try:
        os.stat(sync.audio_path(word_id, kind))
        return True
    except OSError:
        return False


def _play_for(word, renderer, kind="word"):
    if word is None:
        return False
    wid = word.get("id")
    if not _audio_exists(wid, kind):
        msg = "no example audio" if kind == "example" else "no audio"
        renderer.status(msg, color=0xFF8855)
        return False
    label = "playing ex" if kind == "example" else "playing"
    renderer.status(label, color=0x66DD99)
    from . import audio as _audio
    _spk_on()
    try:
        ok = _audio.play(sync.audio_path(wid, kind))
    finally:
        _spk_off()
    renderer.status(None)
    return bool(ok)


def _commit_view(reviews, word, t_start, played, marked_known=False):
    """Append a review record for the word being left. Coalesces by
    word_id so re-visits within the session sum their durations.

    `marked_known=True` means the user explicitly pressed Enter on this
    word — Mac side will short-circuit SRS quality inference to 5 (max).
    The flag is sticky: any Enter press during the session marks it."""
    if word is None:
        return
    wid = word.get("id")
    if wid is None or wid < 0:
        return
    dur = time.ticks_diff(time.ticks_ms(), t_start)
    if dur < 0:
        dur = 0
    for r in reviews:
        if r["word_id"] == wid:
            r["duration_ms"] += dur
            r["played_audio"] = r["played_audio"] or played
            r["marked_known"] = r["marked_known"] or marked_known
            r["viewed"] = True
            return
    reviews.append({
        "word_id": wid,
        "viewed": True,
        "duration_ms": dur,
        "played_audio": bool(played),
        "marked_known": bool(marked_known),
    })


def _splash(msg):
    Lcd.fillRect(0, 0, Lcd.width(), Lcd.height(), 0x000000)
    Lcd.setFont(M5.Lcd.FONTS.DejaVu18)
    Lcd.setTextColor(0xFFD040, 0x000000)
    w = Lcd.textWidth(msg, M5.Lcd.FONTS.DejaVu18)
    Lcd.setCursor((Lcd.width() - w) // 2, Lcd.height() // 2 - 10)
    Lcd.print(msg)


def run():
    Lcd.clear(0x000000)
    _splash("syncing...")
    cfg, state = _load_session()
    renderer = _render.Renderer(state)
    renderer.draw()
    if cfg is None:
        renderer.status("no config", color=0xFF8855)
    elif not state.online:
        renderer.status("offline", color=0xFF8855)

    kb = MatrixKeyboard()
    reviews = []
    cur_played = False
    t_start = time.ticks_ms()
    SPACE = KeyCode.KEYCODE_SPACE if hasattr(KeyCode, "KEYCODE_SPACE") else 32

    try:
        while True:
            k = kb.get_key()
            if k is None:
                time.sleep_ms(30)
                continue
            # Arrow keys on Cardputer-Adv require Fn modifier — annoying
            # to hold. Mirror snake/launcher convention so single-key
            # WASD + ;,./ also navigate. (Fn+arrows still work too.)
            if k == KeyCode.KEYCODE_ESC:
                break
            elif k == KeyCode.KEYCODE_LEFT or k == ord("a") or k == ord(","):
                _commit_view(reviews, state.current(), t_start, cur_played)
                state.prev()
                cur_played = False
                t_start = time.ticks_ms()
                renderer.draw()
                if cfg is None:
                    renderer.status("no config", color=0xFF8855)
                elif not state.online:
                    renderer.status("offline", color=0xFF8855)
            elif k == KeyCode.KEYCODE_RIGHT or k == ord("d") or k == ord("/"):
                _commit_view(reviews, state.current(), t_start, cur_played)
                state.next()
                cur_played = False
                t_start = time.ticks_ms()
                renderer.draw()
                if cfg is None:
                    renderer.status("no config", color=0xFF8855)
                elif not state.online:
                    renderer.status("offline", color=0xFF8855)
            elif k == KeyCode.KEYCODE_ENTER:
                # Explicit "I know this word" — mark + auto-advance.
                _commit_view(reviews, state.current(), t_start, cur_played,
                             marked_known=True)
                state.next()
                cur_played = False
                t_start = time.ticks_ms()
                renderer.draw()
                renderer.status("known!", color=0x66DD99)
            elif k == KeyCode.KEYCODE_UP or k == ord("w") or k == ord(";"):
                renderer.scroll(-1)
            elif k == KeyCode.KEYCODE_DOWN or k == ord("s") or k == ord("."):
                renderer.scroll(+1)
            elif k == SPACE or k == 32:
                ok = _play_for(state.current(), renderer, kind="word")
                cur_played = cur_played or ok
            elif k == ord("p"):
                # Play the example sentence audio (if the Mac generated
                # one for this word). Counts as "played_audio" too — the
                # user is still engaging with the pronunciation.
                ok = _play_for(state.current(), renderer, kind="example")
                cur_played = cur_played or ok
            elif k == ord("r"):
                # Force-refresh: re-sync from Mac.
                if cfg is not None:
                    _commit_view(reviews, state.current(), t_start, cur_played)
                    if reviews:
                        sync.submit_reviews(cfg, state.batch_id, reviews)
                        reviews = []
                    _splash("syncing...")
                    result = sync.ensure_synced(cfg, force=True)
                    state = WordState(result["batch_id"], result["words"],
                                      online=result["online"])
                    renderer = _render.Renderer(state)
                    renderer.draw()
                    if not state.online:
                        renderer.status("offline", color=0xFF8855)
                    cur_played = False
                    t_start = time.ticks_ms()
            time.sleep_ms(20)
    finally:
        # Commit the in-progress view, upload, silence codec on the way out.
        _commit_view(reviews, state.current(), t_start, cur_played)
        if cfg is not None and reviews:
            try:
                sync.submit_reviews(cfg, state.batch_id, reviews)
            except Exception:
                pass
        _spk_off()
