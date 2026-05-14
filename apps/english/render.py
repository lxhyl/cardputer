"""Word card renderer with vertical scroll over the body region.

Layout (240x135):

  y= 0..28  Word headline (DejaVu24, baseline near y=2)
  y=28..44  IPA + POS meta line (DejaVu12)
  y=44..46  Divider line
  y=46..120 Body region (~74 px, fits 5 lines of DejaVu12 + padding)
            wraps EN definition, EX example, CN pinyin
  y=120..134 Bottom hint
  x=237..239 Scrollbar in body region when needed
"""

import M5
from M5 import Lcd

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x777777
_ACCENT = 0xFFD040
_GOOD = 0x66DD99
_WARN = 0xFF8855
_BLUE = 0x6FB0FF

_F_BIG = M5.Lcd.FONTS.DejaVu24
_F_MID = M5.Lcd.FONTS.DejaVu18
_F_SMALL = M5.Lcd.FONTS.DejaVu12

_TOP_H = 28
_META_H = 16
_DIV_Y = _TOP_H + _META_H  # = 44
_HINT_H = 14
_BODY_Y = _DIV_Y + 4
_BODY_H = 135 - _HINT_H - _BODY_Y - 2  # leave a 2 px gutter
_LINE_H = 14
_VISIBLE_LINES = _BODY_H // _LINE_H  # 5

_BODY_X = 4
_BODY_RIGHT = 234
_SCROLL_X = 236

_PREFIX_EN = "EN"
_PREFIX_EX = "EX"
_PREFIX_CN = "CN"
_PREFIX_W = 24  # px reserved for the label column


def _safe_text_width(s, font):
    try:
        return Lcd.textWidth(s, font)
    except Exception:
        # Fall back to a rough 7px per char estimate.
        return 7 * len(s)


def _wrap(text, font, max_w):
    """Greedy word-wrap. Returns list of lines."""
    if not text:
        return []
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        candidate = w if not cur else (cur + " " + w)
        if _safe_text_width(candidate, font) <= max_w:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            # Long single token: hard split
            if _safe_text_width(w, font) > max_w:
                buf = ""
                for ch in w:
                    if _safe_text_width(buf + ch, font) > max_w:
                        if buf:
                            lines.append(buf)
                        buf = ch
                    else:
                        buf += ch
                cur = buf
            else:
                cur = w
    if cur:
        lines.append(cur)
    return lines


def build_body_lines(word_obj):
    """Compose the wrapped body lines for the given word dict.

    Each returned element is (prefix, text) where prefix is "" for
    continuation lines."""
    out = []
    text_w = _BODY_RIGHT - _BODY_X - _PREFIX_W

    def _add_block(prefix, text):
        if not text:
            return
        lines = _wrap(text, _F_SMALL, text_w)
        if not lines:
            return
        out.append((prefix, lines[0]))
        for cont in lines[1:]:
            out.append(("", cont))

    # CN gloss first so the user lands on it without scrolling — the
    # Chinese meaning is the highest-signal hint for an English learner.
    _add_block(_PREFIX_CN, word_obj.get("pinyin") or "")
    if word_obj.get("def_en"):
        if out: out.append(("", ""))
        _add_block(_PREFIX_EN, word_obj["def_en"])
    if word_obj.get("example"):
        if out: out.append(("", ""))
        _add_block(_PREFIX_EX, word_obj["example"])
    return out


