# Pawgram Lisans Sunucusu

Bu klasör müşteri paketine eklenmez. Lisans kararları ayrı bir sunucuda verilir; müşteriye yalnızca Pawgram uygulaması ve public doğrulama anahtarı dağıtılır.

## Güvenlik modeli

- 128 bitten yüksek rastgele lisans kodları
- Veritabanında lisans kodu yerine SHA-256 özeti
- Ed25519 ile imzalanmış, cihaz bağlı ve kısa ömürlü kullanım belgesi
- Varsayılan 24 saat çevrimdışı tolerans
- Cihaz sınırı, iptal, süre uzatma ve cihaz sıfırlama
- Aktivasyon denemelerine hız sınırı
- Yönetici işlemleri için yüksek entropili ayrı API anahtarı
- Telegram oturumu, telefon numarası veya API Hash lisans sunucusuna gönderilmez

## Yerel çalıştırma

Proje kökünde:

```powershell
$env:PYTHONPATH = ".packages"
python -m license_server.run_server
```

Ardından `http://127.0.0.1:8010` adresi açılır ve `license_server/.env` içindeki yönetici anahtarı girilir.

## Üretim şartları

1. Lisans sunucusu müşterinin bilgisayarından ayrı bir VPS üzerinde çalışmalıdır.
2. Alan adı ve geçerli TLS sertifikası kullanılmalıdır; istemcide `LICENSE_SERVER_URL=https://license.example.com` tanımlanmalıdır.
3. `license_server/data/signing_key.pem` ve `license_server/.env` yalnızca sunucuda tutulmalı, erişimleri kısıtlanmalıdır.
4. İmza özel anahtarının şifreli çevrimdışı yedeği alınmalıdır. Anahtar kaybolursa mevcut çevrimdışı belgeler yenilenemez.
5. Reverse proxy üzerinde ek IP hız sınırı, güvenlik duvarı ve düzenli veritabanı yedeği yapılandırılmalıdır.
6. GitHub token’ı veya imza özel anahtarı Pawgram EXE içine kesinlikle gömülmemelidir.

## Ticari paket

Ticari EXE hazırlanırken aşağıdaki değerler yapılandırılır:

```env
LICENSE_REQUIRED=true
LICENSE_SERVER_URL=https://lisans-alan-adiniz.example
```

Kişisel geliştirme sürümünde `LICENSE_REQUIRED=false` bırakılır. Bu, mevcut sahibin kurulumunu lisans sunucusu devreye alınmadan kilitlememek içindir.
