def draw(lcd, x, y, size, on_dark):
    fg = 0xFFFFFF if on_dark else 0x000000
    bg = 0x002A55 if on_dark else 0xCCE4FF
    lcd.fillRect(x, y, size, size, bg)
    # Stylised Bluetooth glyph centred in the icon
    cx = x + size // 2
    pad = max(2, size // 6)
    top = y + pad
    bot = y + size - pad
    mid = (top + bot) // 2
    qy = top + (bot - top) // 4
    by = top + 3 * (bot - top) // 4
    w = max(2, size // 4)
    # Spine
    lcd.drawLine(cx, top, cx, bot, fg)
    # Upper diamond: spine top → right-mid → upper-quarter on left side
    lcd.drawLine(cx, top, cx + w, mid, fg)
    lcd.drawLine(cx + w, mid, cx - w, qy, fg)
    # Lower diamond: spine bot → right-mid → lower-quarter on left side
    lcd.drawLine(cx, bot, cx + w, mid, fg)
    lcd.drawLine(cx + w, mid, cx - w, by, fg)
