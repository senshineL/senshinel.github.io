import unittest
from unittest.mock import patch
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import hainan_watch as watch


def snapshot(*fares):
    return {"flight_number": "HU7711", "departure_date": "2026-11-09",
            "origin": "PEK", "destination": "SZX",
            "fares": [{"cabin": c, "price": p, "brand": c, "title": c} for c, p in fares]}


RULES = {"rates": {"U": "0.00", "N": "0.50", "V": "0.50", "E": "1.00", "B": "0.00", "P": "0.00"}}


class MatchingTests(unittest.TestCase):
    def test_more_expensive_eligible_fare_does_not_trigger(self):
        result = watch.evaluate(snapshot(("U", "670"), ("E", "920")), RULES, "0.50")
        self.assertFalse(result["matched"])
        self.assertEqual(result["cheapest_cabins"], ["U"])

    def test_eligible_fare_tied_for_overall_minimum_triggers(self):
        result = watch.evaluate(snapshot(("U", "670"), ("N", "670.00")), RULES, "0.50")
        self.assertTrue(result["matched"])
        self.assertEqual(result["eligible_fares"][0]["cabin"], "N")

    def test_higher_economy_class_is_eligible(self):
        self.assertTrue(watch.evaluate(snapshot(("E", "920")), RULES, "0.50")["matched"])

    def test_b_and_p_are_not_eligible(self):
        for cabin in ("B", "P"):
            self.assertFalse(watch.evaluate(snapshot((cabin, "500")), RULES, "0.50")["matched"])

    def test_unknown_cheapest_class_fails_closed_even_when_tied(self):
        with self.assertRaises(watch.DataError):
            watch.evaluate(snapshot(("A", "500"), ("V", "500")), RULES, "0.50")

    def test_decimal_price_comparison(self):
        result = watch.evaluate(snapshot(("U", "1000"), ("V", "999.90")), RULES, "0.50")
        self.assertTrue(result["matched"])
        self.assertEqual(Decimal(result["lowest_price"]), Decimal("999.90"))

    def test_dedup_key_changes_when_flight_date_changes(self):
        first = snapshot(("V", "900"))
        result = watch.evaluate(first, RULES, "0.50")
        second = dict(first, departure_date="2026-11-10")
        self.assertNotEqual(watch.event_key(first, result), watch.event_key(second, result))

    def test_mismatch_between_summary_and_detail_fails_closed(self):
        raw = {"summary_price": "600", "departure_time": "16:30", "fares": [
            {"button": "立即选购", "button_class": "dis-yuding", "cabin": "N舱",
             "price": "￥900", "tax_note": "（不含税费）", "brand": "NORMAL", "title": "经济舱"}]}
        with self.assertRaises(watch.DataError):
            watch.normalize_snapshot(raw, {"flight_number": "HU7711", "departure_date": "2026-11-09"})


class EffectiveDateTests(unittest.TestCase):
    @staticmethod
    def rule(start, end, p_rate):
        epoch = lambda text: int(datetime.fromisoformat(text + "T00:00:00+08:00").timestamp() * 1000)
        return {"startDate": epoch(start), "endDate": epoch(end) if end else None,
                "segtyp": "D", "qpqsBigClassDtos": [{"qpQsItems": [
                    {"cabin": "N", "qp": "0.50"}, {"cabin": "V", "qp": "0.50"},
                    {"cabin": "P", "qp": p_rate}]}]}

    def test_uses_flight_date_and_includes_end_date(self):
        payload = {"code": "200", "data": [
            self.rule("2024-11-11", None, "0.00"),
            self.rule("2020-08-14", "2024-11-10", "0.50")]}
        self.assertEqual(watch.parse_rules(payload, "2024-11-10")["P"], "0.50")
        self.assertEqual(watch.parse_rules(payload, "2024-11-11")["P"], "0.00")
        self.assertEqual(watch.parse_rules(payload, "2026-11-09")["P"], "0.00")

    def test_no_applicable_rule_fails_closed(self):
        with self.assertRaises(watch.DataError):
            watch.parse_rules({"code": "200", "data": [self.rule("2027-01-01", None, "0.00")]}, "2026-11-09")


class NotificationTests(unittest.TestCase):
    def test_notification_text_is_passed_as_arguments(self):
        title = '提醒 "N舱"'
        body = '¥900\n正文含有 $(echo example) 和 "引号"'
        with patch.object(watch.sys, "platform", "darwin"), \
                patch.object(watch.subprocess, "run") as run:
            watch.notify_macos(title, body, "HU7711")
            self.assertEqual(run.call_args.args[0],
                             ["/usr/bin/osascript", "-", title, "HU7711", body])
            self.assertNotIn(body, run.call_args.kwargs["input"])
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertTrue(run.call_args.kwargs["check"])

    def test_notification_submission_failure_is_reported(self):
        with patch.object(watch.sys, "platform", "darwin"), \
                patch.object(watch.subprocess, "run", side_effect=OSError("unavailable")):
            with self.assertRaises(watch.NotificationError):
                watch.notify_macos("提醒", "内容")

    def test_match_notification_contains_flight_price_and_cabin(self):
        sample = snapshot(("V", "900"))
        result = watch.evaluate(sample, RULES, "0.50")
        with patch.object(watch, "notify_macos") as notify:
            watch.notify_match(sample, result)
            title, body, subtitle = notify.call_args.args
            self.assertEqual(title, "海航机票符合条件")
            self.assertIn("HU7711", subtitle)
            self.assertIn("2026-11-09", subtitle)
            self.assertIn("¥900", body)
            self.assertIn("V舱", body)


class WatcherTests(unittest.TestCase):
    def run_cycles(self, results, notification_failures=None):
        config = {"flight_number": "HU7711", "departure_date": "2026-11-09",
                  "origin": "PEK", "destination": "SZX", "interval_seconds": 900}
        with TemporaryDirectory() as folder, \
                patch.object(watch, "LOCAL", Path(folder)), \
                patch.object(watch.sys, "platform", "darwin"), \
                patch.object(watch, "check", side_effect=results + [KeyboardInterrupt]), \
                patch.object(watch.time, "sleep"), \
                patch.object(watch, "notify_match", side_effect=notification_failures) as notify, \
                patch.object(watch, "notify_macos") as send, \
                patch("builtins.print"):
            # 固定测试时钟，避免飞行日期过后测试失效。
            with patch.object(watch, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 11, 7, tzinfo=watch.TZ)
                with self.assertRaises(KeyboardInterrupt):
                    watch.watch(config)
            return notify.call_count, send.call_count

    @staticmethod
    def cycle(matched=True):
        sample = snapshot(("V", "900")) if matched else snapshot(("U", "670"), ("E", "920"))
        sample["departure_at"] = "2026-11-09T16:30:00+08:00"
        return sample, watch.evaluate(sample, RULES, "0.50")

    def test_identical_match_does_not_send_again(self):
        notify_count, _ = self.run_cycles([self.cycle(), self.cycle()])
        self.assertEqual(notify_count, 1)

    def test_eligibility_returns_sends_again(self):
        notify_count, _ = self.run_cycles([self.cycle(), self.cycle(False), self.cycle()])
        self.assertEqual(notify_count, 2)

    def test_notification_failure_does_not_suppress_retry(self):
        notify_count, _ = self.run_cycles([self.cycle(), self.cycle()], [RuntimeError(), None])
        self.assertEqual(notify_count, 2)

    def test_repeated_query_failure_warns_once(self):
        _, warning_count = self.run_cycles([watch.DataError("offline")] * 4)
        self.assertEqual(warning_count, 1)


if __name__ == "__main__":
    unittest.main()
