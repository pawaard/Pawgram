from pathlib import Path
from textwrap import wrap

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "Pawgram_Kullanim_Kilavuzu.pdf"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

FONT_DIR = Path(r"C:\Windows\Fonts")
pdfmetrics.registerFont(TTFont("PawgramRegular", FONT_DIR / "segoeui.ttf"))
pdfmetrics.registerFont(TTFont("PawgramBold", FONT_DIR / "segoeuib.ttf"))

WIDTH, HEIGHT = A4
BG = HexColor("#07101A")
PANEL = HexColor("#0E1927")
BORDER = HexColor("#26394F")
TEXT = HexColor("#E8EEF7")
MUTED = HexColor("#8492A6")
BLUE = HexColor("#22ACEF")
GREEN = HexColor("#2ED49B")
YELLOW = HexColor("#E7B84C")

c = canvas.Canvas(str(OUTPUT), pagesize=A4)
page_number = 0
y = HEIGHT - 72


def background(title: str | None = None):
    global page_number, y
    page_number += 1
    c.setFillColor(BG)
    c.rect(0, 0, WIDTH, HEIGHT, fill=1, stroke=0)
    c.setFillColor(HexColor("#0A3048"))
    c.circle(WIDTH - 60, HEIGHT + 20, 145, fill=1, stroke=0)
    c.setFillColor(BG)
    c.circle(WIDTH - 28, HEIGHT + 25, 120, fill=1, stroke=0)
    if title:
        c.setFont("PawgramBold", 10)
        c.setFillColor(BLUE)
        c.drawString(48, HEIGHT - 38, "PAWGRAM")
        c.setFont("PawgramRegular", 8)
        c.setFillColor(MUTED)
        c.drawRightString(WIDTH - 48, HEIGHT - 38, title)
        c.setStrokeColor(BORDER)
        c.line(48, HEIGHT - 50, WIDTH - 48, HEIGHT - 50)
    c.setFont("PawgramRegular", 8)
    c.setFillColor(MUTED)
    c.drawString(48, 28, "Pawgram · Telegram Yönetim Konsolu")
    c.drawRightString(WIDTH - 48, 28, str(page_number))
    y = HEIGHT - 78


def new_page(title: str):
    c.showPage()
    background(title)


def ensure_space(required: float, title: str):
    if y - required < 55:
        new_page(title)


def heading(text: str, title: str, size: int = 20):
    global y
    ensure_space(size + 22, title)
    c.setFont("PawgramBold", size)
    c.setFillColor(TEXT)
    c.drawString(48, y, text)
    y -= size + 12


def paragraph(text: str, title: str, color=MUTED, width_chars: int = 92, gap: int = 9):
    global y
    lines = []
    for block in text.split("\n"):
        lines.extend(wrap(block, width=width_chars) or [""])
    ensure_space(len(lines) * 14 + gap, title)
    c.setFont("PawgramRegular", 10)
    c.setFillColor(color)
    for line in lines:
        c.drawString(48, y, line)
        y -= 14
    y -= gap


def card(number: str, name: str, description: str, title: str, accent=BLUE):
    global y
    lines = wrap(description, width=76)
    height = 48 + max(0, len(lines) - 1) * 12
    ensure_space(height + 10, title)
    c.setFillColor(PANEL)
    c.setStrokeColor(BORDER)
    c.roundRect(48, y - height + 12, WIDTH - 96, height, 8, fill=1, stroke=1)
    c.setFillColor(HexColor("#0D3C59"))
    c.circle(72, y - 10, 14, fill=1, stroke=0)
    c.setFont("PawgramBold", 10)
    c.setFillColor(accent)
    c.drawCentredString(72, y - 14, number)
    c.setFont("PawgramBold", 11)
    c.setFillColor(TEXT)
    c.drawString(96, y - 4, name)
    c.setFont("PawgramRegular", 9)
    c.setFillColor(MUTED)
    line_y = y - 20
    for line in lines:
        c.drawString(96, line_y, line)
        line_y -= 12
    y -= height + 10


def bullets(items: list[str], title: str):
    global y
    for item in items:
        lines = wrap(item, width=82)
        ensure_space(len(lines) * 13 + 7, title)
        c.setFillColor(GREEN)
        c.circle(55, y - 3, 2.5, fill=1, stroke=0)
        c.setFont("PawgramRegular", 9.5)
        c.setFillColor(TEXT)
        for index, line in enumerate(lines):
            c.drawString(66, y - index * 13, line)
        y -= len(lines) * 13 + 6


