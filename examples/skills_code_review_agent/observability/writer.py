"""Write the unified trace as machine-readable JSON and a guided Markdown replay."""

from dataclasses import dataclass
from pathlib import Path

from reports.writers import ReportWriter

from .models import ReviewRunTrace


@dataclass(frozen=True)
class RunTraceArtifacts:
    """Paths for the two unified trace views."""

    json_path: Path
    markdown_path: Path


class RunTraceWriter:
    """Render a ReviewRunTrace without adding orchestration logic to Workflow."""

    def __init__(self, report_writer: ReportWriter) -> None:
        self.report_writer = report_writer

    def write(self, trace: ReviewRunTrace) -> RunTraceArtifacts:
        json_path = self.report_writer.write_private_artifact(
            trace.task_id,
            "run_trace.json",
            trace.model_dump_json(indent=2),
        )
        markdown_path = self.report_writer.write_private_artifact(
            trace.task_id,
            "run_trace.md",
            self._to_markdown(trace),
        )
        return RunTraceArtifacts(json_path=json_path, markdown_path=markdown_path)

    @staticmethod
    def _block(value: object) -> str:
        import json

        text = value if isinstance(value, str) else json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        return ReportWriter._code_block(text)

    @classmethod
    def _to_markdown(cls, trace: ReviewRunTrace) -> str:
        input_summary = dict(trace.input_summary)
        input_preview = input_summary.pop("redacted_preview", "")
        lines = [
            "# Review Run Trace",
            "",
            f"- Task: `{trace.task_id}`",
            f"- Mode: `{trace.mode}`",
            f"- Status: `{trace.status}`",
            f"- Model calls: `{len(trace.model_calls)}`",
            f"- Tool calls: `{len(trace.tool_interactions)}`",
            "",
            "## 1. Input",
            "",
            cls._block(input_summary),
            "",
            "### Changed-code preview",
            "",
            cls._block(input_preview),
            "",
            "## 2. Model and Tool Rounds",
            "",
        ]
        tools_by_model: dict[int, list[TraceToolInteraction]] = {}
        unassigned = []
        for tool in trace.tool_interactions:
            if tool.model_call_index is None:
                unassigned.append(tool)
            else:
                tools_by_model.setdefault(tool.model_call_index, []).append(tool)
        for call in trace.model_calls:
            lines.extend(
                [
                    f"### Model Call {call.call_index}",
                    "",
                    "#### Exact SDK request — formatted for reading",
                    "",
                    *ReportWriter._formatted_sdk_request(call.request),
                    "#### Terminal SDK response — formatted for reading",
                    "",
                    *ReportWriter._formatted_sdk_response(call.terminal_response or {}),
                    "",
                ]
            )
            for tool in tools_by_model.get(call.call_index, []):
                lines.extend(cls._tool_markdown(tool))
        if unassigned:
            lines.extend(["### Tool calls not linked to a terminal model response", ""])
            for tool in unassigned:
                lines.extend(cls._tool_markdown(tool))
        lines.extend(
            [
                "## 3. Output Transformation",
                "",
                "### Raw model arguments to `set_model_response`",
                "",
                cls._block(trace.outcome.raw_set_model_response_arguments or {}),
                "",
                "### SDK-validated Agent output",
                "",
                cls._block(trace.outcome.sdk_validated_agent_output or {}),
                "",
                "### Trusted normalized report output",
                "",
                cls._block(trace.outcome.normalized_report_output),
                "",
                "## 4. Backend Lifecycle",
                "",
                cls._block(trace.backend_timeline),
                "",
                "## 5. Monitoring",
                "",
                cls._block(trace.monitoring),
                "",
            ]
        )
        return "\n".join(lines)

    @classmethod
    def _tool_markdown(cls, tool) -> list[str]:
        return [
            f"#### Tool {tool.sequence}: `{tool.tool}`",
            "",
            f"- Call ID: `{tool.call_id}`",
            "",
            "Requested arguments:",
            "",
            *ReportWriter._formatted_content_part(
                {
                    "function_call": {
                        "name": tool.tool,
                        "id": tool.call_id,
                        "args": tool.arguments,
                    }
                },
                1,
            ),
            "",
            "Filter decisions:",
            "",
            cls._block(tool.policy_decisions),
            "",
            "Sandbox executions:",
            "",
            cls._block(tool.sandbox_runs),
            "",
            "Response returned to the Agent:",
            "",
            *ReportWriter._formatted_content_part(
                {
                    "function_response": {
                        "name": tool.tool,
                        "id": tool.call_id,
                        "response": tool.response,
                    }
                },
                1,
            ),
            "",
        ]
