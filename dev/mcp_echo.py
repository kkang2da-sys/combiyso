# -*- coding: utf-8 -*-
"""컴비서 MCP 연동 테스트용 미니 MCP 서버 (stdio, 줄바꿈 JSON-RPC)."""
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


TOOLS = [
    {"name": "echo", "description": "텍스트를 그대로 돌려줍니다.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string", "description": "돌려줄 텍스트"}},
                     "required": ["text"]}},
    {"name": "add", "description": "두 수를 더합니다.",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "echo-test", "version": "1.0.0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                text = "에코: " + str(args.get("text", ""))
            elif name == "add":
                try:
                    text = "합계: " + str(float(args.get("a", 0)) + float(args.get("b", 0)))
                except Exception as e:
                    send({"jsonrpc": "2.0", "id": mid, "result": {
                        "content": [{"type": "text", "text": f"오류: {e}"}], "isError": True}})
                    continue
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"알 수 없는 도구: {name}"}], "isError": True}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
