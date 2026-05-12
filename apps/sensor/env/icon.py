def draw(lcd, x, y, size, on_dark):
    """Thermometer for the environment monitor."""
    fg = 0xFF8C64       # warm orange
    glass = 0xCCCCCC if on_dark else 0x666666
    cx = x + size // 2
    bulb_r = max(3, size // 5)
    bulb_y = y + size - bulb_r - 1
    stem_w = max(3, size // 6)
    stem_top = y + 2
    stem_bot = bulb_y
    # Glass outline (stem + bulb)
    lcd.fillCircle(cx, bulb_y, bulb_r, glass)
    lcd.fillRect(cx - stem_w // 2 - 1, stem_top, stem_w + 2, stem_bot - stem_top + 1, glass)
    # Mercury fill (slightly inset)
    lcd.fillCircle(cx, bulb_y, bulb_r - 1, fg)
    fill_top = stem_top + size // 3   # filled to ~2/3 height
    lcd.fillRect(cx - stem_w // 2, fill_top, stem_w, stem_bot - fill_top + 1, fg)
