"""Compatibility facade for application tool execution and provider schemas."""

from .application.services.tool_executor import ToolCallExecutor as ApplicationToolCallExecutor
from .application.services.tool_executor import ToolExecutionResult as ToolCallResult
from .domain.value_objects import ToolCall
from .infrastructure.llm.tool_schemas import anthropic_tool_schemas, openai_tool_schemas
from .infrastructure.security import EnvironmentSecretRedactor


class ToolCallExecutor(ApplicationToolCallExecutor):
    def __init__(self, tools, redactor=None) -> None:
        super().__init__(tools, redactor or EnvironmentSecretRedactor())


__all__ = [
    "ToolCall",
    "ToolCallExecutor",
    "ToolCallResult",
    "anthropic_tool_schemas",
    "openai_tool_schemas",
]
