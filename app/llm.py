# -*- coding: utf-8 -*-
"""LLM 백엔드: OpenAI 호환 API + Ollama(로컬/다른 PC).

chat_stream(cfg, messages, tools=None) 은
  {"delta": "..."}  를 여러 번 내보내고
  마지막에 {"final": True, "text": 전체텍스트, "tool_calls": [...]} 를 내보낸다.

complete(cfg, messages) 는 비스트리밍으로 전체 텍스트를 반환한다(요약용).

v1.5.0: `ollama_url` 에 **다른 PC 의 주소**(예: 192.168.0.10:11434)를 넣어
원격 Ollama 를 그대로 쓸 수 있다. 주소는 자동 정규화되고, 연결 실패 시
무엇을 확인해야 하는지 한국어로 안내한다.

v1.6.0: `PROVIDER_PRESETS`(Gemini·OpenAI·OpenRouter·Groq 등) + `api_models()`
— API 키만으로 여러 LLM 서비스를 쓸 수 있다(모두 OpenAI 호환 경로).
"""
import json
import threading
import time

from . import perf
from .net import session as _session

HOST_HINT = (
    "다른 PC 의 Ollama 를 쓰려면 그 PC 에서 Ollama 를 "
    "`OLLAMA_HOST=0.0.0.0` 으로 실행하고 11434 포트를 방화벽에서 허용해야 합니다."
)

