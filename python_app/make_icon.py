"""Draw the app icon (a polygraph trace) as polygraph.ico next to this script.

Run it only to change the icon; the .ico is committed. Needs Pillow (not an app dependency).
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent
BG = (18, 30, 54)        # dark navy
GRID = (34, 52, 86)
TRACE = (64, 230, 120)   # monitor green


def draw(size: int) -> Image.Image:
    ss = 8  # supersample
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(S * 0.2)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=BG)
    if size >= 48:  # faint chart-paper grid only where it can be seen
        for i in range(1, 4):
            x = S * i / 4
            d.line([(x, S * 0.08), (x, S * 0.92)], fill=GRID, width=max(ss, S // 128))
            d.line([(S * 0.08, x), (S * 0.92, x)], fill=GRID, width=max(ss, S // 128))
    # Trace: calm, one big spike, calm again. Fewer wiggles at small sizes.
    if size <= 24:
        pts = [(0.10, 0.55), (0.32, 0.55), (0.42, 0.22), (0.55, 0.82), (0.65, 0.50), (0.90, 0.50)]
        w = 0.13
    else:
        pts = [(0.08, 0.55), (0.18, 0.50), (0.26, 0.58), (0.34, 0.52), (0.42, 0.18),
               (0.53, 0.86), (0.62, 0.40), (0.70, 0.56), (0.80, 0.49), (0.92, 0.53)]
        w = 0.075
    pts = [(x * S, y * S) for x, y in pts]
    d.line(pts, fill=TRACE, width=int(S * w), joint="curve")
    rad = S * w / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=TRACE)
    return img.resize((size, size), Image.LANCZOS)


sizes = [16, 24, 32, 48, 64, 128, 256]
frames = {s: draw(s) for s in sizes}
frames[256].save(OUT / "polygraph.ico", sizes=[(s, s) for s in sizes],
                 append_images=[frames[s] for s in sizes[:-1]])
