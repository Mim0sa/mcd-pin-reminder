"""Read-only Streamable HTTP MCP client. Credentials never enter output."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.request

ENDPOINT = "https://mcp.mcd.cn"
ROOT = Path(__file__).resolve().parent.parent


class QueryError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        # Never forward an authenticated request to a redirected destination.
        raise QueryError("麦当劳 MCP 返回了非预期重定向，请稍后重试。")


def load_token():
    token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if not token:
        path = ROOT / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip() == "MCD_MCP_TOKEN":
                    token = value.strip().strip("\"'")
                    break
    if not token:
        raise QueryError("请在项目 .env 或环境变量中设置 MCD_MCP_TOKEN。")
    if "\n" in token or "\r" in token:
        raise QueryError("MCD_MCP_TOKEN 格式无效。")
    return token


class McdClient:
    def __init__(self, token):
        self.token = token
        self.session = None
        self.request_id = 0
        self.protocol = "2025-03-26"
        self.opener = urllib.request.build_opener(NoRedirect())
        result = self.rpc("initialize", {
            "protocolVersion": self.protocol,
            "capabilities": {},
            "clientInfo": {"name": "mcd-pin-reminder", "version": "0.1.0"},
        })
        protocol = result.get("protocolVersion")
        if not isinstance(protocol, str) or protocol not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
            raise QueryError("麦当劳 MCP 返回了不支持的协议版本。")
        self.protocol = protocol
        self.rpc("notifications/initialized", notification=True)

    def rpc(self, method, params=None, notification=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            self.request_id += 1
            payload["id"] = self.request_id
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(ENDPOINT, json.dumps(payload).encode(), headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if notification:
                    return {}
                if response.headers.get("Content-Type", "").startswith("text/event-stream"):
                    parsed = self.read_sse(response, payload["id"])
                else:
                    parsed = json.load(response)
        except urllib.error.HTTPError as exc:
            raise QueryError(f"麦当劳 MCP 请求失败（HTTP {exc.code}），请检查凭据或稍后重试。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise QueryError("无法连接麦当劳 MCP，请检查网络后重试。") from None
        except (ValueError, UnicodeError):
            raise QueryError("麦当劳 MCP 返回了无法解析的数据。") from None
        if not isinstance(parsed, dict) or parsed.get("id") != payload["id"]:
            raise QueryError("麦当劳 MCP 响应与请求不匹配。")
        if "error" in parsed or not isinstance(parsed.get("result"), dict):
            raise QueryError("麦当劳 MCP 未成功完成查询。")
        return parsed["result"]

    @staticmethod
    def read_sse(response, request_id):
        data = []
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                if data:
                    parsed = json.loads("\n".join(data))
                    if isinstance(parsed, dict) and parsed.get("id") == request_id:
                        return parsed
                    data = []
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            parsed = json.loads("\n".join(data))
            if isinstance(parsed, dict) and parsed.get("id") == request_id:
                return parsed
        raise QueryError("麦当劳 MCP 未返回完整查询响应。")

    def call_tool(self, name, arguments=None):
        if name not in {"order-list", "query-order"}:
            raise QueryError("本项目只支持只读订单查询。")
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return unwrap_tool_result(result)


def unwrap_tool_result(result):
    if not isinstance(result, dict) or result.get("isError"):
        raise QueryError("订单查询失败，无法判断徽章状态。")
    if isinstance(result.get("structuredContent"), dict):
        return validate_business_result(result["structuredContent"])
    if "content" in result:
        if not isinstance(result["content"], list):
            raise QueryError("订单工具返回了无效的内容格式。")
        for block in result["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                try:
                    parsed = json.loads(block["text"])
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(parsed, dict) and "data" in parsed:
                    return validate_business_result(parsed)
        raise QueryError("订单工具没有返回可解析的业务数据。")
    return validate_business_result(result)


def validate_business_result(payload):
    # The live service's envelope uses success=true and code=200. Fixtures may
    # omit these fields, but a declared business failure must never mean zero
    # orders, even if the server includes an empty list alongside the error.
    if payload.get("success") is False or ("code" in payload and payload["code"] not in (200, "200")):
        raise QueryError("麦当劳订单查询未成功，无法判断徽章状态。")
    return payload


def extract_orders(payload):
    payload = unwrap_tool_result(payload)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise QueryError("订单列表缺失或格式不符，无法判断徽章状态。")
    if any(not isinstance(order, dict) for order in data["list"]):
        raise QueryError("订单列表包含无效记录，无法判断徽章状态。")
    return data["list"]
