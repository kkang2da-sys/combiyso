# -*- coding: utf-8 -*-
"""컴비서 로컬 웹 서버 (FastAPI)."""
import json
import queue as _queue
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)

from . import (agent, agents, attach, backup, commands, config, context, diag,
               discover, llm, mcp, pdftext, perf, permissions, runs, sessions,
               skills, sysres, vcs)
from .approval import BROKER
from .tools import APPROVAL_LABEL, TOOL_SPECS

app = FastAPI(title="컴비서", version=config.VERSION)


def ui_path() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "ui" / "index.html"
        if p.exists():
            return p
    return Path(__file__).resolve().parent / "ui" / "index.html"


def asset_path(name: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "assets" / name
        if p.exists():
            return p
    return Path(__file__).resolve().parent.parent / "assets" / name


def sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _reload_mcp_async(cfg):
    def run():
        try:
            mcp.MANAGER.reload(cfg.get("mcp_servers") or [])
        except Exception as e:
            diag.log_error("mcp.reload", e)
    threading.Thread(target=run, daemon=True).start()


def _cfg(effective=True):
    """설정 로드(+자동 최적화). 실패해도 기본값으로 계속."""
    try:
        c = config.load_config()
        return config.effective(c) if effective else c
    except Exception as e:
        diag.log_error("config.load", e)
        return dict(config.DEFAULTS)


@app.middleware("http")
async def _safe_middleware(request: Request, call_next):
    """어떤 API 오류도 서버를 죽이지 않고 JSON 으로 돌려준다."""
    try:
        return await call_next(request)
    except Exception as e:
        diag.log_error("http:" + request.url.path, e)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.on_event("startup")
def _startup():
    try:
        c = config.load_config()
    except Exception:
        c = dict(config.DEFAULTS)
    try:
        skills.ensure_examples()
    except Exception as e:
        diag.log_error("skills.ensure_examples", e)
    try:
        runs.sweep_startup()     # 지난번에 죽은 실행은 '중단됨'으로 표시
        runs.prune()
    except Exception as e:
        diag.log_error("runs.startup", e)
    try:
        perf.detect(force=True)          # 하드웨어 등급 캐시
        perf.gpu_info(wait=False)        # GPU/VRAM 은 백그라운드에서
        diag.log_event("startup", f"v{config.VERSION} · tier={perf.detect()['tier']}")
    except Exception as e:
        diag.log_error("perf.detect", e)
    _reload_mcp_async(c)


@app.on_event("shutdown")
def _shutdown():
    """서버가 정상 종료될 때 모델을 내려 메모리/VRAM 을 반납."""
    try:
        sysres.exit_cleanup()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 기본
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    p = ui_path()
    if p.exists():
        # UI 는 항상 새로 받는다(구버전 화면이 브라우저 캐시에 남는 문제 방지)
        return FileResponse(str(p), headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    return JSONResponse({"error": "index.html 없음", "path": str(p)}, status_code=500)


@app.get("/favicon.ico")
def favicon():
    p = asset_path("combiyso.ico")
    if p.exists():
        return FileResponse(str(p), media_type="image/x-icon")
    return Response(status_code=204)


@app.get("/api/health")
def health():
    s = perf.detect()
    return {"status": "ok", "name": config.APP_NAME, "version": config.VERSION,
            "tier": s.get("tier"), "mem_total_mb": s.get("mem_total_mb"),
            "mem_avail_mb": s.get("mem_avail_mb")}


@app.get("/api/meta")
def api_meta():
    return {
        "kind_order": permissions.KIND_ORDER,
        "kind_label": permissions.KIND_LABEL,
        "action_label": permissions.ACTION_LABEL,
        "presets": permissions.PRESETS,
        "approval_labels": APPROVAL_LABEL,
        "default_permissions": config.DEFAULT_PERMISSIONS,
        "tiers": perf.TIER_LABEL,
        "profiles": perf.PROFILES,
    }


@app.get("/api/tools")
def api_tools():
    cfg = _cfg()
    adef = agents.get_agent(cfg.get("agent") or "build")
    allowed = adef.get("tools") or "*"
    core = []
    for s in TOOL_SPECS:
        name = s["function"]["name"]
        core.append({"name": name, "description": s["function"]["description"],
                     "server": "내장", "allowed": agent._allows(allowed, name)})
    ext = []
    for full, server, t in mcp.MANAGER.all_tools():
        ext.append({"name": full, "description": (t.get("description") or "")[:200],
                    "server": server, "allowed": agent._allows(allowed, full)})
    return {"tools": core + ext, "agent": adef.get("id"), "approval_labels": APPROVAL_LABEL}


# --------------------------------------------------------------------------- #
# 설정
# --------------------------------------------------------------------------- #
def _config_payload():
    """UI 가 바로 쓸 수 있는 설정 + 시스템/성능 정보."""
    c = config.load_config()
    eff = config.effective(c)
    c["_config_path"] = str(config.CONFIG_PATH)
    c["_data_dir"] = str(config.data_dir())
    c["_version"] = config.VERSION
    total, avail = config.mem_info()
    live = perf.GOV.update()
    hw = perf.detect()
    tier = eff.get("_perf_tier") or hw.get("tier")
    c["_low_spec_active"] = bool(eff.get("_low_spec_active"))
    c["_mem_total_mb"] = live.get("total_mb") or total
    c["_mem_free_mb"] = live.get("avail_mb") or avail
    c["_perf"] = {
        **hw,
        "hw_tier": hw.get("tier"),
        "tier": tier,
        "tier_label": perf.TIER_LABEL.get(tier, tier),
        "tier_desc": perf.TIER_DESC.get(tier, ""),
        "applied": eff.get("_perf_applied") or {},
        "active": bool(eff.get("_low_spec_active")),
    }
    c["_perf_live"] = live
    c["_meta"] = api_meta()
    c["_agents"] = [{"id": a["id"], "description": a.get("description", ""),
                     "mode": a.get("mode"), "builtin": bool(a.get("builtin"))}
                    for a in agents.list_agents()]
    return c


@app.get("/api/config")
def get_config():
    return _config_payload()


@app.post("/api/config")
async def set_config(request: Request):
    body = await request.json()
    old = config.load_config()
    merged = config.save_config(body)
    if (old.get("mcp_servers") or []) != (merged.get("mcp_servers") or []):
        _reload_mcp_async(merged)
    return _config_payload()


@app.post("/api/config/reset")
def reset_config():
    c = config.reset_config()
    _reload_mcp_async(c)
    return _config_payload()


@app.post("/api/config/preset")
async def apply_preset(request: Request):
    body = await request.json()
    name = (body.get("preset") or "ask").strip()
    cfg = config.load_config()
    if name in permissions.PRESETS:
        cfg["permission_actions"] = dict(permissions.PRESETS[name])
        config.save_config(cfg)
    return _config_payload()


@app.get("/api/models")
def get_models(url: str = ""):
    cfg = _cfg(effective=False)
    try:
        models = llm.list_models(cfg, url or None)
        return {"ok": True, "models": models, "provider": cfg.get("provider"),
                "current": llm.active_model(cfg),
                "endpoint": llm.normalize_url(url) if url else llm.active_endpoint(cfg)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}


@app.post("/api/test")
async def test_conn(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = None
    # 빈/부분 설정이 들어와도 기존 설정을 덮어쓰지 않는다(부분 값은 병합).
    if isinstance(body, dict) and body:
        base = config.load_config()
        base.update({k: v for k, v in body.items() if not str(k).startswith("_")})
        config.save_config(base)
    cfg = _cfg(effective=False)
    ok, msg = llm.ping(cfg)
    return {"ok": ok, "message": msg}


# --------------------------------------------------------------------------- #
# 모델 서버 (다른 PC 의 Ollama 포함)
# --------------------------------------------------------------------------- #
@app.get("/api/servers")
def api_servers():
    cfg = config.load_config()
    local = llm.normalize_url("127.0.0.1:11434")
    lan = [f"http://{ip}:11434" for ip in perf.lan_addresses()]
    return {"servers": cfg.get("ollama_servers") or [],
            "active": llm.normalize_url(cfg.get("ollama_url")),
            "local": local, "lan": lan,
            "hint": llm.HOST_HINT}


@app.post("/api/ollama/probe")
async def api_ollama_probe(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = config.load_config()
    return llm.probe(cfg, str((body or {}).get("url") or "").strip() or None)


@app.get("/api/ollama")
def ollama_info(url: str = ""):
    cfg = config.load_config()
    try:
        installed = llm.ollama_tags(cfg, url or None)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": [], "running": []}
    return {"ok": True, "endpoint": llm.normalize_url(url) if url else llm.ollama_base(cfg),
            "models": installed, "running": llm.ollama_ps(cfg, url or None)}


@app.post("/api/ollama/pull")
async def ollama_pull(request: Request):
    body = await request.json()
    model = (body.get("model") or "").strip()
    url = (body.get("url") or "").strip() or None
    cfg = config.load_config()

    def gen():
        if not model:
            yield sse({"type": "error", "message": "모델 이름이 필요합니다."})
            yield sse({"type": "done"})
            return
        for p in llm.ollama_pull(cfg, model, url):
            yield sse({"type": "progress", **p})
        yield sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/api/ollama/delete")
async def ollama_delete(request: Request):
    body = await request.json()
    cfg = config.load_config()
    try:
        llm.ollama_delete(cfg, (body.get("model") or "").strip(),
                          (body.get("url") or "").strip() or None)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/ollama/unload")
async def ollama_unload(request: Request):
    """올라가 있는 모델을 메모리/VRAM 에서 내린다."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = config.load_config()
    model = str((body or {}).get("model") or "").strip()
    url = str((body or {}).get("url") or "").strip() or llm.ollama_base(cfg)
    if model:
        ok, err = llm.unload_model(cfg, url, model)
        return {"ok": ok, "error": err, "unloaded": [model] if ok else [],
                "freed_mb": 0}
    try:
        return sysres.unload_models(cfg, urls=[url])
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "unloaded": [], "freed_mb": 0}


# --------------------------------------------------------------------------- #
# LLM 자동 탐색 (v1.6.0) — 이 PC / 내부 네트워크 / API 키 서비스
# --------------------------------------------------------------------------- #
@app.get("/api/llm/local")
def api_llm_local(start: int = 0):
    """이 PC 의 Ollama 상태 + 모델. start=1 이면 꺼져 있을 때 자동으로 켠다."""
    cfg = config.load_config()
    return discover.local_info(cfg, start=bool(start))


@app.post("/api/llm/local/start")
def api_llm_local_start():
    """이 PC 의 Ollama 를 켠다(명시적 버튼)."""
    cfg = config.load_config()
    discover.invalidate()
    return discover.local_info(cfg, start=True)


@app.get("/api/llm/scan")
def api_llm_scan(refresh: int = 0, subnets: int = 0):
    """같은 네트워크(내부 IP)에서 Ollama 서버를 찾는다."""
    cfg = config.load_config()
    try:
        limit = int(subnets or cfg.get("lan_scan_subnets") or 3)
    except Exception:
        limit = 3
    return discover.scan_lan(cfg, refresh=bool(refresh), limit_subnets=limit)


@app.get("/api/llm/presets")
def api_llm_presets():
    """API 키로 쓸 수 있는 LLM 서비스 목록."""
    return {"presets": llm.PROVIDER_PRESETS}


@app.post("/api/llm/api-models")
async def api_llm_api_models(request: Request):
    """OpenAI 호환 서버의 모델 목록(키만 있으면 조회 가능)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    return llm.api_models(str(body.get("base_url") or ""),
                          str(body.get("api_key") or ""),
                          str(body.get("preset") or ""),
                          refresh=bool(body.get("refresh")))


@app.post("/api/llm/quick-connect")
async def api_llm_quick_connect(request: Request):
    """API 키만 넣으면 끝 — 서비스 자동 감지 → 모델 선택 → 저장 → 바로 사용."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    key = str(body.get("key") or "").strip()
    pid = str(body.get("preset") or "").strip()
    base = str(body.get("base_url") or "").strip().rstrip("/")
    name = str(body.get("name") or "").strip()
    if pid in ("", "auto"):
        pid = llm.detect_preset(key)
    p = llm.preset_by_id(pid) or {}
    base = base or str(p.get("base_url") or "").rstrip("/")
    if not key and not base:
        return {"ok": False, "error": "API 키를 입력하세요."}
    if not base:
        return {"ok": False,
                "error": "Base URL 이 없습니다. '직접 입력'에서 주소를 넣어 주세요."}

    res = llm.api_models(base, key, pid)
    models = list(res.get("models") or [])
    model = ""
    for m in (p.get("models") or []):
        if (not models) or (m in models):
            model = m
            break
    if not model:
        model = models[0] if models else ""

    cfg = config.load_config()
    patch = {"provider": "openai", "openai_base_url": base,
             "openai_api_key": key, "openai_preset": pid}
    if model:
        patch["openai_model"] = model
    trial = dict(cfg)
    trial.update(patch)
    ok, msg = llm.ping(trial)

    if ok:
        rows = [r for r in (cfg.get("openai_presets") or []) if r.get("id") != pid]
        rows.append({"id": pid, "name": name or p.get("name") or pid,
                     "base_url": base, "api_key": key, "model": model})
        cfg.update(patch)
        cfg["openai_presets"] = rows
        config.save_config(cfg)
    return {"ok": bool(ok), "preset": pid, "name": name or p.get("name") or pid,
            "base_url": base, "model": model, "models": models,
            "count": len(models), "source": res.get("source") or "",
            "note": res.get("note") or "", "message": msg,
            "key_hint": p.get("key_hint") or ""}


@app.post("/api/llm/use-model")
async def api_llm_use_model(request: Request):
    """모델만 바꿔서 즉시 적용(저장 버튼 불필요)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    model = str((body or {}).get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "모델명이 비어 있습니다."}
    cfg = config.load_config()
    if (cfg.get("provider") or "ollama") != "openai":
        return {"ok": False, "error": "API 모드가 아닙니다. 먼저 API 서비스를 연결하세요."}
    cfg["openai_model"] = model
    pid = cfg.get("openai_preset") or ""
    base = (cfg.get("openai_base_url") or "").rstrip("/")
    for r in (cfg.get("openai_presets") or []):
        if (r.get("id") == pid
                and str(r.get("base_url") or "").rstrip("/") == base):
            r["model"] = model
    cfg = config.save_config(cfg)
    return {"ok": True, "model": cfg.get("openai_model"),
            "provider": cfg.get("provider")}


# --------------------------------------------------------------------------- #
# 성능 / 자원 (v1.5.0)
# --------------------------------------------------------------------------- #
@app.get("/api/perf")
def api_perf():
    cfg = config.load_config()
    eff = config.effective(cfg)
    return {"sys": perf.detect(force=True), "live": perf.GOV.update(force=True),
            "tier": eff.get("_perf_tier"), "active": bool(eff.get("_low_spec_active")),
            "applied": eff.get("_perf_applied") or {},
            "process_mb": sysres.proc_mem_mb(),
            "profiles": perf.PROFILES, "labels": perf.TIER_LABEL}


@app.get("/api/resources")
def api_resources():
    cfg = config.load_config()
    live = perf.GOV.update(force=True)
    gpu = perf.gpu_info(wait=True)
    try:
        running = llm.ollama_ps_url(cfg, timeout=3)
    except Exception:
        running = []
    return {"live": live, "gpu": gpu, "process_mb": sysres.proc_mem_mb(),
            "tier": perf.resolve_tier(cfg), "ollama": running,
            "endpoint": llm.active_endpoint(cfg),
            "last_cleanup": sysres.last_report()}


@app.post("/api/resources/cleanup")
async def api_cleanup(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = config.load_config()
    try:
        rep = sysres.cleanup(cfg,
                             unload=bool((body or {}).get("unload", True)),
                             trim=bool((body or {}).get("trim", True)),
                             include_remote=bool((body or {}).get("remote", False)))
        return {"ok": True, **rep}
    except Exception as e:
        diag.log_error("resources.cleanup", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/logs")
def api_logs(n: int = 40):
    return {"path": str(diag.LOG_PATH), "text": diag.tail(n)}


# --------------------------------------------------------------------------- #
# 세션
# --------------------------------------------------------------------------- #
@app.get("/api/sessions")
def api_sessions(q: str = ""):
    arr = sessions.search(q) if q else sessions.list_sessions()
    active = {}
    for r in runs.list_runs(active_only=True):
        sid = r.get("session_id")
        if not sid:
            continue
        if sid not in active or r.get("updated", 0) > active[sid][1]:
            active[sid] = (r.get("id"), r.get("updated", 0), r.get("status"))
    for a in arr:
        info = active.get(a.get("id"))
        a["run_id"] = info[0] if info else None
        a["active"] = bool(info and info[2] == "running")
        a["resumable"] = bool(info and info[2] != "running")
    return arr


@app.post("/api/sessions")
def api_new_session():
    s = sessions.new_session()
    return {"id": s["id"], "title": s["title"]}


@app.get("/api/sessions/{sid}")
def api_get_session(sid: str):
    s = sessions.load(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    return s


@app.delete("/api/sessions/{sid}")
def api_del_session(sid: str):
    ok = sessions.delete(sid)
    attach.drop_all(sid)
    return {"ok": ok}


@app.delete("/api/sessions")
def api_del_all_sessions():
    n = sessions.delete_all()
    try:
        for d in config.ATTACH_DIR.iterdir():
            if d.is_dir():
                attach.drop_all(d.name)
    except Exception:
        pass
    return {"ok": True, "deleted": n}


@app.post("/api/sessions/{sid}/rename")
async def api_rename(sid: str, request: Request):
    body = await request.json()
    return {"ok": sessions.rename(sid, body.get("title"))}


@app.get("/api/sessions/{sid}/export")
def api_export(sid: str, format: str = "md"):
    s = sessions.load(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    title = str(s.get("title") or "대화").replace("/", "_").replace("\\", "_")[:60]
    if format == "json":
        data = sessions.export_json(sid) or "{}"
        fname, media = f"{title}.json", "application/json"
    else:
        data = sessions.export_markdown(sid) or ""
        fname, media = f"{title}.md", "text/markdown"
    return Response(content=data.encode("utf-8"), media_type=media + "; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename*=UTF-8''{quote(fname)}"})


@app.post("/api/sessions/import")
async def api_import(request: Request):
    body = await request.json()
    s, err = sessions.import_json(body.get("data") or "")
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "id": s["id"], "title": s.get("title")}


@app.get("/api/usage")
def api_usage(session_id: str = ""):
    cfg = _cfg()
    s = sessions.load(session_id) if session_id else None
    msgs = (s or {}).get("messages") or []
    info = context.usage_info([{"role": m.get("role"), "content": m.get("content")}
                               for m in msgs], cfg)
    info["messages"] = len(msgs)
    return info


# --------------------------------------------------------------------------- #
# 파일 탐색
# --------------------------------------------------------------------------- #
@app.get("/api/fs")
def api_fs(path: str = ""):
    try:
        if not path:
            drives = [str(Path(f"{c}:\\")) for c in "CDEFGH" if Path(f"{c}:\\").exists()]
            home = str(Path.home())
            return {"path": "", "parent": None, "dirs": [home] + drives, "root": True}
        p = Path(path).expanduser()
        if p.is_file():
            p = p.parent
        if not p.exists():
            return JSONResponse({"error": f"경로 없음: {p}"}, status_code=404)
        dirs = [str(c) for c in sorted(p.iterdir(), key=lambda x: x.name.lower()) if c.is_dir()]
        parent = str(p.parent) if p.parent != p else None
        return {"path": str(p), "parent": parent, "dirs": dirs[:400], "root": False}
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


# --------------------------------------------------------------------------- #
# 규칙 파일 / MCP / 백업
# --------------------------------------------------------------------------- #
def _rules_path(cfg):
    return Path(str(cfg.get("workspace") or "")) / (cfg.get("rules_file") or "COMPIYSO.md")


@app.get("/api/rules")
def get_rules():
    cfg = config.load_config()
    p = _rules_path(cfg)
    content = ""
    try:
        if p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return {"path": str(p), "exists": p.is_file(), "content": content,
            "enabled": bool(cfg.get("use_rules_file", True))}


@app.post("/api/rules")
async def set_rules(request: Request):
    body = await request.json()
    cfg = config.load_config()
    p = _rules_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            backup.backup(p, "rules")
        p.write_text(str(body.get("content") or ""), encoding="utf-8")
        return {"ok": True, "path": str(p)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/mcp")
def get_mcp():
    return {"servers": mcp.MANAGER.status()}


@app.post("/api/mcp/reload")
def reload_mcp():
    cfg = config.load_config()
    mcp.MANAGER.reload(cfg.get("mcp_servers") or [])
    return {"ok": True, "servers": mcp.MANAGER.status()}


@app.get("/api/backups")
def get_backups():
    return {"backups": backup.list_backups()}


@app.post("/api/backups/{bid}/restore")
def restore_backup(bid: str):
    ok, msg = backup.restore(bid)
    return {"ok": ok, "message": msg}


@app.delete("/api/backups/{bid}")
def del_backup(bid: str):
    return {"ok": backup.delete_backup(bid)}


@app.delete("/api/backups")
def clear_backups():
    return {"ok": True, "deleted": backup.clear_all()}


# --------------------------------------------------------------------------- #
# 체크포인트 (git 되돌리기)
# --------------------------------------------------------------------------- #
@app.get("/api/checkpoints")
def api_checkpoints():
    cfg = _cfg()
    return {"status": vcs.status(cfg), "checkpoints": vcs.checkpoints(cfg, 50)}


@app.post("/api/checkpoints")
async def api_checkpoint_make(request: Request):
    cfg = _cfg()
    try:
        body = await request.json()
    except Exception:
        body = {}
    info = vcs.make_checkpoint(cfg, str((body or {}).get("label") or ""))
    return {"ok": bool(info and info.get("ok")), "info": info}


@app.post("/api/checkpoints/restore")
async def api_checkpoint_restore(request: Request):
    cfg = _cfg()
    body = await request.json()
    ok, msg = vcs.restore(cfg, str((body or {}).get("sha") or ""))
    return {"ok": ok, "message": msg}


@app.delete("/api/checkpoints")
def api_checkpoints_clear():
    cfg = _cfg()
    return {"ok": vcs.clear(cfg)}


@app.get("/api/attachments/{sid}/{aid}")
def api_attachment(sid: str, aid: str):
    p = attach.path_of(sid, aid)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(p))


# --------------------------------------------------------------------------- #
# 스킬
# --------------------------------------------------------------------------- #
@app.get("/api/skills")
def api_skills():
    cfg = config.load_config()
    disabled = set(cfg.get("disabled_skills") or [])
    out = []
    for s in skills.list_skills():
        s = dict(s)
        s["enabled"] = s.get("id") not in disabled
        s["body"] = ""
        out.append(s)
    return {"skills": out, "dir": str(config.SKILLS_DIR),
            "enabled": bool(cfg.get("skills_enabled", True))}


@app.get("/api/skills/{sid}")
def api_skill(sid: str):
    s = skills.get_skill(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    return s


@app.post("/api/skills")
async def api_save_skill(request: Request):
    body = await request.json()
    try:
        s = skills.save_skill(body.get("id") or "", body.get("name") or "",
                              body.get("description") or "", body.get("body") or "")
        return {"ok": True, "skill": s}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.delete("/api/skills/{sid}")
def api_del_skill(sid: str):
    return {"ok": skills.delete_skill(sid)}


@app.post("/api/skills/{sid}/toggle")
async def api_toggle_skill(sid: str, request: Request):
    body = await request.json()
    cfg = config.load_config()
    dis = set(cfg.get("disabled_skills") or [])
    if body.get("enabled", True):
        dis.discard(sid)
    else:
        dis.add(sid)
    cfg["disabled_skills"] = sorted(dis)
    config.save_config(cfg)
    return {"ok": True, "disabled": cfg["disabled_skills"]}


# --------------------------------------------------------------------------- #
# 에이전트
# --------------------------------------------------------------------------- #
@app.get("/api/agents")
def api_agents():
    return {"agents": agents.list_agents()}


@app.get("/api/agents/{aid}")
def api_agent(aid: str):
    return agents.get_agent(aid)


@app.post("/api/agents")
async def api_save_agent(request: Request):
    body = await request.json()
    try:
        a = agents.save_agent(body.get("id") or "", body.get("description") or "",
                              body.get("mode") or "primary", body.get("tools") or "*",
                              body.get("body") or "", body.get("temperature"))
        return {"ok": True, "agent": a}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.delete("/api/agents/{aid}")
def api_del_agent(aid: str):
    return {"ok": agents.delete_agent(aid), "builtin_only": agents.is_builtin_only(aid)}


# --------------------------------------------------------------------------- #
# 명령
# --------------------------------------------------------------------------- #
@app.get("/api/commands")
def api_commands():
    return {"commands": commands.list_commands()}


@app.post("/api/commands")
async def api_save_command(request: Request):
    body = await request.json()
    try:
        c = commands.save_command(body.get("id") or "", body.get("description") or "",
                                  body.get("body") or "")
        return {"ok": True, "command": c}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.delete("/api/commands/{cid}")
def api_del_command(cid: str):
    return {"ok": commands.delete_command(cid)}


@app.get("/api/slash")
def api_slash():
    """입력창 '/' 자동완성 목록."""
    cfg = config.load_config()
    out = []
    for c in commands.list_commands():
        if c.get("kind") in ("prompt", "local"):
            out.append({"kind": "command", "id": c["id"], "description": c.get("description", ""),
                        "builtin": bool(c.get("builtin"))})
    if cfg.get("skills_enabled", True):
        disabled = set(cfg.get("disabled_skills") or [])
        for s in skills.list_skills():
            if s.get("id") in disabled:
                continue
            out.append({"kind": "skill", "id": s.get("id"),
                        "description": s.get("description", ""), "builtin": False})
    return {"items": out}


# --------------------------------------------------------------------------- #
# 승인 / 중지
# --------------------------------------------------------------------------- #
@app.post("/api/approve")
async def api_approve(request: Request):
    body = await request.json()
    ok = BROKER.resolve(body.get("id"), body.get("decision", "deny"), bool(body.get("remember")))
    return {"ok": ok}


@app.post("/api/stop")
async def api_stop(request: Request):
    body = await request.json()
    agent.request_stop(body.get("session_id"))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 채팅(에이전트)
# --------------------------------------------------------------------------- #
def _local_reply(text):
    return sse({"type": "local", "text": text})


def _cmd_help():
    lines = ["## 사용 가능한 명령", ""]
    for c in commands.list_commands():
        lines.append(f"- `/{c['id']}` — {c.get('description') or ''}")
    sl = skills.list_skills()
    lines += ["", "## 사용 가능한 스킬", ""]
    if sl:
        for s in sl:
            lines.append(f"- `/{s.get('id')}` — {s.get('name')}: {(s.get('description') or '')[:120]}")
    else:
        lines.append("(없음 — 설정 → 스킬에서 추가)")
    lines += ["", "명령은 입력창에 `/` 를 치면 자동완성됩니다."]
    return "\n".join(lines)


def _cmd_tools(cfg):
    adef = agents.get_agent(cfg.get("agent") or "build")
    allowed = adef.get("tools") or "*"
    lines = [f"## 사용 가능한 도구 (에이전트: {adef.get('id')})", ""]
    for s in TOOL_SPECS:
        n = s["function"]["name"]
        lines.append(f"- `{n}` {'✅' if agent._allows(allowed, n) else '⛔'} — "
                     f"{s['function']['description']}")
    ext = mcp.MANAGER.all_tools()
    if ext:
        lines += ["", "## MCP 도구", ""]
        for full, server, t in ext:
            lines.append(f"- `{full}` [{server}] — {(t.get('description') or '')[:120]}")
    return "\n".join(lines)


def _cmd_skills(cfg):
    out = ["## 사용 가능한 스킬", ""]
    disabled = set(cfg.get("disabled_skills") or [])
    for s in skills.list_skills():
        mark = "⛔" if s.get("id") in disabled else "✅"
        out.append(f"{mark} `/{s.get('id')}` — **{s.get('name')}**: {s.get('description')}")
    if len(out) == 2:
        out.append("(없음 — 설정 → 스킬에서 추가하세요)")
    return "\n".join(out)


def _cmd_compact(cfg, s):
    msgs = [{"role": "system", "content": "요약용"}]
    for m in s.get("messages") or []:
        if m.get("role") in ("user", "assistant"):
            msgs.append({"role": m["role"], "content": str(m.get("content") or "")})
    if len(msgs) < 4:
        return None, "요약할 내용이 부족합니다."
    force = dict(cfg)
    force.update({"auto_compact": True, "compact_threshold": 1,
                  "keep_recent_messages": max(2, int(cfg.get("keep_recent_messages") or 8))})
    new_msgs, info = context.maybe_compact(force, msgs, lambda m: llm.complete(cfg, m))
    if not info:
        return None, "압축할 내용이 없습니다."
    s["messages"] = [{"role": m.get("role"), "content": m.get("content"), "ts": time.time()}
                     for m in new_msgs if m.get("role") in ("user", "assistant")]
    sessions.save(s)
    return info, (f"컨텍스트를 압축했습니다. (약 {info['before']:,} → {info['after']:,} 토큰, "
                  f"{info['removed']}개 메시지 요약)")


def _cmd_undo(cfg=None):
    cfg = cfg or config.load_config()
    if vcs.enabled(cfg):
        rows = vcs.checkpoints(cfg, 1)
        if rows:
            ok, msg = vcs.restore(cfg, rows[0]["sha"])
            return rows[0], (("🔙 " + msg) if ok else ("실패: " + msg))
    items = backup.list_backups(1)
    if not items:
        return None, "되돌릴 체크포인트/백업이 없습니다."
    ok, msg = backup.restore(items[0]["id"])
    return items[0], (("🔙 " + msg) if ok else ("실패: " + msg))


# --------------------------------------------------------------------------- #
# 실행 저널 / 중단 복구
# --------------------------------------------------------------------------- #
@app.get("/api/runs")
def api_runs(all: int = 0):
    now = time.time()
    out = []
    for r in runs.list_runs(active_only=not bool(all)):
        st = r.get("status")
        out.append({
            "id": r.get("id"), "session_id": r.get("session_id"),
            "agent": r.get("agent"), "status": st,
            "step": r.get("step", 0), "phase": r.get("phase", ""),
            "detail": r.get("detail", ""), "started": r.get("started"),
            "updated": r.get("updated"), "error": r.get("error", ""),
            "note": r.get("note", ""), "stats": r.get("stats", {}),
            "resumable": st in runs.RESUMABLE and bool((r.get("state") or {}).get("messages")),
            "active": st == "running",
            "age": round(now - (r.get("updated") or now), 1),
        })
    return {"runs": out, "free_mb": runs.mem_free_mb()}


@app.post("/api/runs/discard")
async def api_discard(request: Request):
    body = await request.json()
    rid = body.get("run_id")
    if not rid:
        rec = runs.active_for_session(body.get("session_id"))
        rid = (rec or {}).get("id")
    return {"ok": runs.discard(rid) if rid else False}


# --------------------------------------------------------------------------- #
# 에이전트 실행 스트림 (워커 스레드 + 큐 → SSE, 하트비트 포함)
# --------------------------------------------------------------------------- #
def _agent_worker(cfg, s, history, agent_id, todos, run_id, q):
    sid = s["id"]
    final_text, trace, todos2 = None, [], list(todos or [])

    def checkpoint(state):
        runs.save_state(run_id, status="running", step=state.get("step"),
                        phase=state.get("phase"), detail=state.get("detail"),
                        todos=state.get("todos"), messages=state.get("messages"),
                        stats=state.get("stats"))

    ok_final = False
    try:
        for ev in agent.run_agent(cfg, history, session_id=sid, agent_id=agent_id,
                                  todos=todos, checkpoint=checkpoint):
            t = ev.get("type")
            if t == "final":
                final_text = ev.get("text", "")
                trace = ev.get("trace") or []
                todos2 = ev.get("todos") or todos2
                ok_final = True
            elif t == "todos":
                todos2 = ev.get("todos") or todos2
                s["todos"] = todos2
                sessions.save(s)
            q.put(ev)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        q.put({"type": "error", "message": msg})
        runs.finish(run_id, "error", msg)
    finally:
        if ok_final:
            s["messages"].append({"role": "assistant", "content": final_text or "",
                                  "ts": time.time(), "trace": trace})
            s["todos"] = todos2
            sessions.save(s)
            runs.finish(run_id, "done")
            q.put({"type": "saved", "title": s["title"], "count": len(s["messages"])})
        else:
            rec = runs.get(run_id)
            if rec and rec.get("status") == "running":
                runs.finish(run_id, "interrupted", "작업이 중단되었습니다.")
        q.put(None)


def _run_stream(s, history, cfg, agent_id, todos, run_id):
    sid = s["id"]
    q = _queue.Queue()
    t0 = time.time()
    threading.Thread(target=_agent_worker,
                     args=(cfg, s, history, agent_id, todos, run_id, q),
                     daemon=True).start()

    def gen():
        yield sse({"type": "session", "id": sid, "title": s["title"]})
        yield sse({"type": "agent", "id": agent_id})
        yield sse({"type": "run", "run_id": run_id, "phase": "시작",
                   "elapsed": 0, "free_mb": runs.mem_free_mb()})
        while True:
            try:
                ev = q.get(timeout=4.0)
            except _queue.Empty:
                live = perf.GOV.update()
                yield sse({"type": "ping", "t": time.time(),
                           "elapsed": round(time.time() - t0, 1),
                           "free_mb": live.get("avail_mb") or runs.mem_free_mb(),
                           "pressure": live.get("pressure", 0),
                           "pressure_label": live.get("pressure_label", ""),
                           "tier": perf.resolve_tier(cfg)})
                continue
            if ev is None:
                break
            if isinstance(ev, dict) and ev.get("type") == "ping":
                ev.update({"pressure": perf.GOV.snapshot().get("pressure", 0),
                           "avail_mb": perf.GOV.snapshot().get("avail_mb")})
            yield sse(ev)
        yield sse({"type": "done", "run_id": run_id})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/resume")
async def api_resume(request: Request):
    body = await request.json()
    rid = body.get("run_id")
    rec = runs.get(rid) if rid else runs.active_for_session(body.get("session_id"))
    if not rec:
        return JSONResponse({"ok": False, "error": "이어서 할 작업이 없습니다."},
                            status_code=404)
    if rec.get("status") == "running":
        return JSONResponse({"ok": False, "error": "아직 실행 중인 작업입니다."},
                            status_code=409)
    msgs = runs.repair_messages((rec.get("state") or {}).get("messages") or [])
    if not msgs:
        return JSONResponse({"ok": False, "error": "저장된 진행 내용이 없습니다."},
                            status_code=404)
    s = sessions.load(rec.get("session_id"))
    if not s:
        return JSONResponse({"ok": False, "error": "세션을 찾을 수 없습니다."},
                            status_code=404)
    cfg = _cfg()
    agent_id = rec.get("agent") or cfg.get("agent") or "build"
    runs.save_state(rec["id"], status="running", phase="이어서", detail="")
    return _run_stream(s, msgs, cfg, agent_id, s.get("todos") or [], rec["id"])


@app.post("/api/chat")
async def api_chat(request: Request):
    body = await request.json()
    sid = body.get("session_id")
    text = (body.get("message") or "").strip()
    attachments = body.get("attachments") or []
    cfg = _cfg()

    s = sessions.load(sid) if sid else None
    if not s:
        s = sessions.new_session()

    # ---------------- 슬래시 명령 ----------------
    expanded = text
    slash = None
    if text.startswith("/") and cfg.get("commands_enabled", True):
        slash = commands.expand(text, skills=skills.list_skills())

    def finish_local(answer, extra_events=()):
        s["messages"].append({"role": "user", "content": text, "ts": time.time()})
        if s.get("title") in ("", "새 대화"):
            s["title"] = text[:40]
        s["messages"].append({"role": "assistant", "content": answer, "ts": time.time(),
                              "trace": []})
        sessions.save(s)

        def gen():
            yield sse({"type": "session", "id": s["id"], "title": s["title"]})
            for ev in extra_events:
                yield sse(ev)
            yield sse({"type": "local", "text": answer})
            yield sse({"type": "final", "text": answer, "trace": []})
            yield sse({"type": "saved", "title": s["title"], "count": len(s["messages"])})
            yield sse({"type": "done"})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    if slash and slash.get("kind") == "local":
        cid = slash["id"]
        if cid == "help":
            return finish_local(_cmd_help())
        if cid == "tools":
            return finish_local(_cmd_tools(cfg))
        if cid == "skills":
            return finish_local(_cmd_skills(cfg))
        if cid == "compact":
            info, answer = _cmd_compact(cfg, s)
            return finish_local(answer, [{"type": "compact", "info": info}] if info else [])
        if cid == "undo":
            rec, answer = _cmd_undo(cfg)
            return finish_local(answer)
    elif slash and slash.get("kind") in ("prompt", "skill"):
        expanded = slash["prompt"]

    # ---------------- 첨부 (이미지 / PDF / 텍스트) ----------------
    attach_refs = []
    if attachments and cfg.get("attachments_enabled", True):
        blocks = []
        max_mb = int(cfg.get("attach_max_upload_mb") or 20)
        for a in attachments[:8]:
            name = str(a.get("name") or "첨부")
            kind = str(a.get("kind") or "").lower()
            mime = str(a.get("mime") or "")
            if kind == "image":
                data = attach.decode_payload(a.get("data"))
                if not data:
                    continue
                if len(data) > max_mb * 1024 * 1024:
                    blocks.append(f"[첨부 이미지: {name}] — 파일이 너무 큽니다"
                                  f"({len(data) // 1048576}MB, 최대 {max_mb}MB)")
                    continue
                ref = attach.save_bytes(s["id"], name, data, mime or "image/png", "image")
                if ref:
                    attach_refs.append(ref)
                blocks.append(f"[첨부 이미지: {name}] ({len(data) // 1024}KB)")
            elif kind == "pdf":
                data = attach.decode_payload(a.get("data"))
                if not data:
                    continue
                if len(data) > max_mb * 1024 * 1024:
                    blocks.append(f"[첨부 PDF: {name}] — 파일이 너무 큽니다"
                                  f"({len(data) // 1048576}MB, 최대 {max_mb}MB)")
                    continue
                txt = pdftext.extract(data, int(cfg.get("pdf_max_pages") or 50),
                                      int(cfg.get("pdf_max_chars") or 60000))
                ref = attach.save_bytes(s["id"], name, data, "application/pdf", "pdf")
                if ref:
                    attach_refs.append(ref)
                if txt:
                    blocks.append(f"[첨부 PDF: {name}]\n```\n{txt}\n```")
                else:
                    blocks.append(f"[첨부 PDF: {name}] — 텍스트를 추출하지 못했습니다. "
                                  f"스캔 이미지 PDF라면 페이지를 이미지로 첨부해 주세요.")
            else:
                content = str(a.get("content") or "")[:60000]
                blocks.append(f"[첨부 파일: {name}]\n```\n{content}\n```")
        if blocks:
            expanded = (expanded + "\n\n" + "\n\n".join(blocks)).strip()

    if text or attach_refs:
        title_seed = text.splitlines()[0] if text else (
            attach_refs[0].get("name") if attach_refs else "새 대화")
        item = {"role": "user", "content": text, "ts": time.time()}
        if attach_refs:
            item["attachments"] = attach_refs
        s["messages"].append(item)
        if s.get("title") in ("", "새 대화"):
            s["title"] = str(title_seed)[:40]
    sessions.save(s)
    sid = s["id"]

    # 이력: 첨부 이미지는 최근 image_history_turns 개 사용자 메시지만 모델에 보낸다
    turns = max(1, int(cfg.get("image_history_turns") or 1))
    history = []
    for m in s["messages"]:
        if m.get("role") not in ("user", "assistant"):
            continue
        history.append({"role": m.get("role"), "content": m.get("content"),
                        "_att": m.get("attachments") or []})
    if expanded != text and history:
        history[-1] = dict(history[-1])
        history[-1]["content"] = expanded
    if cfg.get("attachments_enabled", True):
        seen = 0
        for h in reversed(history):
            if h.get("role") != "user":
                continue
            refs = h.get("_att") or []
            if not any(str(r.get("kind")) == "image" for r in refs):
                continue
            seen += 1
            if seen > turns:
                break
            imgs = attach.load_images(sid, refs)
            if imgs:
                h["images"] = imgs
    for h in history:
        h.pop("_att", None)

    agent_id = cfg.get("agent") or "build"
    run = runs.start(sid, agent_id, note=text[:80])
    return _run_stream(s, history, cfg, agent_id, s.get("todos") or [], run["id"])
