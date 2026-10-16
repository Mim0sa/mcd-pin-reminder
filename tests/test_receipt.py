import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from check_pin import check, parse_time
from mcd_client import ROOT, extract_orders
from render_receipt import display_width, render_receipt

CALENDAR = ROOT / "references" / "badge-periods.json"
NOW = parse_time("2026-10-10T12:00:00+08:00")


class ReceiptTest(unittest.TestCase):
    def result(self, orders):
        return check(NOW, CALENDAR, orders=orders)

    def test_all_current_orders_shown_cancelled_excluded_from_spending(self):
        payload = json.loads((ROOT / "examples/receipt-orders.json").read_text())
        result = self.result(extract_orders(payload))
        receipt = render_receipt(result)
        self.assertEqual(len(result["orders"]), 3)
        self.assertEqual(result["successful_order_count"], 2)
        self.assertIn("已取消", receipt)
        self.assertIn("46.40", receipt)
        self.assertNotIn("68.40", receipt)
        self.assertNotIn("99.00", receipt)
        self.assertNotIn("上期订单", receipt)
        self.assertIn("PIN GOT!", receipt)

    def test_cjk_long_names_wrap_without_breaking_ticket(self):
        store = "长门店名" * 30
        result = self.result([{"createTime": "2026-10-09", "orderStatus": "6", "storeName": store}])
        receipt = render_receipt(result)
        for line in receipt.splitlines():
            if line.startswith("|"):
                self.assertLessEqual(display_width(line), 54)
                self.assertEqual(line, line.rstrip())
        joined = "".join(line[2:].strip() for line in receipt.splitlines() if line.startswith("|"))
        self.assertIn(store, joined)

    def test_missing_amount_is_not_claimed_zero(self):
        result = self.result([{"createTime": "2026-10-09", "orderStatus": "6"}])
        receipt = render_receipt(result)
        self.assertIn("CNY --", receipt)
        self.assertIn("无法计算完整消费合计", receipt)
        self.assertNotIn("成功订单实付合计", receipt)

    def test_decimal_amounts_sum_exactly(self):
        records = [{"createTime": "2026-10-09", "orderStatus": "6", "realTotalAmount": value} for value in ("0.1", "0.2")]
        self.assertIn("CNY 0.30", render_receipt(self.result(records)))

    def test_empty_receipt_shows_no_badge(self):
        receipt = render_receipt(self.result([]))
        self.assertIn("PIN NOT YET", receipt)
        self.assertIn("本次未查到本节气内的订单", receipt)

    def test_current_missing_badge_shows_time_until_deadline(self):
        for checked_at, expected in (
            ("2026-10-08T00:00:00+08:00", "距截止还有 15 天 0 小时"),
            ("2026-10-16T11:41:00+08:00", "距截止还有 6 天 12 小时"),
            ("2026-10-22T23:00:00+08:00", "距截止还有 0 天 1 小时"),
            ("2026-10-22T23:59:59+08:00", "距截止不足 1 小时"),
            ("2026-10-23T00:00:00+08:00", "距截止还有 15 天 0 小时"),
        ):
            with self.subTest(checked_at=checked_at):
                result = check(parse_time(checked_at), CALENDAR, orders=[])
                self.assertIn(expected, render_receipt(result))

    def test_historical_period_does_not_show_deadline_countdown(self):
        for offset in (1, 2):
            with self.subTest(offset=offset):
                result = check(NOW, CALENDAR, orders=[], periods_ago=offset)
                self.assertNotIn("距截止", render_receipt(result))

    def test_obtained_or_unknown_badge_does_not_show_deadline_countdown(self):
        for status in ("6", "999"):
            with self.subTest(status=status):
                result = self.result([{"createTime": "2026-10-09", "orderStatus": status}])
                self.assertNotIn("距截止", render_receipt(result))

    def test_unknown_status_never_receives_badge_stamp(self):
        receipt = render_receipt(self.result([{"createTime": "2026-10-09", "orderStatus": "999"}]))
        self.assertIn("PIN UNKNOWN", receipt)
        self.assertNotIn("PIN GOT!", receipt)

    def test_invalid_amount_and_terminal_controls_are_not_printed(self):
        records = [{"createTime": "2026-10-09", "orderStatus": "6", "realTotalAmount": "NaN", "storeName": "餐厅\x1b\n```"}]
        receipt = render_receipt(self.result(records))
        self.assertNotIn("\x1b", receipt)
        self.assertNotIn("```", receipt)
        self.assertNotIn("NaN", receipt)

    def test_query_failure_prints_unknown_receipt(self):
        receipt = render_receipt({"outcome": "unknown", "error": "无法连接麦当劳 MCP"})
        self.assertIn("无法判断", receipt)
        self.assertNotIn("PIN NOT YET", receipt)

    def test_invalid_external_result_is_rejected(self):
        for invalid in ([], {"outcome": "invalid"}, {"outcome": "obtained", "orders": None}):
            with self.subTest(result=invalid), self.assertRaises(ValueError):
                render_receipt(invalid)

    def test_external_invalid_amount_is_rejected(self):
        for amount in ("NaN", "Infinity", "-1", "1E999999999"):
            result = self.result([{"createTime": "2026-10-09", "orderStatus": "6"}])
            result["orders"][0]["amount"] = amount
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                render_receipt(result)

    def test_latest_five_displayed_hidden_success_still_counts(self):
        orders = [{"createTime": f"2026-10-09 {hour:02d}:00:00", "orderStatus": "6" if hour == 1 else "7", "storeName": f"shop-{hour}", "realTotalAmount": "12.30"} for hour in range(1, 8)]
        receipt = render_receipt(self.result(orders))
        self.assertEqual(sum(line.startswith("| #") for line in receipt.splitlines()), 5)
        self.assertNotIn("shop-1", receipt)
        self.assertNotIn("shop-2", receipt)
        self.assertLess(receipt.index("shop-7"), receipt.index("shop-3"))
        self.assertIn("其余 2 单已省略", receipt)
        self.assertIn("本期订单 : 7 笔", receipt)
        self.assertIn("CNY 12.30", receipt)
        self.assertIn("PIN GOT!", receipt)

    def test_previous_empty_period_uses_requested_copy(self):
        result = check(NOW, CALENDAR, orders=[], previous=True)
        receipt = render_receipt(result)
        self.assertIn("上个节气 : 秋分", receipt)
        self.assertIn("未查询到成功订单，快去吃一顿麦麦吧！", receipt)


if __name__ == "__main__":
    unittest.main()
