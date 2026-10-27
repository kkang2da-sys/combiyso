# -*- coding: utf-8 -*-
"""대화 세션 저장/불러오기/검색/내보내기 (개수 제한 없음).

성능 포인트
- 세션 목록은 JSON 전문을 매번 읽지 않고 **메타 인덱스**(`_index.json`)를 쓴다.
  파일의 mtime/size 가 그대로면 저장된 메타를 재사용한다(저사양·느린 디스크 대비).
- 저장은 **원자적**(tmp → replace) + 압축 JSON. 중간에 죽어도 파일이 깨지지 않는다.
"""
import json
import os
import threading
import time
import uuid

from .config import SESSIONS_DIR

INDEX_NAME = "_index.json"
_LOCK = threading.RLock()
_CACHE = {"dir": "", "data": {}, "loaded": False}


# --------------------------------------------------------------------------- #
# 내부 도우미
# --------------------------------------------------------------------------- #
def _dir():
    return SESSIONS_DIR


def _index_path():
    return _dir() / INDEX_NAME


def _reset_if_moved():
    d = str(_dir())
    if _CACHE["dir"] != d:
        _CACHE["dir"] = d
        _CACHE["data"] = {}
        _CACHE["loaded"] = False


def _load_index():
    """메모리 인덱스 로드(최초 1회) — 디스크 인덱스가 있으면 재사용."""
    _reset_if_moved()
    if _CACHE["loaded"]:
        return _CACHE["data"]
    data = {}
    try:
        raw = json.loads(_index_path().read_text(encoding="utf-8"))
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if isinstance(sessions, dict):
            data = sessions
    except Exception:
        data = {}
    _CACHE["data"] = data
    _CACHE["loaded"] = True
    return data


def _flush_index(force=False):
    """인덱스를 디스크에 저장. force=False 면 1초에 한 번만 쓴다."""
    _reset_if_moved()
    now = time.time()
    if not force and now - _CACHE.get("flushed", 0) < 1.0:
        return
    _CACHE["flushed"] = now
    try:
        p = _index_path()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"v": 1, "sessions": _CACHE["data"]},
                                  ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def drop_cache():
    """메타 인덱스 메모리 캐시를 비운다(종료/자원 회복용).

    다음 목록 조회 때 디스크에서 다시 읽으므로 안전하다.
    """
    with _LOCK:
        had = bool(_CACHE.get("data"))
        _CACHE["data"] = {}
        _CACHE["loaded"] = False
        _CACHE.pop("flushed", None)
    return had


def _path(sid):
    return _dir() / f"{sid}.json"


def _safe_id(sid):
    return "".join(ch for ch in str(sid or "") if ch.isalnum() or ch in "-_")


def _meta(d):
    msgs = d.get("messages") or []
    last = ""
    for m in reversed(msgs):
        if m.get("role") in ("user", "assistant") and m.get("content"):
            last = m.get("content")
            break
    return {"id": d.get("id"), "title": d.get("title", ""),
            "updated": d.get("updated", 0), "created": d.get("created", 0),
            "count": len(msgs), "preview": str(last)[:120]}


def _stamp(p):
    st = p.stat()
    return int(st.st_mtime_ns), int(st.st_size)


# --------------------------------------------------------------------------- #
# 공개 API
# --------------------------------------------------------------------------- #
def new_session(title="새 대화"):
    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    s = {"id": sid, "title": title, "created": time.time(), "updated": time.time(),
         "messages": [], "todos": []}
    save(s)
    return s


