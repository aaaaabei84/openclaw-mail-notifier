#!/usr/bin/env python3
"""邮件过滤脚本：增量检查新邮件，纯规则过滤（不用 AI），不落盘。

用法：
  python3 tools/mail_filter.py          # 检查新邮件，输出重要邮件（无重要则无输出）
  python3 tools/mail_filter.py --list   # 列出本次新邮件全部主题（调试用）

与 mail_sync.py 的区别：不落盘、不拉正文、不调 AI，只按规则判断。
"""
import email
import email.header
from email.utils import parseaddr
import imaplib
import random
import socket
import ssl
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

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
    },
}

BASE = config.BASE
STATE_DIR = config.STATE_DIR
STATE_FILE = STATE_DIR / "filter_state.json"  # 独立断点，与 mail_sync 互不影响
LOCK_FILE = STATE_DIR / "mail_filter.lock"
MAX_FEISHU_RETRIES_PER_RUN = 10

# ── 硬跳过域名（营销/社交/游戏/无关，一律不检查）──
HARD_SKIP_DOMAINS = (
    # 游戏平台
    'steampowered.com', 'steamchina.com', 'ubisoft.com', 'rockstargames.com',
    'epicgames.com', 'b5csgo.com.cn', 'gamezys.cn', 'flingtrainer',
    # 社交
    'instagram.com', 'facebookmail.com', 'facebook.com', 'x.com', 'twitter.com',
    # 营销/简报/广告
    'insideapple.apple.com', 'microsoft.start', 'email2.microsoft.com',
    'microsoftrewards', 'onlyfans.com',
    # 测试工具营销（Perforce/BlazeMeter 全是广告）
    'perforce.com', 'blazemeter.com',
    # 求职平台营销
    'lanqiao.cn', 'shixiseng.com',
    # 视频/其他
    'netflix.com', 'fiawec.com', 'sportall.fr', 'uooconline.com',
)

# ── 主题硬跳过（营销词，命中即不检查）──
HARD_SKIP_SUBJECT = (
    '广告', '促销', '优惠', '抽奖', '折扣', 'deal', 'sale', 'promo',
    'newsletter', '简报', '日报', '推荐', '你可能喜欢', '精彩时刻',
    '错过的', '粉丝专页', '生日快乐', '开始使用', 'get started', 'welcome',
    '欢迎', '每日', '订阅', '礼包', '免费', 'ebook', 'webinar', 'guide',
    'report', '提升', '护航', '春招', 'offer来了', '领取',
)

# ── 重要触发词：命中任一即输出 ──
IMPORTANT_SUBJECT = (
    # 账号安全（必须）
    'sign in', 'sign-in', '新设备登录', '登录提醒', '密码更改', '密码重置',
    '密码修改', '重置密码', '安全提醒', 'security alert', '登录验证',
    'verify your device', 'review this sign', '连接到 microsoft 帐户的新应用',
    '账号被封锁', '功能被', 'blocked', 'suspended',
    # 财务
    '扣费', '账单', '退款', '发票', 'payment', 'charge', 'invoice',
    'billing', 'receipt', '续费', '到期', 'added a card', '付款',
    # 工作/求职（关键节点，非营销）
    '聘用', '入职', 'offer', '面试邀请', '面试通知', '录用',
    # 学业关键
    '考试成绩', '选课', '毕业', '学籍', '学分', '四六级', '期末考试',
    # 工作系统通知（TAPD 等）
    '邀请您加入', 'tapd', '工单', '变更', '权限',
)

# 安全词必须先于硬跳过规则判断，避免 x.com 等平台的安全告警被营销规则吞掉。
SECURITY_SUBJECT = (
    'sign in', 'sign-in', '新设备登录', '登录提醒', '密码更改', '密码重置',
    '密码修改', '重置密码', '安全提醒', 'security alert', '登录验证',
    'verify your device', 'review this sign', '连接到 microsoft 帐户的新应用',
    '账号被封锁', '功能被', 'blocked', 'suspended',
)

# ── 纯验证码/确认类：默认不报（用户自己触发注册/登录时才需要）──
CODE_PATTERNS = (
    '验证码', '确认码', 'confirm your email', 'confirm your e-mail',
    '验证你的', '验证您的', 'activate', '激活', 'verify your email',
    'verification code', 'confirmation code', '激活邮件', '注册',
)




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


# ── TUN 代理绕过（毛毛雨/猫猫云 fake-ip 劫持 IMAP TLS 握手）──
# 原理: 用 IP_BOUND_IF 把 socket 绑到物理网卡直连，DNS 也用物理网卡发查询拿真实 IP，
#       绕开 TUN 的 fake-ip 和代理规则。梯子开着时邮件也能直连。
# 坑: macOS 的 IP_BOUND_IF(=25) 参数是 ifindex(int)，不是接口名字符串。
IP_BOUND_IF = 25
_DNS_SERVERS = ("223.5.5.5", "114.114.114.114")


