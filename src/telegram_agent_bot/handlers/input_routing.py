"""Telegram UI helpers for choosing how active-turn input is submitted."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .callback_data import CB_INPUT_ROUTE


INPUT_ROUTE_STEER = "steer"
INPUT_ROUTE_QUEUE = "queue"
INPUT_ROUTE_INTERRUPT = "interrupt"
INPUT_ROUTE_CANCEL = "cancel"
INPUT_ROUTE_MODES = frozenset(
    {
        INPUT_ROUTE_STEER,
        INPUT_ROUTE_QUEUE,
        INPUT_ROUTE_INTERRUPT,
        INPUT_ROUTE_CANCEL,
    }
)


def build_input_routing_keyboard(
    record_id: int,
    *,
    native_queue: bool = True,
) -> InlineKeyboardMarkup:
    """Build a compact mode picker whose callbacks contain no prompt text."""

    def button(label: str, mode: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{CB_INPUT_ROUTE}{mode}:{record_id}",
        )

    return InlineKeyboardMarkup(
        [
            [
                button("↪️ 引导当前任务", INPUT_ROUTE_STEER),
                button(
                    "⏭️ 原生排到下一轮" if native_queue else "⏭️ Bot 排到下一轮",
                    INPUT_ROUTE_QUEUE,
                ),
            ],
            [
                button("⎋ 中断并发送", INPUT_ROUTE_INTERRUPT),
                button("取消", INPUT_ROUTE_CANCEL),
            ],
        ]
    )


def parse_input_routing_callback(data: str) -> tuple[str, int] | None:
    """Parse ``ir:<mode>:<record_id>`` callback data."""
    if not data.startswith(CB_INPUT_ROUTE):
        return None
    try:
        mode, record_id_text = data[len(CB_INPUT_ROUTE) :].split(":", 1)
        record_id = int(record_id_text)
    except (TypeError, ValueError):
        return None
    if mode not in INPUT_ROUTE_MODES or record_id <= 0:
        return None
    return mode, record_id
