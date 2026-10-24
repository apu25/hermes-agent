"""Messaging budgets follow the current turn pipeline without replacing it."""
from types import SimpleNamespace

from agent.tool_guardrails import ToolCallGuardrailController
from gateway.config import Platform
from gateway.operational_limits import apply_file_search_limit, messaging_iteration_limit


def test_budgets_preserve_smaller_limits_and_local_turns():
    assert messaging_iteration_limit({}, Platform.FEISHU, 500) == 12
    assert messaging_iteration_limit({}, Platform.FEISHU, 4) == 4
    assert messaging_iteration_limit({}, Platform.LOCAL, 500) == 500
    assert messaging_iteration_limit({}, Platform.FEISHU, 500, "/deep diagnose") == 32
    assert messaging_iteration_limit({"gateway": {"operational_guardrails": {"normal_max_api_calls": 0}}}, Platform.FEISHU, 500) == 500


def test_cached_controller_resets_search_budget_between_normal_and_deep_turns():
    agent = SimpleNamespace(_tool_guardrails=ToolCallGuardrailController())
    original = agent._tool_guardrails.config.loop_caps.max_web_searches
    apply_file_search_limit(agent, {}, Platform.FEISHU)
    controller = agent._tool_guardrails
    for i in range(8):
        assert controller.before_call("search_files", {"query": str(i)}).allows_execution
    assert controller.before_call("search_files", {"query": "blocked"}).should_halt
    controller.reset_for_turn()
    apply_file_search_limit(agent, {}, Platform.FEISHU, "/deep diagnose")
    for i in range(10):
        assert controller.before_call("search_files", {"query": str(i)}).allows_execution
    assert controller.config.loop_caps.max_web_searches == original