def _phys_ifname():
    """返回有 IPv4 地址的物理网卡名（优先 en*），无则 None。"""
    try:
        result = subprocess.run(
            ["ifconfig", "-l"], capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for name in result.stdout.split():
        if not name.startswith("en"):
            continue
        try:
            address = subprocess.run(
                ["ipconfig", "getifaddr", name],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if address.returncode == 0 and address.stdout.strip():
            return name
    return None


def _skip_dns_name(payload: bytes, offset: int) -> int:
    while True:
        if offset >= len(payload):
            raise ValueError("DNS name 越界")
        length = payload[offset]
        if length & 0xC0 == 0xC0:
            if offset + 2 > len(payload):
                raise ValueError("DNS 压缩指针越界")
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        if length > 63 or offset + length > len(payload):
            raise ValueError("DNS label 非法")
        offset += length


def _parse_dns_a(resp, tid):
    """解析 DNS 响应中的 A 记录；格式异常时返回空列表。"""
    try:
        if len(resp) < 12 or resp[:2] != struct.pack(">H", tid):
            return []
        flags, questions, answers = struct.unpack(">HHH", resp[2:8])
        if flags & 0x000F or not (flags & 0x8000):
            return []
        offset = 12
        for _ in range(questions):
            offset = _skip_dns_name(resp, offset)
            if offset + 4 > len(resp):
                return []
            offset += 4
        ips = []
        for _ in range(answers):
            offset = _skip_dns_name(resp, offset)
            if offset + 10 > len(resp):
                return []
            rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", resp[offset:offset + 10])
            offset += 10
            if offset + rdlen > len(resp):
                return []
            if rtype == 1 and rclass == 1 and rdlen == 4:
                ips.append(".".join(str(byte) for byte in resp[offset:offset + 4]))
            offset += rdlen
        return ips
    except (ValueError, struct.error):
        return []


def _dns_a(domain, ifname, timeout=4):
    """绑物理网卡向公共 DNS 查 A 记录，绕开 fake-ip。返回真实 IP 列表"""
    tid = random.randint(0, 65535)
    hdr = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\x00"
    q += struct.pack(">HH", 1, 1)
    try:
        idx = socket.if_nametoindex(ifname)
    except OSError:
        return []
    for server in _DNS_SERVERS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, struct.pack("I", idx))
                u.settimeout(timeout)
                u.sendto(hdr + q, (server, 53))
                resp, _ = u.recvfrom(4096)
            ips = _parse_dns_a(resp, tid)
            if ips:
                return ips
        except OSError:
            continue
    return []


class _DirectIMAP4_SSL(imaplib.IMAP4_SSL):
    """覆盖 _create_socket: 绑物理网卡 + 真实 IP 直连，绕开 TUN 代理"""

    def _create_socket(self, timeout):
        ifname = _phys_ifname()
        if ifname:
            try:
                idx = socket.if_nametoindex(ifname)
                ips = _dns_a(self.host, ifname)
                last_err = None
                for ip in ips:
                    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    raw.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF,
                                   struct.pack("I", idx))
                    raw.settimeout(timeout)
                    try:
                        raw.connect((ip, self.port))
                        return self.ssl_context.wrap_socket(
                            raw, server_hostname=self.host)
                    except OSError as e:
                        last_err = e
                        raw.close()
                if last_err:
                    raise last_err
            except OSError:
                pass  # 直连失败则回退普通路径
        return super()._create_socket(timeout)


def connect(acct):
    config.require_mail_credentials()
    ctx = ssl.create_default_context()
    M = _DirectIMAP4_SSL(acct["host"], acct["port"], ssl_context=ctx, timeout=15)
    M.login(acct["user"], acct["pass"])
    return M



def _matches_sender_domain(from_addr: str, patterns: tuple[str, ...]) -> bool:
    address = parseaddr(from_addr or "")[1].lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    for pattern in patterns:
        pattern = pattern.lower()
        if "." in pattern and (domain == pattern or domain.endswith("." + pattern)):
            return True
        if "." not in pattern and pattern in domain:
            return True
    return False

def classify(subject: str, from_addr: str):
    """返回 'skip' | 'important' | 'normal'。normal = 不跳过但也不重要（不输出）"""
    sub = (subject or "").lower()

    # 1. 安全告警优先，不能被社交/营销域名规则吞掉。
    if any(k in sub for k in SECURITY_SUBJECT):
        return "important"

    # 2. 硬跳过：域名或主题命中 → 不看
    if _matches_sender_domain(from_addr, HARD_SKIP_DOMAINS):
        return "skip"
    if any(k in sub for k in HARD_SKIP_SUBJECT):
        return "skip"

    # 3. 其他重要主题
    if any(k in sub for k in IMPORTANT_SUBJECT):
        return "important"

    # 4. 纯验证码/确认/激活类 → 不报
    if any(p in sub for p in CODE_PATTERNS):
        return "normal"

    # 5. 兜底：不跳过也没命中 → 不报
    return "normal"


