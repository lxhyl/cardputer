def draw(lcd, x, y, size, on_dark):
    fg = 0xFFFFFF if on_dark else 0x000000
    bg = 0x002A55 if on_dark else 0xCCE4FF
    accent = 0x40A0FF
    lcd.fillRect(x, y, size, size, bg)
    # Stylised Bluetooth glyph centred — same as the old btmacro icon, since
    # this is now the unified BLE input device app.
    cx = x + size // 2
    pad = max(2, size // 6)
    top = y + pad
    bot = y + size - pad
    mid = (top + bot) // 2
    qy = top + (bot - top) // 4
    by = top + 3 * (bot - top) // 4
    w = max(2, size // 4)
    lcd.drawLine(cx, top, cx, bot, fg)
    lcd.drawLine(cx, top, cx + w, mid, fg)
    lcd.drawLine(cx + w, mid, cx - w, qy, fg)
    lcd.drawLine(cx, bot, cx + w, mid, fg)
    lcd.drawLine(cx + w, mid, cx - w, by, fg)
    # Tiny accent dot to hint "input" / "active"
    lcd.fillRect(cx + w + 1, mid - 1, 2, 2, accent)
