"""Agent-node server for the socket backend plugin."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import replace
from typing import Any

from telegram_codex_bot.backends.base import AgentTarget, CreateSessionRequest
from telegram_codex_bot.backends.local import LocalTmuxBackend
from telegram_codex_bot.session import _session_ids_match, session_manager
from telegram_codex_bot.session_monitor import NewMessage

from .protocol import (
    listing_to_dict,
    message_to_dict,
    root_to_dict,
    session_to_dict,
    target_from_dict,
    target_session_id,
)

logger = logging.getLogger(__name__)


class AgentNodeServer:
    """TCP server that exposes one machine's local Codex backend."""

    def __init__(
        self,
        *,
        node_id: str,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.node_id = node_id
        self.host = host
        self.port = port
        self.local_backend = LocalTmuxBackend()
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        """Prepare local tmux/monitor resources and start listening."""
        self.local_backend.prepare()
        await self.local_backend.start(self._publish_message)
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
        )
        logger.info("Agent node %s listening on %s:%d", self.node_id, self.host, self.port)

    async def stop(self) -> None:
        """Stop the TCP server and transcript monitor."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self._subscribers):
            writer.close()
            await writer.wait_closed()
        self._subscribers.clear()
        await self.local_backend.stop()

    async def serve_forever(self) -> None:
        """Run until cancelled."""
        await self.start()
        assert self._server is not None
        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            await self.stop()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            request = json.loads(line.decode())
            if request.get("op") == "subscribe":
                await self._handle_subscribe(reader, writer)
                return

            result = await self._dispatch(request)
            await self._write_response(writer, {"ok": True, "result": result})
        except Exception as exc:
            logger.exception("Agent node request failed")
            await self._write_response(writer, {"ok": False, "error": str(exc)})
        finally:
            if writer not in self._subscribers:
                writer.close()
                await writer.wait_closed()

    async def _handle_subscribe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._subscribers.add(writer)
        logger.info("Center subscribed to node %s events", self.node_id)
        try:
            await reader.read()
        finally:
            self._subscribers.discard(writer)
            logger.info("Center unsubscribed from node %s events", self.node_id)

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "create_session":
            return await self._create_session(request)
        if op == "send_message":
            result = await self.local_backend.send_message(
                self._local_target(request),
                str(request.get("text", "")),
            )
            return {"ok": result.ok, "message": result.message}
        if op == "send_control":
            result = await self.local_backend.send_control(
                self._local_target(request),
                str(request.get("key", "")),
            )
            return {"ok": result.ok, "message": result.message}
        if op == "capture":
            text = await self.local_backend.capture(
                self._local_target(request),
                with_ansi=bool(request.get("with_ansi", False)),
            )
            return {"text": text}
        if op == "list_roots":
            roots = await self.local_backend.list_roots()
            return {"roots": [root_to_dict(root) for root in roots]}
        if op == "list_directory":
            listing = await self.local_backend.list_directory(
                self.node_id,
                str(request.get("path", "")),
                root_path=str(request.get("root_path", "")),
            )
            return {"listing": listing_to_dict(listing)}
        if op == "list_sessions":
            sessions = await self.local_backend.list_sessions(
                self.node_id,
                str(request.get("cwd", "")),
            )
            return {"sessions": [session_to_dict(session) for session in sessions]}
        raise ValueError(f"Unsupported op {op!r}")

    async def _create_session(self, request: dict[str, Any]) -> dict[str, Any]:
        result = await self.local_backend.create_session(
            CreateSessionRequest(
                cwd=str(request.get("cwd", "")),
                window_name=str(request.get("window_name", "")),
                resume_session_id=str(request.get("resume_session_id", "")),
                account_name=str(request.get("account_name", "")),
            )
        )
        window_id = result.target.window_id if result.target else ""
        if result.ok and window_id:
            session_manager.prepare_window_launch(
                window_id,
                cwd=str(request.get("cwd", "")),
                window_name=result.display_name,
                account_name=str(request.get("account_name", "")),
            )
            resume_session_id = str(request.get("resume_session_id", ""))
            if resume_session_id:
                session_manager.register_session_to_window(
                    window_id,
                    resume_session_id,
                    str(request.get("cwd", "")),
                    window_name=result.display_name,
                    persist_session_map=True,
                )
        return {
            "ok": result.ok,
            "message": result.message,
            "display_name": result.display_name,
            "window_id": window_id,
            "session_id": target_session_id(self.node_id, window_id),
        }

    def _local_target(self, request: dict[str, Any]) -> AgentTarget:
        raw_target = request.get("target", {})
        target = target_from_dict(raw_target if isinstance(raw_target, dict) else {})
        window_id = target.window_id
        prefix = f"{self.node_id}:"
        if not window_id and target.session_id.startswith(prefix):
            window_id = target.session_id[len(prefix) :]
        return AgentTarget(
            backend_id="local",
            node_id="local",
            session_id="",
            window_id=window_id,
        )

    async def _publish_message(self, message: NewMessage) -> None:
        if not self._subscribers:
            return
        window_id = self._window_for_session(message.session_id)
        routed_message = message
        if window_id:
            routed_message = replace(
                message,
                session_id=target_session_id(self.node_id, window_id),
            )
        event = {
            "op": "message",
            "node_id": self.node_id,
            "window_id": window_id,
            "message": message_to_dict(routed_message),
        }
        payload = json.dumps(event, ensure_ascii=False).encode() + b"\n"
        for writer in list(self._subscribers):
            try:
                writer.write(payload)
                await writer.drain()
            except Exception:
                self._subscribers.discard(writer)

    def _window_for_session(self, session_id: str) -> str:
        for window_id, state in list(session_manager.window_states.items()):
            if _session_ids_match(state.session_id, session_id):
                return window_id
        return ""

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
    ) -> None:
        writer.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        await writer.drain()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a TelegramCodexBot agent node")
    parser.add_argument(
        "--node-id",
        default=os.getenv("TELEGRAM_CODEX_AGENT_NODE_ID", "local"),
        help="Stable node id shown by the center bot",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("TELEGRAM_CODEX_AGENT_NODE_HOST", "127.0.0.1"),
        help="Host/IP to bind",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("TELEGRAM_CODEX_AGENT_NODE_PORT", "8765")),
        help="TCP port to bind",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("TELEGRAM_CODEX_AGENT_NODE_LOG_LEVEL", "INFO"),
        help="Python logging level",
    )
    return parser


async def _amain(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = AgentNodeServer(node_id=args.node_id, host=args.host, port=args.port)
    await server.serve_forever()


def main() -> None:
    """CLI entry point."""
    args = _build_parser().parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass
