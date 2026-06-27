#!/usr/bin/env python3
"""Watch for Hermes delivery failures and re-alert after delivery recovers.

Designed for no-agent cron use:
- reads only small tails of local logs
- prints nothing when there is nothing new/pending
- keeps pending alerts until this watchdog's own previous delivery succeeded
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


HOME = Path.home()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
LOG_DIR = HERMES_HOME / "logs"
CRON_JOBS = HERMES_HOME / "cron" / "jobs.json"
STATE_FILE = HERMES_HOME / "delivery-failure-watchdog.json"
SCRIPT_NAME = "delivery-failure-watchdog.py"

MAX_TAIL_BYTES = 256 * 1024
LOOKBACK_HOURS = 24
REPEAT_INTERVAL_SECONDS = 60 * 60
MAX_PENDING = 50
MAX_ACKNOWLEDGED = 200

LOGS = [
    LOG_DIR / "agent.log",
    LOG_DIR / "gateway.error.log",
    LOG_DIR / "gateway.log",
]

PATTERNS = [
    ("cron_delivery_error", re.compile(r"cron\.scheduler: Job '([^']+)': delivery error: (.+)", re.I)),
    ("feishu_send_error", re.compile(r"\[Feishu\] Send error: (.+)", re.I)),
    ("feishu_send_retry", re.compile(r"\[Feishu\] Send attempt \d+/\d+ failed for chat ([^;]+); retrying .*: (.+)", re.I)),
    ("unconfirmed_live_send", re.compile(r"live adapter send to ([^ ]+) returned unconfirmed result .*error=(.+?)\), falling back", re.I)),
    ("shutdown_notify_failed", re.compile(r"Failed to send shutdown notification to ([^:]+):([^:]+): (.+)", re.I)),
]


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def tail_text(path: Path, max_bytes: int = MAX_TAIL_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse_ts(line: str) -> datetime | None:
    raw = line[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


def event_key(event: dict) -> str:
    basis = "|".join(str(event.get(k, "")) for k in ("ts", "source", "kind", "detail"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def truncate(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def collect_events() -> list[dict]:
    cutoff = datetime.now() - timedelta(hours=LOOKBACK_HOURS)
    events: list[dict] = []
    for path in LOGS:
        text = tail_text(path)
        if not text:
            continue
        for line in text.splitlines():
            ts = parse_ts(line)
            if ts and ts < cutoff:
                continue
            for kind, pattern in PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                detail = truncate(match.group(0))
                event = {
                    "ts": ts.isoformat(sep=" ") if ts else "",
                    "source": path.name,
                    "kind": kind,
                    "detail": detail,
                }
                event["key"] = event_key(event)
                events.append(event)
                break
    deduped = {}
    for event in events:
        deduped[event["key"]] = event
    return sorted(deduped.values(), key=lambda item: (item.get("ts") or "", item["key"]))


def own_last_delivery_error() -> str | None:
    try:
        jobs = json.loads(CRON_JOBS.read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, json.JSONDecodeError):
        return None
    for job in jobs:
        if job.get("script") == SCRIPT_NAME or job.get("name") == "delivery-failure-watchdog":
            err = job.get("last_delivery_error")
            return str(err) if err else None
    return None


def render(events: list[dict], own_error: str | None, repeat: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"## Hermes delivery failure watchdog · {now}",
        "",
        "检测到 Hermes/Feishu 投递失败记录；这类错误可能导致应发消息只留在本地日志里。",
    ]
    if own_error:
        lines.extend([
            "",
            f"注意：watchdog 上一轮自己的投递也失败了，因此以下 pending 摘要会继续保留并重发。错误：{truncate(own_error)}",
        ])
    elif repeat:
        lines.extend([
            "",
            "这是 pending 摘要的低频重复提醒；如果你已经看到过，可以按日志路径复查后忽略。",
        ])

    counts: dict[str, int] = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    lines.extend(["", "类型统计:"])
    for kind, count in sorted(counts.items()):
        lines.append(f"- {kind}: {count}")

    lines.extend(["", "最近样本:"])
    for event in events[-8:]:
        ts = event.get("ts") or "(no timestamp)"
        lines.append(f"- {ts} · {event['source']} · {event['kind']} · {event['detail']}")

    lines.extend([
        "",
        "本提醒由 no-agent 本地脚本生成；平时无异常时静默，不调用模型。",
    ])
    return "\n".join(lines)


def main() -> int:
    state = read_state()
    own_error = own_last_delivery_error()

    pending: dict[str, dict] = {
        item["key"]: item for item in state.get("pending", []) if isinstance(item, dict) and item.get("key")
    }
    last_emit_keys = set(state.get("last_emit_keys") or [])
    acknowledged_keys = set(state.get("acknowledged_keys") or [])

    if last_emit_keys and not own_error:
        for key in last_emit_keys:
            pending.pop(key, None)
        acknowledged_keys.update(last_emit_keys)
        last_emit_keys = set()

    for event in collect_events():
        if event["key"] in acknowledged_keys:
            continue
        pending.setdefault(event["key"], event)

    if len(pending) > MAX_PENDING:
        pending = dict(sorted(pending.items(), key=lambda item: (item[1].get("ts") or "", item[0]))[-MAX_PENDING:])
    if len(acknowledged_keys) > MAX_ACKNOWLEDGED:
        acknowledged_keys = set(sorted(acknowledged_keys)[-MAX_ACKNOWLEDGED:])

    pending_events = sorted(pending.values(), key=lambda item: (item.get("ts") or "", item["key"]))
    now = time.time()
    last_emit_at = float(state.get("last_emit_at") or 0)
    new_keys = set(pending) - set(state.get("known_pending_keys") or [])
    should_repeat = bool(pending_events) and (now - last_emit_at) >= REPEAT_INTERVAL_SECONDS

    next_state = {
        "updated_at": now,
        "pending": pending_events,
        "known_pending_keys": sorted(pending),
        "acknowledged_keys": sorted(acknowledged_keys),
        "last_emit_keys": sorted(last_emit_keys),
        "last_emit_at": last_emit_at,
    }

    if not pending_events:
        write_state(next_state)
        return 0

    if not new_keys and not own_error and not should_repeat:
        write_state(next_state)
        return 0

    emit_keys = sorted(pending)
    next_state["last_emit_keys"] = emit_keys
    next_state["last_emit_at"] = now
    write_state(next_state)
    print(render(pending_events, own_error, repeat=not bool(new_keys)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
