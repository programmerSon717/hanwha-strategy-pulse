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

# 본문이 없을 때 페이지에 적는 한 줄. 지어내지 않고 사실만 밝힌다.
NOTE_PAYWALL = "원문이 유료회원 전용이라 본문을 싣지 않았습니다. 아래 링크에서 보세요."
NOTE_FAILED = "본문을 가져오지 못했습니다. 아래 링크에서 원문을 보세요."

# 기사 본문이 들어 있을 만한 영역. 후보를 모두 뽑아 보고 가장 많이 나오는 쪽을 쓴다.
_CONTAINERS = [
    r'(?is)<div[^>]*itemprop=["\']articleBody["\'][^>]*>(.*)',
    r'(?is)<article[^>]*itemprop=["\']articleBody["\'][^>]*>(.*)',
    r'(?is)<(?:div|article|section)[^>]*(?:id|class)=["\'][^"\']*'
    r'(?:article[-_]?body|news[-_]?body|article[-_]?content|article[-_]?txt'
    r'|view[-_]?con|articleCont|news[-_]?txt|^article$)[^"\']*["\'][^>]*>(.*)',
    r'(?is)<article[^>]*>(.*?)</article>',
]

# 본문이 아닌 상투 문구. 매체마다 다른 자리에 끼어든다.
_BOILERPLATE = re.compile(
    r"무단\s*전재|재배포\s*금지|저작권자|구독하기|기사제보|ⓒ|Copyright"
    r"|@.*\.(?:com|co\.kr)|기자\s*$|댓글|로그인|회원가입|많이\s*본"
    r"|관련\s*기사|이전\s*기사|다음\s*기사"
    # 브라우저·앱 권유 안내 (잠깐! 현재 Internet Explorer 8이하 …)
    r"|Internet\s*Explorer|최신\s*브라우저|브라우저\s*\(Browser\)|앱\s*설치"
    r"|푸시\s*알림|뉴스레터\s*구독|카카오톡\s*채널|네이버에서\s*구독"
    r"|사진\s*=|이미지\s*=|자료\s*=\s*$", re.I)


# 본문이 끝나고 추천·인기 기사 목록이 시작되는 지점.
# 컨테이너 정규식이 (.*) 탐욕 매칭이라 문서 끝까지 먹는 바람에 "바비인형
# 제조사 마텔 주가 폭등", "럭셔리 미니밴 시대" 같은 광고성 추천 기사 제목이
# 본문에 섞여 들어왔다(2026-10-02). 여기서 잘라낸다.
_TAIL = re.compile(
    r"(?is)(관련\s*기사|많이\s*본|추천\s*기사|인기\s*기사|주요\s*뉴스"
    r"|실시간\s*뉴스|핫\s*이슈|많이\s*읽은|이\s*기사[를와]?\s*공유"
    r"|<footer|(?:id|class)=[\"\'][^\"\']*"
    r"(?:related|recommend|popular|ranking|most[-_]?read|aside|footer)"
    r"|taboola|outbrain|dable)")


def _cut_tail(html_text: str) -> str:
    m = _TAIL.search(html_text)
    return html_text[:m.start()] if m and m.start() > 300 else html_text


# 본문 문장이 아니라 **기사 제목**처럼 보이는 줄. 추천 목록의 잔재다.
#
# 한국어 기사 본문은 거의 언제나 종결어미로 끝난다(…했다 / …이다 / …전망이다).
# 반면 제목은 명사·인용부호로 끝난다("럭셔리 미니밴 시대", "…머니무브").
# 그래서 **종결어미가 없고 짧은 줄**을 제목으로 본다.
# 짧이 제한을 두는 이유: 긴 줄은 제목일 가능성이 낮고, 표·인용처럼 종결어미가
# 없는 본문도 있어서 과하게 자르지 않으려는 안전장치다.
_SENT_END = re.compile(
    r"(?:다|음|임|함|요|죠|까|냐|랴|네|오|소|듯|것|터|뿐|중|간|년|월|일|원|%|\))"
    r"[.!?\u3002\"\'\u201d\u2019]*\s*$")
HEADLINE_MAX_LEN = 90


def _looks_like_headline(line: str) -> bool:
    if len(line) > HEADLINE_MAX_LEN:
        return False
    return not _SENT_END.search(line)


def _key(s: str) -> str:
    """제목 비교용 정규화 — 공백·기호를 지우고 비교한다."""
    return re.sub(r"[^\w가-힣]+", "", s or "")[:60]