# --------------------------------------------------------------------------- #
# API 키로 쓰는 LLM 서비스 (모두 OpenAI 호환 /chat/completions)
# --------------------------------------------------------------------------- #
# `models` 는 서버에서 목록을 못 받아올 때 쓰는 기본값이다.
PROVIDER_PRESETS = [
    {"id": "gemini", "name": "Google Gemini", "group": "API 키",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "key_hint": "AIza...", "keys_url": "https://aistudio.google.com/apikey",
     "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
                "gemini-2.0-flash"],
     "note": "Google AI Studio 에서 무료 키 발급 가능"},
    {"id": "openai", "name": "OpenAI", "group": "API 키",
     "base_url": "https://api.openai.com/v1",
     "key_hint": "sk-...", "keys_url": "https://platform.openai.com/api-keys",
     "models": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o4-mini"],
     "note": ""},
    {"id": "openrouter", "name": "OpenRouter (여러 모델 통합)", "group": "API 키",
     "base_url": "https://openrouter.ai/api/v1",
     "key_hint": "sk-or-...", "keys_url": "https://openrouter.ai/keys",
     "models": [], "note": "Claude·GPT·Gemini·Llama 등을 한 키로"},
    {"id": "groq", "name": "Groq (초고속)", "group": "API 키",
     "base_url": "https://api.groq.com/openai/v1",
     "key_hint": "gsk_...", "keys_url": "https://console.groq.com/keys",
     "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                "openai/gpt-oss-120b", "qwen/qwen3-32b"],
     "note": "무료 한도가 넉넉한 편"},
    {"id": "together", "name": "Together AI", "group": "API 키",
     "base_url": "https://api.together.xyz/v1",
     "key_hint": "…", "keys_url": "https://api.together.ai/settings/api-keys",
     "models": [], "note": ""},
    {"id": "deepseek", "name": "DeepSeek", "group": "API 키",
     "base_url": "https://api.deepseek.com/v1",
     "key_hint": "sk-...", "keys_url": "https://platform.deepseek.com/api_keys",
     "models": ["deepseek-chat", "deepseek-reasoner"], "note": "가성비 좋음"},
    {"id": "xai", "name": "xAI Grok", "group": "API 키",
     "base_url": "https://api.x.ai/v1",
     "key_hint": "xai-...", "keys_url": "https://console.x.ai",
     "models": ["grok-4", "grok-3", "grok-3-mini"], "note": ""},
    {"id": "mistral", "name": "Mistral", "group": "API 키",
     "base_url": "https://api.mistral.ai/v1",
     "key_hint": "…", "keys_url": "https://console.mistral.ai/api-keys",
     "models": ["mistral-large-latest", "mistral-small-latest",
                "codestral-latest"], "note": ""},
    {"id": "cerebras", "name": "Cerebras", "group": "API 키",
     "base_url": "https://api.cerebras.ai/v1",
     "key_hint": "csk-...", "keys_url": "https://cloud.cerebras.ai",
     "models": ["llama-3.3-70b", "qwen-3-32b"], "note": "매우 빠름"},
    {"id": "fireworks", "name": "Fireworks AI", "group": "API 키",
     "base_url": "https://api.fireworks.ai/inference/v1",
     "key_hint": "fw_...", "keys_url": "https://fireworks.ai/account/api-keys",
     "models": [], "note": ""},
    {"id": "anthropic", "name": "Anthropic Claude (OpenAI 호환)", "group": "API 키",
     "base_url": "https://api.anthropic.com/v1",
     "key_hint": "sk-ant-...", "keys_url": "https://console.anthropic.com/settings/keys",
     "models": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
     "note": "Anthropic 의 OpenAI 호환 엔드포인트"},
    {"id": "lmstudio", "name": "LM Studio (내 PC)", "group": "로컬",
     "base_url": "http://127.0.0.1:1234/v1",
     "key_hint": "", "keys_url": "",
     "models": [], "note": "LM Studio 에서 서버를 켜야 함"},
    {"id": "vllm", "name": "vLLM / llama.cpp 서버", "group": "로컬",
     "base_url": "http://127.0.0.1:8000/v1",
     "key_hint": "", "keys_url": "",
     "models": [], "note": "직접 띄운 OpenAI 호환 서버"},
    {"id": "ollama_openai", "name": "Ollama (OpenAI 호환 경로)", "group": "로컬",
     "base_url": "http://127.0.0.1:11434/v1",
     "key_hint": "", "keys_url": "",
     "models": [], "note": "Ollama 의 /v1 엔드포인트"},
    {"id": "custom", "name": "직접 입력", "group": "기타",
     "base_url": "", "key_hint": "", "keys_url": "", "models": [], "note": ""},

    # ---- v1.6.1 추가 서비스 ----
    {"id": "cohere", "name": "Cohere (Command)", "group": "유료 API",
     "base_url": "https://api.cohere.ai/compatibility/v1",
     "key_hint": "…", "keys_url": "https://dashboard.cohere.com/api-keys",
     "models": ["command-a-03-2025", "command-r-plus-08-2024"],
     "note": "OpenAI 호환 경로 · 한국어 준수"},
    {"id": "perplexity", "name": "Perplexity (검색형)", "group": "유료 API",
     "base_url": "https://api.perplexity.ai",
     "key_hint": "pplx-...", "keys_url": "https://www.perplexity.ai/settings/api",
     "models": ["sonar-pro", "sonar"],
     "note": "웹 검색 내장 · 도구 호출은 지원하지 않음(대화 전용)"},
    {"id": "moonshot", "name": "Moonshot · Kimi", "group": "유료 API",
     "base_url": "https://api.moonshot.ai/v1",
     "key_hint": "sk-...", "keys_url": "https://platform.moonshot.ai/console/api-keys",
     "models": ["kimi-k2-0905-preview", "moonshot-v1-128k"],
     "note": "긴 문맥(128k) · 중국"},
    {"id": "zhipu", "name": "Zhipu GLM", "group": "유료 API",
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "key_hint": "…", "keys_url": "https://open.bigmodel.cn/usercenter/apikeys",
     "models": ["glm-4.6", "glm-4.5"],
     "note": "가성비 좋은 중국 모델"},
    {"id": "dashscope", "name": "Qwen (알리바바 DashScope)", "group": "유료 API",
     "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
     "key_hint": "sk-...", "keys_url": "https://bailian.console.alibabacloud.com",
     "models": ["qwen3-max", "qwen3-coder-plus", "qwen-plus"],
     "note": "Qwen3 계열 · 코딩 강함"},
    {"id": "upstage", "name": "Upstage Solar (한국어)", "group": "무료 한도",
     "base_url": "https://api.upstage.ai/v1",
     "key_hint": "up-...", "keys_url": "https://console.upstage.ai/api-keys",
     "models": ["solar-pro2", "solar-mini"],
     "note": "한국어 특화 · 무료 크레딧"},
    {"id": "ai21", "name": "AI21 (Jamba)", "group": "유료 API",
     "base_url": "https://api.ai21.com/studio/v1",
     "key_hint": "…", "keys_url": "https://studio.ai21.com/account/api-key",
     "models": ["jamba-large-1.7"], "note": ""},
    {"id": "nvidia", "name": "NVIDIA NIM", "group": "무료 한도",
     "base_url": "https://integrate.api.nvidia.com/v1",
     "key_hint": "nvapi-...", "keys_url": "https://build.nvidia.com",
     "models": ["meta/llama-3.3-70b-instruct", "qwen/qwen3-235b-a22b"],
     "note": "무료 크레딧 제공"},
    {"id": "sambanova", "name": "SambaNova (초고속)", "group": "무료 한도",
     "base_url": "https://api.sambanova.ai/v1",
     "key_hint": "…", "keys_url": "https://cloud.sambanova.ai/apis",
     "models": ["Meta-Llama-3.3-70B-Instruct"], "note": "토큰/초 매우 빠름"},
    {"id": "hyperbolic", "name": "Hyperbolic", "group": "유료 API",
     "base_url": "https://api.hyperbolic.xyz/v1",
     "key_hint": "…", "keys_url": "https://app.hyperbolic.xyz/settings",
     "models": ["meta-llama/Llama-3.3-70B-Instruct"], "note": "저가 오픈모델"},
    {"id": "novita", "name": "Novita AI", "group": "유료 API",
     "base_url": "https://api.novita.ai/v3/openai",
     "key_hint": "sk_...", "keys_url": "https://novita.ai/settings/key-management",
     "models": [], "note": "저가 · 모델 목록에서 선택"},
    {"id": "nebius", "name": "Nebius AI Studio", "group": "유료 API",
     "base_url": "https://api.studio.nebius.ai/v1",
     "key_hint": "…", "keys_url": "https://studio.nebius.ai/settings/api-keys",
     "models": [], "note": ""},
    {"id": "deepinfra", "name": "DeepInfra", "group": "유료 API",
     "base_url": "https://api.deepinfra.com/v1/openai",
     "key_hint": "…", "keys_url": "https://deepinfra.com/dash/api_keys",
     "models": [], "note": "저가 오픈모델"},
    {"id": "github", "name": "GitHub Models", "group": "무료 한도",
     "base_url": "https://models.github.ai/inference",
     "key_hint": "ghp_... / github_pat_...",
     "keys_url": "https://github.com/settings/tokens",
     "models": ["openai/gpt-4.1", "openai/gpt-4o-mini"],
     "note": "GitHub 토큰으로 무료 한도 사용"},
    {"id": "llama_cpp", "name": "llama.cpp 서버 (내 PC)", "group": "로컬",
     "base_url": "http://127.0.0.1:8080/v1",
     "key_hint": "", "keys_url": "", "models": [],
     "note": "llama-server --host 0.0.0.0 --port 8080"},
    {"id": "oobabooga", "name": "text-generation-webui (내 PC)", "group": "로컬",
     "base_url": "http://127.0.0.1:5000/v1",
     "key_hint": "", "keys_url": "", "models": [], "note": ""},
]