def save(s):
    if not s or not s.get("id"):
        return s
    s["updated"] = time.time()
    sid = _safe_id(s["id"])
    if not sid:
        return s
    p = _path(sid)
    with _LOCK:
        try:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(s, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
            os.replace(tmp, p)
            idx = _load_index()
            idx[sid] = {"stamp": list(_stamp(p)), "meta": _meta(s)}
            _flush_index()
        except Exception:
            pass
    return s


def load(sid):
    sid = _safe_id(sid)
    if not sid or sid == INDEX_NAME[:-5]:
        return None
    p = _path(sid)
    if not p.exists():
        return None
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        s.setdefault("messages", [])
        s.setdefault("todos", [])
        return s
    except Exception:
        return None


def _scan(force=False):
    """폴더를 훑어 (변경된 것만) 메타를 갱신하고 목록을 반환."""
    idx = _load_index()
    out = []
    seen = set()
    changed = False
    try:
        files = list(_dir().glob("*.json"))
    except Exception:
        return out
    for p in files:
        if p.name == INDEX_NAME:
            continue
        sid = p.stem
        if not _safe_id(sid) == sid:
            continue
        seen.add(sid)
        try:
            stamp = list(_stamp(p))
        except Exception:
            continue
        entry = idx.get(sid)
        if entry and entry.get("stamp") == stamp and not force:
            meta = entry.get("meta") or {}
        else:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                meta = _meta(d)
            except Exception:
                continue
            idx[sid] = {"stamp": stamp, "meta": meta}
            changed = True
        if not meta.get("id"):
            meta = dict(meta)
            meta["id"] = sid
        out.append(meta)
    # 사라진 파일 정리
    for sid in list(idx.keys()):
        if sid not in seen:
            idx.pop(sid, None)
            changed = True
    if changed:
        _flush_index(force=True)
    out.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return out


def list_sessions():
    with _LOCK:
        return _scan()


def search(query: str):
    q = (query or "").strip().lower()
    with _LOCK:
        metas = _scan()
    if not q:
        return metas
    hits = []
    for meta in metas:                       # 제목은 인덱스만으로 검색(빠름)
        if q in str(meta.get("title", "")).lower():
            hits.append(meta)
    if hits:
        return hits
    for meta in metas:                       # 본문 검색은 필요할 때만 파일을 연다
        d = load(meta.get("id"))
        if not d:
            continue
        for m in (d.get("messages") or []):
            if q in str(m.get("content", "")).lower():
                hit = dict(meta)
                hit["match"] = str(m.get("content", ""))[:160]
                hits.append(hit)
                break
    hits.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return hits


def delete(sid):
    sid = _safe_id(sid)
    p = _path(sid)
    ok = False
    if p.exists():
        try:
            p.unlink()
            ok = True
        except Exception:
            ok = False
    with _LOCK:
        idx = _load_index()
        if sid in idx:
            idx.pop(sid, None)
            _flush_index(force=True)
    return ok


def delete_all():
    n = 0
    for p in _dir().glob("*.json"):
        if p.name == INDEX_NAME:
            continue
        try:
            p.unlink()
            n += 1
        except Exception:
            pass
    with _LOCK:
        _load_index().clear()
        _flush_index(force=True)
    return n


def rename(sid, title):
    s = load(sid)
    if not s:
        return False
    s["title"] = str(title or "").strip()[:80] or s.get("title", "새 대화")
    save(s)
    return True


def set_todos(sid, todos):
    s = load(sid)
    if not s:
        return False
    s["todos"] = todos or []
    save(s)
    return True


def export_markdown(sid):
    s = load(sid)
    if not s:
        return None
    lines = [f"# {s.get('title', '대화')}", ""]
    lines.append(f"- 내보낸 시각: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 메시지: {len(s.get('messages') or [])}개")
    lines.append("")
    for m in s.get("messages") or []:
        role = m.get("role")
        who = {"user": "🧑 사용자", "assistant": "🤖 컴비서"}.get(role, role)
        lines.append(f"## {who}")
        lines.append("")
        lines.append(str(m.get("content") or ""))
        trace = m.get("trace") or []
        if trace:
            lines.append("")
            lines.append("<details><summary>도구 실행 기록</summary>")
            lines.append("")
            for t in trace:
                lines.append(f"- `{t.get('name')}` {json.dumps(t.get('args') or {}, ensure_ascii=False)}")
            lines.append("")
            lines.append("</details>")
        lines.append("")
    return "\n".join(lines)


def export_json(sid):
    s = load(sid)
    if not s:
        return None
    return json.dumps(s, ensure_ascii=False, indent=2)


def import_json(text):
    try:
        d = json.loads(text)
    except Exception as e:
        return None, f"JSON 오류: {e}"
    if not isinstance(d, dict) or "messages" not in d:
        return None, "올바른 세션 파일이 아닙니다."
    d.setdefault("title", "가져온 대화")
    d["id"] = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d.setdefault("created", time.time())
    d.setdefault("todos", [])
    save(d)
    return d, None
