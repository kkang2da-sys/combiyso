# -*- coding: utf-8 -*-
"""MCP (Model Context Protocol) stdio 클라이언트.

설정에 등록한 외부 MCP 서버(예: filesystem, github, sqlite …)의 도구를
컴비서 에이전트의 도구 목록에 합쳐 준다.

전송 규약: 줄바꿈으로 구분된 JSON-RPC 2.0 (stdio).
"""
import json
import os
import shutil
import subprocess
import threading

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "combiyso", "version": "1.0.0"}


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, spec: dict):
        self.spec = spec or {}
        self.name = str(self.spec.get("name") or "mcp").strip() or "mcp"
        self.command = str(self.spec.get("command") or "").strip()
        self.args = [str(a) for a in (self.spec.get("args") or [])]
        self.env_extra = {str(k): str(v) for k, v in (self.spec.get("env") or {}).items()}
        self.cwd = self.spec.get("cwd") or None
        self.timeout = int(self.spec.get("timeout") or 30)

        self.proc = None
        self.tools = []
        self.server_info = None
        self.last_error = ""
        self.stderr_tail = ""

        self._id = 0
        self._lock = threading.Lock()
        self._responses = {}
        self._events = {}
        self._threads = []

    # ---------------- 수명주기 ---------------- #
    @property
    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def start(self):
        if self.alive:
            return
        if not self.command:
            raise MCPError(f"[{self.name}] command 가 비어 있습니다.")
        env = dict(os.environ)
        env.update(self.env_extra)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")

        exe = shutil.which(self.command) or self.command
        cmd = [exe] + self.args
        use_shell = bool(os.name == "nt" and str(exe).lower().endswith((".cmd", ".bat")))
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      env=env, cwd=self.cwd, shell=use_shell)
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(cmd, **kwargs)
        except FileNotFoundError:
            if os.name == "nt":
                kwargs["shell"] = True
                try:
                    self.proc = subprocess.Popen(cmd, **kwargs)
                except Exception as e:
                    raise MCPError(f"[{self.name}] 실행 실패: {type(e).__name__}: {e}")
            else:
                raise MCPError(f"[{self.name}] 실행 실패: 명령을 찾을 수 없습니다 ({self.command})")
        except Exception as e:
            raise MCPError(f"[{self.name}] 실행 실패: {type(e).__name__}: {e}")

        self._spawn(self._read_loop)
        self._spawn(self._err_loop)

        res = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
            "clientInfo": CLIENT_INFO,
        }, timeout=self.timeout)
        if isinstance(res, dict):
            self.server_info = res.get("serverInfo")
        self._notify("notifications/initialized", {})

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def _spawn(self, fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self._threads.append(t)

    # ---------------- 입출력 ---------------- #
    def _read_loop(self):
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is not None and ("result" in msg or "error" in msg):
                    with self._lock:
                        self._responses[mid] = msg
                        ev = self._events.get(mid)
                    if ev:
                        ev.set()
        except Exception:
            pass

    def _err_loop(self):
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                s = raw.decode("utf-8", errors="replace").rstrip()
                if s:
                    self.stderr_tail = (self.stderr_tail + "\n" + s)[-2000:]
        except Exception:
            pass

    def _write(self, obj: dict):
        if not self.alive:
            raise MCPError(f"[{self.name}] 서버가 실행 중이 아닙니다.")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except Exception as e:
            raise MCPError(f"[{self.name}] 전송 실패: {type(e).__name__}: {e}")

    def _notify(self, method: str, params: dict):
        try:
            self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})
        except Exception:
            pass

    def _request(self, method: str, params: dict = None, timeout: int = 30):
        if not self.alive:
            raise MCPError(f"[{self.name}] 서버가 실행 중이 아닙니다.")
        with self._lock:
            self._id += 1
            mid = self._id
            ev = threading.Event()
            self._events[mid] = ev
        self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        if not ev.wait(timeout):
            with self._lock:
                self._events.pop(mid, None)
            raise MCPError(f"[{self.name}] '{method}' 응답 시간 초과({timeout}s)")
        with self._lock:
            msg = self._responses.pop(mid, None)
            self._events.pop(mid, None)
        if not msg:
            raise MCPError(f"[{self.name}] '{method}' 응답 없음")
        if "error" in msg:
            err = msg["error"]
            raise MCPError(f"[{self.name}] {err.get('message') if isinstance(err, dict) else err}")
        return msg.get("result")

    # ---------------- MCP 기능 ---------------- #
    def list_tools(self) -> list:
        res = self._request("tools/list", {}, timeout=self.timeout)
        self.tools = (res or {}).get("tools") or []
        return self.tools

    def call_tool(self, tool_name: str, arguments: dict, timeout: int = 180) -> str:
        res = self._request("tools/call",
                            {"name": tool_name, "arguments": arguments or {}},
                            timeout=timeout) or {}
        parts = []
        for c in res.get("content") or []:
            if not isinstance(c, dict):
                parts.append(str(c))
                continue
            t = c.get("type")
            if t == "text":
                parts.append(c.get("text") or "")
            elif t == "resource":
                r = c.get("resource") or {}
                parts.append(r.get("text") or json.dumps(r, ensure_ascii=False)[:4000])
            elif t == "image":
                parts.append("[이미지 결과]")
            else:
                parts.append(json.dumps(c, ensure_ascii=False)[:2000])
        text = "\n".join(p for p in parts if p)
        if res.get("isError"):
            text = "오류: " + text
        return text or "(빈 응답)"


