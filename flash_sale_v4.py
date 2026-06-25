"""
Shopee Flash Sale Sniper Bot v4 — Ultimate Edition
New features:
- 📂 Multi-account support (accounts/ directory, parallel execution)
- 📋 YAML config file (config.yaml — all settings externalized)
- 🔍 Keyword filter (include/exclude keywords, case-insensitive)
- 🗄️ SQLite database (purchase history tracking)
- 👁️ Always-on monitor mode (--monitor flag, auto-buy on price drop)
- 📝 File-based logging (rotation, both console and file handlers)
- 🐳 Docker support (Dockerfile + docker-compose.yml)
"""

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

import aiohttp
import yaml

try:
    import ntplib as _ntplib
    HAS_NTP = True
except ImportError:
    HAS_NTP = False

import sqlite3
from functools import partial

# ═══════════════════════════════════════════════════════════
# SHOPEE API ENDPOINTS
# ═══════════════════════════════════════════════════════════

BASE = "https://shopee.co.id"
API = {
    "item_info":       f"{BASE}/api/v2/item/get",
    "flash_sale":      f"{BASE}/api/v4/flash_sale/flash_sale_batch_get_items",
    "flash_sessions":  f"{BASE}/api/v4/flash_sale/get_all_sessions",
    "account_info":    f"{BASE}/api/v2/user/account_info",
    "addresses":       f"{BASE}/api/v1/addresses",
    "add_cart":        f"{BASE}/api/v4/cart/add_to_cart",
    "checkout_get":    f"{BASE}/api/v4/checkout/get_quick",
    "place_order":     f"{BASE}/api/v4/checkout/place_order",
}

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
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
# CONFIG — YAML-based configuration with defaults
# ═══════════════════════════════════════════════════════════

@dataclass
class Config:
    """Loads configuration from YAML file with sensible defaults."""

    # Main settings
    cookie_dir: str = "accounts"
    default_max_price: int = 1000
    scan_pages: int = 5
    concurrent_requests: int = 5
    request_delay: float = 0.02
    subtract_seconds: float = 0.5
    max_retries: int = 10
    payment_channel_id: int = 8001400

    # Keyword filter
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)

    # Proxy
    proxy_file: str = "proxies.txt"
    proxy_protocol: str = "http"
    proxy_rotate: bool = True

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Database
    database: str = "shopee_sniper.db"

    # Monitor mode
    monitor_interval: int = 60
    monitor_price_drop_threshold: float = 0.9

    # Logging
    log_dir: str = "logs"
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        # Normalize log level
        self.log_level = self.log_level.upper()

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        """Load config from YAML file. Missing keys use defaults."""
        cfg = cls()
        if not os.path.exists(path):
            print(f"⚠️  Config file {path} not found, using defaults")
            return cfg

        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"⚠️  Error loading config: {e}, using defaults")
            return cfg

        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

        cfg.__post_init__()
        return cfg

    def account_config(self, account_dir: str) -> dict:
        """Load per-account config override from account_dir/config.yaml."""
        override_path = os.path.join(account_dir, "config.yaml")
        overrides: dict[str, Any] = {}
        if os.path.exists(override_path):
            try:
                with open(override_path, "r") as f:
                    overrides = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"⚠️  Error loading account config {override_path}: {e}")

        result: dict[str, Any] = {
            "max_price": overrides.get("max_price", self.default_max_price),
            "payment_channel_id": overrides.get("payment_channel_id", self.payment_channel_id),
            "include_keywords": overrides.get("include_keywords", self.include_keywords),
            "exclude_keywords": overrides.get("exclude_keywords", self.exclude_keywords),
        }
        return result


# ═══════════════════════════════════════════════════════════
# DATABASE — SQLite purchase attempt logger
# ═══════════════════════════════════════════════════════════

