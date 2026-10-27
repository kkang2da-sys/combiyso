# -*- coding: utf-8 -*-
"""컨텍스트(토큰) 추정 + 자동 압축.

정확한 토크나이저 없이 문자 종류로 근사한다(한글/한자 1자 ≈ 1토큰, 라틴 ≈ 1/3.6).
"""
import json
import re

# 한글/한자/일본어/전각 문자 — 1자 ≈ 1토큰
_CJK_RE = re.compile(
    r"[\u1100-\u11ff\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\u3000-\u303f\uff00-\uff60]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    n = len(text)
    cjk = len(_CJK_RE.findall(text))          # C 레벨 정규식 — 순수 파이썬 루프보다 훨씬 빠름
    return int(cjk + (n - cjk) / 3.6) + 4


def effective_limit(cfg: dict) -> int:
    """압축 기준 토큰 수.

    Ollama 는 모델의 실제 컨텍스트(num_ctx)를 넘으면 조용히 잘라버리므로,
    설정값과 컨텍스트 창의 85% 중 작은 쪽을 쓴다.
    """
    limit = int(cfg.get("compact_threshold") or 24000)
    if (cfg.get("provider") or "ollama") == "ollama":
        try:
            n_ctx = int(cfg.get("ollama_num_ctx") or 0)
        except Exception:
            n_ctx = 0
        if n_ctx > 0:
            limit = min(limit, max(1024, int(n_ctx * 0.85)))
    return limit


IMAGE_TOKENS = 900          # 이미지 1장 추정(픽셀 수에 따라 다르지만 근사치)


def content_tokens(content) -> int:
    """content 가 문자열이거나 OpenAI 형식(파트 배열)일 때의 토큰 추정."""
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                t = part.get("type")
                if t == "image_url":
                    total += IMAGE_TOKENS
                elif part.get("text"):
                    total += estimate_tokens(part["text"])
            else:
                total += estimate_tokens(str(part))
        return total
    return estimate_tokens(str(content or ""))


def messages_tokens(messages: list) -> int:
    total = 0
    for m in messages or []:
        total += 8
        total += content_tokens(m.get("content"))
        # 첨부 이미지(ollama images / 내부 images)는 base64 를 세지 않고 장당 근사
        total += IMAGE_TOKENS * len(m.get("images") or [])
        if m.get("tool_calls"):
            total += estimate_tokens(json.dumps(m["tool_calls"], ensure_ascii=False))
        if m.get("name"):
            total += 4
    return total


def usage_info(messages: list, cfg: dict) -> dict:
    used = messages_tokens(messages)
    limit = effective_limit(cfg)
    return {"tokens": used, "limit": limit,
            "percent": min(100, int(used * 100 / limit)) if limit else 0}


SUMMARY_PROMPT = (
    "다음은 사용자와 AI의 이전 대화 일부입니다. 나중에 이어서 작업할 수 있도록 "
    "핵심만 한국어로 간결하게 요약하세요. 반드시 보존할 것: "
    "① 사용자의 목표/요청 ② 이미 만들거나 수정한 파일 경로와 내용 요약 "
    "③ 실행한 명령과 결과 ④ 아직 안 끝난 일(다음 단계). "
    "인사말 없이 요약 본문만 출력하세요."
)


def maybe_compact(cfg: dict, messages: list, complete_fn):
    """필요하면 오래된 메시지를 요약으로 대체한 새 목록을 반환.

    returns (messages, info|None)
    """
    if not cfg.get("auto_compact", True):
        return messages, None
    limit = effective_limit(cfg)
    if messages_tokens(messages) <= limit:
        return messages, None

    sys_msgs = [m for m in messages if m.get("role") == "system"]
    body = [m for m in messages if m.get("role") != "system"]
    keep_n = max(2, int(cfg.get("keep_recent_messages") or 8))
    if len(body) <= keep_n + 2:
        return messages, None

    old, recent = body[:-keep_n], body[-keep_n:]
    transcript = []
    for m in old:
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "tool":
            content = "[도구결과] " + content[:1500]
        transcript.append(f"[{role}] {content[:3000]}")
    text = "\n\n".join(transcript)[:60000]

    try:
        summary = complete_fn([
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": text},
        ])
    except Exception as e:
        summary = f"(요약 실패: {type(e).__name__}: {e})"
    summary = (summary or "").strip() or "(요약 없음)"

    new_messages = list(sys_msgs)
    new_messages.append({
        "role": "user",
        "content": "[이전 대화 요약 — 참고용]\n" + summary,
    })
    new_messages.append({"role": "assistant",
                         "content": "네, 이전 내용을 기억하고 이어서 진행하겠습니다."})
    new_messages.extend(recent)
    info = {"before": messages_tokens(messages), "after": messages_tokens(new_messages),
            "removed": len(old)}
    return new_messages, info
