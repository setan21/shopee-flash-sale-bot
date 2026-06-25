"""
Shopee Flash Sale Sniper Bot v3 — Enhanced Fork
New features added:
- 🔄 Proxy rotation support (avoid IP bans)
- 📱 Telegram notifications (success/fail alerts)
- 🕐 Scheduled snipe (auto-wait for multiple flash sales)
- 📊 Statistics tracking (success rate, latency)
- 🔁 Auto-retry with exponential backoff
- 🛡️ Rate limit detection + auto-cooldown
"""

import json, time, hashlib, asyncio, aiohttp, ntplib, sys, os, re, random
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

COOKIE_FILE = "cookies.json"
PRODUCT_URL = ""
TARGET_TIMESTAMP = 0
QUANTITY = 1
SUBTRACT_SECONDS = 0.5
CONCURRENT_REQUESTS = 5
REQUEST_DELAY = 0.02
MAX_RETRIES = 10
PAYMENT_CHANNEL_ID = 8001400  # ShopeePay
ADDRESS_ID = 0

# Auto-scan config
AUTO_SCAN = True
MAX_PRICE = 1000
SCAN_PAGES = 5

# Proxy config (NEW)
PROXY_FILE = "proxies.txt"        # One proxy per line: ip:port or user:pass@ip:port
PROXY_PROTOCOL = "http"           # http or socks5
PROXY_ROTATE = True               # Rotate proxy per request
PROXY = ""                        # Single proxy override (leave empty to use file)

# Telegram config (NEW)
TELEGRAM_BOT_TOKEN = ""           # @BotFather token
TELEGRAM_CHAT_ID = ""             # Your chat ID

# ═══════════════════════════════════════════════════════════
# SHOPEE API ENDPOINTS
# ═══════════════════════════════════════════════════════════

BASE = "https://shopee.co.id"
API = {
    "item_info":      f"{BASE}/api/v2/item/get",
    "flash_sale":     f"{BASE}/api/v4/flash_sale/flash_sale_batch_get_items",
    "flash_sessions": f"{BASE}/api/v4/flash_sale/get_all_sessions",
    "account_info":   f"{BASE}/api/v2/user/account_info",
    "addresses":      f"{BASE}/api/v1/addresses",
    "add_cart":       f"{BASE}/api/v4/cart/add_to_cart",
    "checkout_get":   f"{BASE}/api/v4/checkout/get_quick",
    "place_order":    f"{BASE}/api/v4/checkout/place_order",
}

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://shopee.co.id/",
    "Origin": "https://shopee.co.id",
    "X-Requested-With": "XMLHttpRequest",
    "X-API-Source": "pc",
    "X-Shopee-Language": "id",
    "af-ac-enc-dat": "null",
}

# ═══════════════════════════════════════════════════════════
# STATISTICS TRACKER (NEW)
# ═══════════════════════════════════════════════════════════

@dataclass
class Stats:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    rate_limits: int = 0
    total_latency: float = 0
    start_time: float = field(default_factory=time.time)
    proxy_rotations: int = 0

    def record(self, success: bool, latency: float = 0, rate_limited: bool = False):
        self.attempts += 1
        if success:
            self.successes += 1
            self.total_latency += latency
        else:
            self.failures += 1
        if rate_limited:
            self.rate_limits += 1

    def summary(self) -> str:
        elapsed = time.time() - self.start_time
        avg_lat = (self.total_latency / self.successes * 1000) if self.successes else 0
        return (
            f"📊 Stats: {self.attempts} attempts | "
            f"✅ {self.successes} success | ❌ {self.failures} fail | "
            f"🚫 {self.rate_limits} rate-limited | "
            f"⚡ {avg_lat:.0f}ms avg latency | "
            f"🔄 {self.proxy_rotations} proxy rotations | "
            f"⏱️ {elapsed:.0f}s total"
        )

stats = Stats()

# ═══════════════════════════════════════════════════════════
# PROXY MANAGER (NEW)
# ═══════════════════════════════════════════════════════════

class ProxyManager:
    def __init__(self, proxy_file: str = "", protocol: str = "http", single_proxy: str = ""):
        self.proxies: list[str] = []
        self.current_idx = 0
        self.protocol = protocol
        self.dead_proxies: set[str] = set()

        if single_proxy:
            self.proxies = [single_proxy]
        elif proxy_file and os.path.exists(proxy_file):
            with open(proxy_file) as f:
                self.proxies = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            random.shuffle(self.proxies)
            print(f"🔄 Loaded {len(self.proxies)} proxies from {proxy_file}")

    def get_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        alive = [p for p in self.proxies if p not in self.dead_proxies]
        if not alive:
            self.dead_proxies.clear()
            alive = self.proxies
        proxy = alive[self.current_idx % len(alive)]
        self.current_idx += 1
        return f"{self.protocol}://{proxy}"

    def mark_dead(self, proxy: str):
        clean = proxy.replace(f"{self.protocol}://", "")
        self.dead_proxies.add(clean)
        stats.proxy_rotations += 1

    @property
    def has_proxies(self) -> bool:
        return len(self.proxies) > 0

# ═══════════════════════════════════════════════════════════
# TELEGRAM NOTIFIER (NEW)
# ═══════════════════════════════════════════════════════════

class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        if self.enabled:
            print(f"📱 Telegram notifications enabled → chat {chat_id}")

    async def send(self, message: str, parse_mode: str = "HTML"):
        if not self.enabled:
            return
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                await session.post(url, json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                })
        except Exception as e:
            print(f"⚠️ Telegram error: {e}")

    async def notify_success(self, item_name: str, price: float, elapsed: float):
        msg = (
            f"⚡ <b>FLASH SALE BERHASIL!</b>\n\n"
            f"🛒 {item_name}\n"
            f"💰 Rp {price:,.0f}\n"
            f"⏱️ {elapsed:.2f}s\n\n"
            f"📊 {stats.summary()}"
        )
        await self.send(msg)

    async def notify_failure(self, item_name: str, errors: list):
        msg = (
            f"❌ <b>Flash Sale Gagal</b>\n\n"
            f"🛒 {item_name}\n"
            f"🚫 Errors: {', '.join(errors[:3])}\n\n"
            f"📊 {stats.summary()}"
        )
        await self.send(msg)

    async def notify_scan(self, items: list):
        if not items:
            return
        lines = [f"🔍 <b>Flash Sale Scan — {len(items)} item ditemukan:</b>\n"]
        for i, item in enumerate(items[:10], 1):
            lines.append(f"  {i}. Rp {item['price_idr']:,.0f} — {item['name'][:40]}")
        await self.send("\n".join(lines))

# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

def generate_if_none_match(body_str: str) -> str:
    h1 = hashlib.md5(body_str.encode()).hexdigest()
    inner = hashlib.md5(("55b03" + h1 + "55b03").encode()).hexdigest()
    return f"55b03-{inner}"

def get_ntp_offset() -> float:
    try:
        c = ntplib.NTPClient()
        resp = c.request("pool.ntp.org", version=3, timeout=5)
        return resp.offset
    except Exception as e:
        print(f"⚠️  NTP sync failed ({e}), using local time")
        return 0.0

def get_accurate_timestamp(offset: float) -> float:
    return time.time() + offset

def load_cookies(path: str) -> dict:
    with open(path, "r") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw}
    return raw

def cookie_string(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())

def get_csrf_token(cookies: dict) -> str:
    return cookies.get("csrftoken", "")

def parse_shopee_url(url: str) -> tuple:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "itemid" in params and "shopid" in params:
        return int(params["itemid"][0]), int(params["shopid"][0])
    match = re.search(r"i\.(\d+)\.(\d+)", url)
    if match:
        return int(match.group(2)), int(match.group(1))
    raise ValueError(f"Could not parse Shopee URL: {url}")

def format_price(price_raw: int) -> str:
    return f"Rp {price_raw / 100000:,.0f}"

# ═══════════════════════════════════════════════════════════
# SHOPEE API CLIENT (ENHANCED)
# ═══════════════════════════════════════════════════════════

