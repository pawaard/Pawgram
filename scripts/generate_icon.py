from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

size = 256
image = Image.new("RGBA", (size, size), (5, 13, 22, 255))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((14, 14, 242, 242), radius=52, fill=(10, 54, 82, 255))
draw.rounded_rectangle((25, 25, 231, 231), radius=45, fill=(11, 39, 61, 255))

glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
glow_draw = ImageDraw.Draw(glow)
glow_draw.ellipse((55, 38, 101, 84), fill=(87, 204, 250, 70))
glow_draw.ellipse((105, 25, 151, 71), fill=(87, 204, 250, 70))
glow_draw.ellipse((155, 38, 201, 84), fill=(87, 204, 250, 70))
glow_draw.ellipse((66, 77, 112, 123), fill=(87, 204, 250, 70))
glow_draw.ellipse((70, 91, 186, 207), fill=(87, 204, 250, 70))
image = Image.alpha_composite(image, glow)
draw = ImageDraw.Draw(image)

blue = (102, 218, 255, 255)
draw.ellipse((61, 44, 95, 78), fill=blue)
draw.ellipse((111, 31, 145, 65), fill=blue)
draw.ellipse((161, 44, 195, 78), fill=blue)
draw.ellipse((72, 83, 106, 117), fill=blue)
draw.ellipse((78, 96, 178, 196), fill=blue)

png_path = ASSETS / "pawgram.png"
ico_path = ASSETS / "pawgram.ico"
image.save(png_path)
image.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(ico_path)

