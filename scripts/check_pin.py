"""Infer a solar-term badge from recent successful orders; never query badge ownership."""

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from mcd_client import McdClient, QueryError, ROOT, extract_orders, load_token
from render_receipt import clean, render_receipt

TIMEZONE = ZoneInfo("Asia/Shanghai")
SUCCESS_STATUSES = {"6", "8"}
PENDING_STATUSES = {"1", "2", "4", "10"}
KNOWN_STATUSES = SUCCESS_STATUSES | PENDING_STATUSES | {"7"}
STATUS_LABELS = {"1": "待支付", "2": "配餐中", "4": "配送中", "6": "已完成", "7": "已取消", "8": "已评价", "10": "配餐中"}
STATUS_ALIASES = {
    "订单已完成": "6", "已完成": "6",
    "订单已评价": "8", "已评价": "8",
    "订单已取消": "7", "已取消": "7",
    "订单待支付": "1", "待支付": "1",
    "配餐中": "2", "配送中": "4",
}


def normalize_status(value):
    # Live responses use Chinese labels despite numeric codes in the schema.
    status = str(value).strip()
    return STATUS_ALIASES.get(status, status)


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间字段缺失")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed.astimezone(TIMEZONE)


def load_period(path, now, previous=False, periods_ago=0):
    if type(periods_ago) is not int or periods_ago < 0 or previous and periods_ago:
        raise QueryError("节气回溯数量必须是非负整数，且不能同时使用 previous。")
    offset = 1 if previous else periods_ago
    calendar = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(calendar, dict):
        raise QueryError("徽章时间段配置必须是 JSON 对象。")
    if calendar.get("timezone") != "Asia/Shanghai":
        raise QueryError("时间段配置必须使用 Asia/Shanghai 时区。")
    # Accept legacy single-year calendars while supporting a continuous timeline.
    years = calendar.get("years", [calendar.get("year")])
    if not isinstance(years, list) or not years or any(type(year) is not int for year in years):
        raise QueryError("时间段配置需要有效的 years 年份列表。")
    if now.year not in years:
        raise QueryError(f"尚未维护 {now.year} 年的徽章时间段，请更新 references/badge-periods.json。")
    if calendar.get("status") not in {"placeholder", "verified"}:
        raise QueryError("时间段配置必须标记为 placeholder 或 verified。")
    periods = calendar.get("periods")
    if not isinstance(periods, list) or not periods:
        raise QueryError("徽章时间段配置为空。")
    matches = []
    previous_end = None
    ids = set()
    for period in periods:
        if not isinstance(period, dict):
            raise QueryError("每个徽章时间段必须是 JSON 对象。")
        start, end = parse_time(period["start"]), parse_time(period["end"])
        if start >= end or previous_end is not None and start != previous_end:
            raise QueryError("徽章时间段存在倒序、重叠或断档，请修正配置。")
        if any(value.time().isoformat() != "00:00:00" for value in (start, end)):
            raise QueryError("徽章时间段边界必须为北京时间 0 点。")
        if not all(isinstance(period.get(key), str) and period[key].strip() for key in ("id", "name")) or period["id"] in ids:
            raise QueryError("徽章时间段需要唯一 id 和名称。")
        if start <= now < end:
            matches.append((period, start, end))
        ids.add(period["id"])
        previous_end = end
    if len(matches) != 1:
        raise QueryError("当前日期没有对应的徽章时间段，请补充配置。")
    if offset:
        current_index = periods.index(matches[0][0])
        if current_index < offset:
            raise QueryError(f"尚未维护往前 {offset} 个节气的时间段，请补充配置。")
        period = periods[current_index - offset]
        return calendar, (period, parse_time(period["start"]), parse_time(period["end"]))
    return calendar, matches[0]


def period_orders(orders, start, end, now):
    """Yield relevant records, retaining missing times as unresolved evidence."""
    for order in orders:
        try:
            created = parse_time(order.get("createTime"))
        except (ValueError, TypeError, OverflowError):
            yield order, None
            continue
        if start <= created < end and created <= now:
            yield order, created


def evaluate_orders(orders, start, end, now):
    successful = []
    pending = 0
    uncertain = 0
    for order, created in period_orders(orders, start, end, now):
        if created is None:
            uncertain += 1
            continue
        status = normalize_status(order.get("orderStatus", ""))
        if status in SUCCESS_STATUSES:
            successful.append(created.isoformat())
        elif status in PENDING_STATUSES:
            pending += 1
        elif status not in KNOWN_STATUSES:
            uncertain += 1
    if successful:
        outcome = "obtained"
    elif uncertain:
        outcome = "unknown"
    else:
        outcome = "not_obtained"
    return {
        "outcome": outcome,
        "has_successful_order": True if successful else None if uncertain else False,
        "successful_order_count": len(successful),
        "pending_order_count": pending,
        "unresolved_order_count": uncertain,
        "latest_successful_order_at": max(successful) if successful else None,
    }


