"""Local messaging budgets, independent of the upstream turn implementation."""
from dataclasses import replace
import re

from gateway.config import Platform


def _normal_limit(config: dict, key: str, default: int) -> int:
    value = config.get("gateway", {}).get("operational_guardrails", {})
    if not isinstance(value, dict):
        value = {}
    try:
        return max(0, int(value.get(key, default)))
    except (TypeError, ValueError):
        return default


def _deep_request(message: str) -> bool:
    return bool(re.search(r"(^\s*/deep(?:\s|$))|深入排查|分析根因", message, re.IGNORECASE))


def messaging_iteration_limit(config: dict, platform: Platform, current: int, message: str = "") -> int:
    if platform == Platform.LOCAL:
        return current
    deep = _deep_request(message)
    cap = _normal_limit(config, "deep_max_api_calls" if deep else "normal_max_api_calls", 32 if deep else 12)
    return min(current, cap) if cap else current


def apply_file_search_limit(agent, config: dict, platform: Platform, message: str = "") -> None:
    if platform == Platform.LOCAL:
        return
    controller = getattr(agent, "_tool_guardrails", None)
    if controller is None:
        return
    cap = 0 if _deep_request(message) else _normal_limit(config, "normal_max_file_searches", 8)
    controller.config = replace(
        controller.config,
        loop_caps=replace(controller.config.loop_caps, max_file_searches=cap),
    )