def fetch_article(url: str, timeout: float = 15,
                  title: str = "") -> tuple[list[str], str]:
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
    # 페이지 자신의 제목도 비교 대상에 넣는다. 우리 headline 은 모델이 다시 쓴
    # 것이라 원문 제목과 글자가 달라, 그것만으로는 중복을 못 거른다.
    page_title = ""
    m = re.search(r'(?is)<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', raw)
    if not m:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    if m:
        page_title = _html.unescape(m.group(1)).strip()

    def harvest(scope: str) -> list[str]:
        """문단을 뽑는다.

        **<p> 태그만 보면 안 된다.** 비즈니스포스트는 <p> 1개에 <br> 47개,
        뉴시스는 <p> 0개에 <br> 24개다 — 문단을 <br> 로 나누는 매체가 많다.
        그래서 <br>·</p>·</div>·</li> 를 전부 줄바꿈으로 바꾼 뒤 줄 단위로 센다.
        """
        t = re.sub(r"(?is)<br\s*/?>", "\n", scope)
        t = re.sub(r"(?is)</(p|div|li|h[1-6]|td|tr)>", "\n", t)
        t = re.sub(r"(?is)<[^>]+>", " ", t)
        t = _html.unescape(t)

        out, seen, total = [], set(), 0
        keys = [k for k in (_key(title), _key(page_title)) if len(k) >= 12]
        for line in t.split("\n"):
            line = re.sub(r"[ \t\u00a0]+", " ", line).strip()
            # "본문 | 매체명" 꼬리를 떼낸다. 페이지 <title> 이 섞여 들어온다.
            line = re.sub(r"\s*\|\s*[^|]{1,24}\s*$", "", line).strip()
            if len(line) < 30 or line in seen:
                continue
            # 제목이 본문 첫 줄로 또 들어오는 매체가 많다.
            # Telegraph 가 제목을 이미 맨 위에 보여주므로 본문에서는 뺀다.
            lk = _key(line)
            if any(lk == k or lk in k or k in lk for k in keys):
                continue
            if _BOILERPLATE.search(line):
                continue
            if _looks_like_headline(line):
                continue
            seen.add(line)
            out.append(line)
            total += len(line)
            if len(out) >= EXCERPT_PARAS or total >= EXCERPT_CHARS:
                break
        return out

    # **후보마다 뽑아 보고 가장 많이 나오는 쪽을 쓴다.**
    # 처음에는 첫 번째로 매치된 컨테이너를 그냥 썼는데, <article> 태그가
    # 관련기사 카드에도 붙어 있어 거기로 범위가 좁혀지면서 본문이 0자가 됐다.
    # 문서 전체도 후보에 넣어 둔다 — 컨테이너를 못 찾는 매체가 많다.
    cands = [_cut_tail(raw)]
    for pat in _CONTAINERS:
        for m in re.finditer(pat, raw):
            if len(m.group(1)) > 300:
                cands.append(_cut_tail(m.group(1)))
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
                  excerpt: list[str], source_url: str, source_name: str,
                  note: str = "") -> list:
    """Telegraph DOM.

    **페이지는 언제나 만든다.** 제목을 누르면 Instant View 로 열려야 한다는 것이
    절대 규칙이다(2026-10-02 사용자 지정). 본문을 못 가져왔다고 페이지를 만들지
    않으면 그 기사만 브라우저로 튕겨 나간다.

    본문이 있으면 본문이 맨 위다. 없으면 — 유료기사라 긁지 않았거나 추출에
    실패한 경우 — **지어내지 않고** 우리가 가진 핵심·주요 내용·시사점을 싣고,
    왜 전문이 없는지 한 줄로 밝힌 뒤 원문으로 보낸다.
    """
    c = []
    if excerpt:
        for t in excerpt:
            c.append({"tag": "p", "children": [t]})
    else:
        if note:
            c.append({"tag": "blockquote", "children": [note]})
        if summary:
            c.append({"tag": "h4", "children": ["핵심"]})
            c.append({"tag": "p", "children": [summary]})
        if bullets:
            c.append({"tag": "h4", "children": ["주요 내용"]})
            c.append({"tag": "ul", "children": [
                {"tag": "li", "children": [b]} for b in bullets]})

    c.append({"tag": "hr"})
    label = f"기사 원문 — {source_name}" if source_name else "기사 원문"
    c.append({"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": source_url}, "children": [label]}]})

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
