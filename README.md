# ⚡ Shopee Flash Sale Sniper Bot v4 (Ultimate Edition)

Bot auto-serbu flash sale Shopee — multi-account, always-on monitor, keyword filter, SQLite DB, Docker support!

**v4 New Features:**
- 📂 **Multi-Account** — scan `accounts/` directory, run all accounts in parallel
- 📋 **YAML Config** — `config.yaml` externalizes all settings
- 🔍 **Keyword Filter** — include/exclude keywords for item names
- 🗄️ **SQLite Database** — purchase history tracking with `shopee_sniper.db`
- 👁️ **Monitor Mode** (`--monitor`) — always-on price checking, auto-buy on price drop
- 📝 **File-based Logging** — rotating logs in `logs/sniper.log`
- 🐳 **Docker Support** — Dockerfile + docker-compose.yml
- 🔄 Proxy rotation, Telegram notifications, stats tracking (from v3)

## 🚀 Quick Start

### 1. Install
```bash
git clone https://github.com/setan21/shopee-flash-sale-bot.git
cd shopee-flash-sale-bot
pip install -r requirements.txt
```

### 2. Setup Accounts

Create subdirectories in `accounts/` — one per Shopee account:

```
accounts/
├── alice/
│   └── cookies.json
├── bob/
│   ├── cookies.json
│   └── config.yaml      # (optional) per-account overrides
└── charlie/
    └── cookies.json
```

Each `cookies.json` is exported via Cookie-Editor Chrome extension (Export → JSON).

### 3. Configure

Edit `config.yaml` to your liking. Key settings:

```yaml
cookie_dir: "accounts"
default_max_price: 1000
scan_pages: 5
concurrent_requests: 5

# Keyword filter (empty = include all)
include_keywords: ["iphone", "airpods"]
exclude_keywords: ["case", "charger"]

# Monitor mode
monitor_interval: 60
monitor_price_drop_threshold: 0.9

# Telegram notifications
telegram_bot_token: ""
telegram_chat_id: ""
```

### 4. Run

**Auto-scan + snipe with all accounts:**
```bash
python3 flash_sale_v4.py
```

**Run a single account:**
```bash
python3 flash_sale_v4.py --account alice
```

**Monitor mode (always-on price checking):**
```bash
python3 flash_sale_v4.py --monitor
```

**Manual mode (single product):**
```bash
python3 flash_sale_v4.py "https://shopee.co.id/..." 1735683600 --cookie cookies.json
```

### 5. Docker

```bash
docker-compose up -d
```

## ⚙️ Config Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `cookie_dir` | accounts | Directory with account subfolders |
| `default_max_price` | 1000 | Max item price (Rp) |
| `scan_pages` | 5 | Flash sale pages to scan |
| `concurrent_requests` | 5 | Parallel checkout requests |
| `request_delay` | 0.02 | Delay between requests (seconds) |
| `subtract_seconds` | 0.5 | Fire N seconds before official start |
| `max_retries` | 10 | Max retry attempts |
| `payment_channel_id` | 8001400 | ShopeePay default |
| `include_keywords` | [] | Required keywords (empty = all) |
| `exclude_keywords` | [] | Banned keywords (empty = none) |
| `proxy_file` | proxies.txt | Proxy list file |
| `proxy_protocol` | http | Proxy protocol (http/socks5) |
| `proxy_rotate` | true | Rotate proxies per request |
| `telegram_bot_token` | "" | Telegram bot token |
| `telegram_chat_id` | "" | Telegram chat ID |
| `database` | shopee_sniper.db | SQLite DB path |
| `monitor_interval` | 60 | Monitor polling interval (seconds) |
| `monitor_price_drop_threshold` | 0.9 | Price drop ratio to trigger buy |
| `log_dir` | logs | Log file directory |
| `log_level` | INFO | Log level (DEBUG/INFO/WARNING/ERROR) |

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

## 📊 Database

Purchase attempts are logged to `shopee_sniper.db` with:
- item_id, shop_id, model_id
- item_name, price
- account_name
- success/failure + error message
- latency (ms)
- timestamp (WIB)
- mode (auto/manual/monitor)

Check recent purchases:
```bash
sqlite3 shopee_sniper.db "SELECT * FROM purchases ORDER BY id DESC LIMIT 10;"
```

## 🎯 Tips

- ⏱️ NTP sync = timing akurat ±5ms
- 🚀 Concurrent checkout = peluang lebih besar
- 🔄 Proxy rotation = hindari IP ban
- 📂 Multi-account = snipe dari banyak akun sekaligus
- 🍪 Cookie harus fresh (re-export kalau expired)
- 👁️ Monitor mode = auto-buy when price drops
