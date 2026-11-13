"""Genera el ícono de EC-Voice: micrófono con ondas de sonido."""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "src" / "web" / "img"


def draw(s: int = 512) -> Image.Image:
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * .22), fill=(15, 150, 138, 255))
    white = (255, 255, 255, 255)
    # Cápsula del micrófono
    d.rounded_rectangle([s * .40, s * .16, s * .60, s * .56], radius=int(s * .10), fill=white)
    # Soporte en U, pie y base
    w = int(s * .035)
    d.arc([s * .31, s * .30, s * .69, s * .66], start=0, end=180, fill=white, width=w)
    d.rectangle([s * .5 - w / 2, s * .66, s * .5 + w / 2, s * .78], fill=white)
    d.rounded_rectangle([s * .38, s * .77, s * .62, s * .82], radius=int(s * .02), fill=white)
    # Ondas a los lados
    for k, r in enumerate((.22, .30)):
        box = [s * (.5 - r), s * (.36 - r * .9), s * (.5 + r), s * (.36 + r * .9)]
        d.arc(box, start=-35, end=35, fill=white, width=int(s * .028))
        d.arc(box, start=145, end=215, fill=white, width=int(s * .028))
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    big.resize((192, 192), Image.LANCZOS).save(OUT / "ecvoice.png")
    big.save(OUT / "ecvoice.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("íconos en", OUT)
