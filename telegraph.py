"""기사별 telegra.ph 페이지 — 텔레그램 안에서 바로 읽게 한다.

**왜 telegra.ph 인가.** 텔레그램의 Instant View 는 도메인마다 IV 템플릿이
등록돼 있어야 동작한다. 글로벌이코노믹처럼 되는 매체가 있고 newsis AMP 처럼
안 되는 매체가 있다 — 안 되면 "Open this link?" 가 뜨고 브라우저로 나간다.
봇이 템플릿을 만들 수는 없다.

telegra.ph 페이지는 **항상** Instant View 가 된다. 그래서 기사마다 페이지를
하나 만들고 제목을 거기로 링크한다. 누르면 텔레그램 안에서 즉시 열린다.
미리보기 카드(크고 거슬린다)는 끈다 — 제목 링크만으로 충분하다.

**본문은 발췌만 싣는다.** 전문을 담으면 공개 URL 에 언론사 기사를 그대로
재게시하는 셈이 된다. 우리 요약·불릿·코멘트를 먼저 놓고, 본문은 앞부분만
인용한 뒤 원문 링크로 보낸다.
"""
import html as _html
import json
import re

import httpx

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
API = "https://api.telegra.ph"
EXCERPT_PARAS = 4          # 본문에서 인용할 문단 수
EXCERPT_CHARS = 1200       # 그 상한


def get_token(store) -> str | None:
    tok = store.get_setting("telegraph_token") if store else None
    if tok:
        return tok
    try:
        r = httpx.post(f"{API}/createAccount", timeout=20, data={
            "short_name": "StrategyPulse",
            "author_name": "Strategy Pulse",
        }).json()
        if r.get("ok"):
            tok = r["result"]["access_token"]
            if store:
                store.put_setting("telegraph_token", tok)
            return tok
    except Exception:
        pass
    return None


def fetch_excerpt(url: str, timeout: float = 15) -> list[str]:
    """기사 본문 앞부분 문단들. 실패하면 빈 목록."""
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=timeout,
                      follow_redirects=True)
        body = r.text
    except Exception:
        return []
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    paras = []
    for m in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", body):
        t = re.sub(r"(?is)<[^>]+>", " ", m.group(1))
        t = _html.unescape(re.sub(r"\s+", " ", t)).strip()
        if len(t) < 30:
            continue
        paras.append(t)
        if len(paras) >= EXCERPT_PARAS:
            break
    total = 0
    out = []
    for t in paras:
        if total + len(t) > EXCERPT_CHARS:
            break
        out.append(t)
        total += len(t)
    return out


def build_content(summary: str, bullets: list[str], why: str,
                  excerpt: list[str], source_url: str, source_name: str) -> list:
    """Telegraph DOM. 우리 분석이 먼저, 본문 발췌는 그 다음, 끝에 원문 링크."""
    c = []
    if summary:
        c.append({"tag": "h4", "children": ["핵심"]})
        c.append({"tag": "p", "children": [summary]})
    if bullets:
        c.append({"tag": "h4", "children": ["주요 내용"]})
        c.append({"tag": "ul",
                  "children": [{"tag": "li", "children": [b]} for b in bullets]})
    if why:
        c.append({"tag": "h4", "children": ["경영전략실 시사점"]})
        c.append({"tag": "blockquote", "children": [why]})
    if excerpt:
        c.append({"tag": "hr"})
        c.append({"tag": "h4", "children": ["본문 발췌"]})
        for t in excerpt:
            c.append({"tag": "p", "children": [t]})
    c.append({"tag": "hr"})
    label = f"기사 전문 보기 — {source_name}" if source_name else "기사 전문 보기"
    c.append({"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": source_url}, "children": [label]}]})
    return c


def create_page(store, title: str, content: list, author: str) -> str | None:
    tok = get_token(store)
    if not tok:
        return None
    try:
        r = httpx.post(f"{API}/createPage", timeout=25, data={
            "access_token": tok,
            "title": title[:200] or "Strategy Pulse",
            "author_name": author[:128],
            "content": json.dumps(content, ensure_ascii=False),
            "return_content": "false",
        }).json()
        if r.get("ok"):
            return r["result"]["url"]
    except Exception:
        pass
    return None
