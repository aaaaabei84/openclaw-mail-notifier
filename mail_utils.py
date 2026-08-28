"""邮箱脚本共享的状态、锁和 IMAP 响应辅助函数。"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class AlreadyRunning(RuntimeError):
    """同一任务已有实例持有锁。"""


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """持有进程级排他锁，防止 cron 重叠执行破坏状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(f"任务已在运行：{path}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_json(path: Path, default: Any) -> Any:
    """读取 JSON；损坏时明确报错，避免把断点悄悄重置为 0。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"状态文件不可读，未做任何重置：{path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    """同目录临时文件 + fsync + replace，避免进程中断留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def imap_uids(status: str, data: Any, operation: str) -> list[int]:
    """校验 IMAP SEARCH 响应并返回 UID。"""
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"IMAP {operation} 失败：status={status!r}")
    raw = data[0]
    if isinstance(raw, str):
        raw = raw.encode("ascii", errors="strict")
    try:
        return [int(item) for item in raw.split()]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"IMAP {operation} 返回了无效 UID 列表") from exc


def imap_payload(status: str, data: Any) -> bytes | None:
    """从 IMAP FETCH 的多段响应中提取首个 bytes payload。"""
    if status != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def safe_logout(client: Any) -> None:
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        pass
