from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "Pawgram_Satis_Afisi.png"
WIDTH, HEIGHT = 1600, 2000


def font(size: int, bold: bool = False):
    name = "seguisb.ttf" if bold else "segoeui.ttf"
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / name), size)


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def centered(draw, text, y, used_font, fill, center=WIDTH // 2):
    bounds = draw.textbbox((0, 0), text, font=used_font)
    draw.text((center - (bounds[2] - bounds[0]) / 2, y), text, font=used_font, fill=fill)


def icon(draw, kind, x, y, color):
    if kind == "users":
        draw.ellipse((x + 12, y + 2, x + 34, y + 24), outline=color, width=4)
        draw.ellipse((x + 38, y + 8, x + 55, y + 25), outline=color, width=4)
        draw.arc((x + 2, y + 22, x + 45, y + 60), 190, 350, fill=color, width=4)
        draw.arc((x + 28, y + 25, x + 65, y + 58), 190, 350, fill=color, width=4)
    elif kind == "rotate":
        draw.arc((x + 5, y + 6, x + 57, y + 57), 200, 35, fill=color, width=5)
        draw.polygon([(x + 54, y + 5), (x + 64, y + 18), (x + 47, y + 20)], fill=color)
        draw.arc((x + 5, y + 6, x + 57, y + 57), 20, 215, fill=color, width=5)
        draw.polygon([(x + 7, y + 58), (x - 1, y + 44), (x + 16, y + 43)], fill=color)
    elif kind == "pulse":
        draw.line((x, y + 34, x + 15, y + 34, x + 24, y + 14, x + 37, y + 51, x + 47, y + 27, x + 65, y + 27), fill=color, width=5, joint="curve")
    elif kind == "shield":
        draw.polygon([(x + 32, y), (x + 61, y + 12), (x + 56, y + 45), (x + 32, y + 63), (x + 8, y + 45), (x + 3, y + 12)], outline=color, fill=None)
        draw.line((x + 18, y + 31, x + 29, y + 42, x + 48, y + 21), fill=color, width=5)
    elif kind == "filter":
        draw.polygon([(x, y + 4), (x + 65, y + 4), (x + 41, y + 31), (x + 41, y + 57), (x + 25, y + 65), (x + 25, y + 31)], outline=color)
    elif kind == "file":
        draw.rounded_rectangle((x + 10, y, x + 55, y + 63), radius=6, outline=color, width=4)
        for offset in (20, 32, 44):
            draw.line((x + 20, y + offset, x + 46, y + offset), fill=color, width=3)


image = Image.new("RGB", (WIDTH, HEIGHT), "#06101e")
pixels = image.load()
assert pixels is not None
for y in range(HEIGHT):
    t = y / HEIGHT
    for x in range(WIDTH):
        radial = max(0, 1 - (((x - 800) / 950) ** 2 + ((y - 520) / 850) ** 2))
        pixels[x, y] = (
            int(5 + 7 * t + 3 * radial),
            int(14 + 10 * t + 20 * radial),
            int(29 + 15 * t + 39 * radial),
        )

glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse((80, 140, 1520, 1050), fill=(0, 127, 255, 50))
gd.ellipse((860, 960, 1770, 1860), fill=(113, 65, 255, 34))
glow = glow.filter(ImageFilter.GaussianBlur(120))
image = Image.alpha_composite(image.convert("RGBA"), glow)
draw = ImageDraw.Draw(image)

BLUE = "#27a8ff"
CYAN = "#42e8e0"
WHITE = "#f4f8ff"
MUTED = "#9fb0c7"
PANEL = "#0b1728"
CARD = "#0d1c30"
BORDER = "#1b3856"

centered(draw, "PAWGRAM", 74, font(104, True), WHITE)
centered(draw, "Telegram Yönetim ve Analiz Paneli", 195, font(35), MUTED)
draw.rounded_rectangle((655, 257, 945, 267), radius=5, fill=BLUE)

# Dashboard hero
shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
sd = ImageDraw.Draw(shadow)
sd.rounded_rectangle((116, 330, 1484, 1030), radius=44, fill=(0, 0, 0, 170))
shadow = shadow.filter(ImageFilter.GaussianBlur(34))
image = Image.alpha_composite(image, shadow)
draw = ImageDraw.Draw(image)
rounded(draw, (95, 305, 1505, 1000), 42, PANEL, BORDER, 3)
rounded(draw, (95, 305, 1505, 382), 42, "#0f2035")
draw.rectangle((95, 348, 1505, 382), fill="#0f2035")
draw.ellipse((132, 334, 150, 352), fill="#ff667d")
draw.ellipse((165, 334, 183, 352), fill="#ffc75b")
draw.ellipse((198, 334, 216, 352), fill="#50dc96")
draw.text((250, 326), "Pawgram Yönetim Paneli", font=font(25, True), fill=WHITE)
rounded(draw, (1245, 326, 1465, 360), 17, "#103a35", "#1f7268")
draw.text((1281, 332), "● Sistem Aktif", font=font(18, True), fill="#68f4c2")

# Sidebar
rounded(draw, (120, 410, 374, 956), 24, "#091525", "#17304b")
draw.text((154, 442), "PAWGRAM", font=font(29, True), fill=WHITE)
for idx, label in enumerate(("Panel", "Session'lar", "Gruplarım", "Aktiflik", "Kuyruk", "Ayarlar")):
    yy = 518 + idx * 65
    if label == "Aktiflik":
        rounded(draw, (140, yy - 12, 354, yy + 38), 14, "#123b62")
    draw.ellipse((157, yy + 2, 170, yy + 15), fill=BLUE if label == "Aktiflik" else "#647b98")
    draw.text((190, yy - 3), label, font=font(20, label == "Aktiflik"), fill=WHITE if label == "Aktiflik" else MUTED)

# Metrics
metrics = (("AKTİF SESSION", "6", BLUE), ("GÜNLÜK KOTA", "18 / 30", CYAN), ("PROXY SAĞLIK", "5 / 6", "#a78bfa"))
for idx, (label, value, color) in enumerate(metrics):
    xx = 410 + idx * 335
    rounded(draw, (xx, 415, xx + 300, 555), 22, CARD, BORDER)
    draw.text((xx + 24, 440), label, font=font(16, True), fill=MUTED)
    draw.text((xx + 24, 476), value, font=font(38, True), fill=color)
    rounded(draw, (xx + 175, 510, xx + 270, 520), 5, "#17304b")
    rounded(draw, (xx + 175, 510, xx + 245, 520), 5, color)

# Chart
rounded(draw, (410, 588, 1018, 936), 24, CARD, BORDER)
draw.text((438, 615), "Aktivite Taraması", font=font(25, True), fill=WHITE)
draw.text((438, 650), "Son 7 gün • güvenli analiz", font=font(17), fill=MUTED)
for line in range(5):
    yy = 714 + line * 42
    draw.line((444, yy, 986, yy), fill="#162b45", width=2)
points = [(450, 847), (535, 810), (620, 828), (705, 749), (790, 771), (875, 698), (970, 720)]
draw.line(points, fill=BLUE, width=7, joint="curve")
for px, py in points:
    draw.ellipse((px - 7, py - 7, px + 7, py + 7), fill=CYAN)

# Right status panel
rounded(draw, (1052, 588, 1466, 936), 24, CARD, BORDER)
draw.text((1080, 615), "Session Durumu", font=font(25, True), fill=WHITE)
sessions = (("Ana Hesap", "SOCKS5", "12 / 30", "#50dc96"), ("Yedek Hesap", "HTTP", "6 / 30", BLUE), ("Session 03", "Proxy Kapalı", "0 / 30", "#ffc75b"))
for idx, (name, proxy, quota, status_color) in enumerate(sessions):
    yy = 682 + idx * 82
    draw.ellipse((1080, yy + 8, 1098, yy + 26), fill=status_color)
    draw.text((1115, yy), name, font=font(19, True), fill=WHITE)
    draw.text((1115, yy + 29), proxy, font=font(15), fill=MUTED)
    draw.text((1352, yy + 10), quota, font=font(16, True), fill=CYAN)

centered(draw, "24 Saat  •  3 Gün  •  7 Gün  •  30 Gün", 1047, font(28, True), CYAN)

# Feature cards
features = (
    ("Çoklu Session", "Sınırsız hesap havuzu", "users", BLUE),
    ("Round-Robin Kota", "Proaktif işlem dağıtımı", "rotate", CYAN),
    ("Aktiflik Taraması", "Mesaj yazarı analizi", "pulse", "#7dd3fc"),
    ("Session Proxy", "SOCKS5 ve HTTP", "shield", "#a78bfa"),
    ("Akıllı Filtreleme", "Yönetici ve geçmiş filtresi", "filter", "#60a5fa"),
    ("CSV ve Yedekleme", "Rapor ve veri güvenliği", "file", "#67e8f9"),
)
for idx, (title, subtitle, kind, color) in enumerate(features):
    col, row = idx % 2, idx // 2
    x = 100 + col * 750
    y = 1140 + row * 205
    rounded(draw, (x, y, x + 650, y + 165), 26, CARD, BORDER, 2)
    rounded(draw, (x + 28, y + 35, x + 118, y + 125), 22, "#102b48", "#235984")
    icon(draw, kind, x + 42, y + 48, color)
    draw.text((x + 145, y + 42), title, font=font(27, True), fill=WHITE)
    draw.text((x + 145, y + 86), subtitle, font=font(19), fill=MUTED)
    draw.line((x + 145, y + 126, x + 580, y + 126), fill="#17304b", width=2)

# Bottom callout
rounded(draw, (170, 1770, 1430, 1860), 32, "#0f3152", "#2d8fd0", 3)
centered(draw, "Windows EXE  •  Tek Tık Kurulum", 1793, font(34, True), WHITE)
centered(draw, "Güvenli  •  Yerel  •  Kullanıcı Dostu", 1907, font(25, True), MUTED)

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
image.convert("RGB").save(OUTPUT, quality=95)
print(OUTPUT)
