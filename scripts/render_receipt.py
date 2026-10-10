"""Render an open-edge ASCII receipt without font-dependent right padding."""

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
import unicodedata

WIDTH = 52
MAX_ORDERS = 5


def clean(value):
    """Keep remote text on the printed ticket, without terminal control codes."""
    return "".join(
        char if not unicodedata.category(char).startswith("C") else " "
        for char in str(value)
    ).replace("`", "'").strip()


def display_width(value):
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def wrap(value, width=WIDTH):
    lines, line = [], ""
    for char in clean(value):
        if line and display_width(line + char) > width:
            lines.append(line)
            line = ""
        line += char
    return lines + [line]


def render_receipt(result):
    validate_result(result)
    rows = []

    def line(value="", center=False, indent=0):
        for chunk in wrap(value, WIDTH - indent):
            chunk = " " * indent + chunk
            # CJK fallback fonts may not be exactly two Latin cells wide in a
            # chat code block. Only ASCII art is centered; text has no right
            # border or right padding that would expose that font difference.
            left = (WIDTH - display_width(chunk)) // 2 if center and chunk.isascii() else 0
            rows.append(("| " + " " * left + chunk).rstrip())

    def rule():
        line("-" * WIDTH)

    def pair(left, right):
        left, right = clean(left), clean(right)
        line(left + " : " + right)

    rows.append("." + "-" * (WIDTH + 2) + ".")
    logo = (
        "██╗     ██╗",
        "████╗ ████║",
        "██╔═██╔═██║",
        "██║ ╚═╝ ██║",
        "██║     ██║",
        "╚═╝     ╚═╝",
    )
    logo_indent = (WIDTH - max(display_width(logo_line) for logo_line in logo)) // 2
    for logo_line in logo:
        line(logo_line, indent=logo_indent)
    line("麦麦 · 节气消费小票", center=True)
    line("DEMO / 虚构订单" if result.get("source") == "fixture" else "订单查询结果", center=True)
    rule()
    if "error" in result:
        line("[ ? ] 徽章状态：无法判断", center=True)
        line(result["error"])
        rows.append("'" + "-" * (WIDTH + 2) + "'")
        return "\n".join(rows)

    period = result["period"]
    start = datetime.fromisoformat(period["start_inclusive"])
    end = datetime.fromisoformat(period["end_exclusive"])
    checked = datetime.fromisoformat(result["checked_at"])
    offset = result.get("periods_ago", 1 if result.get("period_selection") == "previous" else 0)
    period_label = {0: "当前节气", 1: "上个节气", 2: "上上个节气"}.get(offset, f"往前 {offset} 个节气")
    pair(period_label, result["badge"]["name"] + " / " + str(start.year))
    line("开始  " + start.strftime("%Y-%m-%d %H:%M") + "  (含)")
    line("结束  " + end.strftime("%Y-%m-%d %H:%M") + "  (不含)")
    line("查询  " + checked.strftime("%Y-%m-%d %H:%M") + "  北京时间")
    if result.get("calendar_status") == "placeholder":
        line("节气日期为暂编演示数据，待替换。")
    rule()

    orders = result.get("orders", [])
    displayed = sorted(orders, key=lambda order: order["created_at"], reverse=True)[:MAX_ORDERS]
    if not orders:
        line("本次未查到本节气内的订单。")
    for index, order in enumerate(displayed, 1):
        time = datetime.fromisoformat(order["created_at"]).strftime("%m/%d %H:%M")
        line(f"#{index:02d}  {time}  [{order['status_label']}]")
        line("门店  " + (order.get("store_name") or "未提供"))
        for item in order.get("items", []):
            line(item["name"] + " x" + str(item["quantity"]), indent=2)
        if not order.get("items"):
            line("餐品明细未提供", indent=2)
        pair("实付" if order["is_successful"] else "订单金额", "CNY " + (order.get("amount") or "--"))
        if index != len(displayed):
            line("." * WIDTH)
    if len(orders) > MAX_ORDERS:
        line(f"展示最近 {MAX_ORDERS} 单，其余 {len(orders) - MAX_ORDERS} 单已省略。")
        line("下方统计与徽章判断仍包含全部本期订单。")
    rule()
    pair("本期订单", str(len(orders)) + " 笔")
    pair("成功订单", str(result["successful_order_count"]) + " 笔")
    consumed = [order for order in orders if order["is_successful"]]
    known_amounts = [Decimal(order["amount"]) for order in consumed if order.get("amount") is not None]
    if len(known_amounts) == len(consumed):
        pair("成功订单实付合计", f"CNY {sum(known_amounts, Decimal('0')):.2f}")
    else:
        pair("成功订单已知金额", f"CNY {sum(known_amounts, Decimal('0')):.2f}")
        line("部分金额未提供，无法计算完整消费合计。")
    rule()
    if result["outcome"] == "obtained":
        line(".--------------------.", center=True)
        line("|    [x] PIN GOT!    |", center=True)
        line("'--------------------'", center=True)
        line(result["badge"]["name"] + "节气内有成功订单，已经获得该徽章！")
    elif result["outcome"] == "not_obtained":
        line("[ ]  PIN NOT YET", center=True)
        line("未查询到成功订单，快去吃一顿麦麦吧！")
    else:
        line("[ ? ]  PIN UNKNOWN", center=True)
        line("部分订单无法确认，暂时无法判断徽章状态。")
    line("徽章按订单推定，以官方实际发放为准。")
    rule()
    line("KEEP YOUR PINS. KEEP YOUR MEMORIES.", center=True)
    rows.append("'" + "-" * (WIDTH + 2) + "'")
    rows.append(" " + "/\\" * ((WIDTH + 2) // 2))
    return "\n".join(rows)


def validate_result(result):
    """Validate the public renderer's input before formatting remote fields."""
    if not isinstance(result, dict) or result.get("outcome") not in {"obtained", "not_obtained", "unknown"}:
        raise ValueError("无效的徽章查询结果")
    if "error" in result:
        if result["outcome"] != "unknown" or not isinstance(result["error"], str):
            raise ValueError("无效的错误结果")
        return
    if not isinstance(result.get("badge"), dict) or not isinstance(result["badge"].get("name"), str):
        raise ValueError("缺少节气名称")
    if not isinstance(result.get("period"), dict):
        raise ValueError("缺少节气区间")
    orders = result.get("orders", [])
    if not isinstance(orders, list):
        raise ValueError("订单明细应为数组")
    if type(result.get("successful_order_count")) is not int or result["successful_order_count"] < 0:
        raise ValueError("成功订单数量无效")
    for order in orders:
        if not isinstance(order, dict) or type(order.get("is_successful")) is not bool:
            raise ValueError("订单记录无效")
        for field in ("created_at", "status_label"):
            if not isinstance(order.get(field), str):
                raise ValueError("订单字段无效")
        if not isinstance(order.get("store_name", ""), str) or not isinstance(order.get("items", []), list):
            raise ValueError("门店或餐品字段无效")
        for item in order.get("items", []):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or "quantity" not in item:
                raise ValueError("餐品记录无效")
        if order.get("amount") is not None:
            if not isinstance(order["amount"], str):
                raise ValueError("金额应为十进制字符串")
            try:
                amount = Decimal(order["amount"])
                if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
                    raise ValueError("金额无效")
            except InvalidOperation:
                raise ValueError("金额无效") from None


def main():
    parser = argparse.ArgumentParser(description="将徽章查询 JSON 渲染为 ASCII 收据。")
    parser.add_argument("--result-file", type=Path, help="查询结果 JSON；未提供时从标准输入读取。")
    parser.add_argument("--markdown", action="store_true", help="添加 Markdown text 代码块。")
    args = parser.parse_args()
    try:
        result = json.loads(args.result_file.read_text(encoding="utf-8")) if args.result_file else json.load(sys.stdin)
        if not isinstance(result, dict):
            raise ValueError("查询结果应为 JSON 对象")
        receipt = render_receipt(result)
    except (ValueError, KeyError, TypeError, OverflowError, OSError):
        print("无法生成收据：查询结果格式无效。", file=sys.stderr)
        return 2
    print("```text\n" + receipt + "\n```" if args.markdown else receipt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
