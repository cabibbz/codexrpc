"""Turn the source microscope artwork into multi-resolution .ico files.

The sources are stock previews on white with light-grey watermark text. Alpha
is derived from distance-from-white, so the artwork keeps its antialiased
edges while near-white pixels (background AND the light watermark) drop out.
"""

import sys
from PIL import Image, ImageDraw

SIZES = [256, 128, 64, 48, 32, 16]
TRANSPARENT_BELOW = 60   # distance-from-white at/below this -> fully transparent
OPAQUE_ABOVE = 105       # at/above this -> fully opaque; between -> ramp


def knock_out_white(src_path: str) -> Image.Image:
    img = Image.open(src_path).convert("RGB")
    px = img.load()
    w, h = img.size
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    op = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            d = 255 - min(r, g, b)          # 0 = white, high = saturated/dark
            if d <= TRANSPARENT_BELOW:
                continue
            if d >= OPAQUE_ABOVE:
                a = 255
            else:
                a = int((d - TRANSPARENT_BELOW) * 255 /
                        (OPAQUE_ABOVE - TRANSPARENT_BELOW))
            op[x, y] = (r, g, b, a)
    return out


def square_with_margin(img: Image.Image, margin: float = 0.06) -> Image.Image:
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    side = int(max(w, h) * (1 + margin * 2))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return canvas


def on_tile(art: Image.Image, side: int = 1024) -> Image.Image:
    """Rounded white tile behind the art. Monochrome-black artwork is invisible
    on a dark taskbar without it."""
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).rounded_rectangle(
        [0, 0, side - 1, side - 1], radius=int(side * 0.20),
        fill=(255, 255, 255, 255))
    a = art.resize((int(side * 0.78), int(side * 0.78)), Image.LANCZOS)
    off = (side - a.size[0]) // 2
    canvas.paste(a, (off, off), a)
    return canvas


def build(src: str, ico_path: str, png_path: str, tile: bool = False) -> None:
    art = square_with_margin(knock_out_white(src))
    master = on_tile(art) if tile else art.resize((1024, 1024), Image.LANCZOS)
    master.resize((256, 256), Image.LANCZOS).save(png_path)
    frames = [master.resize((s, s), Image.LANCZOS) for s in SIZES]
    frames[0].save(ico_path, format="ICO",
                   sizes=[(s, s) for s in SIZES], append_images=frames[1:])
    print(f"{ico_path}  <- {src}")


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2], sys.argv[3], "--tile" in sys.argv)
