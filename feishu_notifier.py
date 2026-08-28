#!/usr/bin/env python3
"""通过 OpenClaw 飞书通道发送消息，带有限重试。"""
import subprocess
import sys
import time

import config

CLI = config.OPENCLAW_CLI
FEISHU_TARGET = config.FEISHU_TARGET
TIMEOUT = 8
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2
# 某些 OpenClaw 版本会在消息已发出后挂住；默认保持旧行为，可在配置中关闭。
TIMEOUT_IS_SUCCESS = getattr(config, "FEISHU_TIMEOUT_IS_SUCCESS", True)


def send(message: str, dry: bool = False) -> bool:
    if not message.strip():
        print("[feishu] 拒绝发送空消息", file=sys.stderr)
        return False
    if dry:
        print(f"[feishu dry-run] 将推送: {message[:80]}")
        return True
    if not FEISHU_TARGET:
        print("[feishu] FEISHU_TARGET 未配置", file=sys.stderr)
        return False

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                [
                    CLI,
                    "message",
                    "send",
                    "--channel",
                    "feishu",
                    "--target",
                    FEISHU_TARGET,
                    "--message",
                    message,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            verdict = "按成功处理" if TIMEOUT_IS_SUCCESS else "按失败处理"
            print(f"[feishu] CLI 超时，投递结果不确定；{verdict}", file=sys.stderr)
            return TIMEOUT_IS_SUCCESS
        except OSError as exc:
            error = f"无法启动 CLI: {exc}"
        else:
            output = (result.stdout or "") + (result.stderr or "")
            ok = result.returncode == 0 and "ret=-2" not in output and "error" not in output.lower()
            if ok:
                print(f"[feishu] 推送成功（第 {attempt} 次）: {output[:80]}", file=sys.stderr)
                return True
            error = f"rc={result.returncode} out={output[:120]}"

        if attempt < RETRY_ATTEMPTS:
            print(
                f"[feishu] 推送失败 {error}，{RETRY_DELAY}s 后重试 "
                f"（第 {attempt}/{RETRY_ATTEMPTS} 次）",
                file=sys.stderr,
            )
            time.sleep(RETRY_DELAY)
        else:
            print(f"[feishu] 推送失败 {error}，已重试 {RETRY_ATTEMPTS} 次", file=sys.stderr)
    return False


def main() -> None:
    dry = "--dry" in sys.argv[1:]
    args = [arg for arg in sys.argv[1:] if arg != "--dry"]
    if not args:
        print('用法: feishu_notifier.py [--dry] "消息内容"', file=sys.stderr)
        raise SystemExit(2)
    ok = send(" ".join(args), dry=dry)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