class Renderer:
    def __init__(self, state):
        self.state = state
        self.scroll_offset = 0  # in lines
        self._body_lines = []
        self._last_word_id = None
        self._last_status_text = None

    def _ensure_lines(self):
        word = self.state.current()
        if word is None:
            self._body_lines = []
            return
        wid = word.get("id")
        if wid != self._last_word_id:
            self._body_lines = build_body_lines(word)
            self.scroll_offset = 0
            self._last_word_id = wid

    def scroll(self, delta):
        self._ensure_lines()
        max_off = max(0, len(self._body_lines) - _VISIBLE_LINES)
        new_off = self.scroll_offset + delta
        if new_off < 0:
            new_off = 0
        if new_off > max_off:
            new_off = max_off
        if new_off != self.scroll_offset:
            self.scroll_offset = new_off
            self._draw_body()
            self._draw_scrollbar()

    def draw(self):
        self._ensure_lines()
        Lcd.fillRect(0, 0, Lcd.width(), Lcd.height(), _BG)
        self._draw_headline()
        self._draw_meta()
        Lcd.drawLine(0, _DIV_Y, Lcd.width(), _DIV_Y, _DIM)
        self._draw_body()
        self._draw_scrollbar()
        self._draw_hint()
        self._draw_status_corner()

    def _draw_headline(self):
        Lcd.fillRect(0, 0, Lcd.width(), _TOP_H, _BG)
        word = self.state.current()
        if word is None:
            Lcd.setFont(_F_MID)
            Lcd.setTextColor(_WARN, _BG)
            Lcd.setCursor(_BODY_X, 4)
            Lcd.print("(no words)")
            return
        head = word.get("headword", "?")
        # Index + total in DejaVu12 on the right
        idx_label = "{}/{}".format(self.state.index + 1, len(self.state.words))
        idx_w = _safe_text_width(idx_label, _F_SMALL)
        # Word in DejaVu24 on the left, leave room for idx
        max_word_w = Lcd.width() - idx_w - _BODY_X * 2 - 6
        font = _F_BIG
        if _safe_text_width(head, _F_BIG) > max_word_w:
            font = _F_MID
        Lcd.setFont(font)
        Lcd.setTextColor(_FG, _BG)
        Lcd.setCursor(_BODY_X, 2)
        Lcd.print(head)

        Lcd.setFont(_F_SMALL)
        Lcd.setTextColor(_DIM, _BG)
        Lcd.setCursor(Lcd.width() - idx_w - _BODY_X, 4)
        Lcd.print(idx_label)

    def _draw_meta(self):
        Lcd.fillRect(0, _TOP_H, Lcd.width(), _META_H, _BG)
        word = self.state.current()
        if word is None:
            return
        ipa = word.get("ipa", "") or ""
        pos = word.get("pos", "") or ""
        Lcd.setFont(_F_SMALL)
        Lcd.setTextColor(_BLUE, _BG)
        Lcd.setCursor(_BODY_X, _TOP_H + 2)
        Lcd.print(ipa)
        if pos:
            ipa_w = _safe_text_width(ipa, _F_SMALL)
            Lcd.setTextColor(_DIM, _BG)
            Lcd.setCursor(_BODY_X + ipa_w + 8, _TOP_H + 2)
            Lcd.print(pos)

    def _draw_body(self):
        # Wipe body region
        Lcd.fillRect(0, _BODY_Y, _BODY_RIGHT - 0, _BODY_H, _BG)
        if not self._body_lines:
            return
        Lcd.setFont(_F_SMALL)
        visible = self._body_lines[self.scroll_offset:self.scroll_offset + _VISIBLE_LINES]
        y = _BODY_Y
        for prefix, text in visible:
            if prefix:
                color = _ACCENT if prefix == _PREFIX_EN else (
                    _GOOD if prefix == _PREFIX_EX else _BLUE)
                Lcd.setTextColor(color, _BG)
                Lcd.setCursor(_BODY_X, y)
                Lcd.print(prefix)
            Lcd.setTextColor(_FG, _BG)
            Lcd.setCursor(_BODY_X + _PREFIX_W, y)
            Lcd.print(text)
            y += _LINE_H

    def _draw_scrollbar(self):
        # Clear column
        Lcd.fillRect(_SCROLL_X, _BODY_Y, 3, _BODY_H, _BG)
        n = len(self._body_lines)
        if n <= _VISIBLE_LINES:
            return
        # Track
        Lcd.drawLine(_SCROLL_X + 1, _BODY_Y, _SCROLL_X + 1, _BODY_Y + _BODY_H - 1, _DIM)
        # Thumb
        thumb_h = max(6, int(_BODY_H * _VISIBLE_LINES / n))
        max_off = n - _VISIBLE_LINES
        if max_off <= 0:
            thumb_y = _BODY_Y
        else:
            thumb_y = _BODY_Y + int((_BODY_H - thumb_h) * self.scroll_offset / max_off)
        Lcd.fillRect(_SCROLL_X, thumb_y, 3, thumb_h, _FG)

    def _draw_hint(self):
        y = 135 - _HINT_H + 1
        Lcd.fillRect(0, y, Lcd.width(), _HINT_H - 1, _BG)
        Lcd.setFont(_F_SMALL)
        Lcd.setTextColor(_DIM, _BG)
        hint = "a/d word  w/s scroll  SPC play  ENT known  ESC"
        Lcd.setCursor((Lcd.width() - _safe_text_width(hint, _F_SMALL)) // 2, y + 1)
        Lcd.print(hint)

    def status(self, text, color=None):
        """Show a one-shot status banner near the bottom-right of the meta row.
        Used for 'syncing...', 'offline', 'playing'. Pass None to clear."""
        # Clear the right portion of the meta row.
        y = _TOP_H + 2
        Lcd.fillRect(Lcd.width() // 2, _TOP_H, Lcd.width() // 2, _META_H, _BG)
        if not text:
            return
        Lcd.setFont(_F_SMALL)
        Lcd.setTextColor(color or _ACCENT, _BG)
        w = _safe_text_width(text, _F_SMALL)
        Lcd.setCursor(Lcd.width() - w - _BODY_X, y)
        Lcd.print(text)

    def _draw_status_corner(self):
        # Reserved for future indicators (WiFi up, audio cached). For now
        # the meta-row right side is left blank and used by status() banners.
        pass
