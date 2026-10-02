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
# 본문을 어디까지 실을지. 페이지를 열면 **바로 기사 본문**이 나와야 한다는
# 사용자 지정(2026-10-02)에 따라 넉넉히 잡는다. 줄이려면 여기만 고친다.
EXCERPT_PARAS = 40
EXCERPT_CHARS = 12000


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


# 유료회원 전용 매체. 본문을 긁어도 안내문만 나온다.
# §31 에도 "더벨·인베스트조선 등은 본문이 막혀 제목·메타데이터·발췌만 올 수
# 있다"고 적혀 있다. 이런 곳은 **IV 페이지를 만들지 않는다.**
PAYWALL_DOMAINS = (
    "thebell.co.kr", "investchosun.com", "dealsite.co.kr", "einfomax.co.kr",
)

# 본문 자리에 이런 문구가 있으면 페이월이다.
PAYWALL_MARKERS = re.compile(
    r"유료\s*회원|유료회원\s*전용|구독.{0,6}회원|로그인.{0,8}이용|회원\s*전용"
    r"|전용입니다|결제.{0,6}후\s*이용|구독.{0,4}후\s*이용", re.I)

# 본문으로 인정할 최소 분량. 이보다 짧으면 제대로 못 가져온 것이다.
MIN_BODY_CHARS = 400

# 기사 본문이 들어 있을 만한 영역. 먼저 여기를 찾고, 없으면 문서 전체에서 <p> 를 턴다.
_CONTAINERS = [
    r'(?is)<article[^>]*>(.*?)</article>',
    r'(?is)<div[^>]*itemprop=["\']articleBody["\'][^>]*>(.*?)</div>\s*</div>',
    r'(?is)<div[^>]*(?:id|class)=["\'][^"\']*(?:article[-_]?body|news[-_]?body'
    r'|article[-_]?content|view[-_]?con|articleCont)[^"\']*["\'][^>]*>(.*?)</div>\s*</div>',
]


def fetch_article(url: str, timeout: float = 15) -> tuple[list[str], str]:
    """(문단 목록, 실패 사유). 성공하면 사유는 빈 문자열.

    **못 가져오면 빈 목록과 사유를 돌려준다.** 호출부는 그때 IV 페이지를
    만들지 않고 원래 기사로 링크해야 한다. 페이월 안내문을 본문이라고
    페이지에 실었다가 지적받았다(2026-10-02, 더벨).
    """
    if any(d in url for d in PAYWALL_DOMAINS):
        return [], "유료회원 전용 매체"
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=timeout,
                      follow_redirects=True)
        raw = r.text
    except Exception as e:
        return [], f"수집 실패({type(e).__name__})"

    raw = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form)[^>]*>.*?</\1>",
                 " ", raw)
    def harvest(scope: str) -> list[str]:
        out, seen, total = [], set(), 0
        for m in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", scope):
            t = re.sub(r"(?is)<[^>]+>", " ", m.group(1))
            t = _html.unescape(re.sub(r"\s+", " ", t)).strip()
            if len(t) < 30 or t in seen:
                continue
            if re.search(r"무단\s*전재|재배포\s*금지|저작권자|구독하기|기사제보"
                         r"|^ⓒ|Copyright|이메일|기자\s*$", t):
                continue
            seen.add(t)
            out.append(t)
            total += len(t)
            if len(out) >= EXCERPT_PARAS or total >= EXCERPT_CHARS:
                break
        return out

    # **후보마다 뽑아 보고 가장 많이 나오는 쪽을 쓴다.**
    # 처음에는 첫 번째로 매치된 컨테이너를 그냥 썼는데, <article> 태그가
    # 관련기사 카드에도 붙어 있어 거기로 범위가 좁혀지면서 본문이 0자가 됐다.
    # 문서 전체도 후보에 넣어 둔다 — 컨테이너를 못 찾는 매체가 많다.
    cands = [raw]
    for pat in _CONTAINERS:
        for m in re.finditer(pat, raw):
            if len(m.group(1)) > 300:
                cands.append(m.group(1))
    paras = max((harvest(c) for c in cands),
                key=lambda ps: sum(len(x) for x in ps), default=[])
    total = sum(len(x) for x in paras)

    joined = " ".join(paras)
    if PAYWALL_MARKERS.search(joined):
        return [], "페이월"
    if total < MIN_BODY_CHARS:
        return [], f"본문 부족({total}자)"
    return paras, ""


def fetch_excerpt(url: str, timeout: float = 15) -> list[str]:
    return fetch_article(url, timeout)[0]


def build_content(summary: str, bullets: list[str], why: str,
                  excerpt: list[str], source_url: str, source_name: str) -> list:
    """Telegraph DOM.

    **기사 본문이 맨 위다.** 핵심·주요 내용·시사점은 텔레그램 메시지에 이미
    있으므로 여기서 또 보여줄 이유가 없다. 페이지를 열면 바로 기사가 나오고,
    우리 분석은 맨 아래에 참고로 붙인다(2026-10-02 사용자 지정).
    """
    c = []
    # 본문이 없으면 애초에 페이지를 만들지 않는다(호출부에서 거른다).
    for t in excerpt:
        c.append({"tag": "p", "children": [t]})

    c.append({"tag": "hr"})
    label = f"기사 원문 — {source_name}" if source_name else "기사 원문"
    c.append({"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": source_url}, "children": [label]}]})

    # 우리 분석은 참고로 맨 아래.
    if why:
        c.append({"tag": "h4", "children": ["경영전략실 시사점"]})
        c.append({"tag": "blockquote", "children": [why]})
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
