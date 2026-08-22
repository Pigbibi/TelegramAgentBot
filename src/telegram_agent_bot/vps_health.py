"""Low-overhead host and AgentBot runtime health monitoring."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import Bot

from .durable_state import DurableRuntimeStore, HealthAlertState
from .turn_admission import turn_admission

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HostMetrics:
    memory_total_bytes: int
    memory_available_bytes: int
    swap_total_bytes: int
    swap_used_bytes: int
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int

    @property
    def swap_used_percent(self) -> float:
        if self.swap_total_bytes <= 0:
            return 0.0
        return 100.0 * self.swap_used_bytes / self.swap_total_bytes

    @property
    def disk_used_percent(self) -> float:
        usable_bytes = self.disk_used_bytes + self.disk_free_bytes
        if usable_bytes <= 0:
            return 0.0
        # Match df's operator-facing percentage. Filesystem-reserved blocks are
        # neither user-visible free space nor application data and must not be
        # reported as consumed by AgentBot.
        return 100.0 * self.disk_used_bytes / usable_bytes


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    host: HostMetrics
    queue_depth: int
    oldest_queue_age_seconds: float
    transcript_backlog_sessions: int
    oldest_transcript_lag_seconds: float
    transcript_backlog_bytes: int
    active_turns: int
    pending_windows: int
    hibernated_sessions: int


@dataclass(frozen=True, slots=True)
class HealthIssue:
    key: str
    message: str


@dataclass(frozen=True, slots=True)
class HealthDecision:
    event: str
    issues: tuple[HealthIssue, ...]


def collect_host_metrics(
    *,
    meminfo_path: Path = Path("/proc/meminfo"),
    disk_path: Path = Path("/"),
) -> HostMetrics:
    """Collect Linux memory and filesystem metrics without a new dependency."""
    values: dict[str, int] = {}
    try:
        for line in meminfo_path.read_text(encoding="utf-8").splitlines():
            key, separator, raw = line.partition(":")
            if not separator:
                continue
            amount = raw.strip().split(maxsplit=1)[0]
            try:
                values[key] = int(amount) * 1024
            except ValueError:
                continue
    except OSError:
        logger.exception("Failed to read host memory metrics from %s", meminfo_path)

    disk = shutil.disk_usage(disk_path)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return HostMetrics(
        memory_total_bytes=values.get("MemTotal", 0),
        memory_available_bytes=values.get(
            "MemAvailable",
            values.get("MemFree", 0),
        ),
        swap_total_bytes=swap_total,
        swap_used_bytes=max(0, swap_total - swap_free),
        disk_total_bytes=disk.total,
        disk_used_bytes=disk.used,
        disk_free_bytes=disk.free,
    )


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TiB"


def format_health_snapshot(
    snapshot: HealthSnapshot,
    issues: tuple[HealthIssue, ...] = (),
) -> str:
    """Render one compact operator-facing health report."""
    host = snapshot.host
    lines = [
        "🩺 AgentBot host health",
        "",
        "Memory: "
        f"{_format_bytes(host.memory_available_bytes)} available / "
        f"{_format_bytes(host.memory_total_bytes)}",
        "Swap: "
        f"{_format_bytes(host.swap_used_bytes)} used / "
        f"{_format_bytes(host.swap_total_bytes)} "
        f"({host.swap_used_percent:.0f}%)",
        "Disk: "
        f"{host.disk_used_percent:.0f}% used, "
        f"{_format_bytes(host.disk_free_bytes)} free",
        "Agent turns: "
        f"{snapshot.active_turns} active, "
        f"{snapshot.queue_depth} queued in "
        f"{snapshot.pending_windows} topic(s), "
        f"{snapshot.hibernated_sessions} sleeping",
        "Transcript delivery: "
        f"{snapshot.transcript_backlog_sessions} lagging session(s), "
        f"{_format_bytes(snapshot.transcript_backlog_bytes)} pending",
    ]
    if issues:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {issue.message}" for issue in issues)
    else:
        lines.extend(["", "Status: healthy"])
    return "\n".join(lines)


class VpsHealthMonitor:
    """Build snapshots and emit cooldown-aware health decisions."""

    def __init__(self, store: DurableRuntimeStore) -> None:
        self.store = store
        self._alert_states: dict[str, HealthAlertState] | None = None
        self._transcript_lag_started: dict[str, float] = {}
        self._transcript_delivery_offsets: dict[str, int] = {}

    def _measure_transcript_lag(
        self,
        backlog: dict[str, int],
        delivery_offsets: dict[str, int],
        *,
        now_monotonic: float,
    ) -> float:
        """Measure time since delivery last progressed, not backlog lifetime."""
        backlog_ids = set(backlog)
        for session_id in list(self._transcript_lag_started):
            if session_id not in backlog_ids:
                self._transcript_lag_started.pop(session_id, None)
                self._transcript_delivery_offsets.pop(session_id, None)

        for session_id in backlog_ids:
            current_offset = delivery_offsets.get(session_id)
            previous_offset = self._transcript_delivery_offsets.get(session_id)
            if session_id not in self._transcript_lag_started or (
                current_offset is not None
                and previous_offset is not None
                and current_offset != previous_offset
            ):
                self._transcript_lag_started[session_id] = now_monotonic
            if current_offset is not None:
                self._transcript_delivery_offsets[session_id] = current_offset

        return max(
            (
                now_monotonic - self._transcript_lag_started[session_id]
                for session_id in backlog_ids
            ),
            default=0.0,
        )

    def _load_alert_states(self) -> dict[str, HealthAlertState]:
        if self._alert_states is not None:
            return self._alert_states
        try:
            self._alert_states = self.store.load_health_alert_states()
        except Exception:
            logger.exception("Failed to load persisted host health alert state")
            self._alert_states = {}
        return self._alert_states

    async def snapshot(self, session_monitor: Any | None) -> HealthSnapshot:
        """Collect bounded host, queue, delivery, and scheduler metrics."""
        host = await asyncio.to_thread(collect_host_metrics)
        now_epoch = time.time()
        try:
            queue_stats = await asyncio.to_thread(self.store.pending_agent_input_stats)
            oldest_queue_age = (
                max(0.0, now_epoch - queue_stats.oldest_created_at_epoch)
                if queue_stats.oldest_created_at_epoch is not None
                else 0.0
            )
            queue_depth = queue_stats.count
        except Exception:
            logger.exception("Failed to collect durable agent input queue metrics")
            oldest_queue_age = 0.0
            queue_depth = 0

        delivery_offsets: dict[str, int] = {}
        if session_monitor is not None and hasattr(
            session_monitor, "delivery_backlog_metrics"
        ):
            backlog_metrics = session_monitor.delivery_backlog_metrics()
            backlog = {
                session_id: pending_bytes
                for session_id, (pending_bytes, _offset) in backlog_metrics.items()
            }
            delivery_offsets = {
                session_id: offset
                for session_id, (_pending_bytes, offset) in backlog_metrics.items()
            }
        else:
            backlog = (
                session_monitor.delivery_backlog_bytes()
                if session_monitor is not None
                and hasattr(session_monitor, "delivery_backlog_bytes")
                else {}
            )
        now_monotonic = time.monotonic()
        oldest_transcript_lag = self._measure_transcript_lag(
            backlog,
            delivery_offsets,
            now_monotonic=now_monotonic,
        )

        scheduler = turn_admission.snapshot()
        from .session import session_manager

        hibernated = sum(
            1 for state in session_manager.window_states.values() if state.hibernated_at
        )
        return HealthSnapshot(
            host=host,
            queue_depth=queue_depth,
            oldest_queue_age_seconds=oldest_queue_age,
            transcript_backlog_sessions=len(backlog),
            oldest_transcript_lag_seconds=oldest_transcript_lag,
            transcript_backlog_bytes=sum(backlog.values()),
            active_turns=len(scheduler.active_windows | scheduler.reserved_windows),
            pending_windows=len(scheduler.pending_windows),
            hibernated_sessions=hibernated,
        )

    @staticmethod
    def issues(snapshot: HealthSnapshot, config: Any) -> tuple[HealthIssue, ...]:
        """Evaluate one snapshot against configurable small-host thresholds."""
        found: list[HealthIssue] = []
        memory_limit = float(config.health_memory_available_mb) * 1024 * 1024
        if (
            snapshot.host.memory_available_bytes > 0
            and snapshot.host.memory_available_bytes <= memory_limit
        ):
            found.append(
                HealthIssue(
                    "memory",
                    "available memory is low "
                    f"({_format_bytes(snapshot.host.memory_available_bytes)})",
                )
            )
        if (
            snapshot.host.swap_total_bytes > 0
            and snapshot.host.swap_used_percent >= config.health_swap_used_percent
        ):
            found.append(
                HealthIssue(
                    "swap",
                    f"swap use is {snapshot.host.swap_used_percent:.0f}%",
                )
            )
        if snapshot.host.disk_used_percent >= config.health_disk_used_percent:
            found.append(
                HealthIssue(
                    "disk",
                    f"root disk use is {snapshot.host.disk_used_percent:.0f}% "
                    f"({_format_bytes(snapshot.host.disk_free_bytes)} free)",
                )
            )
        if (
            snapshot.queue_depth > 0
            and snapshot.oldest_queue_age_seconds >= config.health_queue_oldest_seconds
        ):
            found.append(
                HealthIssue(
                    "agent_queue",
                    f"oldest queued prompt has waited "
                    f"{snapshot.oldest_queue_age_seconds / 60:.0f} min",
                )
            )
        if (
            snapshot.transcript_backlog_sessions > 0
            and snapshot.oldest_transcript_lag_seconds
            >= config.health_transcript_lag_seconds
        ):
            found.append(
                HealthIssue(
                    "transcript_lag",
                    f"transcript delivery has lagged for "
                    f"{snapshot.oldest_transcript_lag_seconds:.0f}s",
                )
            )
        return tuple(found)

    def decide(
        self,
        issues: tuple[HealthIssue, ...],
        *,
        cooldown_seconds: float,
        now_epoch: float | None = None,
    ) -> HealthDecision:
        """Choose alert, recovery, or silence without changing persisted state."""
        current_time = time.time() if now_epoch is None else now_epoch
        states = self._load_alert_states()
        current_keys = {issue.key for issue in issues}
        previously_active = {key for key, state in states.items() if state.active}
        if not current_keys and previously_active:
            return HealthDecision("recovery", ())
        if not current_keys:
            return HealthDecision("none", ())
        due = any(
            key not in states
            or not states[key].active
            or current_time - states[key].last_sent_at_epoch >= cooldown_seconds
            for key in current_keys
        )
        return HealthDecision("alert" if due else "none", issues)

    def record(
        self,
        decision: HealthDecision,
        *,
        now_epoch: float | None = None,
    ) -> None:
        """Persist an alert/recovery only after Telegram delivery succeeds."""
        current_time = time.time() if now_epoch is None else now_epoch
        states = self._load_alert_states()
        current_keys = {issue.key for issue in decision.issues}
        if decision.event == "alert":
            for key in current_keys:
                state = HealthAlertState(True, current_time)
                states[key] = state
                self.store.save_health_alert_state(
                    key,
                    active=True,
                    last_sent_at_epoch=current_time,
                )
            for key in set(states) - current_keys:
                old = states[key]
                if old.active:
                    states[key] = HealthAlertState(False, old.last_sent_at_epoch)
                    self.store.save_health_alert_state(
                        key,
                        active=False,
                        last_sent_at_epoch=old.last_sent_at_epoch,
                    )
        elif decision.event == "recovery":
            for key, old in list(states.items()):
                if not old.active:
                    continue
                states[key] = HealthAlertState(False, old.last_sent_at_epoch)
                self.store.save_health_alert_state(
                    key,
                    active=False,
                    last_sent_at_epoch=old.last_sent_at_epoch,
                )

    async def run(self, bot: Bot, config: Any, session_monitor_getter: Any) -> None:
        """Periodically send private health alerts to configured operators."""
        interval = max(30.0, float(config.health_check_interval_seconds))
        while True:
            try:
                snapshot = await self.snapshot(session_monitor_getter())
                issues = self.issues(snapshot, config)
                decision = self.decide(
                    issues,
                    cooldown_seconds=float(config.health_alert_cooldown_seconds),
                )
                if decision.event == "alert":
                    text = "⚠️ Host health warning\n\n" + format_health_snapshot(
                        snapshot,
                        decision.issues,
                    )
                elif decision.event == "recovery":
                    text = "✅ AgentBot host health recovered."
                else:
                    text = ""

                if text:
                    delivered = True
                    for user_id in sorted(config.allowed_users):
                        try:
                            await bot.send_message(chat_id=user_id, text=text)
                        except Exception:
                            delivered = False
                            logger.exception(
                                "Failed to send host health event to user %d",
                                user_id,
                            )
                    if delivered:
                        self.record(decision)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Host health monitor iteration failed")
            await asyncio.sleep(interval)
