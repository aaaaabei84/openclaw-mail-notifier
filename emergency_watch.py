#!/usr/bin/env python3
"""紧急发件人独立监控：增量检测并登记紧急推送任务。"""
import email
from email.utils import parseaddr
import subprocess
import sys

import config
from mail_filter import ACCOUNTS, BASE, STATE_DIR, connect, decode_header_val
from mail_utils import (
    AlreadyRunning,
    atomic_write_json,
    exclusive_lock,
    imap_payload,
    imap_uids,
    load_json,
    safe_logout,
)

EMERGENCY_SENDER = config.EMERGENCY_SENDER.strip().lower()
WATCH_STATE = STATE_DIR / "emergency_watch_state.json"
LOCK_FILE = STATE_DIR / "emergency_watch.lock"


def _main_locked() -> None:
    if not EMERGENCY_SENDER:
        return

    state = load_json(WATCH_STATE, {})
    last_uid = int(state.get("last_uid", 0))
    client = None
    try:
        client = connect(ACCOUNTS["qq"])
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP SELECT INBOX 失败：status={status!r}")

        if not state.get("initialized"):
            status, data = client.uid("search", None, "ALL")
            uids = imap_uids(status, data, "SEARCH")
            atomic_write_json(
                WATCH_STATE,
                {"last_uid": max(uids) if uids else 0, "initialized": True},
            )
            return

        status, data = client.uid(
            "search", None, f"UID {last_uid + 1}:*" if last_uid > 0 else "ALL"
        )
        new_uids = [uid for uid in imap_uids(status, data, "SEARCH") if uid > last_uid]
        last_processed_uid = last_uid

        for uid in new_uids:
            status, data = client.uid(
                "fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            raw = imap_payload(status, data)
            if raw is None:
                print(f"[emergency-watch] UID {uid} 拉取失败，留待下次重试", file=sys.stderr)
                break

            msg = email.message_from_bytes(raw)
            subject = decode_header_val(msg.get("Subject"))
            from_addr = decode_header_val(msg.get("From"))
            date = msg.get("Date", "")
            sender = parseaddr(from_addr)[1].strip().lower()
            if sender == EMERGENCY_SENDER:
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(BASE / "tools" / "emergency_push.py"),
                            "--init",
                            str(uid),
                            subject,
                            from_addr,
                            date,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    print(f"[emergency-watch] 登记 uid={uid} 失败：{exc}", file=sys.stderr)
                    break
                if result.returncode != 0:
                    print(
                        f"[emergency-watch] 登记 uid={uid} 失败 rc={result.returncode}: "
                        f"{(result.stderr or result.stdout)[:160]}",
                        file=sys.stderr,
                    )
                    break
                print(f"[emergency-watch] 已登记紧急推送: uid={uid} 来自 {sender}")
            last_processed_uid = uid

        if last_processed_uid > last_uid:
            state["last_uid"] = last_processed_uid
            state["initialized"] = True
            atomic_write_json(WATCH_STATE, state)
    finally:
        safe_logout(client)


def main() -> None:
    try:
        with exclusive_lock(LOCK_FILE):
            _main_locked()
    except AlreadyRunning as exc:
        print(f"[emergency-watch] {exc}，本次跳过", file=sys.stderr)


if __name__ == "__main__":
    main()
