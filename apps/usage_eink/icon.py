"""Icon: a tiny e-ink display rectangle showing a percent sign."""


def draw(lcd, x, y, size, on_dark):
    fg = 0xFFFFFF if on_dark else 0x000000
    accent = 0xE5A642  # amber, evokes the e-ink "almost-yellow" cast

    # Outer screen frame
    pad = max(1, size // 8)
    lcd.drawRect(x + pad, y + pad, size - 2 * pad, size - 2 * pad, fg)
    lcd.drawRect(x + pad + 1, y + pad + 1,
                 size - 2 * pad - 2, size - 2 * pad - 2, fg)

    # A bold "%" inside (two dots + diagonal)
    cx = x + size // 2
    cy = y + size // 2
    r = max(1, size // 10)
    lcd.fillCircle(cx - size // 4, cy - size // 4, r, accent)
    lcd.fillCircle(cx + size // 4, cy + size // 4, r, accent)
    # Diagonal slash
    lcd.drawLine(cx - size // 4 + r, cy + size // 4 - 1,
                 cx + size // 4 - r, cy - size // 4 + 1, accent)
    lcd.drawLine(cx - size // 4 + r + 1, cy + size // 4 - 1,
                 cx + size // 4 - r + 1, cy - size // 4 + 1, accent)
