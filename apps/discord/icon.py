def draw(lcd, x, y, size, on_dark):
    """Discord-style speech bubble in brand 'Blurple'."""
    blurple = 0x5865F2
    white = 0xFFFFFF
    edge = 0x000000 if not on_dark else 0xFFFFFF
    cx = x + size // 2
    body_w = size - 2
    body_h = size - 5
    bx = x + 1
    by = y + 1
    # Rounded body
    lcd.fillRoundRect(bx, by, body_w, body_h, 3, blurple)
    # Three dots = chat indicator
    cy = by + body_h // 2
    spacing = max(2, size // 5)
    for dx in (-spacing, 0, spacing):
        lcd.fillCircle(cx + dx, cy, 1, white)
    # Tail at bottom-left, pointing down-right
    tx = bx + 3
    ty = by + body_h - 1
    lcd.fillTriangle(tx, ty, tx + 4, ty, tx + 1, ty + 3, blurple)
