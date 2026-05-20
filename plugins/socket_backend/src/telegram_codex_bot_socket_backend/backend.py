"""Center-side socket backend for TelegramCodexBot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from telegram_codex_bot.backends.base import (
    AgentTarget,
    BackendInfo,
    CreateSessionRequest,
    CreateSessionResult,
    MessageCallback,
    SendResult,
)
from telegram_codex_bot.backends.browser import BrowserRoot, DirectoryListing
from telegram_codex_bot.session import CodexSession

from .protocol import (
    listing_from_dict,
    message_from_dict,
    root_from_dict,
    session_from_dict,
    target_session_id,
    target_to_dict,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeAddress:
    """One reachable agent node."""

    host: str
    port: int


def _parse_nodes(raw: str) -> dict[str, NodeAddress]:
    """Parse node_id=host:port pairs from an environment string."""
    nodes: dict[str, NodeAddress] = {}
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                "Socket node must use node_id=host:port, got " f"{item!r}"
            )
        node_id, address = item.split("=", 1)
        node_id = node_id.strip()
        host, sep, port_str = address.strip().rpartition(":")
        if not node_id or not sep or not host or not port_str.isdigit():
            raise ValueError(
                "Socket node must use node_id=host:port, got " f"{item!r}"
            )
        nodes[node_id] = NodeAddress(host=host, port=int(port_str))
    return nodes


class SocketClusterBackend:
    """Backend that proxies Codex work to agent nodes over TCP sockets."""

    backend_id = "socket-cluster"

    def __init__(
        self,
        *,
        nodes: dict[str, NodeAddress] | None = None,
        timeout: float | None = None,
        reconnect_delay: float | None = None,
    ) -> None:
        raw_nodes = os.getenv("TELEGRAM_CODEX_BOT_SOCKET_NODES", "")
        self.nodes = nodes if nodes is not None else _parse_nodes(raw_nodes)
        self.timeout = timeout or float(
            os.getenv("TELEGRAM_CODEX_BOT_SOCKET_TIMEOUT", "20")
        )
        self.reconnect_delay = reconnect_delay or float(
            os.getenv("TELEGRAM_CODEX_BOT_SOCKET_RECONNECT_DELAY", "5")
        )
        self._message_callback: MessageCallback | None = None
        self._subscribe_tasks: list[asyncio.Task[None]] = []
        self._running = False

    def info(self) -> BackendInfo:
        return BackendInfo(
            backend_id=self.backend_id,
            display_name="Socket agent cluster",
            mode="remote",
        )

    def prepare(self) -> None:
        """Validate node configuration before polling starts."""
        if not self.nodes:
            raise ValueError(
                "TELEGRAM_CODEX_BOT_SOCKET_NODES is required for socket-cluster"
            )

    async def start(self, message_callback: MessageCallback) -> None:
        """Start subscription loops for node transcript events."""
        if self._running:
            logger.warning("Socket backend already started")
            return
        self._message_callback = message_callback
        self._running = True
        for node_id in self.nodes:
            self._subscribe_tasks.append(
                asyncio.create_task(self._subscribe_loop(node_id))
            )

    async def stop(self) -> None:
        """Stop subscription loops."""
        self._running = False
        for task in self._subscribe_tasks:
            task.cancel()
        if self._subscribe_tasks:
            await asyncio.gather(*self._subscribe_tasks, return_exceptions=True)
        self._subscribe_tasks.clear()

    async def create_session(
        self, request: CreateSessionRequest
    ) -> CreateSessionResult:
        """Create or resume a Codex session on the selected node."""
        node_id = request.node_id or self._single_node_id()
        if not node_id:
            return CreateSessionResult(
                ok=False,
                message="Choose an agent node before creating a session.",
            )
        try:
            result = await self._request(
                node_id,
                {
                    "op": "create_session",
                    "cwd": request.cwd,
                    "window_name": request.window_name,
                    "resume_session_id": request.resume_session_id,
                    "account_name": request.account_name,
                },
            )
        except Exception as exc:
            logger.exception("Socket create_session failed on %s", node_id)
            return CreateSessionResult(ok=False, message=str(exc))

        ok = bool(result.get("ok"))
        window_id = str(result.get("window_id", ""))
        session_key = str(result.get("session_id", "")) or target_session_id(
            node_id, window_id
        )
        target = (
            AgentTarget(
                backend_id=self.backend_id,
                node_id=node_id,
                session_id=session_key,
                window_id=window_id,
            )
            if ok
            else None
        )
        return CreateSessionResult(
            ok=ok,
            message=str(result.get("message", "")),
            target=target,
            display_name=str(result.get("display_name", "")),
        )

    async def send_message(self, target: AgentTarget, text: str) -> SendResult:
        return await self._send_to_target(target, {"op": "send_message", "text": text})

    async def send_control(self, target: AgentTarget, key: str) -> SendResult:
        return await self._send_to_target(target, {"op": "send_control", "key": key})

    async def capture(
        self,
        target: AgentTarget,
        *,
        with_ansi: bool = False,
    ) -> str | None:
        try:
            result = await self._request(
                target.node_id,
                {
                    "op": "capture",
                    "target": target_to_dict(target),
                    "with_ansi": with_ansi,
                },
            )
        except Exception:
            logger.exception("Socket capture failed for %s", target)
            return None
        text = result.get("text")
        return str(text) if text is not None else None

    async def list_roots(self) -> list[BrowserRoot]:
        """Return project roots exposed by every configured node."""
        roots: list[BrowserRoot] = []
        for node_id in self.nodes:
            try:
                result = await self._request(node_id, {"op": "list_roots"})
            except Exception:
                logger.exception("Socket list_roots failed on %s", node_id)
                continue
            for item in result.get("roots", []):
                if isinstance(item, dict):
                    root = root_from_dict(item)
                    roots.append(
                        BrowserRoot(
                            label=root.label or node_id,
                            path=root.path,
                            backend_id=self.backend_id,
                            node_id=node_id,
                        )
                    )
        return roots

    async def list_directory(
        self,
        node_id: str,
        path: str,
        *,
        root_path: str = "",
    ) -> DirectoryListing:
        result = await self._request(
            node_id,
            {
                "op": "list_directory",
                "path": path,
                "root_path": root_path,
            },
        )
        return listing_from_dict(result.get("listing", {}))

    async def list_sessions(self, node_id: str, cwd: str) -> list[CodexSession]:
        result = await self._request(
            node_id,
            {
                "op": "list_sessions",
                "cwd": cwd,
            },
        )
        sessions: list[CodexSession] = []
        for item in result.get("sessions", []):
            if isinstance(item, dict):
                sessions.append(session_from_dict(item))
        return sessions

    async def _send_to_target(
        self,
        target: AgentTarget,
        payload: dict[str, Any],
    ) -> SendResult:
        try:
            result = await self._request(
                target.node_id,
                {**payload, "target": target_to_dict(target)},
            )
        except Exception as exc:
            logger.exception("Socket send failed for %s", target)
            return SendResult(False, str(exc))
        return SendResult(
            ok=bool(result.get("ok")),
            message=str(result.get("message", "")),
        )

    async def _request(self, node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        if node is None:
            raise ValueError(f"Unknown socket agent node {node_id!r}")

        request = {"id": uuid.uuid4().hex, "node_id": node_id, **payload}
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(node.host, node.port),
            timeout=self.timeout,
        )
        try:
            writer.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line:
                raise ConnectionError("agent node closed without a response")
            response = json.loads(line.decode())
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error", "request failed")))
            result = response.get("result", {})
            return result if isinstance(result, dict) else {}
        finally:
            writer.close()
            await writer.wait_closed()

    async def _subscribe_loop(self, node_id: str) -> None:
        node = self.nodes[node_id]
        while self._running:
            try:
                reader, writer = await asyncio.open_connection(node.host, node.port)
                try:
                    request = {
                        "id": uuid.uuid4().hex,
                        "op": "subscribe",
                        "node_id": node_id,
                    }
                    writer.write(
                        json.dumps(request, ensure_ascii=False).encode() + b"\n"
                    )
                    await writer.drain()
                    while self._running:
                        line = await reader.readline()
                        if not line:
                            raise ConnectionError("subscription closed")
                        event = json.loads(line.decode())
                        await self._handle_event(event)
                finally:
                    writer.close()
                    await writer.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Socket subscription lost for %s: %s", node_id, exc
                )
                await asyncio.sleep(self.reconnect_delay)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("op") != "message" or self._message_callback is None:
            return
        message_data = event.get("message")
        if not isinstance(message_data, dict):
            return
        await self._message_callback(message_from_dict(message_data))

    def _single_node_id(self) -> str:
        if len(self.nodes) == 1:
            return next(iter(self.nodes))
        return ""