class ShopeeClient:
    def __init__(self, cookies: dict, proxy_manager: Optional[ProxyManager] = None):
        self.cookies = cookies
        self.csrf_token = get_csrf_token(cookies)
        self.cookie_str = cookie_string(cookies)
        self.session = None
        self.address_id = ADDRESS_ID
        self.account_info = None
        self.proxy_manager = proxy_manager

    async def _init_session(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=10)
            self.session = aiohttp.ClientSession(
                headers={
                    **HEADERS_BASE,
                    "Cookie": self.cookie_str,
                    "X-Csrftoken": self.csrf_token,
                },
                timeout=timeout
            )

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        await self._init_session()
        body = kwargs.get("json", {})
        body_str = json.dumps(body, separators=(",", ":")) if body else url
        headers = kwargs.pop("headers", {})
        headers["If-None-Match-"] = generate_if_none_match(body_str)

        for attempt in range(3):
            proxy = None
            if self.proxy_manager and self.proxy_manager.has_proxies:
                proxy = self.proxy_manager.get_proxy()

            try:
                start = time.time()
                async with self.session.request(method, url, headers=headers, proxy=proxy, **kwargs) as resp:
                    latency = time.time() - start

                    if resp.status == 429:
                        stats.record(False, rate_limited=True)
                        if proxy:
                            self.proxy_manager.mark_dead(proxy)
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue

                    data = await resp.json()

                    # Check for Shopee rate limit in response body
                    if data.get("error") == 99999 or "rate" in str(data.get("error_msg", "")).lower():
                        stats.record(False, rate_limited=True)
                        if proxy:
                            self.proxy_manager.mark_dead(proxy)
                        await asyncio.sleep(1)
                        continue

                    stats.record(True, latency)
                    return data

            except Exception as e:
                if proxy:
                    self.proxy_manager.mark_dead(proxy)
                if attempt == 2:
                    stats.record(False)
                    return {"error": -1, "error_msg": str(e)}
                await asyncio.sleep(0.3)

    async def get_account_info(self) -> dict:
        data = await self._request("GET", API["account_info"] + "?skip_address=1")
        if data.get("error"):
            raise Exception(f"Auth failed: {data}")
        self.account_info = data.get("data", {})
        return self.account_info

    async def get_addresses(self) -> list:
        data = await self._request("GET", API["addresses"])
        addresses = data.get("data", {}).get("addresses", [])
        if addresses and not self.address_id:
            for addr in addresses:
                if addr.get("is_default"):
                    self.address_id = addr["addressid"]
                    break
            if not self.address_id and addresses:
                self.address_id = addresses[0]["addressid"]
        return addresses

    async def get_item_info(self, item_id: int, shop_id: int) -> dict:
        url = f"{API['item_info']}?itemid={item_id}&shopid={shop_id}"
        return await self._request("GET", url)

    async def get_flash_sale_sessions(self) -> list:
        all_sessions = []
        for page in range(SCAN_PAGES):
            offset = page * 20
            url = f"{API['flash_sessions']}?limit=20&offset={offset}&need_items=1&with_dp_items=1"
            data = await self._request("GET", url)
            sessions = data.get("data", {}).get("sessions", [])
            if not sessions:
                break
            all_sessions.extend(sessions)
            await asyncio.sleep(0.3)
        return all_sessions

    async def get_flash_sale_items(self, session_id: int, item_ids: list) -> list:
        ids_str = ",".join(str(i) for i in item_ids[:50])
        url = f"{API['flash_sale']}?session_id={session_id}&item_ids={ids_str}&need_detail=1"
        data = await self._request("GET", url)
        return data.get("data", {}).get("items", [])

    async def add_to_cart(self, item_id: int, shop_id: int, model_id: int, qty: int = 1) -> dict:
        body = {
            "checkout": True,
            "client_source": 1,
            "donot_add_checkout": 0,
            "itemid": item_id,
            "modelid": model_id,
            "quantity": qty,
            "shopid": shop_id,
        }
        return await self._request("POST", API["add_cart"], json=body)

    async def get_checkout(self, item_id: int, shop_id: int, model_id: int, qty: int) -> dict:
        body = {
            "selected_address_id": self.address_id,
            "shoporders": [{
                "shop": {"shopid": shop_id},
                "items": [{
                    "itemid": item_id,
                    "modelid": model_id,
                    "quantity": qty,
                }],
                "shipping": {"channel_id": 0},
            }],
            "channel_payment_option_list": [{"payment_channel_id": PAYMENT_CHANNEL_ID}],
        }
        return await self._request("POST", API["checkout_get"], json=body)

    async def place_order(self, checkout_data: dict) -> dict:
        return await self._request("POST", API["place_order"], json=checkout_data)

    async def close(self):
        if self.session:
            await self.session.close()

# ═══════════════════════════════════════════════════════════
# FLASH SALE SCANNER (ENHANCED)
# ═══════════════════════════════════════════════════════════

