def draw(lcd, x, y, size, on_dark):
    """Morse pattern icon: a dot, a dash, then SOS-ish row of dots."""
    fg = 0xFFFFFF if on_dark else 0x000000
    accent = 0xFFD040
    cx = x + size // 2
    cy = y + size // 2
    dot_r = max(1, size // 12)
    dash_w = max(4, size // 3)
    dash_h = max(2, size // 9)

    # Top row: dot - dash - dot
    row_y = cy - max(2, size // 5)
    px = x + 2
    lcd.fillCircle(px + dot_r, row_y, dot_r, accent)
    px += dot_r * 2 + 2
    lcd.fillRect(px, row_y - dash_h // 2, dash_w, dash_h, fg)
    px += dash_w + 2
    lcd.fillCircle(px + dot_r, row_y, dot_r, accent)

    # Bottom row: dash - dot - dot
    row_y = cy + max(2, size // 5)
    px = x + 2
    lcd.fillRect(px, row_y - dash_h // 2, dash_w, dash_h, fg)
    px += dash_w + 2
    lcd.fillCircle(px + dot_r, row_y, dot_r, accent)
    px += dot_r * 2 + 2
    lcd.fillCircle(px + dot_r, row_y, dot_r, accent)
