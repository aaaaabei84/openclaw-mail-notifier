"""配置模板：复制为 config.py；优先通过环境变量提供敏感配置。"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("MAIL_STATE_DIR", BASE / "mail" / "state"))

QQ_MAIL_USER = os.environ.get("QQ_MAIL_USER", "")
QQ_MAIL_PASS = os.environ.get("QQ_MAIL_PASS", "")


def require_mail_credentials() -> None:
    if not QQ_MAIL_USER or not QQ_MAIL_PASS:
        raise ValueError("邮件凭证未配置：请设置 QQ_MAIL_USER 和 QQ_MAIL_PASS 环境变量")


# 不使用 OPENCLAW_CLI 环境变量，避免与 OpenClaw gateway 内置变量冲突。
OPENCLAW_CLI = os.environ.get("MAIL_CLI_PATH", "openclaw")
FEISHU_TARGET = os.environ.get("FEISHU_TARGET", "")
WECHAT_TARGET = os.environ.get("WECHAT_TARGET", "")
WECHAT_ACCOUNT = os.environ.get("WECHAT_ACCOUNT", "")
SUMMARY_JOB_ID = os.environ.get("SUMMARY_JOB_ID", "")

# 某些 OpenClaw 版本会在消息已送达后仍挂住。默认保持兼容；若更重视严格确认，设为 0。
FEISHU_TIMEOUT_IS_SUCCESS = os.environ.get("FEISHU_TIMEOUT_IS_SUCCESS", "1").lower() not in {
    "0", "false", "no"
}
