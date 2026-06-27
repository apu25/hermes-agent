#!/Users/minim4/.hermes/hermes-agent/venv/bin/python3
"""Stable Hermes IT health probe for cron.

This script intentionally uses only the Python standard library. Cron runs in a
minimal background environment, so importing site packages such as yaml or
requests makes the health check less reliable than the system it is checking.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path


HOME = Path("/Users/minim4")
HERMES_HOME = HOME / ".hermes"
CONFIG_PATH = HERMES_HOME / "config.yaml"
PREFILL_PATH = HERMES_HOME / "feishu-rules.json"
STATE_PATH = HERMES_HOME / "it-health-state.json"
ERRORS_LOG = HERMES_HOME / "logs" / "errors.log"
CRON_JOBS = HERMES_HOME / "cron" / "jobs.json"
HERMES_BIN = "/Users/minim4/.local/bin/hermes"
MAX_MEMORY_CHARS = {"MEMORY.md": 2200, "USER.md": 1375}
FEISHU_HOSTS = [
    "open.feishu.cn",
    "accounts.feishu.cn",
    "api22-eeft-gateway-hl.feishu.cn",
]
CLASH_ACTIVE_CONFIG = HOME / "Library" / "Application Support" / "io.github.clash-verge-rev.clash-verge-rev" / "clash-verge.yaml"
FEISHU_DIRECT_RULES = [
    "DOMAIN-SUFFIX,feishu.cn,DIRECT",
    "DOMAIN-SUFFIX,feishu.net,DIRECT",
    "DOMAIN-SUFFIX,feishuapp.cn,DIRECT",
    "DOMAIN-SUFFIX,feishuapp.com,DIRECT",
    "DOMAIN-SUFFIX,larksuite.com,DIRECT",
    "DOMAIN-SUFFIX,larkoffice.com,DIRECT",
    "DOMAIN-SUFFIX,larkofficeapp.com,DIRECT",
    "DOMAIN-SUFFIX,larksuitecdn.com,DIRECT",
]


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def now_label() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def safe_read(path: Path, limit: int | None = None) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:] if limit else data


def write_state(payload: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def feishu_safe(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if re.fullmatch(r"\s*-{3,}\s*", line):
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [cell.strip(" *") for cell in line.strip().strip("|").split("|")]
            if cells and not all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
                lines.append("- " + " · ".join(cell for cell in cells if cell))
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _launchd_gateway_pid() -> int | None:
    domain = f"gui/{os.getuid()}"
    r = run(["launchctl", "print", f"{domain}/ai.hermes.gateway"], timeout=10)
    if r.returncode != 0:
        return None
    m = re.search(r"(?m)^\s*pid = (\d+)\b", r.stdout)
    if not m:
        return None
    pid = int(m.group(1))
    return pid if _pid_alive(pid) else None


def _process_table_gateway_pid() -> int | None:
    r = run(["ps", "auxww"], timeout=10)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if "hermes_cli.main" not in line or "gateway" not in line or "run" not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if _pid_alive(pid):
            return pid
    return None


def disk_check(issues: list[dict], fixes: list[str], summary: dict) -> None:
    usage = shutil.disk_usage(HOME)
    pct = round(usage.used / usage.total * 100, 1)
    summary["disk"] = f"{pct}% ({usage.used // (1024 ** 3)}Gi/{usage.total // (1024 ** 3)}Gi)"

    if pct > 80:
        cache = HERMES_HOME / "cache"
        removed = 0
        if cache.exists():
            for child in cache.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    removed += 1
                except OSError:
                    pass
        fixes.append(f"清理 Hermes cache 条目 {removed} 个")
        issues.append({"code": "disk_high", "severity": "warn", "text": f"磁盘使用率 {pct}% 超过 80%"})

    if pct > 90:
        archived = 0
        for log in (HERMES_HOME / "logs").glob("*.log"):
            try:
                if log.stat().st_size < 10 * 1024 * 1024:
                    continue
                gz_path = log.with_suffix(log.suffix + ".gz")
                with log.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                log.write_text("", encoding="utf-8")
                archived += 1
            except OSError:
                pass
        fixes.append(f"压缩归档大日志 {archived} 个")


def memory_check(summary: dict) -> None:
    r = run(["vm_stat"], timeout=10)
    page_size = 16384
    free_pages = inactive_pages = speculative_pages = 0
    for line in r.stdout.splitlines():
        if "page size of" in line:
            m = re.search(r"page size of (\d+) bytes", line)
            if m:
                page_size = int(m.group(1))
        value_match = re.search(r"(\d+)\.", line)
        value = int(value_match.group(1)) if value_match else 0
        if line.startswith("Pages free:"):
            free_pages = value
        elif line.startswith("Pages inactive:"):
            inactive_pages = value
        elif line.startswith("Pages speculative:"):
            speculative_pages = value
    reclaim_gib = (free_pages + inactive_pages + speculative_pages) * page_size / (1024 ** 3)
    summary["memory"] = f"{reclaim_gib:.1f}Gi reclaimable"


def cpu_check(summary: dict) -> None:
    try:
        load1, load5, load15 = os.getloadavg()
        summary["cpu"] = f"load {load1:.2f}/{load5:.2f}/{load15:.2f}"
    except OSError:
        summary["cpu"] = "unknown"


def gateway_check(issues: list[dict], fixes: list[str], summary: dict) -> None:
    pid = _launchd_gateway_pid() or _process_table_gateway_pid()
    if pid:
        summary["gateway"] = f"running (pid {pid})"
        return

    r = run([HERMES_BIN, "gateway", "status"], timeout=30)
    text = (r.stdout + r.stderr).strip()
    running = r.returncode == 0 and (
        "Gateway is running" in text
        or "running" in text.lower()
        or re.search(r"Gateway is supervised by launchd\s+\(PID\s+\d+\)", text) is not None
        or re.search(r'"PID"\s*=\s*\d+', text) is not None
        or re.search(r"\bPID:\s*\d+", text) is not None
    )
    summary["gateway"] = "running" if running else "not running"
    if running:
        return
    issues.append({"code": "gateway_down", "severity": "critical", "text": "Hermes gateway 未运行"})
    restart = run([HERMES_BIN, "gateway", "restart"], timeout=60)
    if restart.returncode == 0:
        fixes.append("已尝试重启 Hermes gateway")
    else:
        fixes.append("尝试重启 Hermes gateway 失败")


def cron_check(issues: list[dict], summary: dict) -> None:
    try:
        jobs = json.loads(CRON_JOBS.read_text(encoding="utf-8")).get("jobs", [])
    except Exception:
        jobs = []
    active = [j for j in jobs if j.get("enabled") and j.get("state") == "scheduled"]
    summary["cron"] = f"{len(active)} active jobs"
    for job in active:
        if job.get("last_delivery_error"):
            issues.append({
                "code": f"delivery_{job.get('id')}",
                "severity": "warn",
                "text": f"{job.get('name', job.get('id'))} 最近投递失败：{job.get('last_delivery_error')}",
            })


def memory_files_check(issues: list[dict], summary: dict) -> None:
    parts = []
    for name, limit in MAX_MEMORY_CHARS.items():
        path = HERMES_HOME / "memories" / name
        chars = len(safe_read(path))
        parts.append(f"{name} {chars}/{limit}")
        if chars > limit * 0.85:
            issues.append({
                "code": f"memory_near_limit_{name}",
                "severity": "warn",
                "text": f"{name} 接近上限：{chars}/{limit} chars",
            })
    summary["memory_files"] = " · ".join(parts)


def prefill_check(issues: list[dict], fixes: list[str], summary: dict) -> None:
    expected = str(PREFILL_PATH)
    text = safe_read(CONFIG_PATH)
    ok = f"prefill_messages_file: {expected}" in text and PREFILL_PATH.exists()
    summary["prefill"] = "ok" if ok else "needs repair"
    if ok:
        return
    issues.append({"code": "prefill_bad", "severity": "warn", "text": "prefill_messages_file 配置缺失或文件不存在"})
    if "prefill_messages_file:" in text:
        text = re.sub(r"(?m)^prefill_messages_file:.*$", f"prefill_messages_file: {expected}", text)
    else:
        text = text.rstrip() + f"\nprefill_messages_file: {expected}\n"
    try:
        CONFIG_PATH.write_text(text, encoding="utf-8")
        fixes.append("已恢复 prefill_messages_file 指向 feishu-rules.json")
    except OSError:
        fixes.append("恢复 prefill_messages_file 失败")


def feishu_direct_rules_present() -> bool:
    text = safe_read(CLASH_ACTIVE_CONFIG)
    return all(rule in text for rule in FEISHU_DIRECT_RULES)


def feishu_network_check(issues: list[dict], summary: dict) -> None:
    import time as _time
    host_results = []
    fake_ip_hosts = []
    direct_rules_ok = feishu_direct_rules_present()
    for host in FEISHU_HOSTS:
        for attempt in (1, 2):
            try:
                ip = socket.gethostbyname(host)
                host_results.append(f"{host}={ip}")
                if ip.startswith("198.18."):
                    fake_ip_hosts.append(f"{host}={ip}")
                break
            except OSError as exc:
                if attempt == 1:
                    _time.sleep(3)
                    continue
                host_results.append(f"{host}=DNS_ERROR")
                issues.append({"code": f"feishu_dns_{host}", "severity": "critical", "text": f"{host} DNS 解析失败（重试后仍失败）：{exc}"})

    ssl_ok = True
    for attempt in (1, 2):
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection(("open.feishu.cn", 443), timeout=8) as sock:
                with ctx.wrap_socket(sock, server_hostname="open.feishu.cn"):
                    pass
            break
        except OSError as exc:
            if attempt == 1:
                _time.sleep(3)
                continue
            ssl_ok = False
            issues.append({"code": "feishu_ssl", "severity": "warn", "text": f"open.feishu.cn SSL 探测失败（重试后仍失败）：{exc}"})

    if fake_ip_hosts and not direct_rules_ok:
        issues.append({
            "code": "feishu_fake_ip_without_direct",
            "severity": "warn",
            "text": "Feishu 域名仍解析到 Clash fake-IP，且未检测到完整 DIRECT 规则",
        })
    elif fake_ip_hosts and not ssl_ok:
        issues.append({
            "code": "feishu_fake_ip_ssl_failed",
            "severity": "warn",
            "text": "Feishu 域名解析到 Clash fake-IP，且 SSL 探测失败",
        })

    summary["feishu_dns"] = " · ".join(host_results)
    summary["feishu_direct_rules"] = "present" if direct_rules_ok else "missing"


def errors_scan(issues: list[dict], summary: dict, previous: dict) -> None:
    try:
        current_size = ERRORS_LOG.stat().st_size
    except OSError:
        current_size = 0
    previous_size = int(previous.get("errors_log_size") or 0)
    if current_size and previous_size and current_size >= previous_size:
        try:
            with ERRORS_LOG.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(previous_size)
                text = handle.read()
        except OSError:
            text = ""
    else:
        text = safe_read(ERRORS_LOG, limit=120_000)

    # Rolling count for feishu_ssl_eof (auto-reconnection is normal)
    FEISHU_EOF_THRESHOLD = 5  # only alert if >5 times per hour
    FEISHU_EOF_WINDOW = 3600  # seconds
    now = dt.datetime.now()
    prev_summary = previous.get("summary", {})
    eof_accumulated = int(prev_summary.get("feishu_ssl_eof_accumulated") or 0)
    eof_window_start = prev_summary.get("feishu_ssl_eof_window_start", "")

    # Detect whether this is an incremental read (normal) or full read (state reset)
    is_incremental = bool(previous_size) and current_size >= previous_size

    # Count NEW eof occurrences in this scan window
    new_eof_count = len(re.findall(r"UNEXPECTED_EOF|SSL EOF", text, flags=re.IGNORECASE))
    if new_eof_count > 0:
        if is_incremental:
            # Normal incremental scan: accumulate
            if eof_window_start:
                try:
                    window_start_dt = dt.datetime.fromisoformat(eof_window_start)
                    elapsed = (now - window_start_dt).total_seconds()
                except (ValueError, TypeError):
                    elapsed = FEISHU_EOF_WINDOW + 1
            else:
                elapsed = FEISHU_EOF_WINDOW + 1

            if elapsed > FEISHU_EOF_WINDOW:
                eof_accumulated = new_eof_count
                eof_window_start = now.isoformat()
            else:
                eof_accumulated += new_eof_count
        else:
            # Full read (state reset / log rotated): don't count historical entries
            eof_accumulated = 0

    patterns = [
        ("feishu_99992402", r"99992402|field validation failed", "Feishu 字段校验失败仍在日志中出现"),
        ("feishu_proxy_refused", r"connect: connection refused|ProxyError", "代理连接拒绝导致 Feishu/网络请求失败"),
        ("feishu_ssl_eof", r"UNEXPECTED_EOF|SSL EOF", "Feishu WebSocket/SSL 连接异常断开"),
        ("prefill_error", r"prefill messages file|Expecting value", "prefill 配置或 JSON 读取异常"),
        ("injection_scanner", r"injection scanner|threat pattern", "安全扫描拦截记录"),
    ]
    matched = []
    for code, pattern, text_label in patterns:
        if code == "feishu_ssl_eof":
            # Only flag ssl_eof if it's unusually frequent
            if eof_accumulated > FEISHU_EOF_THRESHOLD:
                matched.append(code)
                issues.append({
                    "code": code,
                    "severity": "warn",
                    "text": f"Feishu WebSocket/SSL 异常断开 (1小时内{eof_accumulated}次)",
                })
            continue
        if re.search(pattern, text, flags=re.IGNORECASE):
            matched.append(code)
            issues.append({"code": code, "severity": "warn", "text": text_label})
    summary["errors_log"] = " · ".join(matched) if matched else "clean"
    summary["errors_log_size"] = current_size
    summary["feishu_ssl_eof_accumulated"] = eof_accumulated
    summary["feishu_ssl_eof_window_start"] = eof_window_start


def issue_fingerprint(issues: list[dict]) -> str:
    keys = sorted({item["code"] for item in issues})
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def render_report(issues: list[dict], fixes: list[str], summary: dict) -> str:
    highest = "critical" if any(i["severity"] == "critical" for i in issues) else "warn"
    marker_title = "🔴" if highest == "critical" else "🟡"
    s = summary
    dns_parts = s.get('feishu_dns', '?')
    dns_ok = all("DNS_ERROR" not in part and not part.startswith("198.18.") for part in dns_parts.split(" · "))
    dns_status = "✅ 正常" if dns_ok and dns_parts != "?" else f"{dns_parts}"
    lines = [
        f"{marker_title} Hermes IT 健康检查 · {now_label()}",
        "",
        "**🖥️ 系统**",
        f"磁盘 {s.get('disk', '?')} · 内存 {s.get('memory', '?')} · CPU {s.get('cpu', '?')}",
        "",
        "**⚙️ 服务**",
        f"Gateway {s.get('gateway', '?')} · Cron {s.get('cron', '?')} · 日志 {s.get('errors_log', '?')}",
        "",
        "**📦 存储**",
        f"{s.get('memory_files', '?')}",
        "",
        "**🌐 网络**",
        f"Feishu DNS {dns_status} · DIRECT {s.get('feishu_direct_rules', '?')}",
        "",
        "**发现的问题**",
        "",
    ]
    for issue in issues:
        marker = "🔴" if issue["severity"] == "critical" else "🟡"
        lines.append(f"- {marker} {issue['text']}")
    if fixes:
        lines.extend(["", "**已执行的自动修复**", ""])
        for fix in fixes:
            lines.append(f"- {fix}")
    lines.extend([
        "",
        "**说明**",
        "",
        "- 高频 IT 检查已脚本化，正常无新增异常时会静默。",
        "- 报告已避免表格和水平分隔线，适配飞书投递。",
    ])
    return feishu_safe("\n".join(lines))


def main() -> int:
    issues: list[dict] = []
    fixes: list[str] = []
    summary: dict = {}
    previous = read_state()

    disk_check(issues, fixes, summary)
    memory_check(summary)
    cpu_check(summary)
    gateway_check(issues, fixes, summary)
    cron_check(issues, summary)
    memory_files_check(issues, summary)
    prefill_check(issues, fixes, summary)
    feishu_network_check(issues, summary)
    errors_scan(issues, summary, previous)

    fingerprint = issue_fingerprint(issues)
    write_state({
        "last_run_at": dt.datetime.now().isoformat(),
        "last_fingerprint": fingerprint,
        "issue_codes": sorted({item["code"] for item in issues}),
        "errors_log_size": summary.get("errors_log_size", 0),
        "summary": summary,
    })

    def compact_summary(s: dict) -> str:
        dns_parts = s.get('feishu_dns', '?')
        dns_ok = all("DNS_ERROR" not in part and not part.startswith("198.18.") for part in dns_parts.split(" · "))
        dns_status = "✅ 正常" if dns_ok and dns_parts != "?" else f"{dns_parts}"
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"运行时间：{now_str}\n\n"
            f"**🖥️ 系统**\n"
            f"磁盘 {s.get('disk', '?')} · 内存 {s.get('memory', '?')} · CPU {s.get('cpu', '?')}\n"
            f"\n"
            f"**⚙️ 服务**\n"
            f"Gateway {s.get('gateway', '?')} · Cron {s.get('cron', '?')} · 日志 {s.get('errors_log', '?')}\n"
            f"\n"
            f"**📦 存储**\n"
            f"{s.get('memory_files', '?')}\n"
            f"\n"
            f"**🌐 网络**\n"
            f"Feishu DNS {dns_status} · DIRECT {s.get('feishu_direct_rules', '?')}"
        )

    if not issues:
        print(compact_summary(summary))
        return 0
    if previous.get("last_fingerprint") == fingerprint and not fixes:
        print(compact_summary(summary))
        return 0
    print(render_report(issues, fixes, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