async def scan_flash_sale(client: ShopeeClient) -> list:
    """Scan flash sale sessions for items under MAX_PRICE."""
    print(f"\n🔍 Scanning flash sale (max price: Rp {MAX_PRICE:,})...")

    sessions = await client.get_flash_sale_sessions()
    if not sessions:
        print("❌ No flash sale sessions found.")
        return []

    print(f"📦 Found {len(sessions)} sessions")

    cheap_items = []
    for session in sessions:
        session_id = session.get("session_id", 0)
        start_time = session.get("start_time", 0)
        items = session.get("items", [])

        if not items:
            continue

        item_ids = [i.get("item_id", i.get("itemid", 0)) for i in items]
        detailed = await client.get_flash_sale_items(session_id, item_ids)

        for item in detailed:
            price = item.get("price", 0)
            price_idr = price / 100000 if price > 100000 else price
            stock = item.get("stock", 0)
            promo_id = item.get("promotion_id", 0)

            if price_idr <= MAX_PRICE and stock > 0:
                cheap_items.append({
                    "item_id": item.get("item_id", item.get("itemid", 0)),
                    "shop_id": item.get("shop_id", item.get("shopid", 0)),
                    "model_id": item.get("model_id", item.get("modelid", 0)),
                    "name": item.get("name", "Unknown"),
                    "price": price,
                    "price_idr": price_idr,
                    "stock": stock,
                    "start_time": start_time,
                    "session_id": session_id,
                    "promo_id": promo_id,
                })

    cheap_items.sort(key=lambda x: x["price_idr"])
    return cheap_items

# ═══════════════════════════════════════════════════════════
# CHECKOUT ENGINE (ENHANCED)
# ═══════════════════════════════════════════════════════════

async def single_checkout(client: ShopeeClient, item_id: int, shop_id: int, model_id: int, attempt: int) -> dict:
    try:
        cart = await client.add_to_cart(item_id, shop_id, model_id, QUANTITY)
        if cart.get("error"):
            return {"success": False, "error": f"cart: {cart.get('error')} - {cart.get('error_msg','')}", "attempt": attempt}

        checkout = await client.get_checkout(item_id, shop_id, model_id, QUANTITY)
        if checkout.get("error"):
            return {"success": False, "error": f"checkout: {checkout.get('error')}", "attempt": attempt}

        order = await client.place_order(checkout)
        if order.get("error"):
            err = order.get("error")
            if err in [2, 9, 110]:
                return {"success": False, "error": f"FATAL: {err}", "attempt": attempt, "fatal": True}
            return {"success": False, "error": f"order: {err}", "attempt": attempt}

        return {"success": True, "data": order, "attempt": attempt}
    except Exception as e:
        return {"success": False, "error": str(e), "attempt": attempt}

async def delayed_checkout(client, item_id, shop_id, model_id, delay, attempt):
    if delay > 0:
        await asyncio.sleep(delay)
    return await single_checkout(client, item_id, shop_id, model_id, attempt)

