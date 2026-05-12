def draw(lcd, x, y, size, on_dark):
    fg = 0xFFFFFF if on_dark else 0x000000
    bg = 0x223344 if on_dark else 0xCDE
    accent = 0x40A0FF
    lcd.fillRect(x, y, size, size, bg)
    # Tiny "monitor / chip" — outer rounded rect, inner bars suggesting stats
    pad = max(2, size // 7)
    lcd.drawRect(x + pad, y + pad, size - pad * 2, size - pad * 2, fg)
    # Three horizontal bars of varying length (stats-like)
    bar_x = x + pad + 2
    bar_w_max = size - pad * 2 - 4
    bar_h = max(1, size // 14)
    gap = max(2, size // 10)
    by = y + pad + gap
    for ratio in (1.0, 0.7, 0.4):
        w = int(bar_w_max * ratio)
        lcd.fillRect(bar_x, by, w, bar_h, accent if ratio > 0.5 else fg)
        by += bar_h + gap
