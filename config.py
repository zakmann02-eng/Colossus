import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise ValueError(f"Required env var '{key}' is not set. Copy .env.example → .env and fill it in.")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


class Config:
    telegram_token: str = ""
    telegram_chat_id: str = ""

    watched_wallets: list[str] = []
    wallet_labels: dict[str, str] = {}

    poll_interval: int = 30
    copy_min_win_rate: float = 0.55
    copy_min_volume: float = 10_000.0

    @classmethod
    def load(cls) -> "Config":
        c = cls()
        c.telegram_token = _require("TELEGRAM_BOT_TOKEN")
        c.telegram_chat_id = _require("TELEGRAM_CHAT_ID")

        wallets_raw = _optional("WATCHED_WALLETS", "")
        c.watched_wallets = [w.strip().lower() for w in wallets_raw.split(",") if w.strip()]

        labels_raw = _optional("WALLET_LABELS", "")
        labels = [l.strip() for l in labels_raw.split(",") if l.strip()]
        c.wallet_labels = {addr: (labels[i] if i < len(labels) else addr[:8]) for i, addr in enumerate(c.watched_wallets)}

        c.poll_interval = int(_optional("POLL_INTERVAL", "30"))
        c.copy_min_win_rate = float(_optional("COPY_TRADE_MIN_WIN_RATE", "0.55"))
        c.copy_min_volume = float(_optional("COPY_TRADE_MIN_VOLUME", "10000"))

        return c
