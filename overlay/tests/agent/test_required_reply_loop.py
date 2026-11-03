"""Exercise required-reply recovery at the real provider request/turn-loop boundary."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from agent.required_reply import required_reply
from gateway.response_filters import is_intentional_silence_response
from tests.agent.test_degenerate_final_recovery import loop_agent, _final, _tool_round


@pytest.mark.parametrize("required,stages,expected,calls", [
    (True, ["[SILENT]", "已查询，结果如下。"], "已查询，结果如下。", 2),
    (True, ["[SILENT]", "NO_REPLY"], "NO_REPLY", 2),
    (False, ["[SILENT]"], "[SILENT]", 1),
])
def test_real_loop_recovery_and_bounded_exhaustion(loop_agent, required, stages, expected, calls):
    requests = []
    responses = iter(_final(text) for text in stages)
    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        return next(responses)
    loop_agent.client.chat.completions.create.side_effect = create
    with (patch.object(loop_agent, "_persist_session"),
          patch.object(loop_agent, "_save_trajectory"),
          patch.object(loop_agent, "_cleanup_task_resources"),
          required_reply(is_intentional_silence_response if required else None)):
        result = loop_agent.run_conversation("帮我查一下彩虹六号市场开了吗")
    assert result["final_response"] == expected
    assert len(requests) == calls
    if calls == 2:
        assert requests[1]["messages"][:len(requests[0]["messages"])] == requests[0]["messages"]
        assert "回复恢复" in requests[1]["messages"][-1]["content"]
        assert not any(row.get("_empty_recovery_synthetic") for row in result["messages"])


def test_recovery_preserves_completed_tool_work(loop_agent):
    loop_agent.valid_tool_names = {"web_search"}
    loop_agent.client.chat.completions.create.side_effect = [
        _tool_round("search-once"), _final("[SILENT]"), _final("已核查。")]
    with (patch.object(loop_agent, "_persist_session"),
          patch.object(loop_agent, "_save_trajectory"),
          patch.object(loop_agent, "_cleanup_task_resources"),
          patch("model_tools.handle_function_call", return_value="search result") as tool,
          required_reply(is_intentional_silence_response)):
        result = loop_agent.run_conversation("查市场状态")
    assert result["final_response"] == "已核查。"
    assert tool.call_count == 1
    assert any(row.get("tool_call_id") == "search-once" for row in result["messages"])
