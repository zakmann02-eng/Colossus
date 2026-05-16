import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

_MAX_MSG_LEN = 4096


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self._bot = Bot(token=token)
        self._chat_id = chat_id
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def send(self, text: str) -> None:
        self._queue.put_nowait(text)

    async def send_async(self, text: str) -> None:
        self._queue.put_nowait(text)

    async def _dispatch(self, text: str) -> None:
        for chunk in self._split(text):
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=chunk,
                    parse_mode="Markdown",
                )
            except TelegramError:
                cleaned = chunk.replace("*", "").replace("`", "").replace("_", "")
                try:
                    await self._bot.send_message(chat_id=self._chat_id, text=cleaned)
                except TelegramError as exc:
                    logger.error("Telegram send failed: %s", exc)
            await asyncio.sleep(0.35)

    @staticmethod
    def _split(text: str) -> list[str]:
        if len(text) <= _MAX_MSG_LEN:
            return [text]
        chunks, current = [], []
        length = 0
        for line in text.splitlines(keepends=True):
            if length + len(line) > _MAX_MSG_LEN:
                chunks.append("".join(current))
                current, length = [], 0
            current.append(line)
            length += len(line)
        if current:
            chunks.append("".join(current))
        return chunks

    async def send_startup_message(self, wallets: dict[str, str]) -> None:
        lines = ["🚀 *Colossus Trading Bot — Online*\n", "👀 *Watching wallets:*"]
        for addr, label in wallets.items():
            lines.append(f"  • `{label}` — `{addr}`")
        lines += [
            "",
            "You'll get an alert on every new trade with a copy-trade recommendation.",
            "Type /positions in Telegram to see open positions (manual — just re-run with --positions flag).",
        ]
        await self._dispatch("\n".join(lines))

    async def run_forever(self) -> None:
        while True:
            text = await self._queue.get()
            await self._dispatch(text)
            self._queue.task_done()
