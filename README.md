# ⚡ Shopee Flash Sale Sniper Bot v3 (Enhanced Fork)

Bot auto-serbu flash sale Shopee — bisa cari sendiri produk Rp 1!

**Fork dari [Muklaszin/shopee-flash-sale-bot](https://github.com/Muklaszin/shopee-flash-sale-bot)** — ditambah fitur baru.

## ✨ Yang Baru di v3

| Fitur | Deskripsi |
|-------|-----------|
| 🔄 **Proxy Rotation** | Rotasi proxy otomatis hindari IP ban |
| 📱 **Telegram Notif** | Notifikasi sukses/gagal via Telegram bot |
| 📊 **Stats Tracking** | Track success rate, latency, proxy rotations |
| 🛡️ **Rate Limit Handling** | Deteksi rate limit + auto-cooldown + proxy switch |
| 🔁 **Exponential Backoff** | Retry dengan delay yang naik bertahap |

## 🚀 Quick Start

### 1. Install
```bash
git clone https://github.com/setan21/shopee-flash-sale-bot.git
cd shopee-flash-sale-bot
pip install -r requirements.txt
```

### 2. Ambil Cookie Shopee
1. Login **shopee.co.id** di Chrome
2. Install ekstensi **Cookie-Editor** (by cgagnier)
3. Klik icon Cookie-Editor → **Export** → **JSON**
4. Simpan sebagai `cookies.json`

### 3. (Opsional) Setup Proxy
Buat file `proxies.txt` — satu proxy per baris:
```
ip1:port1
ip2:port2
user:pass@ip3:port3
```

### 4. (Opsional) Setup Telegram Notif
Di `flash_sale_v3.py`:
```python
TELEGRAM_BOT_TOKEN = "your_bot_token"  # dari @BotFather
TELEGRAM_CHAT_ID = "your_chat_id"       # dari @userinfobot
```

### 5. Jalankan

**Mode Auto-Scan** (bot cari produk murah sendiri):
```bash
python3 flash_sale_v3.py
```

**Mode Manual** (kasih link produk):
```bash
python3 flash_sale_v3.py "https://shopee.co.id/Produk-Name-i.123456.789012"
```

**Mode Manual + Timer:**
```bash
python3 flash_sale_v3.py "https://shopee.co.id/..." 1735683600
```

## ⚙️ Config

| Variable | Default | Deskripsi |
|----------|---------|-----------|
| `AUTO_SCAN` | True | Bot cari produk murah otomatis |
| `MAX_PRICE` | 1000 | Harga maksimum Rp |
| `SCAN_PAGES` | 5 | Halaman flash sale di-scan |
| `QUANTITY` | 1 | Jumlah item dibeli |
| `CONCURRENT_REQUESTS` | 5 | Request checkout paralel |
| `PAYMENT_CHANNEL_ID` | 8001400 | ShopeePay |
| `PROXY_FILE` | proxies.txt | File daftar proxy |
| `PROXY_ROTATE` | True | Rotasi proxy per request |

## 💳 Payment Channels

| Channel | ID |
|---------|-----|
| ShopeePay | 8001400 |
| COD | 89000 |
| Transfer Bank | 8005200 |
| BCA | 89052001 |
| Mandiri | 89052002 |
| BNI | 89052003 |
| BRI | 89052004 |

## 🎯 Tips Menang

- ⏱️ NTP sync = timing akurat ±5ms
- 🚀 5 concurrent checkout = peluang 5x lebih besar
- 🔄 Proxy rotation = hindari IP ban
- 📡 Direct API = jauh lebih cepat dari browser
- 🍪 Cookie harus fresh (re-export kalau expired)
- 📱 Telegram notif = tau hasilnya real-time

## 📄 License

MIT — Fork dari [Muklaszin/shopee-flash-sale-bot](https://github.com/Muklaszin/shopee-flash-sale-bot)