# Kapak
background()
c.setFillColor(HexColor("#0D4A6E"))
c.roundRect(48, HEIGHT - 190, 74, 74, 17, fill=1, stroke=0)
c.setFillColor(BLUE)
for x, yy, radius in [(68, HEIGHT-140, 6), (85, HEIGHT-150, 6), (102, HEIGHT-140, 6), (76, HEIGHT-162, 6), (87, HEIGHT-177, 17)]:
    c.circle(x, yy, radius, fill=1, stroke=0)
c.setFont("PawgramBold", 36)
c.setFillColor(TEXT)
c.drawString(48, HEIGHT - 260, "Pawgram")
c.setFont("PawgramRegular", 16)
c.setFillColor(BLUE)
c.drawString(48, HEIGHT - 290, "Kurulum ve Kullanım Kılavuzu")
c.setFont("PawgramRegular", 10)
c.setFillColor(MUTED)
c.drawString(48, HEIGHT - 320, "Windows teslim sürümü · v1.0")
c.setFillColor(PANEL)
c.setStrokeColor(BORDER)
c.roundRect(48, 120, WIDTH - 96, 120, 12, fill=1, stroke=1)
c.setFont("PawgramBold", 12)
c.setFillColor(GREEN)
c.drawString(68, 210, "Hızlı başlangıç")
c.setFont("PawgramRegular", 10)
c.setFillColor(TEXT)
for idx, line in enumerate([
    "1. Baslat.bat veya Pawgram.exe dosyasını açın.",
    "2. Yönetici parolanızı oluşturun.",
    "3. Telegram API bilgilerinizi panelden kaydedin.",
    "4. Telefon hesabınızı bağlayıp taramanızı oluşturun.",
]):
    c.drawString(68, 187 - idx * 19, line)

# Kurulum
new_page("Kurulum")
heading("1. Kurulum ve başlatma", "Kurulum")
paragraph("Pawgram teslim klasörü taşınabilir bir Windows uygulamasıdır. Python veya ek kütüphane kurmanız gerekmez. Klasörün tamamını aynı konumda tutun; özellikle _internal klasörünü silmeyin.", "Kurulum")
card("1", "Teslim klasörünü açın", "Pawgram klasörünü masaüstü veya belgeler gibi yazma izniniz olan bir konuma çıkarın.", "Kurulum")
card("2", "Baslat.bat dosyasına çift tıklayın", "Pawgram.exe başlatılır ve yönetim paneli varsayılan internet tarayıcınızda otomatik açılır.", "Kurulum")
card("3", "Windows güvenlik uyarısı", "İlk çalıştırmada güvenlik duvarı sorarsa yalnızca özel ağ erişimine izin vermeniz yeterlidir. Pawgram sadece 127.0.0.1 yerel adresinde çalışır.", "Kurulum", YELLOW)

# İlk kurulum
new_page("İlk kurulum")
heading("2. İlk kurulum sihirbazı", "İlk kurulum")
card("1", "Yönetici parolası", "En az 8 karakterli, tahmin edilmesi zor bir parola oluşturun. Parola tek yönlü olarak özetlenir ve açık biçimde saklanmaz.", "İlk kurulum")
card("2", "Telegram API bilgileri", "my.telegram.org adresinden aldığınız API ID ve API Hash değerlerini Ayarlar ekranına girin. API Hash şifreli saklanır.", "İlk kurulum")
card("3", "Telefon hesabı", "Session'lar ekranından numarayı uluslararası biçimde girin. Telegram uygulamasına gelen kodu ve varsa 2FA parolasını panelde tamamlayın.", "İlk kurulum")
paragraph("API Hash, doğrulama kodu ve 2FA parolası sistem loglarına yazılmaz.", "İlk kurulum", GREEN)

