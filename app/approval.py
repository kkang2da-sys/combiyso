# -*- coding: utf-8 -*-
"""도구 실행 승인 브로커.

에이전트가 위험한 작업(쓰기/삭제/이동/명령 실행)을 하기 전에 UI 로 승인 요청을 보내고,
사용자의 응답(허용 / 이 세션 동안 허용 / 거부)을 기다린다.
"""
import threading
import time
import uuid


class ApprovalBroker:
    def __init__(self):
        self._pending = {}
        self._session_allow = {}          # {session_id: {kind, ...}}
        self._lock = threading.Lock()

    # ---------------- 세션 허용 ---------------- #
    def allow_for_session(self, sid: str, kind: str):
        with self._lock:
            self._session_allow.setdefault(sid or "", set()).add(kind)

    def is_session_allowed(self, sid: str, kind: str) -> bool:
        with self._lock:
            return kind in self._session_allow.get(sid or "", set())

    def clear_session(self, sid: str = None):
        with self._lock:
            if sid is None:
                self._session_allow.clear()
            else:
                self._session_allow.pop(sid or "", None)

    # ---------------- 요청/응답 ---------------- #
    def create(self, kind: str, title: str, detail: str = "", extra: dict = None,
               sid: str = "") -> str:
        aid = "ap_" + uuid.uuid4().hex[:12]
        with self._lock:
            self._pending[aid] = {
                "id": aid, "kind": kind, "sid": sid or "", "title": title,
                "detail": detail, "extra": extra or {}, "event": threading.Event(),
                "decision": "deny", "created": time.time(),
            }
        return aid

    def wait(self, aid: str, timeout: float = 600.0) -> str:
        with self._lock:
            item = self._pending.get(aid)
        if not item:
            return "deny"
        item["event"].wait(timeout)
        with self._lock:
            item = self._pending.pop(aid, None)
        return (item or {}).get("decision", "deny")

    def resolve(self, aid: str, decision: str, remember: bool = False) -> bool:
        with self._lock:
            item = self._pending.get(aid)
        if not item:
            return False
        if remember and decision == "allow":
            self.allow_for_session(item.get("sid", ""), item["kind"])
        item["decision"] = "allow" if decision == "allow" else "deny"
        item["event"].set()
        return True

    def cancel_all(self):
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
        for it in items:
            it["decision"] = "deny"
            it["event"].set()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


BROKER = ApprovalBroker()
