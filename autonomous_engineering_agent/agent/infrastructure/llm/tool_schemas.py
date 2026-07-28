from typing import Any


def openai_tool_schemas() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": _openai_function_schema(name, schema)}
        for name, schema in _tool_argument_schemas().items()
    ]


def anthropic_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": schema["description"],
            "input_schema": {
                "type": "object",
                "properties": schema["properties"],
                "required": schema.get("required", []),
                "additionalProperties": False,
            },
        }
        for name, schema in _tool_argument_schemas().items()
    ]


def _openai_function_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": schema["description"],
        "parameters": {
            "type": "object",
            "properties": schema["properties"],
            "required": schema.get("required", []),
            "additionalProperties": False,
        },
    }


def _tool_argument_schemas() -> dict[str, dict[str, Any]]:
    no_arguments: dict[str, Any] = {"properties": {}, "required": []}
    return {
        "list_files": {"description": "List tracked files in the repository.", **no_arguments},
        "read_file": {
            "description": "Read a repository file by relative path.",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 120000},
            },
            "required": ["path"],
        },
        "search_text": {
            "description": "Search the repository with ripgrep-compatible text.",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
        "apply_patch": {
            "description": "Apply a unified git patch to the repository.",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
        },
        "write_file": {
            "description": "Write exact content to a repository-relative file.",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        "run_command_in_sandbox": {
            "description": "Run an allowlisted command inside the Docker sandbox.",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
            },
            "required": ["command"],
        },
        "git_diff": {"description": "Return the current git diff.", **no_arguments},
        "git_status": {"description": "Return short git status.", **no_arguments},
    }
