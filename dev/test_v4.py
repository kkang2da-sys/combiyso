# -*- coding: utf-8 -*-
"""v1.4.0 신규 기능 테스트 (서버 없이 in-process).

  - 병렬 하위 에이전트 (읽기 전용 판정 / 이벤트 병합)
  - git 체크포인트 & 되돌리기 (vcs)
  - 첨부 (attach / pdftext / llm 메시지 변환 / 토큰 추정)

실행:  .venv\\Scripts\\python.exe dev\\test_v4.py
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 데이터 폴더를 임시로 (실제 %APPDATA%\compbiyso 를 건드리지 않는다) ──
_TMPDATA = Path(tempfile.mkdtemp(prefix="cb_v4_"))
os.environ["APPDATA"] = str(_TMPDATA)

from app import agent as agentmod          # noqa: E402
from app import attach                     # noqa: E402
from app import config                     # noqa: E402
from app import context                    # noqa: E402
from app import llm as llmmod              # noqa: E402
from app import pdftext                    # noqa: E402
from app import vcs                        # noqa: E402

WS = Path(tempfile.mkdtemp(prefix="cb_ws_"))
PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [NG] {name}   {detail}")


def sec(t):
    print("\n" + "=" * 64 + f"\n> {t}\n" + "=" * 64)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def make_pdf(text="Hello Combiyso PDF", compress=False) -> bytes:
    content = f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode("latin-1")
    if compress:
        content = zlib.compress(content)
        fobj = b"<< /Length %d /Filter /FlateDecode >>" % len(content)
    else:
        fobj = b"<< /Length %d >>" % len(content)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        fobj + b"\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, start=1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


def test_pdf():
    sec("PDF 텍스트 추출")
    plain = make_pdf("Hello Combiyso PDF")
    comp = make_pdf("Compressed Stream Text", compress=True)
    ok("내장 추출기(비압축)", "Hello Combiyso PDF" in pdftext._builtin(plain))
    ok("내장 추출기(FlateDecode)", "Compressed Stream Text" in pdftext._builtin(comp))
    ok("extract() 비압축", "Hello Combiyso" in pdftext.extract(plain))
    ok("extract() 압축", "Compressed Stream" in pdftext.extract(comp))
    ok("pypdf 설치 감지", pdftext.have_pypdf())
    ok("빈 PDF → 빈 문자열", pdftext.extract(b"not a pdf") == "")
    ok("max_chars 제한", len(pdftext.extract(plain, max_chars=8)) <= 8)


# --------------------------------------------------------------------------- #
# 첨부
# --------------------------------------------------------------------------- #
def test_attach():
    sec("첨부 저장/로드")
    b = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    durl = "data:image/png;base64," + base64.b64encode(b).decode()
    ok("data URL 디코드", attach.decode_payload(durl) == b)
    ok("순수 base64 디코드", attach.decode_payload(base64.b64encode(b).decode()) == b)

    ref = attach.save_bytes("ses1", "cat.png", b, "image/png", "image")
    ok("저장 후 참조 반환", bool(ref.get("id")) and ref["kind"] == "image")
    ok("바이트 왕복", attach.read_bytes("ses1", ref["id"]) == b)
    ok("b64 왕복", base64.b64decode(attach.b64("ses1", ref["id"])) == b)
    ok("경로 탈출 차단", attach.path_of("ses1", "../../config.json") is None)
    ok("없는 첨부 → None", attach.path_of("ses1", "nope.png") is None)

    imgs = attach.load_images("ses1", [ref, {"id": "x", "kind": "pdf"}])
    ok("이미지만 로드", len(imgs) == 1 and base64.b64decode(imgs[0]) == b)
    ok("drop_all", attach.drop_all("ses1") and attach.read_bytes("ses1", ref["id"]) == b"")


# --------------------------------------------------------------------------- #
# 메시지 변환 / 토큰
# --------------------------------------------------------------------------- #
def test_llm_messages():
    sec("멀티모달 메시지 · 토큰 추정")
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "이 그림 뭐야?", "images": ["AAAABBBB"]},
            {"role": "assistant", "content": "음", "tool_calls": [
                {"function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "content": "결과", "name": "read_file"}]

    o = llmmod._to_openai_messages(msgs)
    ok("openai: content 파트 배열", isinstance(o[1]["content"], list))
    ok("openai: image_url 포함", any(p.get("type") == "image_url" for p in o[1]["content"]))
    ok("openai: data URL prefix", o[1]["content"][1]["image_url"]["url"].startswith("data:image/png"))
    ok("openai: images 키 제거", "images" not in o[1])
    ok("openai: tool 메시지 유지", o[3]["role"] == "tool" and o[3]["name"] == "read_file")

    ol = llmmod._to_ollama_messages(msgs)
    ok("ollama: images 배열", ol[1]["images"] == ["AAAABBBB"])
    ok("ollama: tool_name", ol[3].get("tool_name") == "read_file")
    ok("ollama: tool_calls 이름", ol[2]["tool_calls"][0]["function"]["name"] == "read_file")

    t_plain = context.messages_tokens([{"role": "user", "content": "가나다"}])
    t_img = context.messages_tokens([{"role": "user", "content": "가나다", "images": ["A" * 40000]}])
    ok("이미지 base64 는 토큰에 미포함", t_img - t_plain == context.IMAGE_TOKENS,
       f"{t_plain} vs {t_img}")
    arr = context.messages_tokens([{"role": "user", "content": [
        {"type": "text", "text": "설명"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 50000}}]}])
    ok("파트 배열 토큰 근사", arr < 2000, arr)


# --------------------------------------------------------------------------- #
# 병렬 하위 에이전트 판정
# --------------------------------------------------------------------------- #
def test_parallel():
    sec("병렬 하위 에이전트 판정")
    cfg = dict(config.DEFAULTS)
    cfg["workspace"] = str(WS)

    ro, aid, _ = agentmod.resolve_subagent(cfg, "explore")
    ok("explore 는 읽기 전용", ro and aid == "explore", f"{ro} {aid}")
    ro2, aid2, _ = agentmod.resolve_subagent(cfg, "build")
    ok("build(primary) → general 폴백·쓰기 가능", not ro2 and aid2 == "general", f"{ro2} {aid2}")
    ro3, aid3, _ = agentmod.resolve_subagent(cfg, "없는에이전트")
    ok("없는 id → general 폴백", aid3 == "general", aid3)

    cfg["subagent_parallel"] = "on"
    ok("on → 병렬", agentmod.parallel_enabled(cfg))
    cfg["subagent_parallel"] = "off"
    ok("off → 순차", not agentmod.parallel_enabled(cfg))
    cfg["subagent_parallel"] = "auto"
    ok("auto+저사양 → 순차", not agentmod.parallel_enabled({**cfg, "_low_spec_active": True}))
    ok("auto+일반 → 병렬", agentmod.parallel_enabled({**cfg, "_low_spec_active": False}))

    # 이벤트 병합(가짜 run_agent 로 워커 동작 검증)
    real = agentmod.run_agent

    def fake_run_agent(cfg2, history, session_id="", agent_id=None, **kw):
        p = history[0]["content"]
        yield {"type": "delta", "text": "무시됨"}
        yield {"type": "tool", "id": "t1", "name": "read_file", "args": {"path": p}}
        yield {"type": "tool_result", "id": "t1", "name": "read_file", "ok": True,
               "result": "R-" + p}
        yield {"type": "final", "text": "F-" + p}

    agentmod.run_agent = fake_run_agent
    try:
        items = [{"sub_id": "s0", "prompt": "aaa", "agent": "explore"},
                 {"sub_id": "s1", "prompt": "bbb", "agent": "explore"},
                 {"sub_id": "s2", "prompt": "ccc", "agent": "explore"}]
        evs = list(agentmod._run_subagents_parallel(cfg, "ses", 0, items))
        types = [e.get("type") for e in evs]
        results = evs[-1].get("__results__") or {}
        ok("3개 결과 수집", results == {"s0": "F-aaa", "s1": "F-bbb", "s2": "F-ccc"}, results)
        ok("sub_id 태깅", all("sub_id" in e for e in evs if e.get("type") == "tool"))
        ok("delta 는 전달 안 함", "delta" not in types)
        ok("end 이벤트 3개", types.count("subagent") == 3, types)
    finally:
        agentmod.run_agent = real


# --------------------------------------------------------------------------- #
# git 체크포인트
# --------------------------------------------------------------------------- #
def test_vcs():
    sec("git 체크포인트 / 되돌리기")
    if not vcs.available():
        print("  (git 이 없어 건너뜀)")
        return
    cfg = {"workspace": str(WS), "git_undo": "on",
           "git_undo_max_files": 20000, "git_undo_max_mb": 400}

    (WS / "keep.txt").write_text("v1", encoding="utf-8")
    (WS / "sub").mkdir(exist_ok=True)
    (WS / "sub" / "a.txt").write_text("A1", encoding="utf-8")

    s1 = vcs.snapshot(cfg, label="첫 지점")
    ok("첫 스냅샷", bool(s1 and s1.get("ok")), s1)
    ok("작업 폴더에 .git 없음", not (WS / ".git").exists())

    (WS / "keep.txt").write_text("v2-수정됨", encoding="utf-8")
    (WS / "새파일.txt").write_text("새로 만든 파일", encoding="utf-8")
    s2 = vcs.snapshot(cfg, label="둘째 지점")
    ok("둘째 스냅샷", bool(s2 and s2.get("ok")), s2)
    same = vcs.snapshot(cfg, label="변화없음")
    ok("변경 없으면 unchanged (커밋 생략)", same.get("unchanged") is True, same)

    rows = vcs.checkpoints(cfg, 10)
    ok("체크포인트 목록", len(rows) == 2 and rows[0]["label"] == "둘째 지점", rows[:2])
    ok("라벨/시각 포함", all(("short" in r and "ts" in r) for r in rows))

    st = vcs.status(cfg)
    ok("status 초기화됨", st["initialized"] and st["count"] >= 2, st)

    ok_restore, msg = vcs.restore(cfg, s1["sha"])
    ok("되돌리기 성공", ok_restore, msg)
    ok("내용 복원(v1)", (WS / "keep.txt").read_text(encoding="utf-8") == "v1")
    ok("새로 만든 파일 삭제", not (WS / "새파일.txt").exists())
    ok("하위 폴더 파일 보존", (WS / "sub" / "a.txt").read_text(encoding="utf-8") == "A1")

    # 되돌리기의 되돌리기: 직전 상태는 '둘째 지점'으로 이미 저장돼 있다
    ok2, msg2 = vcs.restore(cfg, s2["sha"])
    ok("둘째 지점으로 복귀 성공", ok2, msg2)
    ok("수정본 복원(v2)", (WS / "keep.txt").read_text(encoding="utf-8") == "v2-수정됨")
    ok("새로 만든 파일도 복원", (WS / "새파일.txt").read_text(encoding="utf-8") == "새로 만든 파일")

    # 스냅샷 없이 바뀐(위험한) 상태에서 되돌리면 'before-undo' 스냅샷이 남아야 한다
    (WS / "keep.txt").write_text("커밋 안 한 변경", encoding="utf-8")
    vcs.restore(cfg, s1["sha"])
    ok("되돌리기 직전 상태 보존(before-undo)",
       any(r["label"].startswith("before-undo") for r in vcs.checkpoints(cfg, 20)))
    bu = [r for r in vcs.checkpoints(cfg, 20) if r["label"].startswith("before-undo")][0]
    vcs.restore(cfg, bu["sha"])
    ok("before-undo 로 복귀", (WS / "keep.txt").read_text(encoding="utf-8") == "커밋 안 한 변경")

    ok("off 설정이면 스냅샷 안 함",
       vcs.snapshot({**cfg, "git_undo": "off"}, "x") is None)
    ok("큰 폴더 가드", vcs._too_big(str(WS), max_files=1, max_mb=400)[0] is True)
    ok("clear", vcs.clear(cfg) is True)
    ok("clear 후 목록 비어 있음", vcs.checkpoints(cfg, 5) == [])
    ok("작업 폴더 파일은 그대로", (WS / "keep.txt").exists())


def main():
    test_pdf()
    test_attach()
    test_llm_messages()
    test_parallel()
    test_vcs()
    print("\n" + "=" * 64)
    print(f"결과: 통과 {PASS} / 실패 {FAIL}")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMPDATA, ignore_errors=True)
        shutil.rmtree(WS, ignore_errors=True)
    sys.exit(code)
