# Pawgram

Telethon tabanlı, çoklu Telegram hesabı ve grup doğrulama yönetim paneli.

## Ticari lisans sistemi

Pawgram 0.2.0 ile ayrı lisans sunucusu, haftalık/aylık/üç aylık kod üretimi, ilk aktivasyonda başlayan süre, cihaz sınırı, cihaz sıfırlama, lisans iptali ve süre uzatma desteği içerir. Lisans sunucusu Ed25519 ile kısa ömürlü kullanım belgesi imzalar; imza private key'i müşteri paketine girmez. Ticari derlemede lisans zorunluluğu EXE içine alınır ve `.env` dosyasından kapatılamaz.

Kişisel kaynak sürümü mevcut sahibin kullanımını kilitlememek için lisanssız çalışır. Ticari dağıtım, HTTPS alan adı hazırlandıktan sonra `scripts/build_commercial.ps1` ile ayrı ve temiz bir pakete dönüştürülür. Sunucu kurulumu ve güvenlik şartları `license_server/README.md` dosyasında açıklanmıştır.

## Otomatik güncelleme

Pawgram başlatıcısı, uygulama açılırken özel `pawaard/Pawgram` deposunun `main` dalını kontrol eder. Yeni commit varsa yalnızca güvenli bir `fast-forward` güncellemesi uygular. Yerel kaynak kodunda değişiklik bulunursa kullanıcı dosyalarını korumak için güncellemeyi atlar. Güncelleme sorunu uygulamanın açılmasını engellemez ve ayrıntılar `data/update.log` dosyasına yazılır.

Özel depoya erişim GitHub CLI/Git Credential Manager oturumuyla sağlanır. Geçici olarak güncelleme kontrolünü kapatmak için `PAWGRAM_SKIP_UPDATE=1` ortam değişkeni kullanılabilir. Veritabanı, Telegram session dosyaları, API bilgileri, proxy parolaları ve diğer çalışma verileri Git deposuna dahil edilmez.

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

> Gerçek kullanıcı davet çalıştırıcısı bu sürümde bilinçli olarak etkin değildir. İşler doğrulanır, adaylar önizlenir ve yönetici onayı kaydedilir. Son çalıştırma katmanı Telegram limitlerine uygun biçimde ayrıca etkinleştirilecektir.

## Güncel davet akışı

Panelde önizlenen adaylardan yalnızca açıkça seçilip onaylanan kişiler, iş oluşturulurken seçilen sabit session üzerinden hedef gruba davet edilir. Günlük kota varsayılan olarak 30'dur. Kota dolduğunda kalan adaylar bekler; FloodWait, PeerFlood veya kullanıcı gizlilik kısıtı gibi kritik Telegram cevaplarında iş durur ve başka session'a aktarılmaz. Başarılı davetler üye geçmişine kaydedilir.

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
