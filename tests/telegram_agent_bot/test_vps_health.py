from types import SimpleNamespace

from telegram_agent_bot.durable_state import DurableRuntimeStore
from telegram_agent_bot.vps_health import (
    HealthIssue,
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


def test_same_health_issue_recurrence_respects_last_alert_cooldown(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monitor = VpsHealthMonitor(store)
    disk = (HealthIssue("disk", "disk"),)
    first = monitor.decide(disk, cooldown_seconds=3600, now_epoch=1000)
    monitor.record(first, now_epoch=1000)
    recovery = monitor.decide((), cooldown_seconds=3600, now_epoch=1100)
    monitor.record(recovery, now_epoch=1100)

    quiet_recurrence = monitor.decide(
        disk,
        cooldown_seconds=3600,
        now_epoch=1200,
    )
    due_recurrence = monitor.decide(
        disk,
        cooldown_seconds=3600,
        now_epoch=4600,
    )

    assert quiet_recurrence.event == "none"
    assert due_recurrence.event == "alert"


def test_health_report_includes_runtime_counts():
    report = format_health_snapshot(_snapshot())

    assert "1 running" in report
    assert "2 sleeping" in report
    assert "Status: healthy" in report


def test_health_report_supports_chinese_when_selected():
    report = format_health_snapshot(_snapshot(), language="zh")

    assert "1 个运行中" in report
    assert "2 个休眠" in report
    assert "状态：正常" in report


def test_health_report_shows_update_waiting_for_idle():
    report = format_health_snapshot(
        _snapshot(update_waiting_for_idle=True, update_blocker_count=2),
        language="zh",
    )

    assert "更新：等待空闲（2 个阻塞项）" in report


def test_health_recovery_waits_until_state_is_stable(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monitor = VpsHealthMonitor(store)
    issues = monitor.issues(_snapshot(), _config())
    first = monitor.decide(issues, cooldown_seconds=86400, now_epoch=1000)
    monitor.record(first, now_epoch=1000)

    not_yet = monitor.decide(
        (),
        cooldown_seconds=86400,
        recovery_stable_seconds=300,
        now_epoch=1100,
    )
    still_not = monitor.decide(
        (),
        cooldown_seconds=86400,
        recovery_stable_seconds=300,
        now_epoch=1399,
    )
    recovered = monitor.decide(
        (),
        cooldown_seconds=86400,
        recovery_stable_seconds=300,
        now_epoch=1400,
    )

    assert not_yet.event == "none"
    assert still_not.event == "none"
    assert recovered.event == "recovery"


def test_health_state_change_notifies_without_waiting_for_repeat_cooldown(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monitor = VpsHealthMonitor(store)
    disk = (HealthIssue("disk", "磁盘"),)
    monitor.record(
        monitor.decide(disk, cooldown_seconds=86400, now_epoch=1000),
        now_epoch=1000,
    )

    changed = monitor.decide(
        (HealthIssue("memory", "内存"),),
        cooldown_seconds=86400,
        now_epoch=1100,
    )

    assert changed.event == "alert"


def test_partial_recovery_updates_state_without_repeating_alert(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monitor = VpsHealthMonitor(store)
    disk = HealthIssue("disk", "disk")
    queue = HealthIssue("agent_queue", "queue")
    initial = monitor.decide((disk, queue), cooldown_seconds=86400, now_epoch=1000)
    monitor.record(initial, now_epoch=1000)

    partial = monitor.decide((queue,), cooldown_seconds=86400, now_epoch=1100)

    assert partial.event == "none"
    assert partial.issues == (queue,)
    monitor.record(partial, now_epoch=1100)
    states = store.load_health_alert_states()
    assert states["disk"].active is False
    assert states["agent_queue"].active is True
    assert (
        monitor.decide((queue,), cooldown_seconds=86400, now_epoch=1200).event == "none"
    )


def test_transcript_lag_requires_delivery_watermark_to_stall(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monitor = VpsHealthMonitor(store)

    assert (
        monitor._measure_transcript_lag(
            {"active": 100}, {"active": 1000}, now_monotonic=10.0
        )
        == 0.0
    )
    assert (
        monitor._measure_transcript_lag(
            {"active": 120}, {"active": 1000}, now_monotonic=131.0
        )
        == 121.0
    )
    assert (
        monitor._measure_transcript_lag(
            {"active": 80}, {"active": 1040}, now_monotonic=132.0
        )
        == 0.0
    )


def test_transcript_lag_tracking_forgets_drained_sessions(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monitor = VpsHealthMonitor(store)
    monitor._measure_transcript_lag(
        {"drained": 10}, {"drained": 20}, now_monotonic=10.0
    )

    assert monitor._measure_transcript_lag({}, {}, now_monotonic=20.0) == 0.0
    assert monitor._transcript_lag_started == {}
    assert monitor._transcript_delivery_offsets == {}
