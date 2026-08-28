#!/usr/bin/env python3
"""邮件同步脚本：IMAP增量拉取 -> 落盘JSON（支持可选SOCKS/HTTP代理）"""
import email
import email.header
import html
import imaplib
import re
import socket
import ssl
import sys
from datetime import datetime

import config
from mail_utils import (
    AlreadyRunning,
    atomic_write_json,
    exclusive_lock,
    imap_payload,
    imap_uids,
    load_json,
    safe_logout,
)

ACCOUNTS = {
    "qq": {
        "host": "imap.qq.com",
        "port": 993,
        "user": config.QQ_MAIL_USER,
        "pass": config.QQ_MAIL_PASS,
        "proxy": None,  # ("host", port) 走代理时填写
    },
}

BASE = config.BASE
STATE_DIR = config.STATE_DIR
MAIL_DIR = BASE / "mail"

# 过滤规则：匹配的邮件不落盘（但 UID 计入断点，避免重复拉取）
SKIP_DOMAINS = (
    # 游戏平台
    'steampowered.com', 'steamchina.com', 'b5csgo.com.cn', 'gamezys.cn',
    'flingtrainer',
    'ubisoft.com', 'rockstargames.com', 'epicgames.com',
    # 社交
    'instagram.com', 'facebookmail.com', 'facebook.com', 'x.com', 'twitter.com',
    # 营销
    'insideapple.apple.com', 'microsoft.start', 'email2.microsoft.com',
    'microsoftrewards', 'onlyfans.com',
    # 视频/其他
    'netflix.com', 'fiawec.com', 'uooconline.com',
)


def should_filter(info: dict) -> bool:
    fr = (info.get("from") or "").lower()
    sub = (info.get("subject") or "").lower()
    if any(dom in fr for dom in SKIP_DOMAINS):
        return True
    if 'steam' in sub:  # 兜底：主题含 steam
        return True
    return False


def decode_header_val(v):
    if not v:
        return ""
    parts = email.header.decode_header(v)
    out = []
    for data, charset in parts:
        if isinstance(data, bytes):
            try:
                out.append(data.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                out.append(data.decode("utf-8", errors="replace"))
        else:
            out.append(data)
    return "".join(out)


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def parse_msg(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    body_text, body_html = "", ""
    attachments = []
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" in disp or (part.get_filename() and ctype not in ("text/plain", "text/html")):
            attachments.append(part.get_filename() or "unnamed")
            continue
        if ctype == "text/plain" and not body_text:
            try:
                payload = part.get_payload(decode=True) or b""
                body_text = payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            except Exception:
                body_text = ""
        elif ctype == "text/html" and not body_html:
            try:
                payload = part.get_payload(decode=True) or b""
                body_html = payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            except Exception:
                body_html = ""

    return {
        "subject": decode_header_val(msg.get("Subject")),
        "from": decode_header_val(msg.get("From")),
        "to": decode_header_val(msg.get("To")),
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
        # AI 判断只看标题+开头，正文裁到 800 字符以内，避免浪费 token
        "text": body_text[:800],
        "html": body_html[:800],
        "attachments": attachments,
    }


def connect(acct):
    config.require_mail_credentials()
    original_socket = socket.socket
    try:
        if acct.get("proxy"):
            import socks
            socks.set_default_proxy(socks.HTTP, acct["proxy"][0], acct["proxy"][1], rdns=True)
            socket.socket = socks.socksocket
        ctx = ssl.create_default_context()
        client = imaplib.IMAP4_SSL(
            acct["host"], acct["port"], ssl_context=ctx, timeout=30
        )
        client.login(acct["user"], acct["pass"])
        return client
    finally:
        socket.socket = original_socket


def sync(acct_name, acct):
    acct_dir = MAIL_DIR / acct_name
    acct_dir.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{acct_name}.json"
    state = load_json(state_file, {})
    last_uid = int(state.get("last_uid", 0))

    client = None
    try:
        client = connect(acct)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP SELECT INBOX 失败：status={status!r}")
        status, data = client.uid(
            "search", None, f"UID {last_uid + 1}:*" if last_uid > 0 else "ALL"
        )
        uids = imap_uids(status, data, "SEARCH")
        new_uids = [uid for uid in uids if uid > last_uid]
        print(f"[{acct_name}] 搜索结果 {len(uids)}，新增 {len(new_uids)} (上次同步到 UID {last_uid})")

        fetched = 0
        skipped = 0
        new_files = []
        last_processed_uid = last_uid
        for uid in new_uids:
            status, data = client.uid("fetch", str(uid), "(RFC822)")
            raw = imap_payload(status, data)
            if raw is None:
                print(f"[{acct_name}] UID {uid} 拉取失败，停止推进断点，留待下次重试", file=sys.stderr)
                break
            info = parse_msg(raw)
            info["uid"] = uid
            if should_filter(info):
                skipped += 1
            else:
                # UID 在邮箱内唯一，固定文件名可防止异常重跑产生重复文件。
                path = acct_dir / f"{uid}.json"
                atomic_write_json(path, info)
                fetched += 1
                new_files.append(str(path))
                if fetched <= 5:
                    print(f"  新邮件: {info['subject'][:60]} | {info['from'][:40]} | {info['date'][:25]}")
            last_processed_uid = uid

        print(f"[{acct_name}] 本次新增 {fetched} 封，过滤跳过 {skipped} 封")
        if new_files:
            print("NEW_FILES:")
            for file_path in new_files:
                print(file_path)

        if last_processed_uid > last_uid:
            state["last_uid"] = last_processed_uid
            state["synced_at"] = datetime.now().isoformat()
            atomic_write_json(state_file, state)
        return fetched
    finally:
        safe_logout(client)


if __name__ == "__main__":
    total = 0
    for name, acct in ACCOUNTS.items():
        if not acct.get("user"):
            continue
        try:
            with exclusive_lock(STATE_DIR / f"mail_sync_{name}.lock"):
                total += sync(name, acct)
        except AlreadyRunning as exc:
            print(f"[{name}] {exc}，本次跳过", file=sys.stderr)
        except Exception as exc:
            print(f"[{name}] 同步失败: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"完成，共拉取 {total} 封")
