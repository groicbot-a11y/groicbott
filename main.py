import os
import time
import json
import base64
import logging
import threading
import websocket
from dotenv import load_dotenv
from greeting import generate_greeting
from auto_token_fetch import fetch_token_sync, validate_token, get_token_expiry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Load environment variables
load_dotenv()

PLACEHOLDER_SUBSTRINGS = ["paste_your_", "your_firebase_", "your_groic_", "your_room_"]

def is_placeholder(val: str) -> bool:
    if not val or not val.strip():
        return True
    val_lower = val.strip().lower()
    return any(p in val_lower for p in PLACEHOLDER_SUBSTRINGS)

def parse_room_code(val: str) -> str:
    """
    Extract clean room code/UID from raw room code, room ID, or full Groic room URL.
    Examples:
        - "0bi6y6kzgv" -> "0bi6y6kzgv"
        - "https://groic.in/room/0bi6y6kzgv?autoJoin=true" -> "0bi6y6kzgv"
    """
    if not val or not isinstance(val, str):
        return ""
    val = val.strip()
    if "groic.in/room/" in val:
        after = val.split("groic.in/room/")[-1]
        return after.split("?")[0].split("/")[0].strip()
    if "/" in val:
        val = val.split("/")[-1].split("?")[0].strip()
    return val

# How long before a token's real expiry we should quietly refresh it, and how
# often to check when we can't determine an expiry from the token itself.
TOKEN_REFRESH_BUFFER_SECONDS = 300
TOKEN_REFRESH_FALLBACK_INTERVAL = 1800

DEFAULT_WSS_ENDPOINTS = [
    "wss://socket-v2.groic.in/socket.io/?EIO=4&transport=websocket"
]

