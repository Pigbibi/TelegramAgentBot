"""Discover provider-supported model ids for the topic picker.

Discovery is best-effort. The configured/default model remains available when
the provider does not expose a list endpoint, credentials are unavailable, or
the request fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values

from .config import config
from .agent_profile import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    normalize_agent_type,
    normalize_effort,
)

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")
_CLAUDE_ALIASES = ("sonnet", "opus")
_CODEX_EXCLUDED_PARTS = (
    "audio",
    "embedding",
    "image",
    "moderation",
    "realtime",
    "search-preview",
    "transcribe",
    "tts",
)


@dataclass(frozen=True)
class CodexModelInfo:
    """Model metadata reported by the installed Codex CLI."""

    model: str
    supported_efforts: tuple[str, ...] = ()
    default_effort: str = ""


def _load_provider_env() -> dict[str, str]:
    """Load provider settings without exporting or logging secret values."""
    values: dict[str, str] = {}
    for path in (config.config_dir / ".env", config.claude_env_file):
        if not isinstance(path, Path) or not path.is_file():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None:
                values[key] = value
    # OPENAI_API_KEY is intentionally read only for model discovery. Claude
    # secrets are normally kept in claude.env because Config scrubs them.
    if os.getenv("OPENAI_API_KEY"):
        values["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
    return values


def _models_url(base_url: str, *, provider: str) -> str:
    """Build the provider's model-list URL from its configured base URL."""
    parsed = urlsplit(base_url.strip().rstrip("/"))
    path = parsed.path.rstrip("/")
    hostname = (parsed.hostname or "").lower()
    if provider == "deepseek" or hostname.endswith("deepseek.com"):
        if path.endswith("/anthropic"):
            path = path[: -len("/anthropic")]
        path = f"{path}/models"
    elif path.endswith("/v1"):
        path = f"{path}/models"
    else:
        path = f"{path}/v1/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _provider_headers(values: dict[str, str], *, provider: str) -> dict[str, str]:
    headers = {"accept": "application/json"}
    api_key = values.get("ANTHROPIC_API_KEY", "").strip()
    auth_token = values.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if provider == "openai":
        api_key = values.get("OPENAI_API_KEY", "").strip()
    if api_key:
        if provider == "anthropic":
            headers["x-api-key"] = api_key
        else:
            headers["authorization"] = f"Bearer {api_key}"
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    return headers


def _extract_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    ids: list[str] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and _MODEL_ID_RE.fullmatch(model_id):
            ids.append(model_id)
    return list(dict.fromkeys(ids))


