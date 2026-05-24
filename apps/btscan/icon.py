"""Icon for the btscan app — a stylized BT triangle inside a magnifier ring."""


def draw(lcd, x, y, size, on_dark):
    fg = 0xFFFFFF if on_dark else 0x000000
    accent = 0x40C8FF
    cx = x + size // 2
    cy = y + size // 2

    # Magnifier ring
    r = size // 2 - 2
    lcd.drawCircle(cx, cy, r, fg)
    lcd.drawCircle(cx, cy, r - 1, fg)

    # Stylized Bluetooth glyph in the center: vertical bar + two crossed
    # diagonals making the "rune" silhouette.
    h = max(6, size // 2)
    top = cy - h // 2
    bot = cy + h // 2
    mid = cy
    bar_x = cx
    side = max(2, h // 4)

    # Vertical center bar
    lcd.drawLine(bar_x, top, bar_x, bot, accent)
    # Top diagonal: from bottom-left to top-right
    lcd.drawLine(bar_x - side, mid - side // 2, bar_x + side, top + 1, accent)
    # Bottom diagonal: bottom-left of midpoint to top of bottom-right corner
    lcd.drawLine(bar_x - side, mid + side // 2, bar_x + side, bot - 1, accent)
    # Top crossover continuing the rune shape
    lcd.drawLine(bar_x, top, bar_x + side, top + side, accent)
    lcd.drawLine(bar_x, bot, bar_x + side, bot - side, accent)
