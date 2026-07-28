from agent.llm import _decision_from_json, _json_object


def test_decision_from_json_parses_tool_calls():
    decision = _decision_from_json(
        """
{
  "summary": "Read and patch",
  "plan": ["inspect"],
  "patches": [],
  "tool_calls": [
    {"name": "read_file", "args": {"path": "app.py"}, "rationale": "Need context"}
  ],
  "done": false
}
"""
    )

    assert decision.tool_calls is not None
    assert decision.tool_calls[0].name == "read_file"
    assert decision.tool_calls[0].args["path"] == "app.py"


def test_json_object_extracts_json_from_text():
    assert _json_object("final\n{\"summary\":\"ok\"}\n")["summary"] == "ok"
