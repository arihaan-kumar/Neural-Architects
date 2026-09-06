"""WebSocket hub for live alert and chat streaming."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("weathergpt.ws")


class ConnectionHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.connections.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.connections:
            return
        data = json.dumps(payload, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self.connections):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


hub = ConnectionHub()
