"""Capture model-bound requests and model-returned responses for observability."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from security import redact_text


class ModelIoRecorder:
    """Record SDK-level model I/O after request processors have run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def before_model(self, _context, request) -> None:
        """Capture the exact LlmRequest presented to the model implementation."""
        self.calls.append(
            {
                "call_index": len(self.calls) + 1,
                "request": self._snapshot(request),
                "response_chunks": [],
                "terminal_response": None,
            }
        )

    def after_model(self, _context, response) -> None:
        """Capture every normalized LlmResponse emitted by the model implementation."""
        if not self.calls:
            return
        snapshot = self._snapshot(response)
        self.calls[-1]["response_chunks"].append(snapshot)
        if not bool(getattr(response, "partial", False)):
            self.calls[-1]["terminal_response"] = snapshot

    def snapshot(self) -> dict[str, Any]:
        """Return the serializable, redacted capture."""
        return {
            "format": "sdk-model-io-v1",
            "capture_point": (
                "LlmAgent before_model_callback/after_model_callback, after request "
                "processors inject skills, tools, history, and output schema"
            ),
            "scope_note": (
                "This is exact SDK-level LlmRequest/LlmResponse data. The OpenAI "
                "adapter may still translate it into provider-specific HTTP JSON."
            ),
            "calls": self.calls,
        }

    @classmethod
    def _snapshot(cls, value: object, *, depth: int = 0) -> object:
        """Convert SDK/Pydantic values to bounded redacted JSON-compatible data."""
        if depth >= 20:
            return "[TRUNCATED_NESTING]"
        if isinstance(value, BaseModel):
            try:
                value = value.model_dump(mode="python", exclude_none=True)
            except Exception:
                value = str(value)
        if isinstance(value, str):
            redacted = redact_text(value)
            return redacted[:100_000] + ("[TRUNCATED]" if len(redacted) > 100_000 else "")
        if isinstance(value, dict):
            return {
                redact_text(str(key))[:500]: cls._snapshot(item, depth=depth + 1)
                for key, item in list(value.items())[:1000]
            }
        if isinstance(value, (list, tuple, set)):
            return [cls._snapshot(item, depth=depth + 1) for item in list(value)[:1000]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return redact_text(str(value))[:100_000]
