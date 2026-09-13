"""Join independently collected runtime evidence into one coherent run trace."""

from __future__ import annotations

from typing import Any

from reports.models import ReviewReport

from .models import ReviewRunTrace
from .models import TraceModelCall
from .models import TraceOutcome
from .models import TraceToolInteraction


def _function_call_ids(response: object) -> list[str]:
    """Extract function-call IDs from one terminal SDK response."""
    if not isinstance(response, dict):
        return []
    content = response.get("content")
    if not isinstance(content, dict):
        return []
    call_ids = []
    for part in content.get("parts", []):
        if not isinstance(part, dict) or not isinstance(part.get("function_call"), dict):
            continue
        call_id = part["function_call"].get("id")
        if isinstance(call_id, str) and call_id:
            call_ids.append(call_id)
    return call_ids


def _model_calls(model_io: dict[str, Any] | None) -> list[TraceModelCall]:
    calls = []
    for fallback_index, item in enumerate((model_io or {}).get("calls", []), start=1):
        if not isinstance(item, dict):
            continue
        terminal = item.get("terminal_response")
        chunks = item.get("response_chunks", [])
        calls.append(
            TraceModelCall(
                call_index=int(item.get("call_index") or fallback_index),
                request=item.get("request") if isinstance(item.get("request"), dict) else {},
                terminal_response=terminal if isinstance(terminal, dict) else None,
                response_chunks=[chunk for chunk in chunks if isinstance(chunk, dict)],
                requested_tool_call_ids=_function_call_ids(terminal),
            )
        )
    return calls


def _tool_interactions(
    report: ReviewReport,
    agent_context: dict[str, Any] | None,
    model_calls: list[TraceModelCall],
) -> list[TraceToolInteraction]:
    call_to_model = {
        call_id: call.call_index
        for call in model_calls
        for call_id in call.requested_tool_call_ids
    }
    responses: dict[str, Any] = {}
    calls = []
    for event in (agent_context or {}).get("events", []):
        if not isinstance(event, dict):
            continue
        call_id = str(event.get("call_id") or "")
        if event.get("type") == "tool_response":
            responses[call_id] = event.get("response")
        elif event.get("type") == "tool_call":
            calls.append(event)

    interactions = []
    observed_run_ids: set[str] = set()
    for sequence, event in enumerate(calls, start=1):
        call_id = str(event.get("call_id") or f"unknown-{sequence}")
        arguments = event.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        command = str(arguments.get("command") or "")
        decisions = [
            decision.model_dump(mode="json")
            for decision in report.filter_decisions
            if command and decision.command == command
        ]
        runs = [
            run.model_dump(mode="json")
            for run in report.sandbox_runs
            if command and run.command == command
        ]
        observed_run_ids.update(str(run.get("run_id") or "") for run in runs)
        interactions.append(
            TraceToolInteraction(
                sequence=sequence,
                model_call_index=call_to_model.get(call_id),
                call_id=call_id,
                tool=str(event.get("tool") or "unknown"),
                arguments=arguments,
                response=responses.get(call_id),
                policy_decisions=decisions,
                sandbox_runs=runs,
            )
        )
    for run in report.sandbox_runs:
        if run.run_id in observed_run_ids:
            continue
        matching_decisions = [
            decision.model_dump(mode="json")
            for decision in report.filter_decisions
            if decision.command == run.command
        ]
        interactions.append(
            TraceToolInteraction(
                sequence=len(interactions) + 1,
                call_id=f"sandbox-run:{run.run_id}",
                tool="sandbox_execution",
                arguments={"command": run.command},
                response={
                    "status": run.status,
                    "stdout": run.stdout_summary,
                    "stderr": run.stderr_summary,
                    "exit_code": run.exit_code,
                },
                policy_decisions=matching_decisions,
                sandbox_runs=[run.model_dump(mode="json")],
            )
        )
    return interactions


def _raw_agent_output(interactions: list[TraceToolInteraction]) -> dict[str, Any] | None:
    for interaction in reversed(interactions):
        if interaction.tool == "set_model_response":
            return interaction.arguments
    return None


def build_review_run_trace(
    *,
    report: ReviewReport,
    mode: str,
    model_io: dict[str, Any] | None,
    agent_context: dict[str, Any] | None,
    trace_lines: list[str],
) -> ReviewRunTrace:
    """Build the authoritative cross-layer trace for one completed workflow."""
    model_calls = _model_calls(model_io)
    interactions = _tool_interactions(report, agent_context, model_calls)
    backend_timeline = [
        {"sequence": index, "event": line}
        for index, line in enumerate(trace_lines, start=1)
    ]
    validated = (agent_context or {}).get("structured_result")
    return ReviewRunTrace(
        task_id=report.task_id,
        mode=mode,
        status=report.status,
        input_summary=report.input_summary.model_dump(mode="json"),
        model_calls=model_calls,
        tool_interactions=interactions,
        backend_timeline=backend_timeline,
        outcome=TraceOutcome(
            raw_set_model_response_arguments=_raw_agent_output(interactions),
            sdk_validated_agent_output=validated if isinstance(validated, dict) else None,
            normalized_report_output=report.analysis.model_dump(mode="json"),
        ),
        monitoring=report.monitoring.model_dump(mode="json"),
    )