class Database:
    """Async SQLite database for logging purchase attempts.
    
    Uses stdlib sqlite3 with asyncio.to_thread() — no external dependencies needed.
    """

    def __init__(self, db_path: str = "shopee_sniper.db"):
        self.db_path = db_path
        self._conn: "sqlite3.Connection | None" = None

    async def connect(self) -> None:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        self._conn = await asyncio.to_thread(_open)
        await self._create_tables()
        print(f"🗄️  SQLite database: {self.db_path}")

    async def _create_tables(self) -> None:
        def _create(conn: sqlite3.Connection) -> None:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    shop_id INTEGER NOT NULL,
                    model_id INTEGER NOT NULL,
                    item_name TEXT DEFAULT '',
                    price_idr REAL DEFAULT 0,
                    account_name TEXT DEFAULT '',
                    success INTEGER DEFAULT 0,
                    error_message TEXT DEFAULT '',
                    latency_ms REAL DEFAULT 0,
                    timestamp TEXT DEFAULT (datetime('now', '+7 hours')),
                    mode TEXT DEFAULT 'auto'
                )
            """)
            conn.commit()
        if self._conn is not None:
            await asyncio.to_thread(_create, self._conn)

    async def log_purchase(
        self,
        item_id: int,
        shop_id: int,
        model_id: int,
        item_name: str,
        price_idr: float,
        account_name: str,
        success: bool,
        error_message: str = "",
        latency_ms: float = 0,
        mode: str = "auto",
    ) -> None:
        if self._conn is None:
            return
        def _log(conn: sqlite3.Connection) -> None:
            try:
                conn.execute(
                    """INSERT INTO purchases
                       (item_id, shop_id, model_id, item_name, price_idr,
                        account_name, success, error_message, latency_ms, mode)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item_id, shop_id, model_id, item_name, price_idr,
                        account_name, 1 if success else 0, error_message,
                        round(latency_ms, 2), mode,
                    ),
                )
                conn.commit()
            except Exception as e:
                print(f"⚠️  DB log error: {e}")
        await asyncio.to_thread(_log, self._conn)

    async def recent_purchases(self, limit: int = 10) -> list[dict]:
        if self._conn is None:
            return []
        def _recent(conn: sqlite3.Connection) -> list[dict]:
            cursor = conn.execute(
                "SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_recent, self._conn)

    async def stats(self) -> dict:
        if self._conn is None:
            return {}
        def _stats(conn: sqlite3.Connection) -> dict:
            cursor = conn.execute(
                "SELECT COUNT(*) as total, SUM(success) as successes FROM purchases"
            )
            row = cursor.fetchone()
            return dict(row) if row else {}
        return await asyncio.to_thread(_stats, self._conn)

    async def close(self) -> None:
        if self._conn:
            def _close(conn: sqlite3.Connection) -> None:
                conn.close()
            await asyncio.to_thread(_close, self._conn)
            self._conn = None


# ═══════════════════════════════════════════════════════════
# LOGGING SETUP
# ═══════════════════════════════════════════════════════════

def setup_logging(config: Config) -> logging.Logger:
    """Configure rotating file + console logging."""
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("shopee_sniper")
    logger.setLevel(getattr(logging, config.log_level, logging.INFO))
    logger.handlers.clear()

    # Formatter
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler with rotation (5MB per file, 3 backups)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "sniper.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


# ═══════════════════════════════════════════════════════════
# STATISTICS TRACKER
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

    def record(self, success: bool, latency: float = 0, rate_limited: bool = False) -> None:
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


# ═══════════════════════════════════════════════════════════
# PROXY MANAGER
# ═══════════════════════════════════════════════════════════

class ProxyManager:
    """Manages proxy rotation and dead proxy tracking."""

    def __init__(
        self,
        proxy_file: str = "",
        protocol: str = "http",
        single_proxy: str = "",
        logger: Optional[logging.Logger] = None,
        stats: Optional[Stats] = None,
    ):
        self.proxies: list[str] = []
        self.current_idx = 0
        self.protocol = protocol
        self.dead_proxies: set[str] = set()
        self.log = logger or logging.getLogger("shopee_sniper")
        self._stats = stats

        if single_proxy:
            self.proxies = [single_proxy]
        elif proxy_file and os.path.exists(proxy_file):
            with open(proxy_file) as f:
                self.proxies = [
                    line.strip()
                    for line in f
                    if line.strip() and not line.startswith("#")
                ]
            random.shuffle(self.proxies)
            self.log.info("🔄 Loaded %d proxies from %s", len(self.proxies), proxy_file)

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

    def mark_dead(self, proxy: str) -> None:
        clean = proxy.replace(f"{self.protocol}://", "")
        self.dead_proxies.add(clean)
        if self._stats:
            self._stats.proxy_rotations += 1

    @property
    def has_proxies(self) -> bool:
        return len(self.proxies) > 0


# ═══════════════════════════════════════════════════════════
# TELEGRAM NOTIFIER
# ═══════════════════════════════════════════════════════════

class TelegramNotifier:
    """Send purchase notifications via Telegram bot."""

    def __init__(
        self,
        token: str = "",
        chat_id: str = "",
        logger: Optional[logging.Logger] = None,
    ):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self.log = logger or logging.getLogger("shopee_sniper")
        if self.enabled:
            self.log.info("📱 Telegram notifications enabled → chat %s", chat_id)

    async def send(self, message: str, parse_mode: str = "HTML") -> None:
        if not self.enabled:
            return
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                await session.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                    },
                )
        except Exception as e:
            self.log.warning("⚠️ Telegram error: %s", e)

    async def notify_success(
        self,
        item_name: str,
        price: float,
        elapsed: float,
        account_name: str = "",
    ) -> None:
        account_tag = f"👤 {account_name}\n" if account_name else ""
        msg = (
            f"⚡ <b>FLASH SALE BERHASIL!</b>\n\n"
            f"{account_tag}"
            f"🛒 {item_name}\n"
            f"💰 Rp {price:,.0f}\n"
            f"⏱️ {elapsed:.2f}s"
        )
        await self.send(msg)

    async def notify_failure(
        self,
        item_name: str,
        errors: list[str],
        account_name: str = "",
    ) -> None:
        account_tag = f"👤 {account_name}\n" if account_name else ""
        msg = (
            f"❌ <b>Flash Sale Gagal</b>\n\n"
            f"{account_tag}"
            f"🛒 {item_name}\n"
            f"🚫 Errors: {', '.join(errors[:3])}"
        )
        await self.send(msg)

    async def notify_scan(self, items: list, account_name: str = "") -> None:
        if not items:
            return
        account_tag = f"👤 {account_name}\n" if account_name else ""
        lines = [
            f"🔍 <b>Flash Sale Scan — {len(items)} item ditemukan:</b>\n",
            account_tag,
        ]
        for i, item in enumerate(items[:10], 1):
            lines.append(
                f"  {i}. Rp {item['price_idr']:,.0f} — {item['name'][:40]}"
            )
        await self.send("\n".join(lines))

    async def notify_monitor_purchase(
        self,
        item_name: str,
        price: float,
        old_price: float,
        account_name: str = "",
    ) -> None:
        account_tag = f"👤 {account_name}\n" if account_name else ""
        msg = (
            f"📉 <b>Monitor Mode — Auto Buy!</b>\n\n"
            f"{account_tag}"
            f"🛒 {item_name}\n"
            f"💰 Rp {old_price:,.0f} → Rp {price:,.0f}\n"
            f"📉 Drop: {(1 - price/old_price)*100:.1f}%"
        )
        await self.send(msg)


# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

def generate_if_none_match(body_str: str) -> str:
    h1 = hashlib.md5(body_str.encode()).hexdigest()
    inner = hashlib.md5(("55b03" + h1 + "55b03").encode()).hexdigest()
    return f"55b03-{inner}"


def get_ntp_offset() -> float:
    if not HAS_NTP:
        print("⚠️  ntplib not installed — using local time")
        return 0.0
    try:
        c = _ntplib.NTPClient()
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


def parse_shopee_url(url: str) -> tuple[int, int]:
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


def matches_keywords(name: str, include: list[str], exclude: list[str]) -> bool:
    """Check if item name matches keyword filter rules.
    
    - If include is non-empty, at least one keyword must match (OR logic).
    - If exclude is non-empty, none of the keywords must match.
    - Empty include list = include all.
    - All comparisons are case-insensitive.
    """
    name_lower = name.lower()
    if include:
        if not any(kw.lower() in name_lower for kw in include):
            return False
    if exclude:
        if any(kw.lower() in name_lower for kw in exclude):
            return False
    return True


def scan_accounts(cookie_dir: str) -> list[dict]:
    """Scan cookie_dir for account subdirectories with cookies.json."""
    accounts: list[dict] = []
    if not os.path.isdir(cookie_dir):
        print(f"⚠️  Account directory '{cookie_dir}' not found")
        return accounts

    for entry in sorted(os.listdir(cookie_dir)):
        subpath = os.path.join(cookie_dir, entry)
        if not os.path.isdir(subpath):
            continue
        cookie_file = os.path.join(subpath, "cookies.json")
        if os.path.exists(cookie_file):
            accounts.append({
                "name": entry,
                "dir": subpath,
                "cookie_file": cookie_file,
            })
            print(f"👤 Found account: {entry}")

    print(f"📂 Total accounts loaded: {len(accounts)}")
    return accounts


# ═══════════════════════════════════════════════════════════
# SHOPEE API CLIENT (ENHANCED)
# ═══════════════════════════════════════════════════════════

