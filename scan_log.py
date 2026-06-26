from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

events: deque = deque(maxlen=200)


def add(
    slug: str,
    question: str,
    price: float | None,
    result: str,
    reason: str,
    triggers: list | None = None,
    conviction: str | None = None,
) -> None:
    events.appendleft({
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "slug": slug[:35],
        "question": question[:70],
        "price": price,
        "result": result,
        "reason": reason,
        "triggers": triggers or [],
        "conviction": conviction,
    })