def _extract_codex_models(payload: object) -> list[CodexModelInfo]:
    """Extract models and reasoning metadata from app-server ``model/list``."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    models: list[CodexModelInfo] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("model") or item.get("id")
        if not isinstance(model_id, str) or not _MODEL_ID_RE.fullmatch(model_id):
            continue

        raw_levels = item.get("supportedReasoningEfforts") or item.get(
            "supported_reasoning_levels"
        )
        efforts: list[str] = []
        if isinstance(raw_levels, list):
            for level in raw_levels:
                raw_effort = (
                    level.get("reasoningEffort") or level.get("effort")
                    if isinstance(level, dict)
                    else level
                )
                if isinstance(raw_effort, str):
                    effort = normalize_effort(raw_effort, "")
                    if effort:
                        efforts.append(effort)

        raw_default = item.get("defaultReasoningEffort") or item.get(
            "default_reasoning_level"
        )
        default_effort = (
            normalize_effort(raw_default, "") if isinstance(raw_default, str) else ""
        )
        supported_efforts = tuple(dict.fromkeys(efforts))
        if default_effort not in supported_efforts:
            default_effort = ""
        models.append(
            CodexModelInfo(
                model=model_id,
                supported_efforts=supported_efforts,
                default_effort=default_effort,
            )
        )
    return list(dict.fromkeys(models))


def _extract_codex_model_ids(payload: object) -> list[str]:
    """Extract model slugs from the Codex app-server model/list response."""
    return [item.model for item in _extract_codex_models(payload)]


def _merge_models(
    preferred: str,
    discovered: list[str],
    *,
    aliases: tuple[str, ...] = (),
) -> tuple[str, ...]:
    # A successful provider catalog is authoritative. Only keep the configured
    # default when discovery returned nothing, so the picker does not advertise
    # a model that the current account/build did not report as available.
    values = ([preferred] if not discovered or preferred in discovered else []) + [
        *discovered,
        *aliases,
    ]
    return tuple(dict.fromkeys(value for value in values if value))


def _codex_model_ids(model_ids: list[str]) -> list[str]:
    """Keep text/reasoning models likely to be accepted by Codex CLI."""
    result: list[str] = []
    for model_id in model_ids:
        lower = model_id.lower()
        if any(part in lower for part in _CODEX_EXCLUDED_PARTS):
            continue
        if lower.startswith(("gpt-", "o1", "o3", "o4", "codex")):
            result.append(model_id)
    return result


async def _fetch_model_ids(
    url: str, headers: dict[str, str], *, provider: str
) -> list[str]:
    try:
        async with httpx.AsyncClient(
            timeout=_DISCOVERY_TIMEOUT, follow_redirects=False
        ) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return _extract_model_ids(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("%s model discovery unavailable: %s", provider, exc)
        return []


async def _discover_codex_models(values: dict[str, str]) -> list[CodexModelInfo]:
    app_server_models = await _discover_codex_app_server_models()
    if app_server_models:
        allowed = set(_codex_model_ids([item.model for item in app_server_models]))
        return [item for item in app_server_models if item.model in allowed]

    if not values.get("OPENAI_API_KEY", "").strip():
        return []
    base_url = values.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    ids = await _fetch_model_ids(
        _models_url(base_url, provider="openai"),
        _provider_headers(values, provider="openai"),
        provider="OpenAI",
    )
    return [CodexModelInfo(model_id) for model_id in _codex_model_ids(ids)]


async def _discover_codex_app_server_models() -> list[CodexModelInfo]:
    """Ask the installed Codex CLI for models available to its current account."""
    try:
        command_parts = shlex.split(config.codex_cli_command)
    except ValueError:
        return []
    if not command_parts:
        return []

    env = os.environ.copy()
    executable_parts: list[str] = []
    for part in command_parts:
        name, separator, value = part.partition("=")
        if separator and name.isidentifier():
            env[name] = value
        else:
            executable_parts.append(part)
    if not executable_parts:
        return []
    executable_parts[0] = shutil.which(executable_parts[0]) or executable_parts[0]

    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *executable_parts,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        if process.stdin is None or process.stdout is None:
            return []

        requests = (
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "telegram_agent_bot",
                        "title": "TelegramAgentBot",
                        "version": "2.0",
                    }
                },
            },
            {"method": "initialized"},
            {"method": "model/list", "id": 2, "params": {}},
        )
        process.stdin.write(
            ("\n".join(json.dumps(request) for request in requests) + "\n").encode()
        )
        await process.stdin.drain()

        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            timeout = max(0.1, deadline - asyncio.get_running_loop().time())
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout)
            except TimeoutError:
                break
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == 2:
                return _extract_codex_models(message.get("result"))
    except (OSError, asyncio.TimeoutError):
        logger.debug("Codex app-server model discovery unavailable", exc_info=True)
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except (TimeoutError, ProcessLookupError):
                process.kill()
    return []


async def _discover_claude_models(values: dict[str, str]) -> list[str]:
    base_url = values.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    hostname = (urlsplit(base_url).hostname or "").lower()
    provider = "deepseek" if hostname.endswith("deepseek.com") else "anthropic"
    if not (
        values.get("ANTHROPIC_API_KEY", "").strip()
        or values.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    ):
        return []
    return await _fetch_model_ids(
        _models_url(base_url, provider=provider),
        _provider_headers(values, provider=provider),
        provider=provider.capitalize(),
    )


def _auto_requested(raw: str) -> bool:
    return not raw.strip() or raw.strip().lower() == "auto"


async def refresh_model_catalog(agent_type: str | None = None) -> None:
    """Refresh auto-configured model choices and Codex effort metadata."""
    if not config.model_discovery_enabled:
        return

    values = _load_provider_env()
    selected_agent = normalize_agent_type(agent_type) if agent_type else None
    codex_task = (
        _discover_codex_models(values)
        if selected_agent in (None, AGENT_CODEX)
        else None
    )
    claude_task = (
        _discover_claude_models(values)
        if selected_agent in (None, AGENT_CLAUDE)
        and _auto_requested(config.claude_models_raw)
        else None
    )

    codex_models: list[CodexModelInfo] = []
    claude_models: list[str] = []
    if codex_task is not None and claude_task is not None:
        codex_models, claude_models = await asyncio.gather(codex_task, claude_task)
    elif codex_task is not None:
        codex_models = await codex_task
    elif claude_task is not None:
        claude_models = await claude_task

    if codex_models:
        if _auto_requested(config.codex_models_raw):
            config.codex_models = _merge_models(
                config.codex_model, [item.model for item in codex_models]
            )
        config.codex_model_efforts = {
            item.model: item.supported_efforts
            for item in codex_models
            if item.supported_efforts
        }
        config.codex_model_default_efforts = {
            item.model: item.default_effort
            for item in codex_models
            if item.default_effort
        }
    if claude_models:
        config.claude_models = _merge_models(
            config.claude_model, claude_models, aliases=_CLAUDE_ALIASES
        )

    logger.info(
        "Model catalog ready: codex=%d, claude=%d",
        len(config.codex_models),
        len(config.claude_models),
    )
