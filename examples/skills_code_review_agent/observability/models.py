"""Typed, unified trace model spanning model, tool, sandbox, and report layers."""

from typing import Any

from pydantic import BaseModel
from pydantic import Field


class TraceModelCall(BaseModel):
    """One exact SDK model request and its normalized response stream."""

    call_index: int
    request: dict[str, Any] = Field(default_factory=dict)
    terminal_response: dict[str, Any] | None = None
    response_chunks: list[dict[str, Any]] = Field(default_factory=list)
    requested_tool_call_ids: list[str] = Field(default_factory=list)


class TraceToolInteraction(BaseModel):
    """One model-requested tool call joined to enforcement and execution evidence."""

    sequence: int
    model_call_index: int | None = None
    call_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    response: Any = None
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    sandbox_runs: list[dict[str, Any]] = Field(default_factory=list)


class TraceOutcome(BaseModel):
    """Show how model output changes across trusted backend boundaries."""

    raw_set_model_response_arguments: dict[str, Any] | None = None
    sdk_validated_agent_output: dict[str, Any] | None = None
    normalized_report_output: dict[str, Any]


class ReviewRunTrace(BaseModel):
    """Single source of truth for understanding one complete review run."""

    schema_version: str = "review-run-trace-v1"
    task_id: str
    mode: str
    status: str
    input_summary: dict[str, Any]
    model_calls: list[TraceModelCall] = Field(default_factory=list)
    tool_interactions: list[TraceToolInteraction] = Field(default_factory=list)
    backend_timeline: list[dict[str, Any]] = Field(default_factory=list)
    outcome: TraceOutcome
    monitoring: dict[str, Any]