class ShopeeClient:
    """Async Shopee API client with proxy support, rate limit handling, logging."""

    def __init__(
        self,
        cookies: dict,
        proxy_manager: Optional[ProxyManager] = None,
        logger: Optional[logging.Logger] = None,
        stats: Optional[Stats] = None,
        config: Optional[Config] = None,
    ):
        self.cookies = cookies
        self.csrf_token = get_csrf_token(cookies)
        self.cookie_str = cookie_string(cookies)
        self.session: Optional[aiohttp.ClientSession] = None
        self.address_id = 0
        self.account_info: Optional[dict] = None
        self.proxy_manager = proxy_manager
        self.log = logger or logging.getLogger("shopee_sniper")
        self._stats = stats or Stats()
        self._config = config or Config()

    async def _init_session(self) -> None:
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=10)
            self.session = aiohttp.ClientSession(
                headers={
                    **HEADERS_BASE,
                    "Cookie": self.cookie_str,
                    "X-Csrftoken": self.csrf_token,
                },
                timeout=timeout,
            )

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict:
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
                async with self.session.request(
                    method, url, headers=headers, proxy=proxy, **kwargs
                ) as resp:
                    latency = time.time() - start

                    if resp.status == 429:
                        self._stats.record(False, rate_limited=True)
                        if proxy:
                            self.proxy_manager.mark_dead(proxy)
                        self.log.warning(
                            "🚫 Rate limited (429), attempt %d/3", attempt + 1
                        )
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue

                    data = await resp.json()

                    # Check for Shopee rate limit in response body
                    error = data.get("error")
                    error_msg = str(data.get("error_msg", ""))
                    if error == 99999 or "rate" in error_msg.lower():
                        self._stats.record(False, rate_limited=True)
                        if proxy:
                            self.proxy_manager.mark_dead(proxy)
                        self.log.warning("🚫 Rate limited (body), attempt %d/3", attempt + 1)
                        await asyncio.sleep(1)
                        continue

                    self._stats.record(True, latency)
                    return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if proxy:
                    self.proxy_manager.mark_dead(proxy)
                if attempt == 2:
                    self._stats.record(False)
                    self.log.error("Request failed after 3 retries: %s", e)
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

    async def get_flash_sale_sessions(self, scan_pages: int = 5) -> list:
        all_sessions = []
        for page in range(scan_pages):
            offset = page * 20
            url = (
                f"{API['flash_sessions']}"
                f"?limit=20&offset={offset}&need_items=1&with_dp_items=1"
            )
            data = await self._request("GET", url)
            sessions = data.get("data", {}).get("sessions", [])
            if not sessions:
                break
            all_sessions.extend(sessions)
            await asyncio.sleep(0.3)
        return all_sessions

    async def get_flash_sale_items(
        self, session_id: int, item_ids: list[int]
    ) -> list:
        ids_str = ",".join(str(i) for i in item_ids[:50])
        url = (
            f"{API['flash_sale']}"
            f"?session_id={session_id}&item_ids={ids_str}&need_detail=1"
        )
        data = await self._request("GET", url)
        return data.get("data", {}).get("items", [])

    async def add_to_cart(
        self, item_id: int, shop_id: int, model_id: int, qty: int = 1
    ) -> dict:
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

    async def get_checkout(
        self, item_id: int, shop_id: int, model_id: int, qty: int, payment_channel_id: int = 8001400
    ) -> dict:
        body = {
            "selected_address_id": self.address_id,
            "shoporders": [
                {
                    "shop": {"shopid": shop_id},
                    "items": [
                        {
                            "itemid": item_id,
                            "modelid": model_id,
                            "quantity": qty,
                        }
                    ],
                    "shipping": {"channel_id": 0},
                }
            ],
            "channel_payment_option_list": [
                {"payment_channel_id": payment_channel_id}
            ],
        }
        return await self._request("POST", API["checkout_get"], json=body)

    async def place_order(self, checkout_data: dict) -> dict:
        return await self._request("POST", API["place_order"], json=checkout_data)

    async def close(self) -> None:
        if self.session:
            await self.session.close()


# ═══════════════════════════════════════════════════════════
# FLASH SALE SCANNER
# ═══════════════════════════════════════════════════════════