# 보기 좋게 그룹 정리 (무료 한도가 있는 서비스)
_FREE_IDS = {"gemini", "groq", "cerebras", "github", "nvidia", "sambanova", "upstage"}
for _p in PROVIDER_PRESETS:
    if _p["group"] == "API 키":
        _p["group"] = "무료 한도" if _p["id"] in _FREE_IDS else "유료 API"

# API 키 접두사로 서비스를 자동 감지한다(키만 붙여넣으면 바로 연결).
KEY_PREFIXES = [
    ("sk-ant-", "anthropic"),
    ("sk-or-", "openrouter"),
    ("sk-proj-", "openai"),
    ("AIza", "gemini"),
    ("gsk_", "groq"),
    ("xai-", "xai"),
    ("csk-", "cerebras"),
    ("fw_", "fireworks"),
    ("nvapi-", "nvidia"),
    ("pplx-", "perplexity"),
    ("up-", "upstage"),
    ("github_pat_", "github"),
    ("ghp_", "github"),
    ("sk_", "novita"),
    ("sk-", "openai"),
]


def detect_preset(api_key: str) -> str:
    """API 키 모양으로 서비스를 추측한다(못 알아보면 openai)."""
    k = str(api_key or "").strip()
    for prefix, pid in KEY_PREFIXES:
        if k.startswith(prefix):
            return pid
    return "openai"


