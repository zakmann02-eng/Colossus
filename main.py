#!/usr/bin/env python3
"""
Colossus Trading Alert Bot
Monitors Polymarket wallets and sends Telegram alerts with copy-trade recommendations.
"""
import argparse
import asyncio
import logging
import sys

import aiohttp

from config import Config
from polymarket.client import PolymarketClient
from telegram_bot.notifier import TelegramNotifier
from watchers.wallet_watcher import WalletWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log"),
    ],
)
logger = logging.getLogger("colossus")


async def run_bot(cfg: Config, show_positions_only: bool = False) -> None:
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        client = PolymarketClient(session)
        notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)

        watcher = WalletWatcher(
            client=client,
            poll_interval=cfg.poll_interval,
            copy_min_win_rate=cfg.copy_min_win_rate,
            copy_min_volume=cfg.copy_min_volume,
            alert_callback=notifier.send_async,
        )

        wallets = list(cfg.watched_wallets)

        if show_positions_only:
            for addr in wallets:
                label = cfg.wallet_labels.get(addr, addr[:8])
                await watcher.add_wallet(addr, label)
            await watcher.post_positions_summary()
            return

        for addr in wallets:
            label = cfg.wallet_labels.get(addr, addr[:8])
            await watcher.add_wallet(addr, label)

        await notifier.send_startup_message(cfg.wallet_labels)

        logger.info("Bot started — polling every %ds", cfg.poll_interval)
        await asyncio.gather(
            notifier.run_forever(),
            watcher.run_forever(),
        )


async def lookup_username(username: str) -> None:
    connector = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(connector=connector) as session:
        client = PolymarketClient(session)
        addr = await client.resolve_username(username)
        if addr:
            print(f"Username '{username}' → wallet address: {addr}")
            print(f"Add to .env:  WATCHED_WALLETS=...{addr}")
        else:
            print(f"Could not resolve username '{username}'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Colossus Trading Alert Bot")
    parser.add_argument("--lookup", metavar="USERNAME", help="Resolve a Polymarket username to a wallet address")
    parser.add_argument("--positions", action="store_true", help="Print current open positions and exit")
    args = parser.parse_args()

    if args.lookup:
        asyncio.run(lookup_username(args.lookup))
        return

    try:
        cfg = Config.load()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        sys.exit(1)

    if not cfg.watched_wallets:
        print("No wallets configured. Set WATCHED_WALLETS in your .env file.")
        sys.exit(1)

    try:
        asyncio.run(run_bot(cfg, show_positions_only=args.positions))
    except KeyboardInterrupt:
        logger.info("Stopped by user.")


if __name__ == "__main__":
    main()
