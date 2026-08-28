#!/usr/bin/env python3
"""紧急邮件推送器：每个紧急事件每分钟推送一次，成功 60 轮后清除。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime

import config
from mail_utils import AlreadyRunning, atomic_write_json, exclusive_lock, load_json

BASE = config.BASE
STATE_FILE = config.STATE_DIR / "emergency_state.json"
LOCK_FILE = config.STATE_DIR / "emergency_push.lock"
CLI = config.OPENCLAW_CLI
WECHAT_TARGET = config.WECHAT_TARGET
WECHAT_ACCOUNT = config.WECHAT_ACCOUNT
FEISHU_HELPER = str(BASE / "tools" / "feishu_notifier.py")
ROUNDS = 60
PER_ROUND = 1
SEND_INTERVAL = 0.8
MAX_QUEUED_EVENTS = 100
DRY = os.environ.get("EMERGENCY_DRY") == "1"


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[emergency] 命令执行异常：{exc}", file=sys.stderr)
        return None


def send_feishu(text: str) -> bool:
    result = _run([sys.executable, FEISHU_HELPER, text], timeout=40)
    if result is not None and result.returncode == 0:
        print("[emergency] 飞书推送成功（逃生通道）", file=sys.stderr)
        return True
    detail = "未启动" if result is None else f"rc={result.returncode}"
    print(f"[emergency] 飞书推送失败 {detail}", file=sys.stderr)
    return False


def send_notification(text: str) -> bool:
    """微信成功即返回；微信不可用或失败时改走飞书。"""
    if DRY:
        print(f"[dry] {text.splitlines()[0]}")
        return True

    if WECHAT_TARGET:
        args = [
            CLI,
            "message",
            "send",
            "--channel",
            "openclaw-weixin",
            "--target",
            WECHAT_TARGET,
        ]
        if WECHAT_ACCOUNT:
            args.extend(["--account", WECHAT_ACCOUNT])
        args.extend(["--message", text])
        result = _run(args, timeout=90)
        if result is not None:
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode == 0 and "ret=-2" not in output:
                print(f"[emergency] 微信已推送: {result.stdout.strip()[:80]}", file=sys.stderr)
                return True
            print(f"[emergency] 微信推送失败 rc={result.returncode}，尝试飞书", file=sys.stderr)
    else:
        print("[emergency] WECHAT_TARGET 未配置，直接尝试飞书", file=sys.stderr)

    return send_feishu(text)


def load_events() -> list[dict]:
    raw = load_json(STATE_FILE, {})
    if not raw:
        return []
    if isinstance(raw.get("events"), list):
        return raw["events"]
    # 兼容旧版单事件状态。
    if "uid" in raw:
        return [raw]
    raise RuntimeError(f"未知的紧急状态格式：{STATE_FILE}")


def save_events(events: list[dict]) -> None:
    if events:
        atomic_write_json(STATE_FILE, {"version": 2, "events": events})
    else:
        try:
            STATE_FILE.unlink()
        except FileNotFoundError:
            pass


def init(uid: str, subject: str, from_addr: str, date: str) -> None:
    """登记事件；按 UID 去重，但不会因已有事件而丢弃新紧急邮件。"""
    events = load_events()
    if any(str(event.get("uid")) == str(uid) for event in events):
        print(f"[emergency] uid={uid} 已登记，忽略重复触发")
        return
    if len(events) >= MAX_QUEUED_EVENTS:
        raise RuntimeError(f"紧急事件队列已满（{MAX_QUEUED_EVENTS}），拒绝丢弃新事件")
    events.append(
        {
            "uid": str(uid),
            "subject": subject,
            "from": from_addr,
            "date": date,
            "round": 0,
            "attempts": 0,
            "started_at": datetime.now().isoformat(),
        }
    )
    if not DRY:
        save_events(events)
    print(f"[emergency] 紧急邮件已登记 uid={uid}，队列共 {len(events)} 条")


def run_round() -> None:
    events = load_events()
    if not events:
        return
    remaining = []
    for event in events:
        current_round = int(event.get("round", 0))
        if current_round >= ROUNDS:
            print(f"[emergency] uid={event.get('uid')} 已完成 {ROUNDS} 轮，状态清除")
            continue
        text = (
            "🚨🚨 紧急邮件！\n"
            f"发件人：{event.get('from', '')}\n"
            f"主题：{event.get('subject', '')}\n"
            f"时间：{str(event.get('date', ''))[:25]}\n"
            f"第 {current_round + 1}/{ROUNDS} 轮 · 请立即查看邮箱"
        )
        success = False
        for index in range(PER_ROUND):
            success = send_notification(text) or success
            if index + 1 < PER_ROUND:
                time.sleep(SEND_INTERVAL)
        if DRY:
            remaining.append(event)
            continue
        event["attempts"] = int(event.get("attempts", 0)) + 1
        event["last_attempt_at"] = datetime.now().isoformat()
        if success:
            event["round"] = current_round + 1
            event["last_success_at"] = event["last_attempt_at"]
            print(f"[emergency] uid={event.get('uid')} 第 {event['round']}/{ROUNDS} 轮完成")
        else:
            event["last_error_at"] = event["last_attempt_at"]
            print(
                f"[emergency] uid={event.get('uid')} 本轮双通道均失败，保留状态下次重试",
                file=sys.stderr,
            )
        if int(event.get("round", 0)) < ROUNDS:
            remaining.append(event)
    if not DRY:
        save_events(remaining)


def main() -> None:
    try:
        with exclusive_lock(LOCK_FILE):
            if "--init" in sys.argv:
                try:
                    index = sys.argv.index("--init")
                    uid, subject, from_addr, date = sys.argv[index + 1:index + 5]
                except ValueError:
                    raise SystemExit("用法: emergency_push.py --init <uid> <subject> <from> <date>")
                if len(sys.argv[index + 1:index + 5]) != 4:
                    raise SystemExit("用法: emergency_push.py --init <uid> <subject> <from> <date>")
                init(uid, subject, from_addr, date)
            else:
                run_round()
    except AlreadyRunning as exc:
        print(f"[emergency] {exc}，本次跳过", file=sys.stderr)


if __name__ == "__main__":
    main()
