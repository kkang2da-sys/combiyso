# -*- coding: utf-8 -*-
"""PDF 에서 텍스트 추출.

- `pypdf` 가 설치돼 있으면 그것을 쓴다(한글 CID 폰트 ToUnicode 매핑까지 처리 → 품질 좋음).
- 없으면 **내장 추출기**로 폴백한다(의존성 0, FlateDecode + Tj/TJ 파싱).
- 스캔 이미지만 있는 PDF 는 텍스트 레이어가 없어 추출되지 않는다(OCR 필요).
"""
import io
import logging
import re
import zlib

# pypdf 는 잘못된 PDF 를 만나면 자체 로거로 경고를 찍는다(콘솔 소음 방지)
logging.getLogger("pypdf").setLevel(logging.CRITICAL)
logging.getLogger("PyPDF2").setLevel(logging.CRITICAL)

_ESC = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
        "(": "(", ")": ")", "\\": "\\"}
_WORD = re.compile(rb"[A-Za-z*'\"]+")
_STREAM_RE = re.compile(rb"stream\r?\n")


def have_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 내장 추출기 (폴백)
# --------------------------------------------------------------------------- #
def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(s):
            break
        c = s[i]
        if c in _ESC:
            out.append(_ESC[c])
            i += 1
        elif c.isdigit():
            j = i
            while j < len(s) and j - i < 3 and s[j].isdigit():
                j += 1
            try:
                out.append(chr(int(s[i:j], 8)))
            except Exception:
                pass
            i = j
        elif c == "\n":
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _streams(data: bytes):
    """압축을 푼 콘텐츠 스트림 목록."""
    out = []
    for m in _STREAM_RE.finditer(data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        raw = data[start:end]
        raw = raw.rstrip(b"\r\n")
        try:
            out.append(zlib.decompress(raw))
        except Exception:
            try:
                out.append(zlib.decompressobj().decompress(raw))
            except Exception:
                out.append(raw)
    return out


def _content_text(c: bytes) -> str:
    parts, i, n = [], 0, len(c)
    while i < n:
        ch = c[i:i + 1]
        if ch == b"(":
            depth, i, buf = 1, i + 1, bytearray()
            while i < n and depth:
                cc = c[i:i + 1]
                if cc == b"\\":
                    buf += c[i:i + 2]
                    i += 2
                    continue
                if cc == b"(":
                    depth += 1
                elif cc == b")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                buf += cc
                i += 1
            parts.append(("s", _unescape(buf.decode("latin-1", "replace"))))
            continue
        if ch == b"<" and c[i:i + 2] != b"<<":
            j = c.find(b">", i)
            if j > 0:
                hexs = re.sub(rb"[^0-9A-Fa-f]", b"", c[i + 1:j])
                if len(hexs) % 2:
                    hexs += b"0"
                try:
                    raw = bytes.fromhex(hexs.decode("ascii"))
                    try:
                        parts.append(("s", raw.decode("utf-16-be")))
                    except Exception:
                        parts.append(("s", raw.decode("latin-1", "replace")))
                except Exception:
                    pass
                i = j + 1
                continue
        m = _WORD.match(c, i)
        if m:
            w = m.group(0).decode("ascii", "replace")
            if w in ("Td", "TD", "T*", "Tm", "ET"):
                parts.append(("nl", None))
            i = m.end()
            continue
        i += 1
    return "".join(v if k == "s" else "\n" for k, v in parts)


def _builtin(data: bytes) -> str:
    return "\n".join(_content_text(s) for s in _streams(data))


# --------------------------------------------------------------------------- #
def _clean(t: str) -> str:
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t\x0b\x0c\u00a0]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def page_count(data: bytes) -> int:
    try:
        import pypdf
        return len(pypdf.PdfReader(io.BytesIO(data)).pages)
    except Exception:
        pass
    return len(re.findall(rb"/Type\s*/Page[^s]", data)) or 0


def extract(data: bytes, max_pages: int = 50, max_chars: int = 60000) -> str:
    """PDF 텍스트 추출. 실패하거나 내용이 없으면 빈 문자열."""
    text = None
    try:
        import pypdf
        r = pypdf.PdfReader(io.BytesIO(data))
        parts = []
        for i, page in enumerate(r.pages):
            if i >= max_pages:
                break
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                parts.append("")
        text = "\n\n".join(parts)
    except Exception:
        text = None
    if not text or len(_clean(text)) < 20:
        alt = _clean(_builtin(data))
        if len(alt) > len(_clean(text or "")):
            text = alt
    return _clean(text or "")[:max_chars]