class GroicBotEngine:
    def __init__(self):
        self.firebase_token = os.getenv("FIREBASE_AUTH_TOKEN", "").strip()
        self.groic_refresh = os.getenv("GROIC_REFRESH_TOKEN", "").strip()
        self.firebase_api_key = os.getenv("GROIC_FIREBASE_API_KEY", "").strip()
        
        # Read room code / UID / URL
        raw_room = os.getenv("ROOM_CODE") or os.getenv("ROOM_UID") or os.getenv("GROIC_ROOM_URL") or ""
        self.room_code = parse_room_code(raw_room)
        self.room_uid = self.room_code  # Compatibility alias

        # Bot image / avatar URL
        self.image_url = os.getenv("BOT_IMAGE_URL", "https://i.ibb.co/zH704h1Z/IMG-20260921-192034-565.jpg").strip()

        # Built-in WSS endpoint resolution (user does not need to configure WSS URLs)
        env_wss = os.getenv("GROIC_WSS_URL", "").strip()
        self.wss_endpoints = [env_wss] if env_wss else DEFAULT_WSS_ENDPOINTS
        
        self.ws = None
        self.running = True
        self.active_users = set()
        self.welcomed_cache = set()

    def ensure_valid_token(self) -> bool:
        if not validate_token(self.firebase_token):
            logging.debug("[AutoFetch] Token missing, invalid, or placeholder. Fetching quietly...")
            fetched_token = fetch_token_sync()
            if fetched_token and validate_token(fetched_token):
                self.firebase_token = fetched_token
                logging.info("[AutoFetch] Token fetched successfully.")
                from auto_token_fetch import update_firebase_photo_url
                update_firebase_photo_url(self.firebase_api_key, self.firebase_token, self.image_url)
                return True
            else:
                logging.error("[AutoFetch] Failed to fetch a valid token automatically.")
                return False
        else:
            from auto_token_fetch import update_firebase_photo_url
            update_firebase_photo_url(self.firebase_api_key, self.firebase_token, self.image_url)
        return True

    # ------------------------------------------------------------------
    # Silent background token refresh
    #
    # Runs on its own daemon thread and never touches the WebSocket / room
    # connection. It just keeps self.firebase_token fresh ahead of time so
    # the bot is never forced to drop out of the room to fetch a new one.
    # ------------------------------------------------------------------
    def _seconds_until_next_refresh(self) -> float:
        exp = get_token_expiry(self.firebase_token)
        if exp:
            remaining = exp - time.time() - TOKEN_REFRESH_BUFFER_SECONDS
            if remaining > 0:
                return remaining
            return 30  # already at/near expiry, refresh soon
        return TOKEN_REFRESH_FALLBACK_INTERVAL

    def _token_refresh_worker(self):
        logging.debug("[TokenRefresher] Background token refresher started.")
        while self.running:
            wait_for = self._seconds_until_next_refresh()
            slept = 0
            # Sleep in short increments so shutdown (self.running=False) is responsive.
            while slept < wait_for and self.running:
                chunk = min(5, wait_for - slept)
                time.sleep(chunk)
                slept += chunk

            if not self.running:
                break

            try:
                fresh_token = fetch_token_sync()
                if fresh_token and validate_token(fresh_token) and fresh_token != self.firebase_token:
                    self.firebase_token = fresh_token
                    logging.info("[TokenRefresher] Token refreshed silently in the background.")
                    from auto_token_fetch import update_firebase_photo_url
                    update_firebase_photo_url(self.firebase_api_key, self.firebase_token, self.image_url)
                else:
                    logging.debug("[TokenRefresher] No new token needed yet; will check again later.")
            except Exception as e:
                logging.debug(f"[TokenRefresher] Background refresh attempt failed silently: {e}")

    def start_background_token_refresher(self):
        if getattr(self, "_refresher_started", False):
            return
        self._refresher_started = True
        t = threading.Thread(target=self._token_refresh_worker, daemon=True, name="TokenRefresher")
        t.start()

    def validate_config(self) -> bool:
        missing = []
        if not self.ensure_valid_token():
            missing.append("FIREBASE_AUTH_TOKEN")
        if is_placeholder(self.room_code):
            missing.append("ROOM_CODE")

        if missing:
            logging.error("Configuration incomplete! Please update your .env file.")
            logging.error(f"Missing or placeholder variables: {', '.join(missing)}")
            logging.info("Set ROOM_CODE (e.g. ROOM_CODE=0bi6y6kzgv or room URL) in .env file.")
            return False
        return True

    def on_message(self, ws, message):
        """
        Engine.IO / Socket.IO message handler focused ONLY on greetings when users join.
        """
        try:
            if isinstance(message, bytes):
                message = message.decode('utf-8', errors='ignore')

            # 1. Engine.IO Ping -> Pong ("2" -> "3")
            if message == "2" or message.startswith("2"):
                try:
                    ws.send("3")
                except Exception as e:
                    logging.warning(f"Failed to send Engine.IO pong: {e}")
                return

            # 2. Engine.IO OPEN (0) -> Connect Socket.IO (40)
            if message.startswith("0"):
                logging.info("[WS] Server handshake open packet received. Sending Socket.IO connect...")
                auth_payload = json.dumps({"token": str(self.firebase_token), "Authorization": str(self.firebase_token)})
                ws.send(f"40{auth_payload}")
                return

            # 3. Socket.IO CONNECT ACK (40) -> Join room code
            if message.startswith("40"):
                if "error" in message.lower() or "unauthorized" in message.lower():
                    logging.error(f"[WS Auth Error] Server rejected token: {message}")
                    self.firebase_token = ""
                    return

                logging.info(f"[WS] Connected to Groic! Joining room code: {self.room_code}")
                join_payload = json.dumps(["joinRoom", {
                    "roomUid": str(self.room_code),
                    "imageUrl": self.image_url,
                    "avatarUrl": self.image_url,
                    "avatar": self.image_url,
                    "photoURL": self.image_url
                }])
                ws.send(f"42{join_payload}")
                return

            # 4. Socket.IO EVENT (42) -> Process room presence, user joins & chat commands
            if message.startswith("42"):
                raw_json = message[2:]
                try:
                    parsed = json.loads(raw_json)
                except Exception:
                    return

                if isinstance(parsed, list) and len(parsed) > 0:
                    event_name = parsed[0]
                    event_data = parsed[1] if len(parsed) > 1 else {}

                    # Presence Update (user list change when users join/leave room)
                    if event_name == "presenceUpdate" and isinstance(event_data, dict):
                        if "activeUsers" in event_data:
                            new_users = {
                                u.get("username", "").strip()
                                for u in event_data["activeUsers"]
                                if isinstance(u, dict) and u.get("username")
                            }
                            if not self.active_users:
                                # Initial room population
                                self.active_users = new_users
                            else:
                                newly_joined = new_users - self.active_users
                                self.active_users = new_users
                                for user in newly_joined:
                                    if user:
                                        welcome_msg = generate_greeting(user)
                                        self.send_message(welcome_msg)

                    # Direct user join event
                    elif event_name in ["join", "enter", "user_joined", "userJoined", "joinRoom"]:
                        user_info = event_data.get("user") or event_data.get("data") or event_data if isinstance(event_data, dict) else {}
                        username = "Guest"
                        if isinstance(user_info, dict):
                            username = (
                                user_info.get("username")
                                or user_info.get("name")
                                or user_info.get("display_name")
                                or "Guest"
                            )
                        if username and username not in self.welcomed_cache:
                            self.welcomed_cache.add(username)
                            welcome_msg = generate_greeting(username)
                            self.send_message(welcome_msg)

                    # Chat Command Handler (e.g. !ping)
                    elif event_name in ["chatMessage", "sendChat", "receiveChat", "message", "new_message"]:
                        msg_text = ""
                        if isinstance(event_data, dict):
                            msg_text = event_data.get("message") or event_data.get("text") or ""
                        elif isinstance(event_data, str):
                            msg_text = event_data

                        if msg_text:
                            msg_text = msg_text.strip()
                            if msg_text.startswith("!ping"):
                                self.send_message("pong 🏓 Bot is online & active!")

        except Exception as e:
            logging.warning(f"Error processing message: {e}")

    def on_error(self, ws, error):
        logging.error(f"[WS Error] {error}")

    def on_close(self, ws, close_status_code, close_msg):
        logging.info(f"[WS] Connection closed (code: {close_status_code}, msg: {close_msg})")

    def on_open(self, ws):
        logging.info("WebSocket transport established. Waiting for server Engine.IO handshake...")

    def send_message(self, text):
        if self.ws and hasattr(self.ws, 'sock') and self.ws.sock and self.ws.sock.connected:
            chat_payload = {
                "message": str(text),
                "text": str(text),
                "roomUid": str(self.room_code),
                "avatarUrl": self.image_url,
                "avatar": self.image_url,
                "photoURL": self.image_url,
                "user": {
                    "imageUrl": self.image_url,
                    "avatarUrl": self.image_url,
                    "avatar": self.image_url,
                    "photoURL": self.image_url
                }
            }
            try:
                pkt = json.dumps(["sendChat", chat_payload])
                self.ws.send(f"42{pkt}")
                logging.info(f"[Sent Chat] {text}")
            except Exception as e:
                logging.error(f"Failed to send message: {e}")
        else:
            logging.warning("WebSocket is not connected. Message not sent.")

    def run(self, auto_reconnect=True):
        if not self.validate_config():
            return

        # Keep the token fresh quietly in the background for the entire
        # lifetime of the bot, so the room connection is never dropped
        # just to fetch a new token.
        self.start_background_token_refresher()

        retry_delay = 5
        endpoint_idx = 0

        while self.running:
            headers = [
                f"Authorization: {self.firebase_token}",
                "Origin: https://groic.in",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language: en-US,en;q=0.9",
                "Cache-Control: no-cache",
                "x-app-version: web",
                "x-device-type: web"
            ]
            if self.groic_refresh:
                headers.append(f"x-groic-refresh: {self.groic_refresh}")

            target_wss = self.wss_endpoints[endpoint_idx % len(self.wss_endpoints)]
            logging.info(f"Connecting to room code '{self.room_code}' via WebSocket server ({target_wss})...")
            self.ws = websocket.WebSocketApp(
                target_wss,
                header=headers,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            # Disable control-frame ping loop in websocket-client (Socket.IO uses Engine.IO text heartbeats "2"/"3")
            self.ws.run_forever(ping_interval=0)

            if not auto_reconnect or not self.running:
                break

            # Safety net only: the background refresher above should already
            # keep this valid. This just covers the rare case where the
            # server rejected the token and on_message cleared it.
            if not validate_token(self.firebase_token):
                self.ensure_valid_token()

            endpoint_idx += 1
            logging.info(f"Reconnecting in {retry_delay} seconds...")
            time.sleep(retry_delay)

if __name__ == "__main__":
    bot = GroicBotEngine()
    bot.run()