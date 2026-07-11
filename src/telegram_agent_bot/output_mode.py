"""Per-topic Telegram output visibility modes."""

OUTPUT_MODE_CLEAN = "clean"
OUTPUT_MODE_TRACE = "trace"
OUTPUT_MODES = (OUTPUT_MODE_CLEAN, OUTPUT_MODE_TRACE)


def normalize_output_mode(value: str | None, default: str = OUTPUT_MODE_CLEAN) -> str:
    """Return a supported output mode, falling back to the safe default."""
    normalized = (value or "").strip().lower()
    if normalized in OUTPUT_MODES:
        return normalized
    return default if default in OUTPUT_MODES else OUTPUT_MODE_CLEAN


def output_mode_label(mode: str) -> str:
    """Return the user-facing label for an output mode."""
    return "Clean" if normalize_output_mode(mode) == OUTPUT_MODE_CLEAN else "Trace"
