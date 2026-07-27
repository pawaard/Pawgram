# Pawgram

Telethon tabanlı, çoklu Telegram hesabı ve grup doğrulama yönetim paneli.

## Ticari lisans sistemi

Pawgram 0.2.0 ile ayrı lisans sunucusu, haftalık/aylık/üç aylık kod üretimi, ilk aktivasyonda başlayan süre, cihaz sınırı, cihaz sıfırlama, lisans iptali ve süre uzatma desteği içerir. Lisans sunucusu Ed25519 ile kısa ömürlü kullanım belgesi imzalar; imza private key'i müşteri paketine girmez. Ticari derlemede lisans zorunluluğu EXE içine alınır ve `.env` dosyasından kapatılamaz.

Kişisel kaynak sürümü mevcut sahibin kullanımını kilitlememek için lisanssız çalışır. Ticari dağıtım, HTTPS alan adı hazırlandıktan sonra `scripts/build_commercial.ps1` ile ayrı ve temiz bir pakete dönüştürülür. Sunucu kurulumu ve güvenlik şartları `license_server/README.md` dosyasında açıklanmıştır.

## Otomatik güncelleme

Python gerektirmeyen Windows sürümü açılışta resmi GitHub Releases alanındaki `pawgram-update.json` manifestini kontrol eder. Yalnızca SHA-256 özeti eşleşen ve Pawgram Ed25519 anahtarıyla imzalanmış paketler kurulur. Güncelleme sırasında `data` klasörü, veritabanı, Telegram session bilgileri, API ayarları ve proxy parolaları korunur. Kurulum başarısız olursa eski sürüm geri yüklenir ve ayrıntılar `data/update.log` dosyasına yazılır.

Güncelleme kontrolünü geçici olarak kapatmak için `PAWGRAM_SKIP_UPDATE=1` ortam değişkeni kullanılabilir. Kaynak kod sürümü otomatik EXE güncellemesi çalıştırmaz.

Bu ilk sürüm şunları içerir:

- Sınırsız sayıda telefon/session kaydı
- Telegram kodu ve 2FA ile güvenli giriş
- Telegram API ID ve API Hash'i doğrudan panelden şifreli kaydetme
- Şifrelenmiş telefon ve Telethon session verisi
- Grup ID, `@kullaniciadi` veya herkese açık `t.me` bağlantısı doğrulama
- Hesabın erişebildiği grupları listeleme
- Çekilecek/gönderilecek grup doğrulamalı iş kuyruğu
- Test/önizleme modu
- FloodWait algılandığında hesabı zorunlu beklemeye alma
- Session, grup ve kuyruk logları
- İlk kullanım kurulum sihirbazı
- Yönetici parolası ve güvenli HttpOnly oturum çerezi
- Aday üye önizleme ve uygunluk sınıflandırması
- Bot, silinmiş hesap ve hedef grupta bulunanları otomatik ayıklama
- Grup davet yetkisi kontrolü
- Açık yönetici onayı
- Planlanan başlangıç ve çalışma saatleri
- Session sağlık puanı ve FloodWait geri sayımı
- Bildirim merkezi
- Tek tıkla SQLite yedeği ve indirme
- Ayrıntılı CSV aday raporu
- Her Telegram hesabına özel, şifreli ve sabit SOCKS5/HTTP proxy
- TXT dosyasından boş hesaplara sırayla toplu proxy dağıtımı
- Proxy yoksa veya yanıt vermiyorsa ana IP'ye dönmeden işlemi durdurma
- Seçili uygun adayları Telegram'ın resmi gruba üye ekleme isteğiyle hedef gruba ekleme
- Üç başarılı eklemeden sonra 30 dakikalık parti beklemesi
- PeerFlood alan hesabı 24 saat dinlendirme ve kalan adayları koruma

## Güncel davet akışı

Panelde önizlenen adaylardan yalnızca açıkça seçilip onaylanan kişiler, iş oluşturulurken seçilen sabit session üzerinden hedef gruba davet edilir. Günlük kota varsayılan olarak 30'dur. Kota dolduğunda kalan adaylar bekler; FloodWait, PeerFlood veya kullanıcı gizlilik kısıtı gibi kritik Telegram cevaplarında iş durur ve başka session'a aktarılmaz. Başarılı davetler üye geçmişine kaydedilir.

Her session için sabit proxy zorunludur. Proxy, Telegram işleminden önce test edilir. Bağlantı kurulamazsa session `proxy_error` durumuna alınır ve uygulama sunucunun ana IP adresi üzerinden bağlantı denemez. Ayarlar ekranındaki **Toplu Proxy Ekle** alanı `host:port`, `host:port:user:pass`, `user:pass@host:port` ve URL biçimlerini kabul eder.

## Kurulum

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

En kolay yöntem, uygulamadaki **Ayarlar → Telegram API bağlantısı** bölümüne [my.telegram.org](https://my.telegram.org) üzerinden aldığınız API ID ve API Hash değerlerini girmektir. Dosya düzenlemeniz gerekmez.

İleri seviye sunucu kurulumu için değerler isteğe bağlı olarak `.env` dosyasından da verilebilir:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
APP_SECRET_KEY=uzun-rastgele-ve-gizli-bir-deger
```

Uygulamayı başlatın:

```powershell
.\.venv\Scripts\python.exe run.py
```

Ardından [http://127.0.0.1:8000](http://127.0.0.1:8000) adresini açın.

İlk açılışta Pawgram sırasıyla:

1. Yönetici parolası oluşturmanızı ister.
2. Telegram API bilgilerini panelden kaydetmenizi sağlar.
3. İlk telefon hesabını bağlama ekranına yönlendirir.
4. Çekilecek ve gönderilecek grupları doğrular.
5. Adayları gerçek işlem yapmadan önizler.
6. Sonuçların yönetici tarafından açıkça onaylanmasını ister.

## Güvenlik davranışı

- Telegram'ın bildirdiği FloodWait süresi değiştirilmeden uygulanır.
- Bir iş, beklemeyi aşmak için otomatik olarak başka hesaba taşınmaz.
- PeerFlood alan hesap 24 saat çalıştırılmaz.
- Üç başarılı üye eklemeden sonra aynı session 30 dakika bekletilir.
- Proxy yoksa veya çalışmıyorsa Telegram istemcisi başlatılmaz ve doğrudan IP bağlantısı yapılmaz.
- API anahtarı, doğrulama kodu, 2FA parolası ve açık session verisi loglanmaz.
- `.env` ve yerel veritabanı Git'e dahil edilmez.
- Üretimde güçlü ve değişmeyen bir `APP_SECRET_KEY` kullanılmalıdır. Bu anahtar değiştirilirse mevcut şifrelenmiş session kayıtları okunamaz.

## Proje yapısı

```text
app/
  config.py             Ortam ayarları
  database.py           SQLite şeması ve loglama
  main.py               FastAPI uçları
  schemas.py            İstek doğrulama modelleri
  security.py           Session/telefon şifreleme
  telegram_service.py   Telethon bağlantı katmanı
static/
  index.html             Yönetim paneli
  styles.css             Koyu tema
  app.js                 API bağlantıları ve arayüz akışları
```

## Sonraki geliştirme

1. Düşük hacimli kontrollü davet çalıştırıcısı
2. Kalıcı arka plan worker'ı ve işlerin yeniden başlatma sonrası devamı
3. Çok yöneticili rol sistemi ve ayrıntılı denetim kayıtları
4. PostgreSQL ve Redis dağıtımı