def preset_by_id(pid: str):
    pid = str(pid or "").strip().lower()
    for p in PROVIDER_PRESETS:
        if p["id"] == pid:
            return p
    return None


_MODELS_CACHE = {}
_MODELS_LOCK = threading.Lock()


def _err_brief(text, limit: int = 140) -> str:
    """오류 응답을 한 줄로 요약(JSON 이면 message 만)."""
    s = str(text or "")
    try:
        obj = json.loads(s)
        if isinstance(obj, list) and obj:
            obj = obj[0]
        if isinstance(obj, dict):
            e = obj.get("error") if isinstance(obj.get("error"), dict) else obj
            msg = e.get("message") or e.get("detail") or ""
            if msg:
                return str(msg)[:limit]
    except Exception:
        pass
    return " ".join(s.split())[:limit]


def api_models(base_url: str, api_key: str = "", preset_id: str = "",
               timeout: float = 12.0, refresh: bool = False) -> dict:
    """모델 목록(60초 캐시). 실제 조회는 `_api_models_impl`."""
    ck = (str(base_url or "").rstrip("/"), str(api_key or "")[-8:], str(preset_id or ""))
    if not refresh:
        with _MODELS_LOCK:
            hit = _MODELS_CACHE.get(ck)
        if hit and (time.time() - hit[0]) < 60.0:
            return hit[1]
    out = _api_models_impl(base_url, api_key, preset_id, timeout)
    if out.get("ok"):
        with _MODELS_LOCK:
            if len(_MODELS_CACHE) > 40:
                _MODELS_CACHE.clear()
            _MODELS_CACHE[ck] = (time.time(), out)
    return out


