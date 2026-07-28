from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.application.ports.outbound import RepositoryTools, SecretRedactor
from agent.domain.value_objects import ToolCall


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    name: str
    args: dict[str, Any]
    ok: bool
    output: str

    redactor: SecretRedactor

    def as_log(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": _redact_args(self.args, self.redactor),
            "ok": self.ok,
            "output": self.redactor.redact(self.output)[-20_000:],
        }


class ToolCallExecutor:
    def __init__(self, tools: RepositoryTools, redactor: SecretRedactor) -> None:
        self._tools = tools
        self._redactor = redactor

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        try:
            return ToolExecutionResult(call.name, call.args, True, self._execute(call), self._redactor)
        except Exception as exc:
            return ToolExecutionResult(call.name, call.args, False, f"{type(exc).__name__}: {exc}", self._redactor)

    def as_log(self, result: ToolExecutionResult) -> dict[str, Any]:
        return result.as_log()

    def _execute(self, call: ToolCall) -> str:
        match call.name:
            case "list_files":
                return "\n".join(self._tools.list_files())
            case "read_file":
                return self._tools.read_file(str(call.args["path"]), int(call.args.get("max_bytes", 80_000)))
            case "search_text":
                return self._tools.search_text(str(call.args["pattern"]))
            case "apply_patch":
                self._tools.apply_patch(str(call.args["patch"]))
                return "patch applied"
            case "write_file":
                self._tools.write_file(str(call.args["path"]), str(call.args["content"]))
                return "file written"
            case "run_command_in_sandbox":
                result = self._tools.run_command_in_sandbox(
                    str(call.args["command"]),
                    timeout_seconds=int(call.args["timeout_seconds"]) if call.args.get("timeout_seconds") else None,
                )
                return (
                    f"exit_code={result.exit_code}\nruntime_seconds={result.runtime_seconds:.3f}\n"
                    f"timed_out={result.timed_out}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            case "git_diff":
                return self._tools.git_diff()
            case "git_status":
                return self._tools.git_status()
            case _:
                raise ValueError(f"Unsupported tool: {call.name}")


def _redact_args(args: dict[str, Any], redactor: SecretRedactor) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in args.items():
        limit = 20_000 if key in {"content", "patch"} else None
        safe = redactor.redact(str(value))
        redacted[key] = safe[:limit] if limit else safe
    return redacted