def receipt_orders(orders, start, end, now):
    """Only retain fields needed for the user's receipt, not full API records."""
    rows = []
    for order, created in period_orders(orders, start, end, now):
        if created is None:
            continue
        status = normalize_status(order.get("orderStatus", ""))
        amount = None
        try:
            number = Decimal(str(order.get("realTotalAmount")))
            if number.is_finite() and number >= 0:
                amount = str(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError):
            pass
        items = []
        products = order.get("orderProductList")
        for item in products if isinstance(products, list) else []:
            if isinstance(item, dict) and item.get("productName"):
                quantity = item.get("quantity")
                items.append({"name": clean(item["productName"]), "quantity": quantity if type(quantity) is int and quantity > 0 else "?"})
        rows.append({
            "created_at": created.isoformat(),
            "status_label": STATUS_LABELS.get(status, "状态未知"),
            "is_successful": status in SUCCESS_STATUSES,
            "store_name": clean(order.get("storeName") or ""),
            "items": items,
            "amount": amount,
        })
    return sorted(rows, key=lambda row: row["created_at"])


def check(now, calendar_path, orders=None, client=None, previous=False, periods_ago=0):
    calendar, (period, start, end) = load_period(calendar_path, now, previous=previous, periods_ago=periods_ago)
    offset = 1 if previous else periods_ago
    source = "fixture" if orders is not None else "mcd_mcp"
    if orders is None:
        client = client or McdClient(load_token())
        orders = extract_orders(client.call_tool("order-list"))
        # Only resolve undocumented statuses inside the selected window.
        for order, created in period_orders(orders, start, end, now):
            if created is not None and normalize_status(order.get("orderStatus", "")) not in KNOWN_STATUSES:
                if order.get("orderId"):
                    try:
                        details = client.call_tool("query-order", {"orderId": order["orderId"]})
                        data = details.get("data")
                        status = data.get("orderStatus") if isinstance(data, dict) else None
                        if status is not None:
                            order["orderStatus"] = status
                    except QueryError:
                        pass  # Preserve uncertainty rather than claiming no orders.
    result = evaluate_orders(orders, start, end, now)
    result.update({
        "badge": {"id": period["id"], "name": period["name"]},
        "period": {"start_inclusive": start.isoformat(), "end_exclusive": end.isoformat()},
        "checked_at": now.isoformat(),
        "calendar_status": calendar["status"],
        "period_selection": "previous" if offset == 1 else "earlier" if offset else "current",
        "periods_ago": offset,
        "orders": receipt_orders(orders, start, end, now),
        "basis": "successful_order_proxy",
        "source": source,
        "history_coverage": "未提供完整历史覆盖范围；结论基于本次返回的近期订单。",
    })
    return result


def main():
    parser = argparse.ArgumentParser(description="查询当前节气段内是否有成功订单，据此判断徽章状态。")
    parser.add_argument("--at", help="指定查询时间（ISO 格式），默认北京时间当前时间。")
    parser.add_argument("--calendar", type=Path, default=ROOT / "references" / "badge-periods.json")
    parser.add_argument("--orders-file", type=Path, help="使用本地演示订单 JSON，不连接 MCP。")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="输出包含本期订单收据明细的 JSON，不含凭据、订单号、地址或电话。")
    output_group.add_argument("--markdown", action="store_true", help="在 ASCII 收据外添加 Markdown text 代码块。")
    period_group = parser.add_mutually_exclusive_group()
    period_group.add_argument("--previous", action="store_true", help="查询上一个节气的订单和徽章。")
    period_group.add_argument("--periods-ago", type=int, default=0, metavar="N", help="查询往前 N 个节气，0 为当前，2 为上上个。")
    args = parser.parse_args()
    try:
        now = parse_time(args.at) if args.at else datetime.now(TIMEZONE)
        orders = extract_orders(json.loads(args.orders_file.read_text(encoding="utf-8"))) if args.orders_file else None
        result = check(now, args.calendar, orders=orders, previous=args.previous, periods_ago=args.periods_ago)
    except (QueryError, ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
        message = str(exc) if isinstance(exc, QueryError) else "本地配置、时间或订单数据格式无效，请检查文件。"
        result = {"outcome": "unknown", "has_successful_order": None, "error": message}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        receipt = render_receipt(result)
        print("```text\n" + receipt + "\n```" if args.markdown else receipt)
    return 2 if result["outcome"] == "unknown" else 0


if __name__ == "__main__":
    sys.exit(main())
