#!/usr/bin/env python3
"""每日汇总投递守卫：记录失败 run，并通过飞书补发。"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys

import config
from mail_utils import AlreadyRunning, atomic_write_json, exclusive_lock, load_json

STATE_DIR = config.STATE_DIR
STATE_FILE = STATE_DIR / "daily_report_guard.json"
LOG_FILE = STATE_DIR / "daily_report_guard.log"
LOCK_FILE = STATE_DIR / "daily_report_guard.lock"
CLI = config.OPENCLAW_CLI
SUMMARY_JOB_ID = config.SUMMARY_JOB_ID
FEISHU_HELPER = str(config.BASE / "tools" / "feishu_notifier.py")
MAX_DAILY_ATTEMPTS = 3
DRY = "--check" in sys.argv
CN = dt.timezone(dt.timedelta(hours=8))


def log(message: str) -> None:
    line = f"{dt.datetime.now(CN).isoformat(timespec='seconds')} {message}"
    if not DRY:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    print(line, file=sys.stderr)


def load_state() -> dict:
    state = load_json(STATE_FILE, {"last_checked_ts": 0, "pendings": []})
    if "pending" in state:
        state["pendings"] = [state["pending"]] if state["pending"] is not None else []
        del state["pending"]
    state.setdefault("pendings", [])
    state.setdefault("last_checked_ts", 0)
    return state


def save_state(state: dict) -> None:
    if not DRY:
        atomic_write_json(STATE_FILE, state)


def run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"命令执行异常：{exc}")
        return None


def fetch_latest_run() -> dict | None:
    if not SUMMARY_JOB_ID:
        log("SUMMARY_JOB_ID 未配置，跳过查询")
        return None
    result = run_command([CLI, "cron", "runs", "--id", SUMMARY_JOB_ID], timeout=60)
    if result is None:
        return None
    if result.returncode != 0:
        log(f"cron runs 查询失败 rc={result.returncode} stderr={result.stderr[:200]}")
        return None
    try:
        payload = json.loads(result.stdout)
        entries = payload.get("entries", [])
        valid = [item for item in entries if isinstance(item, dict) and "ts" in item]
        return max(valid, key=lambda item: item["ts"]) if valid else None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        log(f"cron runs 解析失败: {exc}")
        return None


def send_feishu(text: str) -> bool:
    result = run_command([sys.executable, FEISHU_HELPER, text], timeout=40)
    if result is not None and result.returncode == 0:
        log("飞书补发成功（逃生通道）")
        return True
    detail = "未启动" if result is None else f"rc={result.returncode}"
    log(f"飞书补发失败 {detail}")
    return False


def send_notification(text: str) -> bool:
    """仅飞书通道推送（微信通道已移除）。"""
    if DRY:
        print(f"[dry] 将发送: {text.splitlines()[0]}")
        return True
    return send_feishu(text)


def build_text(pending: dict) -> str:
    summary = pending.get("summary") or "（原汇总内容为空）"
    return f"⚠️ 补发邮件汇总（{pending['date']}，原通知未送达）\n{summary}"


def attempt_oldest_pending(state: dict, today: str) -> None:
    pendings = state["pendings"]
    if not pendings:
        return
    pending = pendings[0]
    if pending.get("last_attempt_date") != today:
        pending["attempts_today"] = 0
    attempts_today = int(pending.get("attempts_today", 0))
    if attempts_today >= MAX_DAILY_ATTEMPTS:
        log(f"pending {pending['date']} 今日已试 {attempts_today} 次，跳过（明日继续）")
        return
    if send_notification(build_text(pending)):
        log(f"pending {pending['date']} 补发成功，清除")
        pendings.pop(0)
        return
    pending["attempts_total"] = int(pending.get("attempts_total", 0)) + 1
    pending["attempts_today"] = attempts_today + 1
    pending["last_attempt"] = dt.datetime.now(CN).isoformat(timespec="seconds")
    pending["last_attempt_date"] = today
    log(f"pending {pending['date']} 补发失败（累计 {pending['attempts_total']} 次），保留")


def _main_locked() -> None:
    state = load_state()
    today = dt.datetime.now(CN).strftime("%Y-%m-%d")

    attempt_oldest_pending(state, today)
    latest = fetch_latest_run()
    if latest is None:
        save_state(state)
        return

    timestamp = latest["ts"]
    if timestamp == state.get("last_checked_ts"):
        save_state(state)
        return
    state["last_checked_ts"] = timestamp

    if latest.get("delivered"):
        log(f"最新汇总 run {timestamp} 投递正常")
        save_state(state)
        return

    pendings = state["pendings"]
    if any(item.get("run_ts") == timestamp for item in pendings):
        log(f"run {timestamp} 已在 pendings 中，跳过")
        save_state(state)
        return

    while len(pendings) >= 7:
        removed = pendings.pop(0)
        log(f"pendings 队列已满，丢弃最早项 {removed.get('date', 'unknown')}")

    pending = {
        "run_ts": timestamp,
        "date": dt.datetime.fromtimestamp(timestamp / 1000, CN).strftime("%Y-%m-%d"),
        "summary": latest.get("summary") or "",
        "attempts_total": 0,
        "attempts_today": 0,
        "last_attempt": None,
        "last_attempt_date": None,
    }
    pendings.append(pending)
    log(f"检测到汇总投递失败（{pending['date']}），已落盘待补报: {pending['summary'][:40]}")
    save_state(state)

    # 若前序队列已清空，新失败立即补发；否则留给后续轮次按 FIFO 处理。
    if pendings and pendings[0] is pending:
        attempt_oldest_pending(state, today)
    else:
        log(f"pendings 队列中有 {len(pendings)} 项，等待前序清理")
    save_state(state)


def main() -> None:
    try:
        with exclusive_lock(LOCK_FILE):
            _main_locked()
    except AlreadyRunning as exc:
        print(f"[daily-report-guard] {exc}，本次跳过", file=sys.stderr)


if __name__ == "__main__":
    main()
