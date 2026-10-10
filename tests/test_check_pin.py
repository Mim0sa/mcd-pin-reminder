import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from check_pin import check, evaluate_orders, load_period, parse_time
from mcd_client import McdClient, NoRedirect, QueryError, ROOT, extract_orders
from render_receipt import render_receipt

CALENDAR = ROOT / "references" / "badge-periods.json"
NOW = parse_time("2026-10-10T12:00:00+08:00")
START = parse_time("2026-10-08T00:00:00+08:00")
END = parse_time("2026-10-23T00:00:00+08:00")


def evaluate(*orders):
    return evaluate_orders(list(orders), START, END, NOW)


def order(time="2026-10-09 12:00:00", status="6"):
    return {"createTime": time, "orderStatus": status}


class OrdersTest(unittest.TestCase):
    def test_completed_and_reviewed_are_successful(self):
        self.assertEqual(evaluate(order(), order(status="8"))["successful_order_count"], 2)

    def test_cancelled_and_processing_are_not_successful(self):
        result = evaluate(*(order(status=status) for status in ["1", "2", "4", "7", "10"]))
        self.assertEqual(result["outcome"], "not_obtained")
        self.assertEqual(result["pending_order_count"], 4)

    def test_outside_period_and_future_orders_are_excluded(self):
        result = evaluate(order("2026-10-07 23:59:59"), order("2026-10-11 00:00:00"), order("2026-10-23 00:00:00"))
        self.assertFalse(result["has_successful_order"])

    def test_start_is_included(self):
        self.assertTrue(evaluate(order("2026-10-08 00:00:00"))["has_successful_order"])

    def test_timezone_conversion(self):
        self.assertTrue(evaluate(order("2026-10-07T16:00:00Z"))["has_successful_order"])
        self.assertFalse(evaluate(order("2026-10-07T15:59:59Z"))["has_successful_order"])

    def test_empty_list(self):
        self.assertEqual(evaluate()["outcome"], "not_obtained")

    def test_unknown_status_does_not_claim_no_orders(self):
        self.assertIsNone(evaluate(order(status="999"))["has_successful_order"])

    def test_bad_time_does_not_claim_no_orders(self):
        self.assertEqual(evaluate(order("not-a-time"))["outcome"], "unknown")

    def test_success_is_sufficient_even_with_uncertain_records(self):
        self.assertTrue(evaluate(order(), order("bad"))["has_successful_order"])

    def test_integer_status(self):
        self.assertTrue(evaluate(order(status=6))["has_successful_order"])

    def test_live_chinese_completed_status(self):
        self.assertTrue(evaluate(order(status="订单已完成"))["has_successful_order"])

    def test_chinese_cancelled_status(self):
        self.assertFalse(evaluate(order(status="订单已取消"))["has_successful_order"])

    def test_status_matching_is_exact_not_substring(self):
        self.assertIsNone(evaluate(order(status="订单未完成"))["has_successful_order"])


