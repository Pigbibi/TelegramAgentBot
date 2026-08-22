from types import SimpleNamespace

from telegram_agent_bot.durable_state import DurableRuntimeStore
from telegram_agent_bot.vps_health import (
    HealthSnapshot,
    HostMetrics,
    VpsHealthMonitor,
    collect_host_metrics,
    format_health_snapshot,
)


def _config():
    return SimpleNamespace(
        health_memory_available_mb=256.0,
        health_swap_used_percent=75.0,
        health_disk_used_percent=80.0,
        health_queue_oldest_seconds=600.0,
        health_transcript_lag_seconds=120.0,
    )


def _snapshot(**overrides):
    values = {
        "host": HostMetrics(
            memory_total_bytes=2 * 1024**3,
            memory_available_bytes=512 * 1024**2,
            swap_total_bytes=4 * 1024**3,
            swap_used_bytes=1 * 1024**3,
            disk_total_bytes=40 * 1024**3,
            disk_used_bytes=32 * 1024**3,
            disk_free_bytes=8 * 1024**3,
        ),
        "queue_depth": 0,
        "oldest_queue_age_seconds": 0.0,
        "transcript_backlog_sessions": 0,
        "oldest_transcript_lag_seconds": 0.0,
        "transcript_backlog_bytes": 0,
        "active_turns": 1,
        "pending_windows": 0,
        "hibernated_sessions": 2,
    }
    values.update(overrides)
    return HealthSnapshot(**values)


def test_collect_host_metrics_reads_linux_meminfo(tmp_path, monkeypatch):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal: 2048 kB\n"
        "MemAvailable: 512 kB\n"
        "SwapTotal: 1024 kB\n"
        "SwapFree: 256 kB\n"
    )
    monkeypatch.setattr(
        "telegram_agent_bot.vps_health.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1100, used=800, free=200),
    )

    metrics = collect_host_metrics(meminfo_path=meminfo)

    assert metrics.memory_total_bytes == 2048 * 1024
    assert metrics.memory_available_bytes == 512 * 1024
    assert metrics.swap_used_bytes == 768 * 1024
    assert metrics.disk_used_percent == 80.0


def test_health_issue_cooldown_survives_monitor_reopen(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monitor = VpsHealthMonitor(store)
    issues = monitor.issues(_snapshot(), _config())
    assert [issue.key for issue in issues] == ["disk"]

    first = monitor.decide(issues, cooldown_seconds=3600, now_epoch=1000)
    assert first.event == "alert"
    monitor.record(first, now_epoch=1000)

    reopened = VpsHealthMonitor(store)
    quiet = reopened.decide(issues, cooldown_seconds=3600, now_epoch=1100)
    repeat = reopened.decide(issues, cooldown_seconds=3600, now_epoch=4600)

    assert quiet.event == "none"
    assert repeat.event == "alert"


def test_health_recovery_is_emitted_once(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monitor = VpsHealthMonitor(store)
    issues = monitor.issues(_snapshot(), _config())
    first = monitor.decide(issues, cooldown_seconds=3600, now_epoch=1000)
    monitor.record(first, now_epoch=1000)

    recovery = monitor.decide((), cooldown_seconds=3600, now_epoch=1100)
    assert recovery.event == "recovery"
    monitor.record(recovery, now_epoch=1100)

    assert (
        VpsHealthMonitor(store).decide((), cooldown_seconds=3600, now_epoch=1200).event
        == "none"
    )


def test_health_report_includes_runtime_counts():
    report = format_health_snapshot(_snapshot())

    assert "1 active" in report
    assert "2 sleeping" in report
    assert "Status: healthy" in report
