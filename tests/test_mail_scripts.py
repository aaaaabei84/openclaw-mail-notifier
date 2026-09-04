import importlib
import datetime as _dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

_tmp = tempfile.TemporaryDirectory()
base = Path(_tmp.name)
config = types.ModuleType("config")
config.BASE = base
config.STATE_DIR = base / "mail" / "state"
config.QQ_MAIL_USER = "user@qq.com"
config.QQ_MAIL_PASS = "secret"
config.OPENCLAW_CLI = "openclaw"
config.FEISHU_TARGET = ""
config.SUMMARY_JOB_ID = "job"
config.FEISHU_TIMEOUT_IS_SUCCESS = True
config.require_mail_credentials = lambda: None
sys.modules["config"] = config

import mail_utils
import mail_filter
import daily_report_guard


class FakeIMAP:
    def __init__(self, uids, headers, fail_uid=None):
        self.uids = uids
        self.headers = headers
        self.fail_uid = fail_uid
        self.logged_out = False

    def select(self, *_args, **_kwargs):
        return "OK", [b""]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [" ".join(map(str, self.uids)).encode()]
        uid = int(args[0])
        if uid == self.fail_uid:
            return "NO", [None]
        return "OK", [(b"meta", self.headers[uid])]

    def logout(self):
        self.logged_out = True


class MailUtilsTests(unittest.TestCase):
    def test_atomic_json_and_load(self):
        path = base / "state.json"
        mail_utils.atomic_write_json(path, {"x": 1})
        self.assertEqual(mail_utils.load_json(path, {}), {"x": 1})

    def test_corrupt_state_is_not_silently_reset(self):
        path = base / "bad.json"
        path.write_text("{")
        with self.assertRaises(RuntimeError):
            mail_utils.load_json(path, {})

    def test_fetch_payload_skips_non_tuple_parts(self):
        self.assertEqual(mail_utils.imap_payload("OK", [b"noise", (b"meta", b"body"), b")"]), b"body")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name)
        mail_filter.STATE_DIR = self.state_dir
        mail_filter.STATE_FILE = self.state_dir / "filter_state.json"
        mail_filter.LOCK_FILE = self.state_dir / "filter.lock"
        mail_filter.config.FEISHU_TARGET = ""

    def tearDown(self):
        self.temp.cleanup()

    def test_security_alert_beats_social_skip(self):
        self.assertEqual(mail_filter.classify("Security alert", "X <notify@x.com>"), "important")

    def test_sender_domain_uses_parsed_address_not_display_name(self):
        self.assertEqual(mail_filter.classify("hello", '"facebook.com" <friend@example.org>'), "normal")
        self.assertEqual(mail_filter.classify("hello", "notice@mail.facebook.com"), "skip")

    def test_list_mode_does_not_advance_checkpoint(self):
        raw = b"Subject: Test\r\nFrom: friend@example.org\r\nDate: now\r\n\r\n"
        client = FakeIMAP([1], {1: raw})
        with mock.patch.object(mail_filter, "connect", return_value=client):
            mail_filter._main_locked(False, True)
        self.assertFalse(mail_filter.STATE_FILE.exists())
        self.assertTrue(client.logged_out)

    def test_fetch_failure_does_not_skip_later_uid(self):
        raw = b"Subject: Test\r\nFrom: friend@example.org\r\nDate: now\r\n\r\n"
        client = FakeIMAP([1, 2, 3], {1: raw, 3: raw}, fail_uid=2)
        with mock.patch.object(mail_filter, "connect", return_value=client):
            mail_filter._main_locked(False, False)
        state = json.loads(mail_filter.STATE_FILE.read_text())
        self.assertEqual(state["last_uid"], 1)
        self.assertEqual(state["daily"]["new_count"], 1)


class _FakeDatetime(_dt.datetime):
    """可固定 now() 的假时钟，用于验证 17:00 窗口切分。"""
    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now


class DailyWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name)
        mail_filter.STATE_DIR = self.state_dir
        mail_filter.STATE_FILE = self.state_dir / "filter_state.json"
        mail_filter.LOCK_FILE = self.state_dir / "filter.lock"
        mail_filter.config.FEISHU_TARGET = ""

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, when, daily_mode=False):
        raw = b"Subject: Test\r\nFrom: friend@example.org\r\nDate: now\r\n\r\n"
        client = FakeIMAP([1, 2], {1: raw, 2: raw})
        _FakeDatetime._now = when
        with mock.patch.object(mail_filter, "datetime", _FakeDatetime), \
                mock.patch.object(mail_filter, "connect", return_value=client):
            mail_filter._main_locked(daily_mode, False)
        return json.loads(mail_filter.STATE_FILE.read_text())

    def test_morning_run_uses_today_bucket(self):
        state = self._run(_dt.datetime(2026, 9, 4, 10, 15))
        self.assertEqual(state["daily"]["date"], "2026-09-04")
        self.assertEqual(state["daily"]["new_count"], 2)

    def test_evening_run_uses_next_day_bucket(self):
        # 17:00 后处理的邮件归入次日桶，次日 17:00 汇总才会包含它们
        state = self._run(_dt.datetime(2026, 9, 4, 18, 15))
        self.assertEqual(state["daily"]["date"], "2026-09-05")
        self.assertEqual(state["daily"]["new_count"], 2)

    def test_daily_summary_uses_today_bucket(self):
        # 17:00 汇总运行本身：报告今日窗口（昨日17:00 → 今日17:00）
        state = self._run(_dt.datetime(2026, 9, 4, 17, 0), daily_mode=True)
        self.assertEqual(state["daily"]["date"], "2026-09-04")
        self.assertEqual(state["daily"]["new_count"], 2)


class GuardTests(unittest.TestCase):
    def test_latest_run_selected_by_timestamp(self):
        result = types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"entries": [{"ts": 1}, {"ts": 3}, {"ts": 2}]}),
            stderr="",
        )
        with mock.patch.object(daily_report_guard, "run_command", return_value=result):
            self.assertEqual(daily_report_guard.fetch_latest_run()["ts"], 3)


if __name__ == "__main__":
    unittest.main()
