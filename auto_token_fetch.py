# auto_token_fetch.py
"""
Automatic Groic token fetcher using Firebase refresh token or Playwright browser automation.
Ported for Groic Token bot.
"""
import os
import json
import base64
import asyncio
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# Token state
_last_fetch_time = None
_last_fetch_success = False
_fetch_count = 0
_consecutive_failures = 0

MAX_RETRIES = 3
TOKEN_MIN_LENGTH = 20
_binary_missing = False

def _get_config():
    return {
        "GROIC_USERNAME": os.getenv("GROIC_USERNAME", ""),
        "GROIC_PASSWORD": os.getenv("GROIC_PASSWORD", ""),
        "GROIC_LOGIN_URL": os.getenv("GROIC_LOGIN_URL", "https://groic.in/login"),
        "GROIC_REFRESH_TOKEN": os.getenv("GROIC_REFRESH_TOKEN", ""),
        "GROIC_FIREBASE_API_KEY": os.getenv("GROIC_FIREBASE_API_KEY", ""),
        "GROIC_HEADLESS": os.getenv("GROIC_HEADLESS", "true").strip().lower() not in ("false", "0", "no")
    }

_JWT_SCRAPER_JS = r'''() => {
    const rx = /eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/;
    const scan = (v) => (typeof v === 'string' ? (v.match(rx) || [null])[0] : null);
    for (const store of [window.localStorage, window.sessionStorage]) {
        try {
            for (let i = 0; i < store.length; i++) {
                const hit = scan(store.getItem(store.key(i)));
                if (hit) return hit;
            }
        } catch (e) {}
    }
    return scan(document.cookie);
}'''

def validate_token(token):
    if not token or not isinstance(token, str):
        return False
    token = token.strip()
    if len(token) < TOKEN_MIN_LENGTH:
        return False
    if not token.startswith("eyJ"):
        return False
    return True

def get_token_expiry(token):
    """
    Decode a JWT's payload (without verifying signature) and return its
    'exp' claim as a unix timestamp, or None if it can't be determined.
    Used only to schedule a quiet background refresh ahead of expiry.
    """
    try:
        if not token or token.count(".") != 2:
            return None
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("exp")
    except Exception:
        return None

def _sync_firebase_refresh(api_key, refresh_token):
    """Blocking call to Google's secure-token endpoint; returns a fresh id_token."""
    url = "https://securetoken.googleapis.com/v1/token?key=" + api_key
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://groic.in/",
        "Origin": "https://groic.in"
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read()).get("id_token")

def update_firebase_photo_url(api_key, token, photo_url=None):
    if not photo_url:
        photo_url = os.getenv("BOT_IMAGE_URL", "https://i.ibb.co/zH704h1Z/IMG-20260921-192034-565.jpg").strip()
    if not api_key or not token or not photo_url:
        return False
    try:
        url = "https://identitytoolkit.googleapis.com/v1/accounts:update?key=" + api_key
        data = json.dumps({
            "idToken": token,
            "photoUrl": photo_url,
            "returnSecureToken": True
        }).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://groic.in/",
            "Origin": "https://groic.in"
        }
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode())
            if res.get("photoUrl"):
                logger.debug(f"[AutoFetch] Synced Firebase profile avatar photoUrl: {photo_url}")
                return True
    except Exception as e:
        logger.debug(f"[AutoFetch] Could not sync Firebase photoUrl: {e}")
    return False

async def refresh_via_firebase():
    """Mint a fresh Groic token from the Firebase refresh token (Google Sign-In accounts)."""
    global _last_fetch_time, _last_fetch_success, _fetch_count
    cfg = _get_config()
    refresh_token = cfg["GROIC_REFRESH_TOKEN"]
    api_key = cfg["GROIC_FIREBASE_API_KEY"]

    if not refresh_token:
        return None

    # If api_key is missing, use Google's public Identity Toolkit API key commonly used by Firebase default setups
    if not api_key:
        api_key = os.getenv("FIREBASE_API_KEY", "")

    if not api_key:
        logger.debug("[AutoFetch] GROIC_FIREBASE_API_KEY not set; skipping Firebase API refresh.")
        return None

    try:
        loop = asyncio.get_event_loop()
        token = await loop.run_in_executor(None, _sync_firebase_refresh, api_key, refresh_token)
        if token and validate_token(token):
            _last_fetch_success = True
            _fetch_count += 1
            _last_fetch_time = datetime.now(timezone.utc)
            logger.debug("[AutoFetch] Token refreshed via Firebase API.")
            update_firebase_photo_url(api_key, token)
            return token
        logger.debug("[AutoFetch] Firebase refresh returned invalid token.")
    except Exception as e:
        logger.debug(f"[AutoFetch] Firebase refresh failed: {e}")
    return None

