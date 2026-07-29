# Pawgram

Pawgram; çoklu Telegram hesabı, güvenli proxy, üye daveti, aktivite taraması,
Heartbeat ve otomatik güncelleme özelliklerine sahip Windows yönetim uygulamasıdır.

## Müşteri sürümü

Müşteri paketi kaynak kod veya Python kurulumu gerektirmez:

1. `Pawgram-Customer-<version>-win64.zip` dosyasını indirin.
2. ZIP içindeki `Pawgram` klasörünü çıkarın.
3. `Pawgram.exe` dosyasına çift tıklayın.
4. Telegram hesabınızı telefon numarası ve Telegram doğrulama koduyla bağlayın.

Telegram API ve varsayılan proxy başlangıç ayarları müşteri paketinde hazırdır.
İlk açılışta yönetici parolası, API ID veya API Hash istenmez. Hiç session yoksa
yalnızca Telegram hesabını bağlamayı anlatan karşılama ekranı gösterilir.

Müşteri paketinin kökünde yalnızca şunlar bulunur:

- `Pawgram.exe`
- `_internal`
- `.env`

Veritabanı, session, log, cache, test, kaynak kod, imzalama anahtarı ve geliştirme
artefaktları dağıtım paketine eklenmez.

## Güvenli varsayılanlar

Yeni müşteri veritabanında:

- Davet gecikmesi: rastgele `20–40` saniye
- Session parti limiti: `3` başarılı davet
- Parti dinlenmesi: `20` dakika
- Günlük koruma limiti: `50`
- FloodWait ve PeerFlood koruması: etkin
- Heartbeat: kapalı

Gecikme her aday arasında güvenli rastgele sayı üreteciyle seçilir. Telegram'ın
bildirdiği FloodWait süresi kısaltılmaz veya atlanmaz.

## Davet session rotasyonu

Davet işlerinde session seçimi merkezi Round‑Robin seçiciyle yapılır. Son başarılı
session saklanır ve sonraki arama onun ardından başlar.

- FloodWait alan session kendi bekleme süresine alınır; kalan adaylar sıradaki uygun
  session ile işlenmeye devam eder.
- PeerFlood alan session 24 saat bekletilir; iş sıradaki uygun session'a geçer.
- Parti limitine ulaşan session `batch_wait` durumuna alınır ve 20 dakika sonra
  otomatik döner.
- Günlük davet kotası dolan session o gün için atlanır.
- Proxy hatası, devre dışı session ve aktif işlem kilidi bulunan session seçilmez.
- Yalnızca bütün session'lar kullanılamıyorsa iş en erken gerçek dönüş zamanına
  planlanır.

Aktivite taraması kendi uygunluk ve session kullanım kurallarını korur; davet
rotasyonunun FloodWait/parti devri tarama davranışını değiştirmez.

## Proxy güvenliği

Her Telegram hesabı sabit SOCKS5 veya HTTP proxy ile çalışır. Proxy, Telegram
istemcisi oluşturulmadan önce test edilir. Bağlantı başarısızsa session
`proxy_error` durumuna alınır ve uygulama ana IP üzerinden bağlantı kurmaz.

Yeni müşteri paketindeki varsayılan proxy, hesap ekleme ekranına otomatik gelir.
Müşteri gerekirse kendi proxy bilgileriyle değiştirebilir.

## Otomatik güncelleme

Windows EXE her açılışta GitHub Releases alanındaki
`pawgram-update.json` manifestini kontrol eder. Manifest Ed25519 ile imzalanır;
paketin SHA‑256 özeti ve resmi GitHub adresi doğrulanmadan kurulum yapılmaz.

Yeni sürüm bulunduğunda:

1. Paket otomatik indirilir.
2. Yalnızca `Pawgram.exe` ve `_internal` değiştirilir.
3. `.env` ve `data` klasörü korunur.
4. Yeni sürüm otomatik başlatılır.
5. Başlangıç health işareti doğrulanır.
6. Başlangıç başarısızsa eski EXE ve runtime geri yüklenip yeniden başlatılır.

Session'lar, proxy ayarları, gruplar, davet ayarları, Heartbeat, lisans ve yerel
tercihler güncelleme sırasında korunur. Ayrıntılar `data/update.log` dosyasına
yazılır.

Kaynak kodla geliştirme sırasında güncelleme kurulmaz. Geçici olarak kapatmak için
`PAWGRAM_SKIP_UPDATE=1` kullanılabilir.

## Release notes ve sürüm geçmişi

Sürüm geçmişi `RELEASE_NOTES.json` içinde tutulur ve Ayarlar ekranında gösterilir.
Uygulama yeni bir sürümle ilk kez açıldığında modern sürüm notları penceresi bir kez
gösterilir. Kullanıcı kapattıktan sonra aynı sürüm için yeniden açılmaz.

## Müşteri release oluşturma

Release yalnızca temiz ve commit edilmiş Git çalışma kopyasından oluşturulur.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_customer_release.ps1 `
  -PythonPath .\.venv\Scripts\python.exe `
  -DatabasePath .\data\console.db
```

Build sonunda müşteri ZIP'i içerik, Windows GUI subsystem, sürüm/publisher bilgisi,
zorunlu yapılandırma ve geliştirici yolu sızıntılarına karşı doğrulanır.

## İmzalı update paketi oluşturma

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_public_release.ps1 `
  -PythonPath <python.exe> `
  -SigningKeyPath .\license_server\data\signing_key.pem
```

Komut şunları üretir:

- `releases/Pawgram-<version>-win64.zip`
- `releases/pawgram-update.json`

Private signing key hiçbir release paketine veya Git deposuna eklenmez.

## Gerçek updater simülasyonu

Aşağıdaki doğrulama başarılı kurulum/restart ve bozuk paket rollback yollarını
çalıştırır; müşteri verilerini güncelleme öncesi ve sonrası karşılaştırır:

```powershell
.\.venv\Scripts\python.exe .\scripts\verify_update_simulation.py `
  --customer-zip .\releases\Pawgram-Customer-0.4.1-win64.zip `
  --update-zip .\releases\Pawgram-0.4.1-win64.zip
```

## Kaynak kodla geliştirme

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install `
  -r requirements.txt -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe run.py
```

Kaynak geliştirme sürümünde Telegram API bilgileri Ayarlar ekranından veya `.env`
üzerinden verilebilir. Panel varsayılan olarak
[http://127.0.0.1:8000](http://127.0.0.1:8000) adresinde açılır.

## Testler ve kalite kontrolleri

```powershell
.\.venv\Scripts\python.exe -m ruff check app license_server scripts tests run.py
.\.venv\Scripts\python.exe -m mypy app license_server scripts run.py --ignore-missing-imports
.\.venv\Scripts\python.exe -m bandit -r app license_server scripts run.py -q -ll
node --check static\app.js
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip_audit `
  -r requirements.txt -r requirements-build.txt --progress-spinner off
```

GitHub Actions aynı kontrolleri ve PyInstaller build'ini Windows üzerinde çalıştırır.
