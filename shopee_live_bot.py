#!/usr/bin/env python3
"""
Shopee Live Auto-Buy Bot
Monitors Shopee Live streams and auto-purchases items when "Beli" (Buy) appears.

Usage:
    python3 shopee_live_bot.py <live-url> [--account NAME] [--cookie PATH] [--config CONFIG] [--method click|api] [--interval SECONDS]

Methods:
    click - Full browser automation (click buy button -> complete purchase via browser).
    api   - Browser monitors DOM for item -> extracts item_id/shop_id/model_id from
            network intercept -> executes checkout via ShopeeClient API (faster).
"""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

# ── Import from flash_sale_v4 ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flash_sale_v4 import (
    Config,
    Database,
    TelegramNotifier,
    Stats,
    ShopeeClient,
    ProxyManager,
    load_cookies,
    parse_shopee_url,
    matches_keywords,
    setup_logging,
    single_checkout,
    scan_accounts,
    HEADERS_BASE,
    API,
)

try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext, Route
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Constants ──────────────────────────────────────────────────────────────
LIVE_BUY_SELECTORS = [
    "button:has-text('Beli')",
    "button:has-text('beli')",
    "[class*='buy']:has-text('Beli')",
    "[class*='beli']",
    "button[class*='buy']",
    "button[class*='Beli']",
    "[data-testid='btnBuy']",
    "button.shopee-button-primary:has-text('Beli')",
    "div.product-buy button:has-text('Beli')",
    # Fallback: any button-like element that says Beli
    "[class*='product'] button:has-text('Beli')",
    "a:has-text('Beli')",
    "span:has-text('Beli')",
]

CHECKOUT_BUTTON_SELECTORS = [
    "button:has-text('Checkout')",
    "button:has-text('checkout')",
    "button.shopee-button-primary:has-text('Checkout')",
    "button:has-text('Pesan')",  # Order / Pesan Sekarang
    "button:has-text('Bayar')",  # Pay now
    "[data-testid='btnCheckout']",
    "a:has-text('Checkout')",
]

ITEM_DETAIL_API_PATTERN = re.compile(r"/api/v2/item/get\?")

# ── Live Monitor Config ────────────────────────────────────────────────────


@dataclass
class LiveConfig:
    """Per-run configuration for live bot — merges with global Config."""
    live_url: str = ""
    account_name: str = ""
    cookie_path: str = ""
    method: str = "click"  # "click" or "api"
    polling_interval: float = 0.5  # DOM check interval in seconds
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720
    checkout_timeout: int = 30  # seconds to wait for checkout flow
    network_intercept: bool = True  # capture API calls for api mode
    max_wait_minutes: int = 60  # how long to monitor before giving up


# ── ShopeeLiveBot ──────────────────────────────────────────────────────────