# Aktivite
new_page("Aktiflik taraması")
heading("3. Özel gruplarda aktif kullanıcı taraması", "Aktiflik taraması")
paragraph("Aktiflik ekranı, seçilen Telegram hesabının erişebildiği gruptaki mesajları inceler ve zaman aralığı içinde mesaj atan benzersiz gerçek kullanıcıları listeler. Botlar ve silinmiş hesaplar sonuçlara dahil edilmez.", "Aktiflik taraması")
bullets([
    "Grup ID, @kullanıcı adı veya grup referansı kullanılabilir.",
    "Hesap özel grupta değilse t.me/+... ya da joinchat/... davet bağlantısını girin. Pawgram katılım isteğini gönderir ve Telegram'dan onayladığınızda otomatik devam eder.",
    "Filtreler: son 24 saat, 3 gün, 7 gün ve 30 gün.",
    "Her kullanıcı için mesaj sayısı ve son mesaj zamanı gösterilir.",
    "Sonuçlar panelde görüntülenebilir ve CSV olarak indirilebilir.",
], "Aktiflik taraması")
card("A", "Proaktif Round-Robin", "Session alanını otomatik bırakırsanız Pawgram işlem başlamadan önce günlük sabit kotayı kontrol eder ve sıradaki uygun hesabı Round-Robin yöntemiyle seçer.", "Aktiflik taraması")
card("B", "Otomatik tekrar", "Saatlik, 6 saatlik, 12 saatlik, günlük veya haftalık tekrar seçilebilir. Pawgram açık kaldığı sürece zamanlanan taramalar çalışır.", "Aktiflik taraması")

# Güvenlik
new_page("FloodWait ve hesap güvenliği")
heading("4. FloodWait ve hesap güvenliği", "FloodWait ve hesap güvenliği")
paragraph("Telegram FloodWait uyguladığında Pawgram ilgili session'ı belirtilen süre boyunca beklemeye alır. Aynı tarama limit aşmak amacıyla başka session'a aktarılmaz. Bekleme tamamlandığında zamanlanan tarama tekrar çalışabilir.", "FloodWait ve hesap güvenliği", YELLOW)
bullets([
    "Her session için günlük aktivite işlem kotası Ayarlar ekranından belirlenir. Varsayılan değer 30'dur.",
    "Yeni bağımsız taramalar, herhangi bir Telegram hatası oluşmadan önce Round-Robin sırasıyla dağıtılır.",
    "Session değişimi yalnızca işlem öncesi kota kontrolünde yapılır; catch/hata yönetimi içinde hesap değiştirilmez.",
    "Banlı, geçersiz veya yeniden doğrulama isteyen session'lar sağlık ekranında sorunlu görünür.",
    "Session sağlık puanı ve kalan FloodWait süresi Session'lar ekranında izlenir.",
    "Başlamış bir iş FloodWait veya hata nedeniyle başka session'a taşınmaz.",
], "FloodWait ve hesap güvenliği")

# Proxy
new_page("Session proxy yönetimi")
heading("5. İsteğe bağlı session proxy yönetimi", "Session proxy yönetimi")
paragraph("Ayarlar ekranındaki Session proxy yönetimi bölümüyle her Telegram hesabına ayrı ve sabit bir SOCKS5 veya HTTP CONNECT proxy atanabilir. Proxy kullanımı zorunlu değildir; yalnızca etkinleştirilen session'larda uygulanır.", "Session proxy yönetimi")
bullets([
    "Proxy türü, sunucu, port ve isteğe bağlı kullanıcı adı/parola panelden girilir.",
    "Proxy kullanıcı adı ve parolası yerel veritabanında şifreli saklanır.",
    "Bağlantıyı test et düğmesi Telegram veri merkezine tünel bağlantısını ve gecikmeyi ölçer.",
    "Proxy etkinse bağlantı başarısız olduğunda işlem durur; doğrudan bağlantıya geri dönülmez.",
    "Proxy işlem sırasında, FloodWait sonrasında veya hata yönetimi içinde otomatik değiştirilmez.",
], "Session proxy yönetimi")
card("!", "Sabit eşleştirme", "Aynı session için sürekli değişen proxy kullanmayın. Güvenilir ve sabit bir proxy tercih edin.", "Session proxy yönetimi", YELLOW)