async def scan_flash_sale(
    client: ShopeeClient,
    config: Config,
    max_price: int,
    include_keywords: list[str],
    exclude_keywords: list[str],
    logger: logging.Logger,
) -> list[dict]:
    """Scan flash sale sessions for items matching criteria."""
    logger.info("🔍 Scanning flash sale (max price: Rp %s)", f"{max_price:,}")

    sessions = await client.get_flash_sale_sessions(config.scan_pages)
    if not sessions:
        logger.warning("❌ No flash sale sessions found.")
        return []

    logger.info("📦 Found %d sessions", len(sessions))

    cheap_items: list[dict] = []
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
            name = item.get("name", "Unknown")

            # Keyword filter
            if not matches_keywords(name, include_keywords, exclude_keywords):
                continue

            if price_idr <= max_price and stock > 0:
                cheap_items.append({
                    "item_id": item.get("item_id", item.get("itemid", 0)),
                    "shop_id": item.get("shop_id", item.get("shopid", 0)),
                    "model_id": item.get("model_id", item.get("modelid", 0)),
                    "name": name,
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
# CHECKOUT ENGINE
# ═══════════════════════════════════════════════════════════

async def single_checkout(
    client: ShopeeClient,
    item_id: int,
    shop_id: int,
    model_id: int,
    attempt: int,
    quantity: int = 1,
    payment_channel_id: int = 8001400,
) -> dict:
    """Execute a single checkout flow: add to cart → get checkout → place order."""
    try:
        cart = await client.add_to_cart(item_id, shop_id, model_id, quantity)
        if cart.get("error"):
            return {
                "success": False,
                "error": f"cart: {cart.get('error')} - {cart.get('error_msg', '')}",
                "attempt": attempt,
            }

        checkout = await client.get_checkout(
            item_id, shop_id, model_id, quantity, payment_channel_id
        )
        if checkout.get("error"):
            return {
                "success": False,
                "error": f"checkout: {checkout.get('error')}",
                "attempt": attempt,
            }

        order = await client.place_order(checkout)
        if order.get("error"):
            err = order.get("error")
            if err in [2, 9, 110]:
                return {
                    "success": False,
                    "error": f"FATAL: {err}",
                    "attempt": attempt,
                    "fatal": True,
                }
            return {
                "success": False,
                "error": f"order: {err}",
                "attempt": attempt,
            }

        return {"success": True, "data": order, "attempt": attempt}
    except Exception as e:
        return {"success": False, "error": str(e), "attempt": attempt}


async def delayed_checkout(
    client: ShopeeClient,
    item_id: int,
    shop_id: int,
    model_id: int,
    delay: float,
    attempt: int,
    quantity: int = 1,
    payment_channel_id: int = 8001400,
) -> dict:
    if delay > 0:
        await asyncio.sleep(delay)
    return await single_checkout(
        client, item_id, shop_id, model_id, attempt,
        quantity=quantity, payment_channel_id=payment_channel_id,
    )


async def snipe_item(
    client: ShopeeClient,
    item: dict,
    ntp_offset: float,
    notifier: TelegramNotifier,
    config: Config,
    logger: logging.Logger,
    account_name: str = "",
    mode: str = "auto",
    payment_channel_id: int = 8001400,
    db: Optional[Database] = None,
) -> bool:
    """Snipe a single flash sale item with concurrent checkout requests."""
    item_id = item["item_id"]
    shop_id = item["shop_id"]
    model_id = item["model_id"]
    name = item["name"][:60]
    price = item["price_idr"]
    start_time = item.get("start_time", 0)

    logger.info("🎯 Target: %s | Rp %s", name, f"{price:,.0f}")
    logger.info("📦 Item: %s | Shop: %s | Model: %s", item_id, shop_id, model_id)

    # Wait for flash sale start time
    if start_time > 0:
        now = get_accurate_timestamp(ntp_offset)
        wait = start_time - config.subtract_seconds - now
        if wait > 0:
            wib = timezone(timedelta(hours=7))
            start_str = datetime.fromtimestamp(start_time, tz=wib).strftime("%H:%M:%S")
            logger.info("⏳ Waiting until %s WIB (%ds)...", start_str, int(wait))

            while wait > 2:
                await asyncio.sleep(min(wait - 1, 2))
                now = get_accurate_timestamp(ntp_offset)
                wait = start_time - config.subtract_seconds - now

            # Precision wait
            while get_accurate_timestamp(ntp_offset) < start_time - config.subtract_seconds:
                await asyncio.sleep(0.001)

    logger.info("🚀 Firing %d checkout requests...", config.concurrent_requests)

    start = time.time()
    tasks = [
        asyncio.create_task(
            delayed_checkout(
                client, item_id, shop_id, model_id,
                i * config.request_delay, i + 1,
                quantity=1,
                payment_channel_id=payment_channel_id,
            )
        )
        for i in range(config.concurrent_requests)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.time() - start

    errors: list[str] = []
    success = False
    for r in results:
        if isinstance(r, dict) and r.get("success"):
            success = True
            logger.info("✅ SUKSES! (%.2fs)", elapsed)
            await notifier.notify_success(name, price, elapsed, account_name)
            break
        elif isinstance(r, dict):
            errors.append(r.get("error", "unknown"))

    if not success:
        for e in errors[:3]:
            logger.error("❌ %s", e)
        await notifier.notify_failure(name, errors, account_name)

    # Log to database
    if db:
        await db.log_purchase(
            item_id=item_id,
            shop_id=shop_id,
            model_id=model_id,
            item_name=name,
            price_idr=price,
            account_name=account_name,
            success=success,
            error_message="; ".join(errors[:3]) if errors else "",
            latency_ms=elapsed * 1000,
            mode=mode,
        )

    return success


# ═══════════════════════════════════════════════════════════
# ACCOUNT RUNNER — manages one account's lifecycle
# ═══════════════════════════════════════════════════════════

class AccountRunner:
    """Manages one Shopee account: auth, scanning, sniping."""

    def __init__(
        self,
        account_info: dict,
        config: Config,
        global_proxy_mgr: Optional[ProxyManager],
        global_notifier: TelegramNotifier,
        global_db: Optional[Database],
        logger: logging.Logger,
        ntp_offset: float,
        shutdown_event: asyncio.Event,
    ):
        self.name: str = account_info["name"]
        self.dir: str = account_info["dir"]
        self.cookie_file: str = account_info["cookie_file"]
        self.config: Config = config
        self.global_proxy_mgr: Optional[ProxyManager] = global_proxy_mgr
        self.global_notifier: TelegramNotifier = global_notifier
        self.global_db: Optional[Database] = global_db
        self.log: logging.Logger = logger
        self.ntp_offset: float = ntp_offset
        self.shutdown_event: asyncio.Event = shutdown_event

        # Per-account overrides
        self.account_cfg: dict = config.account_config(self.dir)
        self.max_price: int = self.account_cfg["max_price"]
        self.payment_channel_id: int = self.account_cfg["payment_channel_id"]
        self.include_keywords: list[str] = self.account_cfg["include_keywords"]
        self.exclude_keywords: list[str] = self.account_cfg["exclude_keywords"]

        # Per-account stats
        self.stats: Stats = Stats()

        # Client (initialized in run())
        self.client: Optional[ShopeeClient] = None

    async def run(self) -> bool:
        """Run the account: auth → scan → snipe. Returns True if at least one item purchased."""
        self.log.info("=" * 50)
        self.log.info("👤 [%s] Starting account", self.name)
        self.log.info("=" * 50)

        if not os.path.exists(self.cookie_file):
            self.log.error("❌ [%s] Cookie file not found: %s", self.name, self.cookie_file)
            return False

        # Load cookies
        try:
            cookies = load_cookies(self.cookie_file)
            self.log.info("✅ [%s] Loaded %d cookies", self.name, len(cookies))
        except Exception as e:
            self.log.error("❌ [%s] Failed to load cookies: %s", self.name, e)
            return False

        # Create per-account proxy manager (or use global)
        proxy_mgr = self.global_proxy_mgr
        if not proxy_mgr:
            proxy_mgr = ProxyManager(
                proxy_file=self.config.proxy_file,
                protocol=self.config.proxy_protocol,
                logger=self.log,
                stats=self.stats,
            )

        # Create client
        self.client = ShopeeClient(
            cookies,
            proxy_manager=proxy_mgr,
            logger=self.log,
            stats=self.stats,
            config=self.config,
        )

        if self.shutdown_event.is_set():
            await self.client.close()
            return False

        # Verify auth
        try:
            info = await self.client.get_account_info()
            username = info.get("username", "unknown")
            self.log.info("👤 [%s] Logged in as: %s", self.name, username)
        except Exception as e:
            self.log.error("❌ [%s] Auth failed: %s", self.name, e)
            await self.client.close()
            return False

        # Get addresses
        await self.client.get_addresses()
        if not self.client.address_id:
            self.log.error(
                "❌ [%s] No shipping address! Add one in Shopee app.",
                self.name,
            )
            await self.client.close()
            return False
        self.log.info("📍 [%s] Address ID: %s", self.name, self.client.address_id)

        # Scan flash sale
        items = await scan_flash_sale(
            self.client, self.config, self.max_price,
            self.include_keywords, self.exclude_keywords,
            self.log,
        )

        if not items:
            self.log.warning("😞 [%s] No matching items found.", self.name)
            await self.client.close()
            return False

        # Notify scan results
        await self.global_notifier.notify_scan(items, self.name)
        self.log.info("🎯 [%s] Found %d items under Rp %s!", self.name, len(items), f"{self.max_price:,}")
        for i, item in enumerate(items, 1):
            wib = timezone(timedelta(hours=7))
            start = (
                datetime.fromtimestamp(item["start_time"], tz=wib).strftime("%H:%M")
                if item.get("start_time")
                else "?"
            )
            self.log.info(
                "   %d. Rp %s | %s | Starts: %s WIB",
                i,
                f"{item['price_idr']:,.0f}",
                item["name"][:40],
                start,
            )

        if self.shutdown_event.is_set():
            await self.client.close()
            return False

        # Snipe all items
        success_count = 0
        for item in items:
            result = await snipe_item(
                self.client, item, self.ntp_offset,
                self.global_notifier, self.config, self.log,
                account_name=self.name,
                mode="auto",
                payment_channel_id=self.payment_channel_id,
                db=self.global_db,
            )
            if result:
                success_count += 1
            if self.shutdown_event.is_set():
                break

        self.log.info("📊 [%s] Results: %d/%d items purchased!", self.name, success_count, len(items))
        self.log.info(self.stats.summary())
        await self.client.close()
        return success_count > 0


# ═══════════════════════════════════════════════════════════
# MONITOR ENGINE — always-on price checking
# ═══════════════════════════════════════════════════════════

class MonitorEngine:
    """Always-on mode: continuously monitor items and auto-buy on price drop."""

    def __init__(
        self,
        account_runner: AccountRunner,
        config: Config,
        logger: logging.Logger,
        shutdown_event: asyncio.Event,
    ):
        self.runner = account_runner
        self.config = config
        self.log = logger
        self.shutdown_event = shutdown_event
        self.monitored_items: list[dict] = []

    async def run(self) -> None:
        """Run the monitor loop until shutdown."""
        self.log.info("👁️  [%s] Monitor mode started (interval: %ds)", self.runner.name, self.config.monitor_interval)

        # Initial scan
        items = await self._scan()
        if items:
            self.monitored_items = items
            self.log.info("👁️  [%s] Monitoring %d items", self.runner.name, len(items))

        while not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.monitor_interval)

                if self.shutdown_event.is_set():
                    break

                # Re-scan for new items and price updates
                fresh_items = await self._scan()
                if not fresh_items:
                    self.log.info("👁️  [%s] No items found in scan, retrying...", self.runner.name)
                    continue

                # Check for price drops
                for fresh in fresh_items:
                    if self.shutdown_event.is_set():
                        break

                    # Find previously monitored item with same ID
                    old = next(
                        (m for m in self.monitored_items if m["item_id"] == fresh["item_id"]),
                        None,
                    )

                    if old and old["price_idr"] > 0:
                        ratio = fresh["price_idr"] / old["price_idr"]
                        threshold = self.config.monitor_price_drop_threshold
                        if ratio <= threshold and fresh["stock"] > 0:
                            self.log.info(
                                "📉 [%s] Price drop detected! Rp %s → Rp %s (%.1f%% drop)",
                                self.runner.name,
                                f"{old['price_idr']:,.0f}",
                                f"{fresh['price_idr']:,.0f}",
                                (1 - ratio) * 100,
                            )

                            # Notify and auto-buy
                            await self.runner.global_notifier.notify_monitor_purchase(
                                fresh["name"],
                                fresh["price_idr"],
                                old["price_idr"],
                                self.runner.name,
                            )

                            # Auto-buy if client is ready
                            if self.runner.client and not self.shutdown_event.is_set():
                                await snipe_item(
                                    self.runner.client, fresh, self.runner.ntp_offset,
                                    self.runner.global_notifier, self.config, self.log,
                                    account_name=self.runner.name,
                                    mode="monitor",
                                    payment_channel_id=self.runner.payment_channel_id,
                                    db=self.runner.global_db,
                                )

                # Update monitored items
                self.monitored_items = fresh_items
                self.log.info(
                    "👁️  [%s] Monitor cycle complete — monitoring %d items",
                    self.runner.name,
                    len(self.monitored_items),
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error("👁️  [%s] Monitor error: %s", self.runner.name, e)

        self.log.info("👁️  [%s] Monitor mode stopped", self.runner.name)

    async def _scan(self) -> list[dict]:
        """Scan flash sale and return matching items."""
        if not self.runner.client:
            return []
        try:
            return await scan_flash_sale(
                self.runner.client, self.config, self.runner.max_price,
                self.runner.include_keywords, self.runner.exclude_keywords,
                self.log,
            )
        except Exception as e:
            self.log.error("👁️  [%s] Scan error: %s", self.runner.name, e)
            return []


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Shopee Flash Sale Sniper Bot v4 — Ultimate Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 flash_sale_v4.py                          # Auto-scan + snipe with all accounts
  python3 flash_sale_v4.py --config myconfig.yaml   # Use custom config
  python3 flash_sale_v4.py --monitor                # Always-on monitor mode
  python3 flash_sale_v4.py --account alice          # Run single account
  python3 flash_sale_v4.py "https://..." 1735683600 # Manual mode with URL and timestamp
        """,
    )
    parser.add_argument("url", nargs="?", default="", help="Product URL (manual mode)")
    parser.add_argument("timestamp", nargs="?", type=int, default=0, help="Target timestamp (manual mode)")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--monitor", action="store_true", help="Always-on monitor mode")
    parser.add_argument("--account", default="", help="Run single account (directory name in accounts/)")
    parser.add_argument("--cookie", default="", help="Cookie file path (overrides account system)")
    parser.add_argument("--log-level", default="", help="Override log level (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--db", default="", help="Database file path override")
    return parser.parse_args()


async def run_accounts(
    accounts: list[dict],
    config: Config,
    proxy_mgr: Optional[ProxyManager],
    notifier: TelegramNotifier,
    db: Optional[Database],
    logger: logging.Logger,
    ntp_offset: float,
    shutdown_event: asyncio.Event,
    monitor_mode: bool = False,
) -> list[bool]:
    """Run all accounts in parallel. Returns list of success booleans."""

    async def run_single(acc_info: dict) -> bool:
        runner = AccountRunner(
            acc_info, config, proxy_mgr, notifier, db, logger,
            ntp_offset, shutdown_event,
        )

        if monitor_mode:
            # Initialize client and auth
            cookies = load_cookies(acc_info["cookie_file"])
            proxy_mgr_local = proxy_mgr or ProxyManager(
                proxy_file=config.proxy_file,
                protocol=config.proxy_protocol,
                logger=logger,
            )
            runner.client = ShopeeClient(
                cookies, proxy_manager=proxy_mgr_local,
                logger=logger, config=config,
            )

            try:
                await runner.client.get_account_info()
                await runner.client.get_addresses()
            except Exception as e:
                logger.error("❌ [%s] Auth failed: %s", acc_info["name"], e)
                await runner.client.close()
                return False

            engine = MonitorEngine(runner, config, logger, shutdown_event)
            await engine.run()
            return True
        else:
            return await runner.run()

    tasks = [asyncio.create_task(run_single(acc)) for acc in accounts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    success_list: list[bool] = []
    for i, r in enumerate(results):
        if isinstance(r, bool):
            success_list.append(r)
        elif isinstance(r, Exception):
            logger.error("❌ Account %s raised exception: %s", accounts[i]["name"], r)
            success_list.append(False)
        else:
            success_list.append(False)

    return success_list


async def manual_mode(
    url: str,
    timestamp: int,
    config: Config,
    cookies: dict,
    proxy_mgr: Optional[ProxyManager],
    notifier: TelegramNotifier,
    db: Optional[Database],
    logger: logging.Logger,
    ntp_offset: float,
    shutdown_event: asyncio.Event,
) -> bool:
    """Manual mode: snipe a specific product URL."""
    logger.info("🎯 Manual mode: %s", url)
    item_id, shop_id = parse_shopee_url(url)

    client = ShopeeClient(
        cookies, proxy_manager=proxy_mgr, logger=logger, config=config,
    )

    item_info = await client.get_item_info(item_id, shop_id)
    models = item_info.get("item", {}).get("models", [])
    model_id = models[0]["modelid"] if models else 0
    price = models[0].get("price", 0) if models else 0
    name = item_info.get("item", {}).get("name", "Unknown")

    item = {
        "item_id": item_id,
        "shop_id": shop_id,
        "model_id": model_id,
        "name": name,
        "price": price,
        "price_idr": price / 100000 if price > 100000 else price,
        "start_time": timestamp if timestamp > 0 else 0,
    }

    result = await snipe_item(
        client, item, ntp_offset, notifier, config, logger,
        account_name="manual",
        mode="manual",
        db=db,
    )

    await client.close()
    return result


async def main() -> None:
    args = parse_args()

    # Load config
    config = Config.load(args.config)

    # Override config from CLI args
    if args.log_level:
        config.log_level = args.log_level.upper()
    if args.db:
        config.database = args.db

    # Setup logging
    logger = setup_logging(config)
    logger.info("=" * 50)
    logger.info("⚡ SHOPEE FLASH SALE SNIPER BOT v4 (Ultimate Edition)")
    logger.info("=" * 50)

    # Setup graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler(signum: int, frame: Any) -> None:
        signame = signal.Signals(signum).name
        logger.info("🛑 Received %s, shutting down gracefully...", signame)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Initialize database
    db: Optional[Database] = None
    try:
        db = Database(config.database)
        await db.connect()
    except Exception as e:
        logger.warning("⚠️  Database init failed: %s", e)
        db = None

    # Initialize proxy manager
    proxy_mgr: Optional[ProxyManager] = None
    if config.proxy_file and os.path.exists(config.proxy_file):
        proxy_mgr = ProxyManager(
            proxy_file=config.proxy_file,
            protocol=config.proxy_protocol,
            logger=logger,
        )

    # Initialize Telegram notifier
    notifier = TelegramNotifier(
        token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        logger=logger,
    )

    # NTP sync
    logger.info("🕐 Syncing NTP...")
    ntp_offset = get_ntp_offset()
    logger.info("⏱️  Offset: %.1fms", ntp_offset * 1000)

    # Manual mode via URL
    if args.url:
        if not args.cookie:
            logger.error("❌ Manual mode requires --cookie <path>")
            return
        if not os.path.exists(args.cookie):
            logger.error("❌ Cookie file not found: %s", args.cookie)
            return
        cookies = load_cookies(args.cookie)
        await manual_mode(
            args.url, args.timestamp, config, cookies,
            proxy_mgr, notifier, db, logger, ntp_offset, shutdown_event,
        )
        if db:
            recent = await db.recent_purchases(5)
            logger.info("📋 Recent purchases:")
            for r in recent:
                logger.info(
                    "   #%d | %s | %s | %s | Rp %s",
                    r["id"], r["item_name"][:30],
                    "✅" if r["success"] else "❌",
                    r["account_name"], f"{r['price_idr']:,.0f}",
                )
        await db.close() if db else None
        return

    # Single cookie mode (backward compat)
    if args.cookie:
        accounts_data = [{
            "name": "default",
            "dir": ".",
            "cookie_file": args.cookie,
        }]
        logger.info("📂 Single cookie mode: %s", args.cookie)
    else:
        # Scan for accounts
        accounts_data = scan_accounts(config.cookie_dir)
        if not accounts_data:
            logger.warning("⚠️  No accounts found in '%s'", config.cookie_dir)
            logger.info("   Create subdirectories with cookies.json inside, e.g.:")
            logger.info("   accounts/alice/cookies.json")
            logger.info("   accounts/bob/cookies.json")
            await db.close() if db else None
            return

    # Filter single account
    if args.account:
        accounts_data = [a for a in accounts_data if a["name"] == args.account]
        if not accounts_data:
            logger.error("❌ Account '%s' not found", args.account)
            await db.close() if db else None
            return
        logger.info("👤 Running single account: %s", args.account)

    if args.monitor:
        logger.info("👁️  Monitor mode enabled — continuous price checking")
        logger.info("   Press Ctrl+C to stop")

    # Run all accounts in parallel
    results = await run_accounts(
        accounts_data, config, proxy_mgr, notifier, db, logger,
        ntp_offset, shutdown_event, monitor_mode=args.monitor,
    )

    # Summary
    if not args.monitor:
        total_success = sum(1 for r in results if r)
        logger.info("=" * 50)
        logger.info("📊 Overall: %d/%d accounts purchased something!", total_success, len(results))
        logger.info("=" * 50)

        # Show recent purchases from DB
        if db:
            recent = await db.recent_purchases(10)
            if recent:
                logger.info("📋 Recent purchases:")
                for r in recent:
                    logger.info(
                        "   #%d | %s | %s | %s | Rp %s",
                        r["id"], r["item_name"][:30],
                        "✅" if r["success"] else "❌",
                        r["account_name"], f"{r['price_idr']:,.0f}",
                    )

    if db:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user.")