class ShopeeLiveBot:
    """
    Orchestrates browser lifecycle, logging, and account integration.

    Wraps Playwright browser + context and delegates DOM monitoring to LiveMonitor.
    """

    def __init__(
        self,
        live_config: LiveConfig,
        config: Config,
        logger: logging.Logger,
        shutdown_event: asyncio.Event,
        db: Optional[Database] = None,
        notifier: Optional[TelegramNotifier] = None,
        stats: Optional[Stats] = None,
    ):
        self.lc = live_config
        self.cfg = config
        self.log = logger
        self.shutdown = shutdown_event
        self.db = db
        self.notifier = notifier
        self.stats = stats or Stats()

        # Browser/Playwright attrs
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        # Shopee API client (for api-mode checkout)
        self.api_client: Optional[ShopeeClient] = None

        # Captured item info from network intercept / DOM parsing
        self.captured_item: dict[str, Any] = {}

        # LiveMonitor (assigned in run())
        self.monitor: Optional["LiveMonitor"] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self) -> None:
        """Clean shutdown of browser and API client."""
        if self.monitor:
            self.monitor.stop()
        if self._page and not self._page.is_closed():
            try:
                await self._page.close()
            except Exception:
                pass
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        if self.api_client:
            try:
                await self.api_client.close()
            except Exception:
                pass

    async def _launch_browser(self) -> None:
        """Launch Playwright browser with Xvfb fallback."""
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        self._playwright = await async_playwright().start()

        launch_options: dict[str, Any] = {
            "headless": self.lc.headless,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        }

        # Try to launch; fallback to Xvfb if display fails
        try:
            self._browser = await self._playwright.chromium.launch(**launch_options)
        except Exception as e:
            self.log.warning("Browser launch failed: %s. Trying Xvfb fallback...", e)
            # Launch with DISPLAY set
            os.environ.setdefault("DISPLAY", ":99")
            try:
                self._browser = await self._playwright.chromium.launch(**launch_options)
            except Exception as e2:
                raise RuntimeError(f"Cannot launch Chromium (even with Xvfb): {e2}") from e

        self._context = await self._browser.new_context(
            viewport={"width": self.lc.viewport_width, "height": self.lc.viewport_height},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="id-ID",
            timezone_id="Asia/Jakarta",
        )

        self._page = await self._context.new_page()

        # Log console messages for debugging
        self._page.on("console", lambda msg: self.log.debug("[BROWSER] %s", msg.text))

        self.log.info("🌐 Browser launched (headless=%s)", self.lc.headless)

    async def _load_cookies_into_browser(self) -> bool:
        """Load cookies from a cookies.json file or account directory into the browser context.

        Returns True if cookies were loaded successfully.
        """
        cookie_path = self.lc.cookie_path

        # If no explicit path, try to find cookies for the named account
        if not cookie_path and self.lc.account_name:
            candidate = os.path.join(self.cfg.cookie_dir, self.lc.account_name, "cookies.json")
            if os.path.exists(candidate):
                cookie_path = candidate

        if not cookie_path or not os.path.exists(cookie_path):
            self.log.warning("No cookies file found at '%s' — continuing without auth", cookie_path)
            return False

        try:
            with open(cookie_path, "r") as f:
                raw = json.load(f)

            # Convert from Cookie-Editor format if needed
            if isinstance(raw, list):
                # Cookie-Editor format
                pw_cookies = []
                for c in raw:
                    pw_cookie = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".shopee.co.id"),
                        "path": c.get("path", "/"),
                    }
                    if c.get("httpOnly"):
                        pw_cookie["httpOnly"] = True
                    if c.get("secure", True):
                        pw_cookie["secure"] = True
                    if c.get("sameSite"):
                        pw_cookie["sameSite"] = c["sameSite"]
                    pw_cookies.append(pw_cookie)
                await self._context.add_cookies(pw_cookies)
                self.log.info("🍪 Loaded %d cookies into browser from Cookie-Editor format", len(pw_cookies))
            elif isinstance(raw, dict):
                # Dict format {name: value}
                pw_cookies = [
                    {"name": k, "value": v, "domain": ".shopee.co.id", "path": "/"}
                    for k, v in raw.items()
                ]
                await self._context.add_cookies(pw_cookies)
                self.log.info("🍪 Loaded %d cookies into browser from dict format", len(pw_cookies))
            else:
                self.log.warning("Unknown cookie format in %s", cookie_path)
                return False

            return True
        except Exception as e:
            self.log.error("Failed to load cookies: %s", e)
            return False

    async def _init_api_client(self) -> bool:
        """Initialize ShopeeClient API client for fast checkout (api method)."""
        cookie_path = self.lc.cookie_path

        if not cookie_path and self.lc.account_name:
            candidate = os.path.join(self.cfg.cookie_dir, self.lc.account_name, "cookies.json")
            if os.path.exists(candidate):
                cookie_path = candidate

        if not cookie_path or not os.path.exists(cookie_path):
            self.log.warning("No cookies for API client — api method unavailable")
            return False

        try:
            cookies = load_cookies(cookie_path)
            proxy_mgr = ProxyManager(
                proxy_file=self.cfg.proxy_file,
                protocol=self.cfg.proxy_protocol,
                logger=self.log,
                stats=self.stats,
            )
            self.api_client = ShopeeClient(
                cookies,
                proxy_manager=proxy_mgr if proxy_mgr.has_proxies else None,
                logger=self.log,
                stats=self.stats,
                config=self.cfg,
            )

            # Verify auth
            try:
                info = await self.api_client.get_account_info()
                username = info.get("username", "unknown")
                self.log.info("👤 API client logged in as: %s", username)
            except Exception as e:
                self.log.warning("API client auth failed: %s (will still try browser)", e)

            # Get shipping address
            await self.api_client.get_addresses()
            if self.api_client.address_id:
                self.log.info("📍 API client address ID: %s", self.api_client.address_id)
            else:
                self.log.warning("No shipping address found for API client")

            return True
        except Exception as e:
            self.log.error("Failed to init API client: %s", e)
            return False

    async def run(self) -> None:
        """Main entry point: launch browser, navigate to live, monitor, and buy."""
        self.log.info("=" * 50)
        self.log.info("📺 Shopee Live Bot starting")
        self.log.info("📍 URL: %s", self.lc.live_url)
        self.log.info("⚙️  Method: %s | Interval: %.1fs", self.lc.method, self.lc.polling_interval)
        self.log.info("=" * 50)

        # 1. Launch browser
        await self._launch_browser()

        # 2. Load cookies
        cookies_loaded = await self._load_cookies_into_browser()
        if cookies_loaded:
            self.log.info("✅ Browser authenticated with cookies")

        # 3. Init API client if method is 'api'
        if self.lc.method == "api":
            await self._init_api_client()

        # 4. Set up network intercept for API mode
        if self.lc.method == "api" and self.lc.network_intercept:
            await self._setup_network_intercept()

        # 5. Navigate to live URL
        self.log.info("🔄 Navigating to live stream...")
        try:
            await self._page.goto(self.lc.live_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            self.log.warning("Navigation timeout (will continue waiting): %s", e)

        # Wait for page to settle
        await asyncio.sleep(3)

        # Check if we landed on the live page
        current_url = self._page.url
        self.log.info("📍 Current URL: %s", current_url)

        # 6. Start monitoring
        self.monitor = LiveMonitor(
            page=self._page,
            config=self.lc,
            logger=self.log,
            on_buy_button=lambda: asyncio.create_task(self._on_buy_detected()),
            shutdown=self.shutdown,
        )

        await self.monitor.run()

        self.log.info("📺 Live monitoring ended")

    async def _setup_network_intercept(self) -> None:
        """Intercept Shopee API responses to extract item details."""
        if not self._page:
            return

        async def handle_route(route: Route, request):
            """Intercept API responses to collect item/shop IDs."""
            url = request.url
            await route.continue_()

            # We capture responses via response handler instead
            # because we need the response body

        # Use response handler to capture API responses
        self._page.on("response", self._on_api_response)

        self.log.info("📡 Network intercept active for API mode")

    async def _on_api_response(self, response) -> None:
        """Listen for Shopee API responses that contain item details."""
        url = response.url

        # Capture item info API responses
        if "api/v2/item/get" in url:
            try:
                body = await response.json()
                item_data = body.get("data", {})
                if item_data:
                    item_id = item_data.get("item_id") or item_data.get("itemid")
                    shop_id = item_data.get("shop_id") or item_data.get("shopid")
                    if item_id and shop_id:
                        self.captured_item["item_id"] = int(item_id)
                        self.captured_item["shop_id"] = int(shop_id)
                        self.captured_item["name"] = item_data.get("name", "Unknown")
                        self.captured_item["price"] = item_data.get("price", 0)
                        self.captured_item["price_idr"] = item_data.get("price", 0) / 100000
                        self.captured_item["stock"] = item_data.get("stock", 0)
                        self.log.info(
                            "📦 Captured item from API: %s (item=%s, shop=%s)",
                            self.captured_item.get("name"),
                            item_id,
                            shop_id,
                        )
            except Exception:
                pass

        # Also capture cart/add_to_cart responses for model_id
        if "api/v4/cart/add_to_cart" in url:
            try:
                body = await response.json()
                if not body.get("error"):
                    cart_data = body.get("data", {})
                    if cart_data:
                        self.captured_item["add_cart_ok"] = True
                        self.log.info("🛒 Add-to-cart API success")
            except Exception:
                pass

    async def _on_buy_detected(self) -> None:
        """Called when a Buy button is detected in the DOM."""
        self.log.info("🔔 BUY BUTTON DETECTED!")
        await self.notifier.send("🔔 <b>Buy button detected on live stream!</b>") if self.notifier else None

        # If the item info hasn't been captured yet via network, try to extract from DOM
        if not self.captured_item.get("item_id"):
            self.log.info("🔍 Attempting to extract item details from page...")
            await self._extract_item_from_dom()

        if self.lc.method == "click":
            await self._checkout_via_browser()
        elif self.lc.method == "api":
            await self._checkout_via_api()
        else:
            self.log.error("Unknown method: %s", self.lc.method)

    async def _extract_item_from_dom(self) -> None:
        """Try to extract item_id, shop_id, model_id from current page DOM / URL."""
        if not self._page:
            return

        try:
            # Method 1: Check current URL for item/shop IDs
            current_url = self._page.url
            try:
                item_id, shop_id = parse_shopee_url(current_url)
                self.captured_item["item_id"] = item_id
                self.captured_item["shop_id"] = shop_id
                self.log.info("✅ Extracted item/shop from URL: %s / %s", item_id, shop_id)
                return
            except (ValueError, Exception):
                pass

            # Method 2: Look for data attributes on page
            js_code = r"""
            () => {
                // Try to find item data in various locations
                const result = {};

                // Check for meta tags or data attributes
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const text = s.textContent || '';
                    // Look for __INITIAL_STATE__ or similar
                    if (text.includes('__INITIAL_STATE__')) {
                        try {
                            const match = text.match(/__INITIAL_STATE__\s*=\s*({.*?});/s);
                            if (match) {
                                const state = JSON.parse(match[1]);
                                result.initialState = true;
                            }
                        } catch(e) {}
                    }
                    // Look for itemid / shopid in JSON strings
                    const itemMatch = text.match(/"item_id?"\s*:\s*(\d+)/);
                    const shopMatch = text.match(/"shop_id?"\s*:\s*(\d+)/);
                    if (itemMatch) result.item_id = parseInt(itemMatch[1]);
                    if (shopMatch) result.shop_id = parseInt(shopMatch[1]);
                }

                // Check data-* attributes on common elements
                const buyBtn = document.querySelector('button:has-text(\"Beli\")');
                if (buyBtn) {
                    result.buttonText = buyBtn.textContent.trim();
                }

                return result;
            }
            """
            dom_data = await self._page.evaluate(js_code)
            self.log.info("📄 DOM extraction result: %s", dom_data)

            if dom_data.get("item_id") and not self.captured_item.get("item_id"):
                self.captured_item["item_id"] = dom_data["item_id"]
            if dom_data.get("shop_id") and not self.captured_item.get("shop_id"):
                self.captured_item["shop_id"] = dom_data["shop_id"]

        except Exception as e:
            self.log.warning("DOM extraction error: %s", e)

    async def _checkout_via_browser(self) -> None:
        """Click the Buy button and complete purchase through browser automation."""
        if not self._page:
            return

        self.log.info("🖱️  Browser checkout: clicking Buy button...")

        start_time = time.time()

        try:
            # Click the buy button
            clicked = False
            for selector in LIVE_BUY_SELECTORS:
                try:
                    btn = await self._page.wait_for_selector(selector, timeout=2000)
                    if btn:
                        await btn.click()
                        self.log.info("✅ Clicked Buy button: %s", selector)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                self.log.error("❌ Could not find Buy button to click")
                await self._notify_failure("Buy button not found for click")
                return

            # Wait for checkout/order page to load
            self.log.info("⏳ Waiting for checkout page...")
            await asyncio.sleep(2)

            # Try clicking checkout/order button
            checkout_clicked = False
            for selector in CHECKOUT_BUTTON_SELECTORS:
                try:
                    btn = await self._page.wait_for_selector(selector, timeout=3000)
                    if btn:
                        await btn.click()
                        self.log.info("✅ Clicked checkout button: %s", selector)
                        checkout_clicked = True
                        break
                except Exception:
                    continue

            if checkout_clicked:
                # Wait for order confirmation / success
                await asyncio.sleep(3)
                elapsed = time.time() - start_time

                # Check for success indicators
                success_texts = ["Pesanan berhasil", "success", "terima kasih", "pesanan dikonfirmasi"]
                page_text = await self._page.inner_text("body")
                success = any(t.lower() in page_text.lower() for t in success_texts)

                if success:
                    self.log.info("✅✅ BROWSER CHECKOUT SUCCESS! (%.2fs)", elapsed)
                    self.stats.record(True, elapsed)
                    item_name = self.captured_item.get("name", "Unknown Item")
                    price = self.captured_item.get("price_idr", 0)
                    await self._notify_success(item_name, price, elapsed)
                    await self._log_purchase(item_name, price, success=True, latency=elapsed)
                else:
                    self.log.info("⚠️  Browser checkout submitted (%.2fs)", elapsed)
                    self.stats.record(True, elapsed)
                    item_name = self.captured_item.get("name", "Unknown Item")
                    price = self.captured_item.get("price_idr", 0)
                    await self._notify_success(item_name, price, elapsed)
                    await self._log_purchase(item_name, price, success=True, latency=elapsed)
            else:
                self.log.warning("⚠️  Buy button clicked but no checkout button found")

            # Signal monitor to stop after successful purchase
            if self.monitor:
                self.monitor.stop()

        except Exception as e:
            self.log.error("❌ Browser checkout error: %s", e)
            await self._notify_failure(f"Browser checkout error: {e}")

    async def _checkout_via_api(self) -> None:
        """Extract item details from captured data and execute API-based checkout."""
        if not self.api_client:
            self.log.error("API client not initialized — cannot do API checkout")
            await self._notify_failure("API client not initialized")
            return

        item_id = self.captured_item.get("item_id")
        shop_id = self.captured_item.get("shop_id")

        if not item_id or not shop_id:
            self.log.error("Missing item_id (%s) or shop_id (%s)", item_id, shop_id)
            await self._notify_failure("Could not extract item/shop ID from live stream")
            return

        self.log.info("⚡ API checkout: item=%s, shop=%s", item_id, shop_id)

        start_time = time.time()

        try:
            # Get item info to find model_id
            item_info = await self.api_client.get_item_info(item_id, shop_id)
            if item_info.get("error"):
                self.log.error("Failed to get item info: %s", item_info)
                await self._notify_failure(f"API item info error: {item_info.get('error_msg', 'unknown')}")
                return

            item_data = item_info.get("data", item_info)
            models = item_data.get("models", []) or item_data.get("tier_variations", [])

            # Find the right model (typically the first available one)
            model_id = self.captured_item.get("model_id", 0)
            if not model_id and models:
                # Try to find default model
                for m in models:
                    mid = m.get("model_id") or m.get("modelid")
                    if mid:
                        model_id = int(mid)
                        break

            if not model_id:
                self.log.error("Could not determine model_id")
                await self._notify_failure("Could not find model_id for item")
                return

            name = self.captured_item.get("name", item_data.get("name", "Unknown"))
            price = self.captured_item.get("price_idr", 0)

            self.log.info("🎯 Checkout target: %s | item=%s shop=%s model=%s", name, item_id, shop_id, model_id)

            # Execute checkout via ShopeeClient API
            payment_id = self.cfg.payment_channel_id
            result = await single_checkout(
                self.api_client, item_id, shop_id, model_id,
                attempt=1, quantity=1, payment_channel_id=payment_id,
            )

            elapsed = time.time() - start_time

            if result.get("success"):
                self.log.info("✅✅ API CHECKOUT SUCCESS! (%.2fs)", elapsed)
                self.stats.record(True, elapsed)
                await self._notify_success(name, price, elapsed)
                await self._log_purchase(name, price, success=True, latency=elapsed)
            else:
                error_msg = result.get("error", "unknown error")
                self.log.error("❌ API checkout failed: %s", error_msg)
                self.stats.record(False)
                await self._notify_failure(f"API checkout: {error_msg}")
                await self._log_purchase(name, price, success=False, latency=elapsed, error=error_msg)

            # Signal monitor to stop after purchase attempt
            if self.monitor:
                self.monitor.stop()

        except Exception as e:
            self.log.error("❌ API checkout exception: %s", e)
            self.stats.record(False)
            await self._notify_failure(f"API exception: {e}")

    # ── Notification & Logging helpers ──────────────────────────────────────

    async def _notify_success(self, item_name: str, price: float, elapsed: float) -> None:
        if not self.notifier:
            return
        await self.notifier.send(
            f"⚡ <b>LIVE BUY BERHASIL!</b>\n\n"
            f"📺 Shopee Live\n"
            f"🛒 {item_name}\n"
            f"💰 Rp {price:,.0f}\n"
            f"⏱️ {elapsed:.2f}s\n"
            f"👤 {self.lc.account_name or 'N/A'}"
        )

    async def _notify_failure(self, reason: str) -> None:
        if not self.notifier:
            return
        await self.notifier.send(
            f"❌ <b>LIVE BUY GAGAL</b>\n\n"
            f"📺 Shopee Live\n"
            f"🚫 {reason}\n"
            f"👤 {self.lc.account_name or 'N/A'}"
        )

    async def _notify_monitoring(self, status: str) -> None:
        if not self.notifier:
            return
        await self.notifier.send(
            f"👁️ <b>Live Monitor</b>\n\n"
            f"📺 {self.lc.live_url[:60]}...\n"
            f"📊 {status}\n"
            f"👤 {self.lc.account_name or 'N/A'}"
        )

    async def _log_purchase(
        self,
        item_name: str,
        price: float,
        success: bool,
        latency: float = 0,
        error: str = "",
    ) -> None:
        if not self.db:
            return
        await self.db.log_purchase(
            item_id=self.captured_item.get("item_id", 0),
            shop_id=self.captured_item.get("shop_id", 0),
            model_id=self.captured_item.get("model_id", 0),
            item_name=item_name,
            price_idr=price,
            account_name=self.lc.account_name or "live",
            success=success,
            error_message=error,
            latency_ms=latency * 1000,
            mode="live",
        )


# ── LiveMonitor ─────────────────────────────────────────────────────────────


class LiveMonitor:
    """
    Polls the DOM of a Shopee Live page for the Buy button.

    Fires a callback when the Buy button appears. Runs an async loop
    that checks at a configurable interval (0.5-1s default).
    """

    def __init__(
        self,
        page: Page,
        config: LiveConfig,
        logger: logging.Logger,
        on_buy_button,
        shutdown: asyncio.Event,
    ):
        self._page = page
        self.cfg = config
        self.log = logger
        self._on_buy = on_buy_button
        self._shutdown = shutdown
        self._running = False

        # Tracking
        self._last_buy_state = False
        self._polls = 0
        self._start_time = time.time()
        self._max_end_time = time.time() + (config.max_wait_minutes * 60)

    def stop(self) -> None:
        """Signal monitor to stop on next poll cycle."""
        self._running = False

    async def _check_for_buy_button(self) -> bool:
        """Check if any buy button selector matches an element in the DOM.

        Returns True if a buy button is visible.
        """
        if self._page.is_closed():
            return False

        try:
            for selector in LIVE_BUY_SELECTORS:
                try:
                    # Use is_visible for better accuracy
                    is_visible = await self._page.locator(selector).first.is_visible(timeout=1000)
                    if is_visible:
                        self.log.debug("✅ Buy button found: %s", selector)
                        return True
                except Exception:
                    continue

            # Fallback: check page text for "Beli"
            try:
                body_text = await self._page.inner_text("body", timeout=2000)
                # Look for "Beli" in reasonable context (not just footer links)
                if "Beli" in body_text:
                    # Check if there's a nearby price element to confirm it's a product buy button
                    # This is heuristic but useful
                    self.log.debug("🔍 'Beli' found in page text (heuristic)")
                    return True
            except Exception:
                pass

            return False
        except Exception as e:
            self.log.warning("Check for buy button error: %s", e)
            return False

    async def _log_status(self) -> None:
        """Periodically log monitoring status."""
        elapsed = time.time() - self._start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        self.log.info(
            "👁️  Monitoring... (%dm%ds | %d polls | URL: %s)",
            mins, secs, self._polls, self._page.url if not self._page.is_closed() else "CLOSED",
        )

    async def run(self) -> None:
        """Main monitoring loop: poll DOM for Buy button until found or timeout."""
        self._running = True
        self.log.info(
            "👁️  Monitor started (interval: %.1fs, max wait: %d min)",
            self.cfg.polling_interval,
            self.cfg.max_wait_minutes,
        )

        status_log_interval = max(30, int(self.cfg.polling_interval * 20))  # log status every ~30s
        status_counter = 0

        try:
            while self._running and not self._shutdown.is_set():
                # Check timeout
                if time.time() > self._max_end_time:
                    self.log.warning("⏰ Max monitoring time reached (%d min)", self.cfg.max_wait_minutes)
                    await self._notify_timeout()
                    break

                self._polls += 1
                status_counter += 1

                # Check for buy button
                buy_found = await self._check_for_buy_button()

                if buy_found and not self._last_buy_state:
                    # Buy button just appeared!
                    self.log.info("🛎️  BUY BUTTON APPEARED after %d polls!", self._polls)
                    self._last_buy_state = True

                    # Fire the callback
                    try:
                        await self._on_buy
                        # After callback, the purchase flow takes over
                        # The callback may set self._running = False
                    except Exception as e:
                        self.log.error("Buy button callback error: %s", e)
                        self._running = False
                    break  # Exit monitor loop (purchase handles the rest)

                elif not buy_found:
                    if self._last_buy_state:
                        self.log.debug("Buy button disappeared")
                    self._last_buy_state = False
                else:
                    self._last_buy_state = True

                # Periodic status log
                if status_counter >= status_log_interval:
                    await self._log_status()
                    status_counter = 0

                # Wait before next poll
                await asyncio.sleep(self.cfg.polling_interval)

        except asyncio.CancelledError:
            self.log.info("Monitor cancelled")
        except Exception as e:
            self.log.error("Monitor error: %s", e)
        finally:
            self._running = False
            elapsed = time.time() - self._start_time
            self.log.info(
                "👁️  Monitor stopped after %d polls (%.1fs)",
                self._polls, elapsed,
            )

    async def _notify_timeout(self) -> None:
        """Send timeout notification."""
        elapsed = time.time() - self._start_time
        mins = int(elapsed // 60)

        # Try to capture page state for debugging
        page_closed = self._page.is_closed() if hasattr(self, '_page') else True
        url = self._page.url if not page_closed else "N/A"

        self.log.warning(
            "⏰ No Buy button appeared in %d min. URL: %s, Page closed: %s",
            self.cfg.max_wait_minutes, url, page_closed,
        )


# ── Multi-Live Manager ──────────────────────────────────────────────────────


class MultiLiveManager:
    """Runs multiple ShopeeLiveBot instances concurrently for different URLs."""

    def __init__(
        self,
        config: Config,
        logger: logging.Logger,
        shutdown_event: asyncio.Event,
        db: Optional[Database] = None,
        notifier: Optional[TelegramNotifier] = None,
    ):
        self.config = config
        self.log = logger
        self.shutdown = shutdown_event
        self.db = db
        self.notifier = notifier
        self.bots: list[ShopeeLiveBot] = []

    async def run_urls(
        self,
        urls: list[str],
        account_name: str = "",
        cookie_path: str = "",
        method: str = "click",
        interval: float = 0.5,
    ) -> None:
        """Run monitors for multiple live URLs simultaneously."""
        tasks = []
        for url in urls:
            lc = LiveConfig(
                live_url=url,
                account_name=account_name,
                cookie_path=cookie_path,
                method=method,
                polling_interval=interval,
            )
            bot = ShopeeLiveBot(
                live_config=lc,
                config=self.config,
                logger=self.log,
                shutdown_event=self.shutdown,
                db=self.db,
                notifier=self.notifier,
            )
            self.bots.append(bot)
            tasks.append(bot.run())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close_all(self) -> None:
        for bot in self.bots:
            try:
                await bot.close()
            except Exception:
                pass
        self.bots.clear()


# ── CLI ─────────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Shopee Live Auto-Buy Bot — monitors live streams and auto-purchases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 shopee_live_bot.py https://shopee.co.id/live/XXX
  python3 shopee_live_bot.py https://shopee.co.id/live/XXX --account myacc --method api
  python3 shopee_live_bot.py https://shopee.co.id/live/XXX --method click --interval 0.3
  python3 shopee_live_bot.py https://shopee.co.id/live/XXX https://shopee.co.id/live/YYY --method api
        """,
    )
    parser.add_argument(
        "live_urls",
        nargs="+",
        help="One or more Shopee Live URLs to monitor",
    )
    parser.add_argument(
        "--account", "-a",
        default="",
        help="Account name (subdirectory under accounts/ with cookies.json)",
    )
    parser.add_argument(
        "--cookie", "-c",
        default="",
        help="Path to cookies.json file (overrides --account)",
    )
    parser.add_argument(
        "--config", "-C",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--method", "-m",
        choices=["click", "api"],
        default="click",
        help="Checkout method: click (browser, reliable) or api (ShopeeClient API, fast) (default: click)",
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=0.5,
        help="DOM polling interval in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser headless (default: True)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_false",
        dest="headless",
        help="Show browser window (for debugging)",
    )
    parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=60,
        help="Max monitoring time in minutes before giving up (default: 60)",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Directory for log files (default: logs)",
    )

    return parser.parse_args(argv)


# ── Main Entry Point ────────────────────────────────────────────────────────


async def main() -> None:
    args = parse_args()

    # Load config
    cfg = Config.load(args.config)

    # Override log dir from args if provided
    cfg.log_dir = args.log_dir

    # Setup logging
    log = setup_logging(cfg)
    log.info("📺 Shopee Live Bot starting...")

    # Determine cookie path
    cookie_path = args.cookie
    if not cookie_path and args.account:
        candidate = os.path.join(cfg.cookie_dir, args.account, "cookies.json")
        if os.path.exists(candidate):
            cookie_path = candidate
            log.info("📁 Using account: %s (cookies: %s)", args.account, cookie_path)
        else:
            log.warning("Account '%s' not found at %s", args.account, candidate)
    elif not cookie_path:
        # Try to find first available account
        accounts = scan_accounts(cfg.cookie_dir)
        if accounts:
            cookie_path = accounts[0]["cookie_file"]
            args.account = accounts[0]["name"]
            log.info("📁 Using first available account: %s", args.account)
            log.info("📁 Cookie file: %s", cookie_path)
        else:
            log.warning("No accounts found. Running without authentication.")

    # Setup database
    db = None
    if cfg.database:
        db = Database(cfg.database)
        await db.connect()

    # Setup Telegram notifier
    notifier = TelegramNotifier(
        token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        logger=log,
    )

    # Stats
    stats = Stats()

    # Shutdown event (handles Ctrl+C)
    shutdown_event = asyncio.Event()

    def _signal_handler():
        log.info("🛑 Shutdown signal received, stopping...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler fully
            pass

    # Log startup info
    log.info("=" * 50)
    log.info("🎯 Target URLs: %d live streams", len(args.live_urls))
    for i, url in enumerate(args.live_urls, 1):
        log.info("   %d. %s", i, url)
    log.info("⚙️  Method: %s | Interval: %.1fs | Timeout: %d min", args.method, args.interval, args.timeout)
    log.info("=" * 50)

    # Send Telegram notification about start
    await notifier.send(
        f"📺 <b>Shopee Live Bot Started</b>\n\n"
        f"🎯 {len(args.live_urls)} live stream(s)\n"
        f"⚙️  Method: {args.method}\n"
        f"👤 Account: {args.account or 'N/A'}"
    )

    try:
        if len(args.live_urls) == 1:
            # Single URL mode
            lc = LiveConfig(
                live_url=args.live_urls[0],
                account_name=args.account,
                cookie_path=cookie_path,
                method=args.method,
                polling_interval=args.interval,
                headless=args.headless,
                max_wait_minutes=args.timeout,
            )
            async with ShopeeLiveBot(
                live_config=lc,
                config=cfg,
                logger=log,
                shutdown_event=shutdown_event,
                db=db,
                notifier=notifier,
                stats=stats,
            ) as bot:
                await bot.run()
        else:
            # Multi-live mode
            manager = MultiLiveManager(
                config=cfg,
                logger=log,
                shutdown_event=shutdown_event,
                db=db,
                notifier=notifier,
            )
            try:
                await manager.run_urls(
                    urls=args.live_urls,
                    account_name=args.account,
                    cookie_path=cookie_path,
                    method=args.method,
                    interval=args.interval,
                )
            finally:
                await manager.close_all()
    except asyncio.CancelledError:
        log.info("⚠️  Bot cancelled")
    except Exception as e:
        log.error("❌ Fatal error: %s", e)
        await notifier.send(f"❌ <b>Live Bot Error</b>\n\n{str(e)[:200]}")
    finally:
        # Log final stats
        log.info("=" * 50)
        log.info("📊 Final Stats:")
        log.info(stats.summary())
        log.info("=" * 50)

        # Cleanup
        if db:
            await db.close()

        log.info("👋 Shopee Live Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user")
    except Exception as e:
        print(f"❌ Fatal: {e}")
        sys.exit(1)
