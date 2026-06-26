"""Generate Capacitor icon/splash source images (house on teal).
Run:  python scripts/make_icons.py
Outputs into ./assets/ which @capacitor/assets turns into Android icons.
"""
import os
from PIL import Image, ImageDraw

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
os.makedirs(ASSETS, exist_ok=True)

TOP = (16, 140, 130)     # teal (lighter, top)
BOTTOM = (12, 93, 87)    # teal (darker, bottom)
WHITE = (255, 255, 255, 255)


def gradient(size):
    img = Image.new("RGB", (size, size), TOP)
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))
        d.line([(0, y), (size, y)], fill=c)
    return img


def draw_house(img, s):
    """Draw a centered white house onto img; s = nominal house size in px."""
    d = ImageDraw.Draw(img)
    W = img.size[0]
    cx, cy = W / 2, W / 2
    # roof (triangle)
    apex = (cx, cy - 0.46 * s)
    left = (cx - 0.52 * s, cy - 0.02 * s)
    right = (cx + 0.52 * s, cy - 0.02 * s)
    d.polygon([apex, left, right], fill=WHITE)
    # body (rounded rectangle)
    bx0, by0 = cx - 0.37 * s, cy - 0.05 * s
    bx1, by1 = cx + 0.37 * s, cy + 0.46 * s
    r = 0.06 * s
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=r, fill=WHITE)
    # door (cut out in teal-ish / transparent)
    dx0, dy0 = cx - 0.12 * s, cy + 0.14 * s
    dx1, dy1 = cx + 0.12 * s, cy + 0.46 * s
    door = (12, 93, 87, 255) if img.mode == "RGBA" else (12, 93, 87)
    d.rounded_rectangle([dx0, dy0, dx1, dy1], radius=0.04 * s, fill=door)


def save(img, name):
    path = os.path.join(ASSETS, name)
    img.save(path)
    print("wrote", path, img.size, img.mode)


# 1) icon-background.png — solid gradient teal (1024)
save(gradient(1024), "icon-background.png")

# 2) icon-foreground.png — transparent, white house padded into adaptive safe zone
fg = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
draw_house(fg, 1024 * 0.40)
save(fg, "icon-foreground.png")

# 3) icon-only.png — full legacy icon (teal + house)
icon = gradient(1024)
draw_house(icon, 1024 * 0.46)
save(icon, "icon-only.png")

# 4) splash.png — large teal canvas with smaller centered house
splash = gradient(2732)
draw_house(splash, 2732 * 0.16)
save(splash, "splash.png")

print("done")