# Üye önizleme
new_page("Grup aktarım önizlemesi")
heading("6. Grup aktarım önizlemesi", "Grup aktarım önizlemesi")
paragraph("Çekilecek ve gönderilecek grup alanları doğrulandıktan sonra Pawgram aday üyeleri analiz eder. Hedef grupta bulunanlar, botlar, silinmiş hesaplar, kaynak grubun sahibi/yöneticileri ve daha önce onaylanmış kullanıcılar otomatik olarak ayrılır.", "Grup aktarım önizlemesi")
card("1", "Yeni aktarım oluşturun", "Telegram hesabı, çekilecek grup, gönderilecek grup, koruma limitleri ve çalışma saatlerini belirleyin.", "Grup aktarım önizlemesi")
card("2", "Önizleme çalıştırın", "Pawgram adayları gerçek işlem yapmadan analiz eder ve uygunluk nedenlerini gösterir.", "Grup aktarım önizlemesi")
card("3", "Sonucu onaylayın", "Yönetici onayı açıkça kaydedilir. Onay Telegram limitlerini değiştirmez ve tek başına kullanıcı daveti göndermez.", "Grup aktarım önizlemesi")
card("4", "Global geçmiş filtresi", "Onaylanan uygun adaylar Pawgram geçmişine kaydedilir ve sonraki JOB önizlemelerinde yeniden uygun aday olarak gösterilmez.", "Grup aktarım önizlemesi")

# Rapor/yedek
new_page("Raporlama ve yedekleme")
heading("7. CSV raporları ve yedekleme", "Raporlama ve yedekleme")
bullets([
    "Aktiflik sonuçları SCAN numarası üzerinden CSV olarak indirilebilir.",
    "Grup aktarım adayları JOB numarası üzerinden CSV olarak indirilebilir.",
    "CSV dosyaları Excel ile uyumlu UTF-8 BOM biçiminde hazırlanır.",
    "Ayarlar > Yedekleme bölümündeki Yeni yedek oluştur düğmesi tüm yerel veritabanının kopyasını alır.",
    "data klasörü session'lar, ayarlar, raporlar ve loglar için kritik verileri içerir; düzenli yedek alın.",
], "Raporlama ve yedekleme")

# Sorun giderme
new_page("Sorun giderme")
heading("8. Sorun giderme", "Sorun giderme")
card("?", "Tarayıcı açılmadı", "Pawgram çalışırken tarayıcıdan http://127.0.0.1:8000 adresini açın. Port doluysa uygulama sonraki uygun portu otomatik seçer.", "Sorun giderme")
card("?", "Grup bulunamadı", "Özel gruba katılım isteği için yalnızca grup ID'si yeterli değildir; t.me/+... veya joinchat/... davet bağlantısını kullanın. Onayı Telegram uygulamasından verdikten sonra Pawgram otomatik kontrol eder.", "Sorun giderme")
card("?", "Kod gelmedi", "Telefon numarasını ülke koduyla girin, Telegram uygulamasındaki servis mesajlarını kontrol edin ve kısa süre içinde tekrar tekrar kod istemeyin.", "Sorun giderme")
card("?", "FloodWait görünüyor", "Bekleme süresinin tamamlanmasını bekleyin. Session'ı silmek veya aynı işi başka hesapla devam ettirmek hesap güvenliğini azaltabilir.", "Sorun giderme")
card("?", "Proxy testi başarısız", "Proxy adresi, portu ve kimlik bilgilerini kontrol edin. Proxy etkin olduğu sürece Pawgram doğrudan bağlantıya düşmez; proxy düzeltilene veya panelden kapatılana kadar session işlemleri durur.", "Sorun giderme")

# Son
new_page("Güvenli kullanım")
heading("9. Güvenli ve sorumlu kullanım", "Güvenli kullanım")
paragraph("Pawgram yalnızca yönetme ve erişim yetkiniz bulunan Telegram topluluklarında kullanılmalıdır. Kullanıcı verilerini üçüncü kişilerle paylaşmayın; CSV ve yedek dosyalarını güvenli bir yerde saklayın. Telegram'ın kullanım koşulları, gizlilik ayarları ve API sınırlamaları her zaman geçerlidir.", "Güvenli kullanım")
paragraph("Pawgram, Telegram tarafından uygulanan FloodWait veya hesap kısıtlamalarını aşmak için tasarlanmamıştır.", "Güvenli kullanım", YELLOW)
c.setFont("PawgramBold", 18)
c.setFillColor(BLUE)
c.drawString(48, y - 20, "Pawgram hazır.")
c.setFont("PawgramRegular", 10)
c.setFillColor(MUTED)
c.drawString(48, y - 42, "Baslat.bat dosyasını açarak yönetim paneline erişebilirsiniz.")

c.save()
print(OUTPUT)
