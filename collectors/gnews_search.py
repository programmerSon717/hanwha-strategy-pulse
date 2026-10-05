"""구글뉴스 **날짜 지정 검색** 수집기 — 지나간 날짜를 메우는 용도.

**평소 수집에는 쓰지 않는다.** 상시 소스는 전부 RSS 인데, RSS 는 최신 항목만
노출한다. 그래서 봇이 멈춰 있던 날의 기사는 되살릴 수 없다 — 10/3~10/4 분을
다시 긁으려고 상시 수집을 세 번 돌렸지만 신규 0건이었다(2026-10-05).

구글뉴스 검색은 `after:`/`before:` 를 받아 지난 날짜를 돌려준다. 그래서
소급 생성(--backfill)에서만 이 수집기를 쓴다.

**날짜 경계는 믿되 확인한다.** 구글이 돌려준 pubDate 로 한 번 더 거른다 —
검색 연산자가 경계를 넉넉하게 잡아 범위 밖 기사를 섞어 주기 때문이다.
"""
import asyncio
import datetime
import email.utils
import re
import urllib.parse

import httpx

from models import NewsItem

KST = datetime.timezone(datetime.timedelta(hours=9))
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")}
BASE = "https://news.google.com/rss/search"

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_LINK = re.compile(r"<link>(.*?)</link>", re.S)
_DATE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_SRC = re.compile(r"<source[^>]*>(.*?)</source>", re.S)
_DESC = re.compile(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", re.S)
_TAG = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return _TAG.sub("", s or "").strip()


async def _one(client: httpx.AsyncClient, query: str,
               since: float, until: float) -> list[NewsItem]:
    a = datetime.datetime.fromtimestamp(since, KST).strftime("%Y-%m-%d")
    # before: 는 그날을 포함하지 않는다. 하루 더해 경계를 맞춘다.
    b = (datetime.datetime.fromtimestamp(until, KST)
         + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    q = f"{query} after:{a} before:{b}"
    url = f"{BASE}?q={urllib.parse.quote(q)}&hl=ko&gl=KR&ceid=KR%3Ako"
    try:
        r = await client.get(url, headers=UA, timeout=25, follow_redirects=True)
        r.raise_for_status()
    except Exception:
        return []

    out = []
    for raw in _ITEM.findall(r.text):
        t, l, d = _TITLE.search(raw), _LINK.search(raw), _DATE.search(raw)
        if not (t and l and d):
            continue
        try:
            ts = email.utils.parsedate_to_datetime(d.group(1)).timestamp()
        except (TypeError, ValueError):
            continue
        # 검색 연산자가 경계를 넉넉히 잡는다. 실제 발행시각으로 다시 건다.
        if not (since < ts <= until):
            continue
        link = _clean(l.group(1))
        if not link:
            continue
        src = _SRC.search(raw)
        # **description 을 본문으로 넣는다.** 비워 두면 모델이 "본문 없음"으로
        # 보고 confidence 를 0.5 이하로 매겨(prompts_bsp §31), 본문불가 관문에서
        # 전량 탈락한다 — 소급 수집이 통째로 무력화된다(2026-10-05 감사).
        desc = _DESC.search(raw)
        body = _clean(desc.group(1)) if desc else ""
        out.append(NewsItem(
            source=_clean(src.group(1)) if src else "구글뉴스 검색",
            unique_id=link,
            title=_clean(t.group(1)),
            url=link,
            body=body[:2000],
            region_hint="국내",
            published_at=ts,
        ))
    return out


async def fetch(client: httpx.AsyncClient, queries: list[str],
                since: float, until: float,
                concurrency: int = 4) -> list[NewsItem]:
    """질의 목록을 구간 안에서 검색해 합친다. unique_id 로 중복을 접는다."""
    sem = asyncio.Semaphore(concurrency)

    async def run(q):
        async with sem:
            items = await _one(client, q, since, until)
            # 구글이 막지 않도록 사이를 띄운다.
            await asyncio.sleep(0.4)
            return items

    groups = await asyncio.gather(*[run(q) for q in queries])
    seen, merged = set(), []
    for g in groups:
        for it in g:
            if it.unique_id in seen:
                continue
            seen.add(it.unique_id)
            merged.append(it)
    return merged


# ──────────────────────────────────────────────────────────────────────
# 유료기사 → 같은 사건의 무료 매체 기사로 교체
#
# 2026-10-05 사용자 지정: "거긴 유료기사니까 다른 무료기사 사이트에서
# 똑같은 내용 있는 거 찾아와서 대체해."
#
# **차단이 아니라 교체다.** 딜사이트·더벨·인베스트조선·연합인포맥스는
# A팀이 실제로 공유하는 딜 전문지이고 담당자이 주신 Source 목록에도 들어
# 있다(실측: 9/11~9/22 공유 96건 중 11건이 이들 매체). 통째로 막으면 딜 뉴스가
# 사라진다. 문제는 매체가 아니라 **전문을 못 읽는 링크**를 보내는 것이다.
# ──────────────────────────────────────────────────────────────────────

# 제목에서 뺄 말 — 매체가 붙이는 말머리·꼬리표. 검색어를 흐린다.
_NOISE = re.compile(r"\[[^\]]{0,12}\]|\([^)]{0,10}\)|[\"'“”‘’]")


def _keywords(title: str, n: int = 6) -> str:
    """제목에서 검색에 쓸 핵심 낱말. 조사·기호를 털어낸다."""
    t = _NOISE.sub(" ", title or "")
    words = [w for w in re.split(r"[\s·,…|/]+", t) if len(w) >= 2]
    return " ".join(words[:n])


def _overlap(a: str, b: str) -> float:
    """제목 낱말 겹침 비율. 같은 사건인지 가늠한다."""
    ta = {w for w in re.split(r"[\s·,…|/]+", _NOISE.sub(" ", a or "")) if len(w) >= 2}
    tb = {w for w in re.split(r"[\s·,…|/]+", _NOISE.sub(" ", b or "")) if len(w) >= 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


async def free_alternative(client: httpx.AsyncClient, title: str,
                           published_at: float | None,
                           is_paywalled, min_overlap: float = 0.45,
                           store=None):
    """같은 사건을 다룬 **무료 매체** 기사. 못 찾으면 None.

    발행시각 ±2일 안에서만 찾는다 — 제목이 비슷한 옛 기사를 끌어오면
    8월 기사가 섞이는 사고가 난다(2026-10-05 KAI 건).

    **주소를 풀어서 유료 여부를 본다.** 구글뉴스가 돌려주는 링크는 전부
    news.google.com 리디렉터라, 풀지 않고 도메인만 보면 딜사이트 기사가
    '무료 대체' 로 돌아온다(2026-10-05 실측).
    """
    import gnews
    q = _keywords(title)
    if not q:
        return None
    if published_at:
        base = datetime.datetime.fromtimestamp(published_at, KST)
        lo = (base - datetime.timedelta(days=2)).timestamp()
        hi = (base + datetime.timedelta(days=2)).timestamp()
    else:
        # 발행시각을 모르면 **지금 기준 ±2일**로 묶는다. 예전엔 1970~2038 전체를
        # 뒤져서 8월 기사가 대체로 딸려올 수 있었다(2026-10-05 감사).
        now = datetime.datetime.now(KST)
        lo = (now - datetime.timedelta(days=2)).timestamp()
        hi = (now + datetime.timedelta(days=1)).timestamp()
    cands = await _one(client, q, lo, hi)
    # 제목이 닮은 순으로 보고, 닮은 것부터 주소를 푼다 — 전부 풀면 느리다.
    scored = sorted(
        ((_overlap(title, it.title), it) for it in cands),
        key=lambda x: -x[0])
    for ov, it in scored[:8]:
        if ov < min_overlap:
            break
        real = gnews.resolve(it.url or "", store)
        if not real or is_paywalled(real):
            continue
        it.url = real          # 발행은 푼 주소로 한다
        return it
    return None