class CalendarTest(unittest.TestCase):
    def test_current_period(self):
        calendar, (period, _, _) = load_period(CALENDAR, NOW)
        self.assertEqual(period["name"], "寒露")
        self.assertEqual(calendar["status"], "verified")

    def test_end_belongs_to_next_period(self):
        _, (period, _, _) = load_period(CALENDAR, END)
        self.assertEqual(period["name"], "霜降")

    def test_previous_period_uses_calendar_boundaries(self):
        _, (period, start, end) = load_period(CALENDAR, NOW, previous=True)
        self.assertEqual(period["id"], "2026-qiufen")
        self.assertEqual(start, parse_time("2026-09-23"))
        self.assertEqual(end, START)

    def test_previous_period_crosses_years(self):
        _, (period, _, _) = load_period(CALENDAR, parse_time("2027-01-05"), previous=True)
        self.assertEqual(period["id"], "2026-dongzhi")

    def test_two_periods_ago_selects_white_dew(self):
        _, (period, start, end) = load_period(CALENDAR, NOW, periods_ago=2)
        self.assertEqual(period["id"], "2026-bailu")
        self.assertEqual(start, parse_time("2026-09-07"))
        self.assertEqual(end, parse_time("2026-09-23"))

    def test_invalid_or_missing_historical_period_is_rejected(self):
        for offset in (-1, 1000):
            with self.subTest(offset=offset), self.assertRaises(QueryError):
                load_period(CALENDAR, NOW, periods_ago=offset)

    def test_previous_year_winter_covers_january(self):
        _, (period, _, _) = load_period(CALENDAR, parse_time("2026-01-01"))
        self.assertEqual(period["id"], "2025-dongzhi")

    def test_unconfigured_year_requires_new_calendar(self):
        with self.assertRaises(QueryError):
            load_period(CALENDAR, parse_time("2028-01-01"))

    def test_2027_new_year_inherits_previous_winter(self):
        _, (period, _, _) = load_period(CALENDAR, parse_time("2027-01-01"))
        self.assertEqual(period["id"], "2026-dongzhi")

    def test_2027_spring_equinox_switches_at_midnight(self):
        _, (before, _, _) = load_period(CALENDAR, parse_time("2027-03-20T23:59:59+08:00"))
        _, (after, _, _) = load_period(CALENDAR, parse_time("2027-03-21T00:00:00+08:00"))
        self.assertEqual(before["id"], "2027-jingzhe")
        self.assertEqual(after["id"], "2027-chunfen")

    def test_2027_last_period_ends_at_2028_minor_cold(self):
        _, (period, _, end) = load_period(CALENDAR, parse_time("2027-12-31"))
        self.assertEqual(period["id"], "2027-dongzhi")
        self.assertEqual(end, parse_time("2028-01-06"))

    def test_all_days_have_exactly_one_period(self):
        from datetime import timedelta
        day = parse_time("2026-01-01")
        while day.year in (2026, 2027):
            load_period(CALENDAR, day)
            day += timedelta(days=1)

    def test_overlap_is_rejected(self):
        calendar = json.loads(CALENDAR.read_text())
        calendar["periods"][1]["start"] = calendar["periods"][0]["start"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            path.write_text(json.dumps(calendar))
            with self.assertRaises(QueryError):
                load_period(path, NOW)

    def test_calendar_rejects_gaps_and_non_midnight_boundaries(self):
        for field, value in (("start", "2026-01-06T00:00:00+08:00"), ("end", "2026-01-20T01:00:00+08:00")):
            calendar = json.loads(CALENDAR.read_text())
            calendar["periods"][1][field] = value
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "calendar.json"
                path.write_text(json.dumps(calendar))
                with self.assertRaises(QueryError):
                    load_period(path, NOW)

    def test_verified_2026_rain_water_starts_on_february_18(self):
        _, (period, _, _) = load_period(CALENDAR, parse_time("2026-02-18"))
        self.assertEqual(period["id"], "2026-yushui")

    def test_2025_winter_boundary_for_early_year_history(self):
        _, (_, start, _) = load_period(CALENDAR, parse_time("2026-01-01"))
        self.assertEqual(start, parse_time("2025-12-21"))


class ResponseTest(unittest.TestCase):
    def test_structured_content(self):
        self.assertEqual(extract_orders({"structuredContent": {"data": {"list": []}}}), [])

    def test_json_text_content(self):
        self.assertEqual(extract_orders({"content": [{"type": "text", "text": '{"data":{"list":[]}}'}]}), [])

    def test_missing_list_is_not_an_empty_list(self):
        with self.assertRaises(QueryError):
            extract_orders({"message": "failure"})

    def test_tool_error_even_with_empty_list(self):
        with self.assertRaises(QueryError):
            extract_orders({"isError": True, "structuredContent": {"data": {"list": []}}})

    def test_business_failure_is_not_an_empty_history(self):
        for envelope in ({"success": False, "code": 200}, {"code": 401}, {"code": 500}):
            with self.subTest(envelope=envelope), self.assertRaises(QueryError):
                extract_orders({**envelope, "data": {"list": []}})

    def test_successful_business_envelope(self):
        self.assertEqual(extract_orders({"success": True, "code": 200, "data": {"list": []}}), [])

    def test_invalid_content_does_not_crash(self):
        for content in (None, "invalid", [None], [{"type": "text", "text": None}]):
            with self.subTest(content=content), self.assertRaises(QueryError):
                extract_orders({"content": content})

    def test_sse_ignores_notification_and_handles_crlf(self):
        stream = io.BytesIO(b': keepalive\r\n\r\ndata: {"jsonrpc":"2.0","method":"progress"}\r\n\r\ndata: {"jsonrpc":"2.0","id":2,"result":{}}\r\n\r\n')
        self.assertEqual(McdClient.read_sse(stream, 2)["id"], 2)

    def test_sse_requires_matching_response(self):
        with self.assertRaises(QueryError):
            McdClient.read_sse(io.BytesIO(b'data: {"id":3}\n\n'), 2)

    def test_write_tools_are_rejected(self):
        client = McdClient.__new__(McdClient)
        with self.assertRaises(QueryError):
            client.call_tool("create-order")

    def test_authenticated_redirect_is_rejected(self):
        with self.assertRaises(QueryError):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.com")


class IntegrationTest(unittest.TestCase):
    def test_invalid_optional_products_do_not_invalidate_success(self):
        for products in (42, {"productName": "bad"}, "bad", [None, 42]):
            with self.subTest(products=products):
                result = check(NOW, CALENDAR, orders=[{**order(), "orderProductList": products}])
                self.assertEqual(result["outcome"], "obtained")
                self.assertEqual(result["orders"][0]["items"], [])

    def test_fixture_labels_source_and_hides_personal_data(self):
        record = {**order(), "orderId": "PRIVATE-ID", "mobilePhone": "PRIVATE-PHONE", "storeName": "PRIVATE-STORE"}
        result = check(NOW, CALENDAR, orders=[record])
        output = json.dumps(result, ensure_ascii=False) + render_receipt(result)
        self.assertEqual(result["source"], "fixture")
        self.assertIn("DEMO / 虚构订单", output)
        self.assertNotIn("暂编演示数据", output)
        self.assertEqual(result["orders"][0]["store_name"], "PRIVATE-STORE")
        for private in ("PRIVATE-ID", "PRIVATE-PHONE"):
            self.assertNotIn(private, output)

    def test_actual_tool_arguments_and_detail_resolution(self):
        class Client:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments=None):
                self.calls.append((name, arguments))
                if name == "order-list":
                    return {"data": {"list": [{**order(status="unknown"), "orderId": "example-id"}]}}
                return {"data": {"orderStatus": "6"}}

        client = Client()
        result = check(NOW, CALENDAR, client=client)
        self.assertTrue(result["has_successful_order"])
        self.assertEqual(client.calls, [("order-list", None), ("query-order", {"orderId": "example-id"})])
        self.assertEqual(result["source"], "mcd_mcp")

    def test_query_failure_propagates(self):
        class Client:
            def call_tool(self, *args):
                raise QueryError("network unavailable")

        with self.assertRaises(QueryError):
            check(NOW, CALENDAR, client=Client())

    def test_detail_failure_preserves_unknown(self):
        class Client:
            def call_tool(self, name, arguments=None):
                if name == "order-list":
                    return {"data": {"list": [{**order(status="unknown"), "orderId": "example-id"}]}}
                raise QueryError("unavailable")

        self.assertEqual(check(NOW, CALENDAR, client=Client())["outcome"], "unknown")


if __name__ == "__main__":
    unittest.main()