async def fetch_token_async():
    global _consecutive_failures, _binary_missing

    # Primary attempt: Firebase Refresh API
    token = await refresh_via_firebase()
    if token:
        _consecutive_failures = 0
        return token

    if _binary_missing:
        logger.debug("[AutoFetch] Playwright binary missing. Skipping browser fetch.")
        return None

    cfg = _get_config()
    if not PLAYWRIGHT_AVAILABLE:
        logger.debug("[AutoFetch] Playwright not installed. Install with `pip install playwright && playwright install chromium`")
        return None

    if not cfg["GROIC_USERNAME"] or not cfg["GROIC_PASSWORD"]:
        logger.debug("[AutoFetch] Credentials (GROIC_USERNAME/GROIC_PASSWORD) not configured for browser login.")
        return None

    for attempt in range(1, MAX_RETRIES + 1):
        logger.debug(f"[AutoFetch] Browser login attempt {attempt}/{MAX_RETRIES}...")
        token = await _do_playwright_fetch(cfg)

        if token and validate_token(token):
            _consecutive_failures = 0
            return token

        if _binary_missing:
            return None

        if attempt < MAX_RETRIES:
            backoff = min(5 * (2 ** (attempt - 1)), 30)
            await asyncio.sleep(backoff)

    _consecutive_failures += 1
    return None

async def _do_playwright_fetch(cfg):
    global _last_fetch_time, _last_fetch_success, _fetch_count, _binary_missing

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=cfg["GROIC_HEADLESS"],
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'
            )
            page = await context.new_page()
            await page.goto(cfg["GROIC_LOGIN_URL"], wait_until='networkidle', timeout=45000)

            for selector in ['button', 'div.tertiary-button', 'text="Sign in"', 'a[href="/login"]']:
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        await asyncio.sleep(2)
                        break
                except Exception:
                    pass

            email_fields = ['input[type="email"]', 'input[name="email"]', 'input[name="username"]', 'input[placeholder*="Email"]']
            pass_fields = ['input[type="password"]', 'input[name="password"]', 'input[placeholder*="Password"]']

            email_filled = False
            for sel in email_fields:
                try:
                    if await page.locator(sel).first.is_visible(timeout=2000):
                        await page.locator(sel).first.fill(cfg["GROIC_USERNAME"])
                        email_filled = True
                        break
                except Exception:
                    pass

            pass_filled = False
            for sel in pass_fields:
                try:
                    if await page.locator(sel).first.is_visible(timeout=2000):
                        await page.locator(sel).first.fill(cfg["GROIC_PASSWORD"])
                        pass_filled = True
                        break
                except Exception:
                    pass

            if not email_filled or not pass_filled:
                logger.debug("[AutoFetch] Failed to locate login input fields.")
                await browser.close()
                return None

            try:
                await page.keyboard.press('Enter')
            except Exception:
                pass

            for _ in range(12):
                await asyncio.sleep(2)
                try:
                    token = await page.evaluate(_JWT_SCRAPER_JS)
                except Exception:
                    token = None
                if token and validate_token(token):
                    await browser.close()
                    _last_fetch_success = True
                    _fetch_count += 1
                    _last_fetch_time = datetime.now(timezone.utc)
                    return token

            await browser.close()
            return None
    except Exception as e:
        logger.debug(f"[AutoFetch] Error during browser fetch: {e}")
        if "Executable doesn't exist" in str(e):
            _binary_missing = True
        return None

def fetch_token_sync():
    """Synchronous entry point to auto-fetch token."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(fetch_token_async())