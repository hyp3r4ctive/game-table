"""In-memory pub/sub for session state changes. Clients subscribe via SSE."""

import asyncio
from collections import defaultdict

_subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)


def subscribe(session_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _subscribers[session_id].append(q)
    return q


def unsubscribe(session_id: int, queue: asyncio.Queue) -> None:
    if queue in _subscribers.get(session_id, []):
        _subscribers[session_id].remove(queue)
    if session_id in _subscribers and not _subscribers[session_id]:
        del _subscribers[session_id]


def publish(session_id: int, event: str = "state_changed", payload: dict | None = None) -> None:
    msg = {"event": event, "payload": payload or {}}
    for q in list(_subscribers.get(session_id, [])):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass
