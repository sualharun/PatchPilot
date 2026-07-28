from __future__ import annotations

import json
from typing import Any, Protocol

from agent.application.services.tool_executor import ToolCallExecutor
from agent.domain.value_objects import AgentDecision, ToolCall
from agent.infrastructure.llm.pricing import TokenUsage
from agent.infrastructure.llm.tool_schemas import anthropic_tool_schemas, openai_tool_schemas


class LLMProvider(Protocol):
    def propose_fix(self, *, model: str, prompt: str) -> AgentDecision:
        ...


class StubLLMProvider:
    """Safe fallback: produce analysis but no code changes."""

    def propose_fix(self, *, model: str, prompt: str) -> AgentDecision:
        return AgentDecision(
            summary="Stub LLM mode produced no patch. Configure OPENAI_API_KEY or ANTHROPIC_API_KEY for edits.",
            plan=["Inspect issue context", "Run setup and tests", "Report failures for human review"],
            patches=[],
            tool_calls=[ToolCall(name="git_status", rationale="Record current repository state.")],
            usage=TokenUsage(provider="stub", model=model).as_dict(),
            done=False,
        )


class OpenAIProvider:
    def __init__(self, api_key: str) -> None:
        from openai import OpenAI

        self.client: Any = OpenAI(api_key=api_key)

    def propose_fix(self, *, model: str, prompt: str) -> AgentDecision:
        response = self.client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a careful coding agent. Return JSON with keys summary, plan, patches, done. "
                        "Prefer tool_calls over raw patches. tool_calls is a list of objects with name, args, "
                        "and rationale. Supported names: list_files, read_file, search_text, apply_patch, "
                        "write_file, git_diff, git_status. Keep edits minimal."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        decision = _decision_from_json(response.choices[0].message.content or "{}")
        usage = TokenUsage(provider="openai", model=model)
        if response.usage:
            usage.add(
                input_tokens=int(getattr(response.usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(response.usage, "completion_tokens", 0) or 0),
            )
        decision.usage = usage.as_dict()
        return decision

    def propose_fix_with_tools(
        self,
        *,
        model: str,
        prompt: str,
        tool_executor: ToolCallExecutor,
        max_tool_rounds: int = 8,
    ) -> AgentDecision:
        usage = TokenUsage(provider="openai", model=model)
        tool_results: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a careful autonomous coding agent for Python repositories. Use tools to inspect "
                    "and edit the repository. Keep edits minimal. When done, return JSON with keys summary, "
                    "plan, patches, done. patches can be empty if edits were made by tools."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        final_text = ""
        for _ in range(max_tool_rounds):
            response = self.client.chat.completions.create(
                model=model,
                temperature=0,
                messages=messages,
                tools=openai_tool_schemas(),
                tool_choice="auto",
            )
            if response.usage:
                usage.add(
                    input_tokens=int(getattr(response.usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(getattr(response.usage, "completion_tokens", 0) or 0),
                )
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                final_text = message.content or ""
                break
            for native_call in message.tool_calls:
                function = native_call.function
                args = _json_object(function.arguments or "{}")
                call = ToolCall(name=function.name, args=args, rationale="provider-native-openai")
                result = tool_executor.execute(call)
                log = result.as_log() | {"rationale": call.rationale}
                tool_results.append(log)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": native_call.id,
                        "content": result.output[-20_000:],
                    }
                )

        if not final_text:
            messages.append(
                {
                    "role": "user",
                    "content": "Return the final JSON summary now. Do not call more tools.",
                }
            )
            response = self.client.chat.completions.create(
                model=model,
                temperature=0,
                messages=messages,
                response_format={"type": "json_object"},
            )
            if response.usage:
                usage.add(
                    input_tokens=int(getattr(response.usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(getattr(response.usage, "completion_tokens", 0) or 0),
                )
            final_text = response.choices[0].message.content or "{}"
        decision = _decision_from_json(final_text)
        decision.tool_results = tool_results
        decision.usage = usage.as_dict()
        return decision


class AnthropicProvider:
    def __init__(self, api_key: str) -> None:
        import anthropic

        self.client: Any = anthropic.Anthropic(api_key=api_key)

    def propose_fix(self, *, model: str, prompt: str) -> AgentDecision:
        message = self.client.messages.create(
            model=model,
            max_tokens=6000,
            temperature=0,
            system=(
                "You are a careful coding agent. Return only JSON with keys summary, plan, patches, done. "
                "Prefer tool_calls over raw patches. tool_calls is a list of objects with name, args, "
                "and rationale. Supported names: list_files, read_file, search_text, apply_patch, "
                "write_file, git_diff, git_status. Keep edits minimal."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        decision = _decision_from_json(text)
        usage = TokenUsage(provider="anthropic", model=model)
        if getattr(message, "usage", None):
            usage.add(
                input_tokens=int(getattr(message.usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(message.usage, "output_tokens", 0) or 0),
            )
        decision.usage = usage.as_dict()
        return decision

    def propose_fix_with_tools(
        self,
        *,
        model: str,
        prompt: str,
        tool_executor: ToolCallExecutor,
        max_tool_rounds: int = 8,
    ) -> AgentDecision:
        usage = TokenUsage(provider="anthropic", model=model)
        tool_results: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        final_text = ""

        for _ in range(max_tool_rounds):
            message = self.client.messages.create(
                model=model,
                max_tokens=6000,
                temperature=0,
                system=(
                    "You are a careful autonomous coding agent for Python repositories. Use tools to inspect "
                    "and edit the repository. Keep edits minimal. When done, return only JSON with keys summary, "
                    "plan, patches, done."
                ),
                tools=anthropic_tool_schemas(),
                messages=messages,
            )
            if getattr(message, "usage", None):
                usage.add(
                    input_tokens=int(getattr(message.usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(message.usage, "output_tokens", 0) or 0),
                )
            tool_uses = [block for block in message.content if getattr(block, "type", "") == "tool_use"]
            if not tool_uses:
                final_text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
                break

            messages.append({"role": "assistant", "content": message.content})
            results = []
            for block in tool_uses:
                args = dict(getattr(block, "input", {}) or {})
                call = ToolCall(name=block.name, args=args, rationale="provider-native-anthropic")
                result = tool_executor.execute(call)
                log = result.as_log() | {"rationale": call.rationale}
                tool_results.append(log)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.output[-20_000:],
                        "is_error": not result.ok,
                    }
                )
            messages.append({"role": "user", "content": results})

        if not final_text:
            messages.append({"role": "user", "content": "Return the final JSON summary now. Do not call more tools."})
            message = self.client.messages.create(
                model=model,
                max_tokens=4000,
                temperature=0,
                system="Return only JSON with keys summary, plan, patches, done.",
                messages=messages,
            )
            if getattr(message, "usage", None):
                usage.add(
                    input_tokens=int(getattr(message.usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(message.usage, "output_tokens", 0) or 0),
                )
            final_text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        decision = _decision_from_json(final_text)
        decision.tool_results = tool_results
        decision.usage = usage.as_dict()
        return decision


def provider_for_model(model: str, openai_api_key: str | None, anthropic_api_key: str | None) -> LLMProvider:
    lowered = model.lower()
    if lowered.startswith("claude") and anthropic_api_key:
        return AnthropicProvider(anthropic_api_key)
    if openai_api_key:
        return OpenAIProvider(openai_api_key)
    if anthropic_api_key:
        return AnthropicProvider(anthropic_api_key)
    return StubLLMProvider()


def _decision_from_json(raw: str) -> AgentDecision:
    data = _json_object(raw)
    patches = data.get("patches") or []
    tool_calls_raw = data.get("tool_calls") or []
    plan = data.get("plan") or []
    if isinstance(plan, str):
        plan = [plan]
    if isinstance(patches, str):
        patches = [patches]
    tool_calls: list[ToolCall] = []
    for raw_call in tool_calls_raw:
        if not isinstance(raw_call, dict) or not raw_call.get("name"):
            continue
        tool_calls.append(
            ToolCall(
                name=raw_call["name"],
                args=dict(raw_call.get("args") or {}),
                rationale=str(raw_call.get("rationale", "")),
            )
        )
    return AgentDecision(
        summary=str(data.get("summary", "")),
        plan=[str(item) for item in plan],
        patches=[str(item) for item in patches if str(item).strip()],
        tool_calls=tool_calls,
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
        done=bool(data.get("done", False)),
    )


def _json_object(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {"summary": raw.strip() or "Model returned no structured response.", "plan": [], "patches": []}
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return {"summary": raw.strip() or "Model returned invalid JSON.", "plan": [], "patches": []}
    return data if isinstance(data, dict) else {"summary": str(data), "plan": [], "patches": []}
