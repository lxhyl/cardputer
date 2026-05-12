def draw(lcd, x, y, size, on_dark):
    """Coiled green snake + red apple."""
    body = 0x00DD66
    apple = 0xFF6464
    eye = 0xFFFFFF
    edge = 0x000000 if on_dark else 0xFFFFFF
    cy = y + size // 2
    # Horizontal snake body across the middle
    lcd.fillRect(x + 2, cy - 1, size - 4, 3, body)
    # Head bump on the right
    lcd.fillCircle(x + size - 4, cy, 3, body)
    lcd.fillCircle(x + size - 3, cy - 1, 1, eye)
    # Apple in the upper-left
    lcd.fillCircle(x + 4, y + 4, 3, apple)
    # Stem on the apple
    lcd.drawPixel(x + 4, y + 1, 0x664400)