def _push_feishu_pending(state: dict) -> None:
    """发送并清理待投递飞书消息；失败项保留到下次运行。"""
    pendings = list(state.get("pending_feishu", []))
    if not pendings or not config.FEISHU_TARGET:
        return
    remaining = []
    helper = str(Path(__file__).parent / "feishu_notifier.py")
    attempted = pendings[:MAX_FEISHU_RETRIES_PER_RUN]
    remaining.extend(pendings[MAX_FEISHU_RETRIES_PER_RUN:])
    for item in attempted:
        try:
            result = subprocess.run(
                [sys.executable, helper, item["message"]],
                capture_output=True,
                text=True,
                timeout=40,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[mail_filter] 飞书推送异常，保留待重试: {exc}", file=sys.stderr)
            remaining.append(item)
            continue
        if result.returncode != 0:
            print(
                f"[mail_filter] 飞书推送失败 rc={result.returncode}，保留待重试: "
                f"{(result.stderr or result.stdout)[:160]}",
                file=sys.stderr,
            )
            remaining.append(item)
    state["pending_feishu"] = remaining


def _main_locked(daily_mode: bool, show_list: bool) -> None:
    acct = ACCOUNTS["qq"]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_json(STATE_FILE, {})
    last_uid = int(state.get("last_uid", 0))

    today = datetime.now().strftime("%Y-%m-%d")
    daily = state.get("daily", {})
    if daily.get("date") != today:
        daily = {"date": today, "new_count": 0, "important_pushed": False}

    client = None
    important = []
    processed_count = 0
    last_processed_uid = last_uid
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
        if show_list:
            print(
                f"[filter] 搜索结果 {len(uids)}，新邮件 {len(new_uids)} "
                f"(检查点 UID {last_uid}；--list 不推进断点)",
                file=sys.stderr,
            )

        for uid in new_uids:
            status, data = client.uid(
                "fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            raw = imap_payload(status, data)
            if raw is None:
                print(f"[filter] UID {uid} 头部拉取失败，停止推进断点，留待下次重试", file=sys.stderr)
                break
            msg = email.message_from_bytes(raw)
            subject = decode_header_val(msg.get("Subject"))
            from_addr = decode_header_val(msg.get("From"))
            date = msg.get("Date", "")
            classification = classify(subject, from_addr)
            if show_list:
                print(f"{uid} | [{classification}] {from_addr[:40]} | {subject[:60]}")
            if classification == "important":
                important.append({
                    "uid": uid,
                    "subject": subject,
                    "from": from_addr,
                    "date": date,
                })
            processed_count += 1
            last_processed_uid = uid
    finally:
        safe_logout(client)

    # --list 是只读诊断，不能吞掉正常任务尚未处理的邮件。
    if show_list:
        return

    daily["new_count"] = int(daily.get("new_count", 0)) + processed_count
    if important:
        daily["important_pushed"] = True
    state["daily"] = daily
    if last_processed_uid > last_uid:
        state["last_uid"] = last_processed_uid
        state["synced_at"] = datetime.now().isoformat()

    pending = state.setdefault("pending_feishu", [])
    known = {str(item.get("uid")) for item in pending}
    for item in important:
        if str(item["uid"]) not in known and config.FEISHU_TARGET:
            pending.append({
                "uid": item["uid"],
                "message": f"📧 {item['subject']}\n发件人：{item['from']}\n时间：{item['date'][:25]}",
                "created_at": datetime.now().isoformat(),
            })
    # 防止长期配置错误导致状态文件无限增长；保留最近 200 条。
    state["pending_feishu"] = pending[-200:]
    atomic_write_json(STATE_FILE, state)

    # stdout 由 cron announce 走微信；只输出本轮新发现，避免飞书重试导致微信重复。
    for item in important:
        print(f"📧 {item['subject']}")
        print(f"发件人：{item['from']}")
        print(f"时间：{item['date'][:25]}")
        print()

    _push_feishu_pending(state)
    atomic_write_json(STATE_FILE, state)

    if daily_mode:
        if daily["new_count"] == 0:
            print("📮 今日邮件汇总：无新邮件")
        elif daily["important_pushed"]:
            print(f"📮 今日邮件汇总：新增 {daily['new_count']} 封（重要邮件已单独推送）")
        else:
            print(f"📮 今日邮件汇总：新增 {daily['new_count']} 封，无重要邮件")


def main():
    daily_mode = "--daily" in sys.argv
    show_list = "--list" in sys.argv
    try:
        with exclusive_lock(LOCK_FILE):
            _main_locked(daily_mode, show_list)
    except AlreadyRunning as exc:
        print(f"[mail_filter] {exc}，本次跳过", file=sys.stderr)


if __name__ == "__main__":
    main()
