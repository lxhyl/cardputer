def draw(lcd, x, y, size, on_dark):
    fg = 0xFFFFFF if on_dark else 0x000000
    bg = 0x002255 if on_dark else 0xCCD8FF
    accent = 0x40A0FF
    lcd.fillRect(x, y, size, size, bg)
    # Tiny mouse silhouette: rounded body + scroll dot
    pad = max(2, size // 6)
    body_w = size - pad * 2
    body_h = size - pad * 2
    bx = x + pad
    by = y + pad
    # outline
    lcd.drawRect(bx, by, body_w, body_h, fg)
    # scroll wheel
    cx = bx + body_w // 2
    wy = by + body_h // 4
    lcd.fillRect(cx - 1, wy, 2, max(2, size // 8), accent)
    # split line for left/right buttons
    lcd.drawLine(cx, by, cx, wy + max(2, size // 8) + 1, fg)
