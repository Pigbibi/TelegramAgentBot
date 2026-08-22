import json

from telegram_agent_bot.runner_pool import (
    RunnerObservation,
    decide_runner_action,
    load_runner_target,
)


def test_load_runner_target_uses_validated_local_metadata(tmp_path):
    root = tmp_path / "actions-runner-demo"
    root.mkdir()
    (root / ".service").write_text("actions.runner.Pigbibi-Demo.demo-vps.service\n")
    (root / ".runner").write_text(
        json.dumps({"gitHubUrl": "https://github.com/Pigbibi/Demo"})
    )

    target = load_runner_target(root)

    assert target.service == "actions.runner.Pigbibi-Demo.demo-vps.service"
    assert target.repository == "Pigbibi/Demo"
    assert target.root == root.resolve()


def test_load_runner_target_accepts_github_runner_bom(tmp_path):
    root = tmp_path / "actions-runner-demo"
    root.mkdir()
    (root / ".service").write_text("actions.runner.Pigbibi-Demo.demo-vps.service\n")
    (root / ".runner").write_text(
        '\ufeff{"gitHubUrl": "https://github.com/Pigbibi/Demo"}',
        encoding="utf-8",
    )

    assert load_runner_target(root).repository == "Pigbibi/Demo"


def test_queued_work_starts_offline_runner():
    decision = decide_runner_action(
        RunnerObservation(
            service_active=False,
            worker_active=False,
            queued_runs=1,
        ),
        last_needed_at_epoch=100,
        now_epoch=200,
        idle_timeout_seconds=600,
    )

    assert decision.action == "start"
    assert decision.last_needed_at_epoch == 200


def test_idle_runner_stops_after_grace():
    decision = decide_runner_action(
        RunnerObservation(
            service_active=True,
            worker_active=False,
            queued_runs=0,
        ),
        last_needed_at_epoch=100,
        now_epoch=701,
        idle_timeout_seconds=600,
    )

    assert decision.action == "stop"


def test_api_failure_never_stops_online_runner():
    decision = decide_runner_action(
        RunnerObservation(
            service_active=True,
            worker_active=False,
            queued_runs=0,
            api_ok=False,
        ),
        last_needed_at_epoch=100,
        now_epoch=1000,
        idle_timeout_seconds=600,
    )

    assert decision.action == "keep"
    assert decision.last_needed_at_epoch == 1000


def test_api_failure_starts_offline_runner_to_fail_open():
    decision = decide_runner_action(
        RunnerObservation(
            service_active=False,
            worker_active=False,
            queued_runs=0,
            api_ok=False,
        ),
        last_needed_at_epoch=100,
        now_epoch=1000,
        idle_timeout_seconds=600,
    )

    assert decision.action == "start"