def _api_models_impl(base_url: str, api_key: str = "", preset_id: str = "",
                     timeout: float = 12.0) -> dict:
    """OpenAI 호환 서버의 모델 목록. 실패하면 프리셋 기본 목록으로 대체.

    반환: {ok, models:[str], source:"api"|"preset", note, error, status}
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "models": [], "source": "", "note": "", "status": 0,
                "error": "Base URL 이 비어 있습니다."}
    headers = {}
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    err, status = "", 0
    try:
        r = _session().get(base + "/models", headers=headers, timeout=(6, timeout))
        status = r.status_code
        if r.status_code < 400:
            data = r.json() or {}
            rows = data.get("data") or data.get("models") or []
            ids = [str(m.get("id") or m.get("name") or "") for m in rows
                   if isinstance(m, dict)]
            ids = sorted({i for i in ids if i})
            if ids:
                return {"ok": True, "models": ids, "source": "api", "status": status,
                        "note": f"{len(ids)}개 모델을 서버에서 받았습니다.", "error": ""}
            err = "서버가 빈 목록을 돌려줬습니다."
        else:
            err = f"HTTP {r.status_code}: {_err_brief(r.text, 90)}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    p = preset_by_id(preset_id)
    if p is None:                      # 프리셋 id 가 없어도 주소로 찾아본다
        for x in PROVIDER_PRESETS:
            if str(x.get("base_url") or "").rstrip("/") == base:
                p = x
                break
    fallback = list((p or {}).get("models") or [])
    if fallback:
        why = f"({err.splitlines()[0][:90]}) " if err else ""
        return {"ok": True, "models": fallback, "source": "preset", "status": status,
                "note": f"목록 API 를 못 써서 기본 목록을 표시합니다. {why}직접 입력해도 됩니다.",
                "error": err}
    return {"ok": False, "models": [], "source": "", "status": status, "note": "",
            "error": err}


def _probe_chat(cfg: dict, timeout: float = 20.0) -> tuple:
    """최소 요청(max_tokens=1)으로 키/엔드포인트가 실제로 동작하는지 확인한다."""
    url = (cfg.get("openai_base_url") or "").rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("openai_api_key"):
        headers["Authorization"] = "Bearer " + cfg["openai_api_key"]
    body = {"model": active_model(cfg), "stream": False, "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}
    r = _session().post(url, headers=headers, json=body, timeout=(8, timeout))
    return r.status_code, r.text[:240]


class LLMError(Exception):
    pass


# --------------------------------------------------------------------------- #
# 주소 / 공통
# --------------------------------------------------------------------------- #
def normalize_url(url, default_port: int = 11434) -> str:
    return perf.normalize_url(url, default_port)


def ollama_base(cfg: dict) -> str:
    return normalize_url((cfg or {}).get("ollama_url") or "127.0.0.1:11434")


def ollama_headers(cfg: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    key = str((cfg or {}).get("ollama_api_key") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _connect_timeout(cfg: dict) -> float:
    try:
        return max(2.0, min(120.0, float((cfg or {}).get("ollama_connect_timeout") or 8)))
    except Exception:
        return 8.0


def _timeout(cfg: dict, read: float):
    return (_connect_timeout(cfg), read)


def _friendly_conn_error(e, url: str) -> str:
    name = type(e).__name__
    if any(k in name.lower() for k in ("connection", "timeout", "connect")):
        return (f"{name}: {e}\n"
                f"→ 서버: {url}\n"
                f"→ 확인: Ollama 가 실행 중인지, 주소/포트가 맞는지, 같은 네트워크인지.\n"
                f"→ {HOST_HINT}")
    return f"{name}: {e}"


def _ollama_options(cfg: dict) -> dict:
    opts = {"temperature": float(cfg.get("temperature", 0.3) or 0)}
    try:
        n_ctx = int(cfg.get("ollama_num_ctx") or 0)
    except Exception:
        n_ctx = 0
    if n_ctx > 0:
        opts["num_ctx"] = n_ctx          # 모델 기본값 대신 명시 → 조용한 잘림 방지
    try:
        n_pred = int(cfg.get("ollama_num_predict") or 0)
    except Exception:
        n_pred = 0
    if n_pred > 0:
        opts["num_predict"] = n_pred
    return opts


def _ollama_body(cfg: dict, messages, tools=None, stream=True) -> dict:
    body = {"model": cfg.get("ollama_model") or "gemma4:12b",
            "messages": _to_ollama_messages(messages),
            "stream": stream,
            "options": _ollama_options(cfg)}
    if tools:
        body["tools"] = tools
    keep = cfg.get("ollama_keep_alive")
    if keep:
        body["keep_alive"] = keep        # 모델을 메모리에 유지 → 재로딩 지연 제거
    think = str(cfg.get("ollama_think") or "auto").strip().lower()
    if think == "off":
        body["think"] = False            # qwen3 등 추론 모델: 생각 단계 생략(2배 빠름)
    elif think == "on":
        body["think"] = True
    return body


def active_model(cfg: dict) -> str:
    if (cfg.get("provider") or "ollama") == "openai":
        return cfg.get("openai_model") or "gpt-4o-mini"
    return cfg.get("ollama_model") or "gemma4:12b"


def active_endpoint(cfg: dict) -> str:
    if (cfg.get("provider") or "ollama") == "openai":
        return (cfg.get("openai_base_url") or "").rstrip("/")
    return ollama_base(cfg)


# --------------------------------------------------------------------------- #
# 모델 목록 / 연결 확인
# --------------------------------------------------------------------------- #
def list_models(cfg: dict, url: str = None):
    provider = cfg.get("provider") or "ollama"
    if provider == "openai" and not url:
        base = (cfg.get("openai_base_url") or "").rstrip("/")
        res = api_models(base, cfg.get("openai_api_key") or "",
                         cfg.get("openai_preset") or "")
        if res.get("ok"):
            return sorted(res.get("models") or [])
        raise LLMError(f"모델 목록을 가져오지 못했습니다: {res.get('error') or '알 수 없는 오류'}"
                       f"\n→ 서버: {base or '(Base URL 없음)'}")
    try:
        base = normalize_url(url) if url else ollama_base(cfg)
        r = _session().get(base + "/api/tags", headers=ollama_headers(cfg),
                           timeout=(_connect_timeout(cfg), 15))
        r.raise_for_status()
        return sorted(m.get("name", "") for m in r.json().get("models", []) if m.get("name"))
    except Exception as e:
        raise LLMError(f"모델 목록을 가져오지 못했습니다: {_friendly_conn_error(e, url or active_endpoint(cfg))}")


def ollama_version(cfg: dict, url: str = None):
    base = normalize_url(url) if url else ollama_base(cfg)
    try:
        r = _session().get(base + "/api/version", headers=ollama_headers(cfg),
                           timeout=(_connect_timeout(cfg), 5))
        if r.status_code == 200:
            return (r.json() or {}).get("version") or ""
    except Exception:
        pass
    return ""


def probe(cfg: dict = None, url: str = None, timeout: float = None) -> dict:
    """서버 한 곳을 확인한다. {ok,url,version,count,models,latency_ms,error}"""
    cfg = cfg or {}
    base = normalize_url(url) if url else ollama_base(cfg)
    t0 = time.time()
    out = {"ok": False, "url": base, "version": "", "count": 0,
           "models": [], "latency_ms": 0, "error": "", "local": perf.is_local_url(base)}
    try:
        r = _session().get(base + "/api/tags", headers=ollama_headers(cfg),
                           timeout=(min(_connect_timeout(cfg), 5), int(timeout or 6)))
        r.raise_for_status()
        models = []
        for m in r.json().get("models", []):
            models.append({"name": m.get("name"),
                           "size": m.get("size", 0),
                           "params": (m.get("details") or {}).get("parameter_size", ""),
                           "family": (m.get("details") or {}).get("family", "")})
        ver = ollama_version(cfg, base)
        out.update({"ok": True, "version": ver, "models": models,
                    "count": len(models)})
    except Exception as e:
        out["error"] = _friendly_conn_error(e, base)
    out["latency_ms"] = int((time.time() - t0) * 1000)
    return out


def ping(cfg: dict):
    provider = cfg.get("provider") or "ollama"
    try:
        if provider == "openai":
            base = (cfg.get("openai_base_url") or "").rstrip("/")
            key = cfg.get("openai_api_key") or ""
            if not base:
                return False, "Base URL 이 비어 있습니다."
            res = api_models(base, key, cfg.get("openai_preset") or "")
            if res.get("ok") and res.get("source") == "api":
                note = ""
                if not key and not perf.is_local_url(base):
                    note = "  ⚠ API 키가 비어 있습니다."
                return True, (f"API 연결 성공 · {base} · 모델 {len(res['models'])}개"
                              f" · 사용: {active_model(cfg)}{note}")
            # 목록 API 를 지원하지 않는 서비스(Gemini 호환 경로 등) → 실제 대화 1토큰으로 확인
            try:
                status, text = _probe_chat(cfg)
            except Exception as e:
                return False, f"{type(e).__name__}: {e}\n→ 서버: {base}"
            if status < 400:
                return True, (f"API 연결 성공 · {base} · 사용: {active_model(cfg)}"
                              "  (모델 목록 API 는 미지원 → 기본 목록 사용)")
            brief = _err_brief(text)
            low = brief.lower()
            if (status in (401, 403) or (not key and status == 400)
                    or (status == 400 and "api key" in low)):
                hint = "API 키가 비어 있습니다." if not key else "API 키가 거부되었습니다."
                return False, f"{hint} (HTTP {status}) · {brief}\n→ 서버: {base}"
            return False, f"API 오류 {status}: {brief}\n→ 서버: {base}"
        base = ollama_base(cfg)
        r = _session().get(base + "/api/tags", headers=ollama_headers(cfg),
                           timeout=_timeout(cfg, 8))
        if r.status_code != 200:
            return False, f"Ollama 응답 {r.status_code} ({base})"
        names = [m.get("name", "") for m in r.json().get("models", [])]
        cur = active_model(cfg)
        hint = "" if cur in names else f"  ⚠ '{cur}' 모델이 없습니다. 다운로드하세요."
        where = "이 PC" if perf.is_local_url(base) else "원격 PC"
        return True, f"Ollama 연결 성공({where}) · {base} · {len(names)}개 설치됨{hint}"
    except Exception as e:
        return False, _friendly_conn_error(e, active_endpoint(cfg))


# --------------------------------------------------------------------------- #
# Ollama 관리 (로컬 / 원격 공용)
# --------------------------------------------------------------------------- #
def ollama_tags(cfg: dict, url: str = None):
    base = normalize_url(url) if url else ollama_base(cfg)
    r = _session().get(base + "/api/tags", headers=ollama_headers(cfg),
                       timeout=_timeout(cfg, 15))
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        out.append({"name": m.get("name"), "size": m.get("size", 0),
                    "modified": m.get("modified_at", ""),
                    "params": (m.get("details") or {}).get("parameter_size", ""),
                    "family": (m.get("details") or {}).get("family", "")})
    return out


def ollama_ps(cfg: dict, url: str = None):
    return ollama_ps_url(cfg, url)


def ollama_ps_url(cfg: dict, url: str = None, timeout: float = 6):
    base = normalize_url(url) if url else ollama_base(cfg)
    try:
        r = _session().get(base + "/api/ps", headers=ollama_headers(cfg),
                           timeout=(min(_connect_timeout(cfg), 2.5), float(timeout)))
        r.raise_for_status()
        return [{"name": m.get("name"), "size": m.get("size", 0),
                 "vram": m.get("size_vram", 0)} for m in r.json().get("models", [])]
    except Exception:
        return []


def ollama_pull(cfg: dict, model: str, url: str = None):
    """모델 다운로드 진행상황 제너레이터. {status, percent, completed, total}"""
    base = normalize_url(url) if url else ollama_base(cfg)
    try:
        r = _session().post(base + "/api/pull", headers=ollama_headers(cfg),
                            json={"name": model, "stream": True},
                            stream=True, timeout=(_connect_timeout(cfg), 3600))
    except Exception as e:
        yield {"status": f"실패: {_friendly_conn_error(e, base)}", "percent": 0}
        return
    if r.status_code >= 400:
        yield {"status": f"오류 {r.status_code}: {r.text[:200]}", "percent": 0}
        return
    for raw in r.iter_lines():
        if not raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if obj.get("error"):
            yield {"status": f"오류: {obj['error']}", "percent": 0}
            return
        total, completed = obj.get("total") or 0, obj.get("completed") or 0
        pct = int(completed * 100 / total) if total else 0
        yield {"status": obj.get("status", ""), "percent": pct,
               "completed": completed, "total": total}


def ollama_delete(cfg: dict, model: str, url: str = None):
    base = normalize_url(url) if url else ollama_base(cfg)
    r = _session().delete(base + "/api/delete", headers=ollama_headers(cfg),
                          json={"name": model}, timeout=_timeout(cfg, 30))
    if r.status_code >= 400:
        raise LLMError(f"삭제 실패 {r.status_code}: {r.text[:200]}")
    return True


def unload_model(cfg: dict, url: str, model: str):
    """모델을 메모리/VRAM 에서 내린다(`keep_alive: 0`). (ok, error) 반환."""
    base = normalize_url(url)
    if not model:
        return False, "모델 이름 없음"
    try:
        r = _session().post(base + "/api/generate", headers=ollama_headers(cfg),
                            json={"model": model, "prompt": "", "keep_alive": 0,
                                  "stream": False},
                            timeout=(3.0, 6.0))
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:160]}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# 메시지 변환
# --------------------------------------------------------------------------- #
def _clean_msg(m):
    return {k: v for k, v in m.items() if k != "images"}


def _to_ollama_messages(messages):
    out = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            item = {"role": "tool", "content": m.get("content") or ""}
            if m.get("name"):
                item["tool_name"] = m["name"]
            out.append(item)
            continue
        item = {"role": role, "content": m.get("content") or ""}
        if role == "user" and m.get("images"):
            item["images"] = list(m["images"])[:6]      # base64 문자열 배열
        if role == "assistant" and m.get("tool_calls"):
            item["tool_calls"] = [
                {"function": {"name": tc.get("function", {}).get("name", ""),
                              "arguments": tc.get("function", {}).get("arguments", "")}}
                for tc in m["tool_calls"]
            ]
        out.append(item)
    return out


def _to_openai_messages(messages):
    """이미지가 있는 사용자 메시지를 OpenAI 멀티모달(content 파트 배열) 형식으로."""
    out = []
    for m in messages:
        imgs = m.get("images") or []
        if imgs and m.get("role") == "user":
            parts = [{"type": "text", "text": str(m.get("content") or "")}]
            for b in imgs[:6]:
                if not b:
                    continue
                url = b if str(b).startswith("data:") else "data:image/png;base64," + str(b)
                parts.append({"type": "image_url", "image_url": {"url": url}})
            out.append({"role": "user", "content": parts})
        else:
            out.append(_clean_msg(m))
    return out


# --------------------------------------------------------------------------- #
# 스트리밍 채팅
# --------------------------------------------------------------------------- #
def chat_stream(cfg: dict, messages: list, tools=None):
    provider = cfg.get("provider") or "ollama"
    last_err = None
    for attempt in range(2):
        try:
            if provider == "openai":
                yield from _openai_stream(cfg, messages, tools)
            else:
                yield from _ollama_stream(cfg, messages, tools)
            return
        except LLMError:
            raise
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1.0)          # 연결 실패는 한 번만, 짧게 재시도
                continue
            if provider == "ollama":
                raise LLMError(_friendly_conn_error(e, ollama_base(cfg))) from last_err
            raise LLMError(f"{type(e).__name__}: {e}") from last_err


def _openai_stream(cfg, messages, tools=None):
    url = (cfg.get("openai_base_url") or "").rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("openai_api_key"):
        headers["Authorization"] = "Bearer " + cfg["openai_api_key"]
    body = {
        "model": cfg.get("openai_model") or "gpt-4o-mini",
        "messages": _to_openai_messages(messages),
        "temperature": float(cfg.get("temperature", 0.3) or 0),
        "stream": True,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    r = _session().post(url, headers=headers, json=body, stream=True, timeout=(20, 900))
    if r.status_code >= 400:
        raise LLMError(f"API 오류 {r.status_code}: {r.text[:500]}")

    parts = []
    tool_calls = []
    try:
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            if not line.startswith("{"):
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            if chunk.get("error"):
                raise LLMError(f"API 오류: {chunk['error']}")
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
                    yield {"delta": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index", 0) or 0)
                    while len(tool_calls) <= idx:
                        tool_calls.append({"id": "", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        tool_calls[idx]["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        tool_calls[idx]["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        tool_calls[idx]["function"]["arguments"] += fn["arguments"]
    finally:
        r.close()
    yield {"final": True, "text": "".join(parts), "tool_calls": tool_calls}


def _ollama_stream(cfg, messages, tools=None):
    url = ollama_base(cfg) + "/api/chat"
    body = _ollama_body(cfg, messages, tools, stream=True)

    r = _session().post(url, headers=ollama_headers(cfg), json=body,
                        stream=True, timeout=_timeout(cfg, 1800))
    if r.status_code >= 400 and "think" in body and "think" in (r.text or "").lower():
        # 구버전/미지원 모델: think 옵션을 빼고 한 번만 재시도
        try:
            r.close()
        except Exception:
            pass
        body.pop("think", None)
        r = _session().post(url, headers=ollama_headers(cfg), json=body,
                            stream=True, timeout=_timeout(cfg, 1800))
    if r.status_code >= 400:
        raise LLMError(f"Ollama 오류 {r.status_code}: {r.text[:500]}")

    parts = []
    tool_calls = []
    try:
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("error"):
                raise LLMError(f"Ollama: {obj['error']}")
            msg = obj.get("message") or {}
            if msg.get("content"):
                parts.append(msg["content"])
                yield {"delta": msg["content"]}
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                tool_calls.append({"id": "call_" + str(len(tool_calls)),
                                   "function": {"name": fn.get("name", ""), "arguments": args}})
            if obj.get("done"):
                break
    finally:
        r.close()
    yield {"final": True, "text": "".join(parts), "tool_calls": tool_calls}


# --------------------------------------------------------------------------- #
# 비스트리밍(요약/압축용)
# --------------------------------------------------------------------------- #
def complete(cfg: dict, messages: list, max_tokens: int = 1200) -> str:
    provider = cfg.get("provider") or "ollama"
    if provider == "openai":
        url = (cfg.get("openai_base_url") or "").rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg.get("openai_api_key"):
            headers["Authorization"] = "Bearer " + cfg["openai_api_key"]
        body = {"model": cfg.get("openai_model") or "gpt-4o-mini",
                "messages": _to_openai_messages(messages),
                "temperature": 0.2, "max_tokens": max_tokens, "stream": False}
        r = _session().post(url, headers=headers, json=body, timeout=(20, 600))
        if r.status_code >= 400:
            raise LLMError(f"API 오류 {r.status_code}: {r.text[:300]}")
        return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    url = ollama_base(cfg) + "/api/chat"
    body = _ollama_body(cfg, messages, stream=False)
    body["options"] = dict(body.get("options") or {}, num_predict=max_tokens)
    r = _session().post(url, headers=ollama_headers(cfg), json=body,
                        timeout=_timeout(cfg, 900))
    if r.status_code >= 400:
        raise LLMError(f"Ollama 오류 {r.status_code}: {r.text[:300]}")
    return ((r.json().get("message") or {}).get("content")) or ""
