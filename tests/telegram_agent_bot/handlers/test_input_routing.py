from telegram_agent_bot.handlers.callback_data import CB_INPUT_ROUTE
from telegram_agent_bot.handlers.input_routing import (
    INPUT_ROUTE_QUEUE,
    INPUT_ROUTE_STEER,
    build_input_routing_keyboard,
    parse_input_routing_callback,
)


def test_input_routing_keyboard_keeps_prompt_out_of_callback_data():
    keyboard = build_input_routing_keyboard(123)
    callback_values = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]

    assert f"{CB_INPUT_ROUTE}{INPUT_ROUTE_STEER}:123" in callback_values
    assert f"{CB_INPUT_ROUTE}{INPUT_ROUTE_QUEUE}:123" in callback_values
    assert all(value is not None and len(value) < 64 for value in callback_values)


def test_claude_input_routing_keyboard_labels_bot_managed_queue():
    keyboard = build_input_routing_keyboard(123, native_queue=False)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "⏭️ Bot 排到下一轮" in labels


def test_parse_input_routing_callback_rejects_invalid_values():
    assert parse_input_routing_callback("ir:queue:7") == ("queue", 7)
    assert parse_input_routing_callback("ir:unknown:7") is None
    assert parse_input_routing_callback("ir:queue:not-an-id") is None
    assert parse_input_routing_callback("other:queue:7") is None