async def snipe_item(client: ShopeeClient, item: dict, ntp_offset: float, notifier: TelegramNotifier) -> bool:
    """Snipe a single flash sale item."""
    item_id = item["item_id"]
    shop_id = item["shop_id"]
    model_id = item["model_id"]
    name = item["name"][:60]
    price = item["price_idr"]
    start_time = item.get("start_time", 0)

    print(f"\n{'='*50}")
    print(f"🎯 Target: {name}")
    print(f"💰 Harga: Rp {price:,.0f}")
    print(f"📦 Item: {item_id} | Shop: {shop_id} | Model: {model_id}")

    # Wait for flash sale start time
    if start_time > 0:
        now = get_accurate_timestamp(ntp_offset)
        wait = start_time - SUBTRACT_SECONDS - now
        if wait > 0:
            wib = timezone(timedelta(hours=7))
            start_str = datetime.fromtimestamp(start_time, tz=wib).strftime('%H:%M:%S')
            print(f"⏳ Waiting until {start_str} WIB ({wait:.0f}s)...")

            while wait > 2:
                await asyncio.sleep(min(wait - 1, 2))
                now = get_accurate_timestamp(ntp_offset)
                wait = start_time - SUBTRACT_SECONDS - now

            # Precision wait
            while get_accurate_timestamp(ntp_offset) < start_time - SUBTRACT_SECONDS:
                await asyncio.sleep(0.001)

    print(f"🚀 Firing {CONCURRENT_REQUESTS} checkout requests...")

    start = time.time()
    tasks = [
        asyncio.create_task(delayed_checkout(client, item_id, shop_id, model_id, i * REQUEST_DELAY, i + 1))
        for i in range(CONCURRENT_REQUESTS)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.time() - start

    errors = []
    for r in results:
        if isinstance(r, dict) and r.get("success"):
            print(f"   ✅ SUKSES! ({elapsed:.2f}s)")
            await notifier.notify_success(name, price, elapsed)
            return True
        elif isinstance(r, dict):
            errors.append(r.get("error", "unknown"))

    for e in errors[:3]:
        print(f"   ❌ {e}")

    await notifier.notify_failure(name, errors)
    return False

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

async def run():
    print("=" * 50)
    print("⚡ SHOPEE FLASH SALE SNIPER BOT v3 (Enhanced)")
    print("=" * 50)

    # Load cookies
    if not os.path.exists(COOKIE_FILE):
        print(f"❌ Cookie file not found: {COOKIE_FILE}")
        print("   1. Login shopee.co.id di Chrome")
        print("   2. Install Cookie-Editor extension")
        print("   3. Export → JSON → simpan sebagai cookies.json")
        return

    cookies = load_cookies(COOKIE_FILE)
    print(f"✅ Loaded {len(cookies)} cookies")

    # Init proxy manager (NEW)
    proxy_mgr = ProxyManager(
        proxy_file=PROXY_FILE,
        protocol=PROXY_PROTOCOL,
        single_proxy=PROXY,
    )

    # Init Telegram notifier (NEW)
    notifier = TelegramNotifier(token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID)

    client = ShopeeClient(cookies, proxy_manager=proxy_mgr)

    # Verify auth
    try:
        info = await client.get_account_info()
        username = info.get("username", "unknown")
        print(f"👤 Logged in as: {username}")
    except Exception as e:
        print(f"❌ Auth failed: {e}")
        await client.close()
        return

    # Get addresses
    await client.get_addresses()
    if not client.address_id:
        print("❌ No shipping address! Add one in Shopee app.")
        await client.close()
        return
    print(f"📍 Address ID: {client.address_id}")

    # NTP sync
    print("\n🕐 Syncing NTP...")
    ntp_offset = get_ntp_offset()
    print(f"⏱️  Offset: {ntp_offset*1000:.1f}ms")

    items_to_snipe = []

    if PRODUCT_URL:
        item_id, shop_id = parse_shopee_url(PRODUCT_URL)
        item_info = await client.get_item_info(item_id, shop_id)
        models = item_info.get("item", {}).get("models", [])
        model_id = models[0]["modelid"] if models else 0
        price = models[0].get("price", 0) if models else 0
        name = item_info.get("item", {}).get("name", "Unknown")

        items_to_snipe.append({
            "item_id": item_id,
            "shop_id": shop_id,
            "model_id": model_id,
            "name": name,
            "price": price,
            "price_idr": price / 100000 if price > 100000 else price,
            "start_time": TARGET_TIMESTAMP if TARGET_TIMESTAMP > 0 else 0,
        })
        print(f"\n🎯 Manual target: {name}")

    elif AUTO_SCAN:
        items_to_snipe = await scan_flash_sale(client)

        if not items_to_snipe:
            print("\n😞 No cheap items found in flash sale.")
            print("   Try lowering MAX_PRICE or check back later.")
            await client.close()
            return

        # Notify via Telegram (NEW)
        await notifier.notify_scan(items_to_snipe)

        print(f"\n{'='*50}")
        print(f"🎯 Found {len(items_to_snipe)} items under Rp {MAX_PRICE:,}!")
        for i, item in enumerate(items_to_snipe, 1):
            wib = timezone(timedelta(hours=7))
            start = datetime.fromtimestamp(item['start_time'], tz=wib).strftime('%H:%M') if item.get('start_time') else '?'
            print(f"   {i}. Rp {item['price_idr']:,.0f} | {item['name'][:40]} | Starts: {start} WIB")
    else:
        print("❌ No product URL and AUTO_SCAN is disabled!")
        await client.close()
        return

    # Snipe all items
    success_count = 0
    for item in items_to_snipe:
        result = await snipe_item(client, item, ntp_offset, notifier)
        if result:
            success_count += 1

    # Summary
    print(f"\n{'='*50}")
    print(f"📊 Results: {success_count}/{len(items_to_snipe)} items purchased!")
    print(stats.summary())
    print(f"{'='*50}")

    await client.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        PRODUCT_URL = sys.argv[1]
    if len(sys.argv) > 2:
        TARGET_TIMESTAMP = int(sys.argv[2])
    if len(sys.argv) > 3:
        COOKIE_FILE = sys.argv[3]

    asyncio.run(run())
