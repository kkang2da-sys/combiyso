# -*- coding: utf-8 -*-
"""웹 검색 + 페이지 텍스트 추출 (API 키 불필요).

DuckDuckGo HTML/Lite 엔드포인트를 사용한다. 실패하면 조용히 빈 결과를 돌려준다.
"""
import html
import re
import urllib.parse

from .net import session

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_RE_A = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_RE_SNIP = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.S | re.I)
_RE_LITE_A = re.compile(r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_RE_LITE_SNIP = re.compile(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.S | re.I)
_RE_TAGS = re.compile(r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>", re.S | re.I)
_RE_ANY_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t\r\f\v]+")
_RE_BLANK = re.compile(r"\n{3,}")
_RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def _clean(s: str) -> str:
    s = _RE_ANY_TAG.sub("", s or "")
    s = html.unescape(s)
    s = _RE_WS.sub(" ", s)
    return s.strip()


def _unwrap(url: str) -> str:
    """DuckDuckGo 리다이렉트(//duckduckgo.com/l/?uddg=...) 를 원래 URL 로."""
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    try:
        if "uddg=" in url:
            q = urllib.parse.urlparse(url).query
            params = urllib.parse.parse_qs(q)
            if params.get("uddg"):
                return params["uddg"][0]
    except Exception:
        pass
    return url


def search(query: str, max_results: int = 5, timeout: int = 20) -> list:
    """[{title, url, snippet}] 반환."""
    query = (query or "").strip()
    if not query:
        return []
    results = []

    # 1) html.duckduckgo.com
    try:
        r = session().post("https://html.duckduckgo.com/html/",
                          data={"q": query, "kl": "kr-kr"},
                          headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code == 200:
            text = r.text
            anchors = _RE_A.findall(text)
            snips = [_clean(x) for x in _RE_SNIP.findall(text)]
            for i, (href, title) in enumerate(anchors):
                url = _unwrap(href)
                if not url.startswith("http"):
                    continue
                results.append({"title": _clean(title), "url": url,
                                "snippet": snips[i] if i < len(snips) else ""})
                if len(results) >= max_results:
                    break
    except Exception:
        pass

    # 2) lite.duckduckgo.com (백업)
    if not results:
        try:
            r = session().get("https://lite.duckduckgo.com/lite/",
                             params={"q": query}, headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200:
                anchors = _RE_LITE_A.findall(r.text)
                snips = [_clean(x) for x in _RE_LITE_SNIP.findall(r.text)]
                for i, (href, title) in enumerate(anchors):
                    url = _unwrap(href)
                    if not url.startswith("http"):
                        continue
                    results.append({"title": _clean(title), "url": url,
                                    "snippet": snips[i] if i < len(snips) else ""})
                    if len(results) >= max_results:
                        break
        except Exception:
            pass

    return results[:max_results]


def strip_html(raw: str) -> str:
    text = _RE_TAGS.sub(" ", raw or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", text, flags=re.I)
    text = _RE_ANY_TAG.sub("", text)
    text = html.unescape(text)
    text = _RE_WS.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _RE_BLANK.sub("\n\n", text)
    return text.strip()


def fetch_text(url: str, max_chars: int = 20000, timeout: int = 25) -> dict:
    """URL 내용을 텍스트로. {ok, url, title, content, error}"""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "url": url, "error": "빈 URL"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = session().get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}
    if r.status_code >= 400:
        return {"ok": False, "url": url, "error": f"HTTP {r.status_code}"}

    ctype = (r.headers.get("Content-Type") or "").lower()
    raw = r.content[:2_000_000]
    enc = r.encoding or "utf-8"
    try:
        text = raw.decode(enc, errors="replace")
    except Exception:
        text = raw.decode("utf-8", errors="replace")

    title = ""
    m = _RE_TITLE.search(text)
    if m:
        title = _clean(m.group(1))

    if "html" in ctype or text.lstrip()[:1] == "<":
        content = strip_html(text)
    else:
        content = text

    truncated = len(content) > max_chars
    return {"ok": True, "url": url, "title": title,
            "content": content[:max_chars], "truncated": truncated}


def format_results(results: list) -> str:
    if not results:
        return "(검색 결과 없음)"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title','')}\n   {r.get('url','')}\n   {r.get('snippet','')}")
    return "\n".join(lines)