class MCPManager:
    """설정에 등록된 모든 MCP 서버를 관리."""

    def __init__(self):
        self.clients = {}          # name -> MCPClient
        self.errors = {}           # name -> str

    def reload(self, specs: list):
        specs = [s for s in (specs or []) if isinstance(s, dict) and s.get("enabled", True)]
        wanted = {}
        for s in specs:
            name = str(s.get("name") or "").strip()
            if name:
                wanted[name] = s
        # 제거된 서버 종료
        for name in list(self.clients):
            if name not in wanted:
                self.clients[name].stop()
                self.clients.pop(name, None)
                self.errors.pop(name, None)
        # 추가/갱신
        for name, spec in wanted.items():
            old = self.clients.get(name)
            if old and old.alive and old.spec == spec:
                continue
            if old:
                old.stop()
            client = MCPClient(spec)
            try:
                client.start()
                client.list_tools()
                self.errors.pop(name, None)
            except Exception as e:
                self.errors[name] = f"{type(e).__name__}: {e}"
            self.clients[name] = client

    def all_tools(self) -> list:
        """[(full_name, server, tool_dict)]"""
        out = []
        for name, c in self.clients.items():
            for t in (c.tools or []):
                tname = t.get("name")
                if tname:
                    out.append((f"mcp__{name}__{tname}", name, t))
        return out

    def call(self, full_name: str, arguments: dict) -> str:
        parts = full_name.split("__", 2)
        if len(parts) != 3:
            raise MCPError(f"잘못된 MCP 도구 이름: {full_name}")
        _, server, tool = parts
        c = self.clients.get(server)
        if not c:
            raise MCPError(f"MCP 서버 없음: {server}")
        if not c.alive:
            c.start()
        return c.call_tool(tool, arguments or {})

    def status(self) -> list:
        out = []
        for name, c in self.clients.items():
            out.append({
                "name": name,
                "alive": c.alive,
                "tool_count": len(c.tools or []),
                "tools": [t.get("name") for t in (c.tools or [])][:50],
                "error": self.errors.get(name, ""),
                "stderr": (c.stderr_tail or "")[-600:],
                "server_info": c.server_info,
            })
        for name, err in self.errors.items():
            if name not in self.clients:
                out.append({"name": name, "alive": False, "tool_count": 0,
                            "tools": [], "error": err, "stderr": "", "server_info": None})
        return out

    def stop_all(self):
        for c in self.clients.values():
            c.stop()
        self.clients.clear()


MANAGER = MCPManager()
