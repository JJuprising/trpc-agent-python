"""Write machine-readable and human-readable review reports."""

import html
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ReviewFinding
from .models import ReviewReport
from security import redact_report


@dataclass(frozen=True)
class ReportArtifacts:
    """Paths generated for a completed report."""

    json_path: Path
    markdown_path: Path
    trajectory_path: Path | None = None
    agent_context_path: Path | None = None
    agent_io_path: Path | None = None
    model_io_path: Path | None = None
    run_trace_json_path: Path | None = None
    run_trace_markdown_path: Path | None = None


class ReportWriter:
    """Render the two report formats required by the example."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write(self, report: ReviewReport) -> ReportArtifacts:
        """Write JSON and Markdown files for one report."""
        report = redact_report(report)
        report_dir = self._report_dir(report.task_id)
        json_path = report_dir / "review_report.json"
        markdown_path = report_dir / "review_report.md"
        # Publish the machine-readable report last so partial pairs are not authoritative.
        self._atomic_write(
            markdown_path,
            self._to_markdown(report),
        )
        self._atomic_write(
            json_path,
            report.model_dump_json(indent=2),
        )
        return ReportArtifacts(json_path=json_path, markdown_path=markdown_path)

    def write_trajectory(self, task_id: str, trace_lines: list[str]) -> Path:
        """Persist the workflow's already-redacted trace beside its report."""
        trajectory_path = self._report_dir(task_id) / "trajectory.log"
        content = "\n".join(trace_lines)
        if content:
            content += "\n"
        self._atomic_write(trajectory_path, content)
        return trajectory_path

    def write_agent_context(self, task_id: str, context: dict[str, Any]) -> Path:
        """Persist bounded, redacted Agent-visible context for one traced run."""
        context_path = self._report_dir(task_id) / "agent_context.json"
        self._atomic_write(
            context_path,
            json.dumps(context, ensure_ascii=False, indent=2, default=str),
        )
        return context_path

    def write_model_io(self, task_id: str, model_io: dict[str, Any]) -> Path:
        """Persist authoritative SDK-level model request/response snapshots."""
        model_io_path = self._report_dir(task_id) / "model_io.json"
        self._atomic_write(
            model_io_path,
            json.dumps(model_io, ensure_ascii=False, indent=2, default=str),
        )
        return model_io_path

    def write_private_artifact(self, task_id: str, name: str, content: str) -> Path:
        """Write one named private artifact through the shared safe output boundary."""
        if Path(name).name != name or name.startswith("."):
            raise ValueError("Artifact name must be a plain visible filename")
        path = self._report_dir(task_id) / name
        self._atomic_write(path, content)
        return path

    def write_agent_io(
        self,
        task_id: str,
        context: dict[str, Any],
        model_io: dict[str, Any] | None = None,
    ) -> Path:
        """Render the Agent run as readable model-and-tool rounds."""
        io_path = self._report_dir(task_id) / "agent_io.md"
        if model_io is not None and model_io.get("calls"):
            self._atomic_write(io_path, self._model_io_markdown(model_io))
            return io_path
        result = context.get("structured_result")
        output = (
            json.dumps(result, ensure_ascii=False, indent=2, default=str)
            if result is not None
            else "No structured Agent output was produced."
        )
        lines = [
            "# Agent Run by Rounds",
            "",
            "This is a bounded, redacted replay of the Agent-visible conversation. "
            "It excludes hidden model reasoning and backend-only audit details.",
            "",
            "## Round 0 — Initial Request Sent to the Model",
            "",
            "The following four inputs are available before the Agent chooses its first tool.",
            "",
            "### System Prompt",
            "",
            self._code_block(context.get("system_instruction", "")),
            "",
            "### Task Prompt",
            "",
            self._code_block(context.get("task_instruction", "")),
            "",
            "### Available Tools",
            "",
            self._code_block(self._tool_contract_summary(context.get("tool_contract"))),
            "",
            "### Required Final Output",
            "",
            self._code_block(self._output_contract_summary(context.get("output_contract"))),
            "",
        ]
        previous_tool_responses: list[object] = []
        for index, round_data in enumerate(self._agent_rounds(context.get("events")), start=1):
            if index == 1:
                round_input = {
                    "carried_forward": "Round 0 initial request",
                    "new_messages": [],
                }
            else:
                round_input = {
                    "carried_forward": (
                        "Round 0 plus all earlier model messages, tool calls, and tool responses"
                    ),
                    "new_messages": previous_tool_responses,
                }
            lines.extend(
                [
                    f"## Round {index}",
                    "",
                    "### Input Received by the Model This Round",
                    "",
                    "`carried_forward` is not duplicated below; `new_messages` shows "
                    "the tool results newly appended since the previous model call.",
                    "",
                    self._code_block(json.dumps(
                        round_input,
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )),
                    "",
                    "### Model Output and Tool Choice",
                    "",
                    self._code_block(json.dumps(
                        {
                            "model_messages": round_data["model_messages"],
                            "tool_calls": round_data["tool_calls"],
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )),
                    "",
                    "### Backend Tool Results Produced After This Round",
                    "",
                    "These are not input to the current round. They become "
                    "`new_messages` in the next round.",
                    "",
                    self._code_block(json.dumps(
                        round_data["tool_responses"],
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )),
                    "",
                ]
            )
            previous_tool_responses = round_data["tool_responses"]
        lines.extend(
            [
                "## Final Structured Output",
                "",
                "This is the Agent's final result, submitted through `set_model_response`.",
                "",
                self._code_block(output),
                "",
                "## Backend-only Details",
                "",
                "Filter audit records, sandbox timings, redaction mechanics, and raw "
                "provider-oriented tool schemas are not new prompts. See `agent_context.json` "
                "for the raw bounded capture and `review_report.json` for audit data.",
                "",
            ]
        )
        content = "\n".join(lines)
        self._atomic_write(io_path, content)
        return io_path

    @classmethod
    def _model_io_markdown(cls, model_io: dict[str, Any]) -> str:
        """Render callback-captured model calls without reconstructing their input."""
        lines = [
            "# Authoritative SDK Model I/O",
            "",
            "Each round below is captured at the model callback boundary after the SDK "
            "has injected Skill content, tools, conversation history, and the output "
            "schema. Requests are not reconstructed from Runner events.",
            "",
            "The OpenAI adapter may still translate this SDK request into a provider-specific "
            "HTTP body. All values shown here are redacted and safety-bounded.",
            "",
        ]
        for call in model_io.get("calls", []):
            if not isinstance(call, dict):
                continue
            call_index = call.get("call_index", len(lines))
            chunks = call.get("response_chunks", [])
            terminal = call.get("terminal_response")
            lines.extend(
                [
                    f"## Model Call {call_index}",
                    "",
                    "### Formatted SDK Request",
                    "",
                    *cls._formatted_sdk_request(call.get("request", {})),
                    "### Formatted Terminal SDK Response",
                    "",
                    *cls._formatted_sdk_response(terminal),
                    "",
                    f"- Stream chunks captured: `{len(chunks) if isinstance(chunks, list) else 0}`",
                    "- The exact SDK request and every stream chunk are preserved in `model_io.json`.",
                    "",
                ]
            )
        return "\n".join(lines)

    @classmethod
    def _formatted_sdk_request(cls, request: object) -> list[str]:
        """Render an SDK request without escaped newlines inside text fields."""
        data = request if isinstance(request, dict) else {}
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        lines = [f"- Model: `{cls._text(data.get('model', ''))}`", ""]
        lines.extend(
            [
                "#### System Instruction",
                "",
                cls._code_block(config.get("system_instruction", "")),
                "",
                "#### Conversation Messages",
                "",
            ]
        )
        contents = data.get("contents", [])
        for message_index, message in enumerate(contents if isinstance(contents, list) else [], start=1):
            message_data = message if isinstance(message, dict) else {}
            lines.extend(
                [
                    f"##### Message {message_index} — role `{cls._text(message_data.get('role', ''))}`",
                    "",
                ]
            )
            for part_index, part in enumerate(message_data.get("parts", []), start=1):
                lines.extend(cls._formatted_content_part(part, part_index))
        lines.extend(["#### Available Tool Declarations", ""])
        tool_groups = config.get("tools", [])
        for group in tool_groups if isinstance(tool_groups, list) else []:
            if not isinstance(group, dict):
                continue
            for declaration in group.get("function_declarations", []):
                if not isinstance(declaration, dict):
                    continue
                parameters = declaration.get("parameters")
                properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
                lines.extend(
                    [
                        f"##### `{cls._text(declaration.get('name', ''))}`",
                        "",
                        str(declaration.get("description", "")).strip(),
                        "",
                        "Parameters: " + ", ".join(
                            f"`{cls._text(name)}`" for name in properties
                        ),
                        "",
                    ]
                )
        return lines

    @classmethod
    def _formatted_sdk_response(cls, response: object) -> list[str]:
        """Render a terminal SDK response as readable content parts and metadata."""
        data = response if isinstance(response, dict) else {}
        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        metadata = {
            key: value
            for key, value in data.items()
            if key not in {"content", "custom_metadata"}
        }
        lines = ["#### Response Metadata", "", cls._code_block(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str)
        ), "", f"#### Response Content — role `{cls._text(content.get('role', ''))}`", ""]
        for part_index, part in enumerate(content.get("parts", []), start=1):
            lines.extend(cls._formatted_content_part(part, part_index))
        if not content:
            lines.extend(["No terminal content was returned.", ""])
        return lines

    @classmethod
    def _formatted_content_part(cls, part: object, part_index: int) -> list[str]:
        """Render one text, function-call, or function-response content part."""
        data = part if isinstance(part, dict) else {}
        if "text" in data:
            label = "Thought summary" if data.get("thought") else "Text"
            return [f"###### Part {part_index} — {label}", "", cls._code_block(data["text"]), ""]
        if isinstance(data.get("function_call"), dict):
            call = data["function_call"]
            return [
                f"###### Part {part_index} — Function call `{cls._text(call.get('name', ''))}`",
                "",
                cls._code_block(json.dumps(call, ensure_ascii=False, indent=2, default=str)),
                "",
            ]
        if isinstance(data.get("function_response"), dict):
            response = data["function_response"]
            lines = [
                f"###### Part {part_index} — Function response `{cls._text(response.get('name', ''))}`",
                "",
                f"Call ID: `{cls._text(response.get('id', ''))}`",
                "",
            ]
            payload = response.get("response")
            if isinstance(payload, dict):
                for key, value in payload.items():
                    rendered = cls._readable_value(value)
                    lines.extend([f"**{cls._text(key)}**", "", cls._code_block(rendered), ""])
            else:
                lines.extend([cls._code_block(cls._readable_value(payload)), ""])
            return lines
        return [
            f"###### Part {part_index} — Other",
            "",
            cls._code_block(json.dumps(data, ensure_ascii=False, indent=2, default=str)),
            "",
        ]

    @staticmethod
    def _readable_value(value: object) -> str:
        """Render JSON encoded inside a string as indented text when possible."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    return json.dumps(json.loads(stripped), ensure_ascii=False, indent=2)
                except (json.JSONDecodeError, TypeError):
                    pass
            return value
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _tool_contract_summary(contract: object) -> str:
        """Summarize provider tool declarations without dumping implementation fields."""
        data = contract if isinstance(contract, dict) else {}
        tools = []
        for declaration in data.get("declarations", []):
            if not isinstance(declaration, dict):
                continue
            parameters = declaration.get("parameters")
            properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
            tools.append({
                "name": declaration.get("name"),
                "description": declaration.get("description"),
                "parameters": sorted(properties) if isinstance(properties, dict) else [],
            })
        return json.dumps(
            {
                "tool_names": data.get("available_tool_names", []),
                "tools": tools,
                "note": data.get("skill_content_note", ""),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    @staticmethod
    def _output_contract_summary(contract: object) -> str:
        """Summarize the schema used by the SDK final-response tool."""
        data = contract if isinstance(contract, dict) else {}
        schema = data.get("json_schema")
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        return json.dumps(
            {
                "instruction": data.get("injected_instruction", ""),
                "tool": "set_model_response",
                "required_fields": schema.get("required", []) if isinstance(schema, dict) else [],
                "fields": sorted(properties) if isinstance(properties, dict) else [],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    @staticmethod
    def _agent_rounds(events: object) -> list[dict[str, list[object]]]:
        """Group the recorded event stream into model-action-result rounds."""
        rounds = []
        current = {"model_messages": [], "tool_calls": [], "tool_responses": []}
        pending_call_ids: set[str] = set()
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "model_message":
                current["model_messages"].append(event.get("text", ""))
            elif event_type == "tool_call":
                current["tool_calls"].append(event)
                call_id = event.get("call_id")
                if isinstance(call_id, str):
                    pending_call_ids.add(call_id)
            elif event_type == "tool_response":
                current["tool_responses"].append(event)
                call_id = event.get("call_id")
                if isinstance(call_id, str):
                    pending_call_ids.discard(call_id)
                if not pending_call_ids:
                    rounds.append(current)
                    current = {"model_messages": [], "tool_calls": [], "tool_responses": []}
        if any(current.values()):
            rounds.append(current)
        return rounds

    def _report_dir(self, task_id: str) -> Path:
        """Create and validate the private directory for one task's artifacts."""
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_metadata = os.lstat(self.output_dir)
        if stat.S_ISLNK(output_metadata.st_mode) or not stat.S_ISDIR(
            output_metadata.st_mode
        ):
            raise ValueError("Report output path must be a directory, not a link")
        report_dir = self.output_dir / task_id
        try:
            report_dir.mkdir(mode=0o700)
        except FileExistsError:
            metadata = os.lstat(report_dir)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Task report path must be a directory, not a link")
        report_dir.chmod(0o700)
        return report_dir

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _text(value: object) -> str:
        """Escape model-controlled text so it cannot create Markdown structure."""
        escaped = html.escape(str(value), quote=False)
        for character in "\\`*_{}[]<>()#+-!|":
            escaped = escaped.replace(character, f"\\{character}")
        return escaped.replace("\r", "").replace("\n", "  \n")

    @staticmethod
    def _inline_code(value: object) -> str:
        text = str(value).replace("\r", " ").replace("\n", " ")
        longest = max(
            (len(match.group(0)) for match in re.finditer(r"`+", text)),
            default=0,
        )
        fence = "`" * max(1, longest + 1)
        padding = " " if text.startswith("`") or text.endswith("`") else ""
        return f"{fence}{padding}{text}{padding}{fence}"

    @staticmethod
    def _code_block(value: object) -> str:
        text = str(value).replace("\r", "")
        longest = max(
            (len(match.group(0)) for match in re.finditer(r"`+", text)),
            default=0,
        )
        fence = "`" * max(3, longest + 1)
        return f"{fence}text\n{text}\n{fence}"

    @staticmethod
    def _to_markdown(report: ReviewReport) -> str:
        lines = [
            "# Code Review Report",
            "",
            f"- Task ID: {ReportWriter._inline_code(report.task_id)}",
            f"- Status: {ReportWriter._inline_code(report.status)}",
            f"- Created: {ReportWriter._inline_code(report.created_at.isoformat())}",
            f"- Completed: {ReportWriter._inline_code(report.completed_at.isoformat())}",
            f"- Repository: {ReportWriter._inline_code(report.repository)}",
            f"- Scope: {ReportWriter._inline_code(report.scope.value)}",
            "- Input: "
            f"{ReportWriter._inline_code(report.input_summary.kind)} / "
            f"{ReportWriter._inline_code(report.input_summary.source)}",
            "",
            "## Summary",
            "",
            ReportWriter._text(report.analysis.summary),
            "",
            f"- Findings: `{len(report.analysis.findings)}`",
            f"- Warnings: `{len(report.analysis.warnings)}`",
            "- Needs human review: "
            f"`{len(report.analysis.needs_human_review)}`",
            "- Severity distribution: "
            f"`{report.monitoring.severity_distribution}`",
            "",
            "## Findings",
            "",
        ]
        if not report.analysis.findings:
            lines.append("No findings.")
        for finding in report.analysis.findings:
            location = finding.file
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            lines.extend(
                [
                    "### "
                    f"[{finding.severity.upper()}] {ReportWriter._text(finding.title)}",
                    "",
                    f"- Category: {ReportWriter._inline_code(finding.category)}",
                    f"- Location: {ReportWriter._inline_code(location)}",
                    f"- Confidence: {ReportWriter._inline_code(f'{finding.confidence:.2f}')}",
                    f"- Source: {ReportWriter._inline_code(finding.source)}",
                    "",
                    ReportWriter._code_block(finding.evidence),
                    "",
                    f"Recommendation: {ReportWriter._text(finding.recommendation)}",
                    "",
                ]
            )
        ReportWriter._append_finding_section(
            lines,
            "Warnings",
            report.analysis.warnings,
        )
        ReportWriter._append_finding_section(
            lines,
            "Needs Human Review",
            report.analysis.needs_human_review,
        )
        lines.extend(["", "## Checks Performed", ""])
        if report.analysis.checks_performed:
            lines.extend(
                f"- {ReportWriter._text(item)}"
                for item in report.analysis.checks_performed
            )
        else:
            lines.append("- None reported.")
        lines.extend(["", "## Filter Decisions", ""])
        if report.filter_decisions:
            for decision in report.filter_decisions:
                lines.append(
                    f"- {ReportWriter._inline_code(decision.decision)} — "
                    f"{ReportWriter._inline_code(decision.command)}: "
                    f"{ReportWriter._text(decision.reason)}"
                )
        else:
            lines.append("- None recorded.")
        lines.extend(["", "## Sandbox Runs", ""])
        if report.sandbox_runs:
            for run in report.sandbox_runs:
                flags = []
                if run.timed_out:
                    flags.append("timed_out")
                if run.output_truncated:
                    flags.append("output_truncated")
                flag_text = f", flags={','.join(flags)}" if flags else ""
                lines.append(
                    f"- {ReportWriter._inline_code(run.status)} — "
                    f"{ReportWriter._inline_code(run.command)} "
                    f"({run.duration_ms:.2f} ms, exit={run.exit_code}{flag_text})"
                )
                if run.stderr_summary:
                    lines.append(f"  Error: {ReportWriter._text(run.stderr_summary)}")
        else:
            lines.append("- No sandbox run recorded.")
        metrics = report.monitoring
        lines.extend(
            [
                "",
                "## Monitoring",
                "",
                f"- Total duration: `{metrics.total_duration_ms:.2f} ms`",
                f"- Sandbox duration: `{metrics.sandbox_duration_ms:.2f} ms`",
                f"- Tool calls: `{metrics.tool_call_count}`",
                f"- Blocked executions: `{metrics.blocked_count}`",
                f"- Findings: `{metrics.finding_count}`",
                f"- Severity distribution: `{metrics.severity_distribution}`",
                f"- Exception distribution: `{metrics.exception_distribution}`",
                "",
                "## Conclusion",
                "",
                ReportWriter._text(report.conclusion),
            ]
        )
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _append_finding_section(
        lines: list[str],
        title: str,
        findings: list[ReviewFinding],
    ) -> None:
        lines.extend(["", f"## {title}", ""])
        if not findings:
            lines.append("None.")
            return
        for finding in findings:
            location = finding.file
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            lines.extend(
                [
                    "- **"
                    f"[{finding.severity.upper()}] {ReportWriter._text(finding.title)}** ",
                    f"  {ReportWriter._inline_code(location)} · "
                    f"{ReportWriter._inline_code(finding.category)} · "
                    f"confidence {ReportWriter._inline_code(f'{finding.confidence:.2f}')}",
                    f"  {ReportWriter._code_block(finding.evidence)}",
                    "  Recommendation: "
                    f"{ReportWriter._text(finding.recommendation)}",
                ]
            )
