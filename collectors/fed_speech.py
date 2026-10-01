"""연준 의장·이사 연설 — RSS 로 잡고 **연설문 본문까지 가져온다**.

왜 따로 만들었나: 기존에 쓰던 `press_all.xml` 에는 **연설이 들어가지 않는다.**
그래서 2026-08-28 워시 의장 잭슨홀 연설 원문이 봇에 도달한 적이 없고, 채널에는
"연설 시작"류 속보만 나갔다. 연설 전용 피드는 `speeches.xml` 이다.

본문을 따로 받는 이유: RSS 의 description 이 176자(장소 안내)뿐이라 그것만으로
요약하면 알맹이가 없고 모델이 지어낼 여지가 생긴다. 연설은 한 달에 몇 건뿐이라
페이지를 직접 받아도 부담이 없다.
"""
import asyncio
import calendar
import html
import re

import feedparser
import httpx

from models import NewsItem

FEED = "https://www.federalreserve.gov/feeds/speeches.xml"
SOURCE = "연준 연설"
UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0 Safari/537.36"),
}

# 정부 사이트 공통 안내문. 본문에 섞이면 요약이 엉뚱해진다.
_BOILER = re.compile(
    r"\.gov (website|websites)|padlock|Federal Reserve, the central bank|"
    r"Back to Top|Last Update|Constitution Avenue")

# 모델에 넘길 본문 상한.
# 6000 으로 잘랐더니 연설 앞부분(인사·AI 얘기)만 들어가고 **시장이 보는 부분
# (물가·금리 판단)이 통째로 잘렸다.** 연준 연설은 뒤로 갈수록 핵심이 나온다.
# 하루 몇 건 안 되므로 넉넉히 준다.
BODY_LIMIT = 18000
# 오래된 연설까지 본문을 받지 않도록 하는 상한(시간).
MAX_AGE_H = 24


def _extract(page: str) -> str:
    m = re.search(r'id="article"(.*)', page, re.S)
    body = m.group(1) if m else page
    paras = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", p)).split())
             for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)]
    keep = [p for p in paras if len(p) > 60 and not _BOILER.search(p)]
    return "\n\n".join(keep)[:BODY_LIMIT]


async def fetch(client: httpx.AsyncClient) -> list[NewsItem]:
    try:
        r = await client.get(FEED, headers=UA, timeout=20, follow_redirects=True)
        r.raise_for_status()
        parsed = await asyncio.to_thread(feedparser.parse, r.content)
    except Exception as e:
        print(f"[fed-speech] 피드 실패 ({type(e).__name__}): {e or '(메시지 없음)'}")
        return []

    import time as _t
    now = _t.time()
    items: list[NewsItem] = []
    for e in parsed.entries[:8]:
        link = getattr(e, "link", "")
        if not link:
            continue
        ts = None
        if getattr(e, "published_parsed", None):
            ts = calendar.timegm(e.published_parsed)
        # 지난 연설의 본문까지 받아올 이유가 없다. 최근 것만 본다.
        if ts and (now - ts) > MAX_AGE_H * 3600:
            continue
        title = getattr(e, "title", "")
        blurb = getattr(e, "summary", "") or ""
        body = blurb
        try:
            page = await client.get(link, headers=UA, timeout=25, follow_redirects=True)
            page.raise_for_status()
            full = _extract(page.text)
            if len(full) > len(blurb):
                body = f"{blurb}\n\n{full}"
        except Exception as ex:
            print(f"[fed-speech] 본문 실패({title[:30]}): {type(ex).__name__}")
        items.append(NewsItem(
            source=SOURCE,
            unique_id=link,
            title=f"[연준 연설] {title}",
            url=link,
            body=body[:BODY_LIMIT],
            region_hint="지표/긴급:연준연설",
            published_at=ts,
        ))
    if items:
        print(f"[fed-speech] 최근 {MAX_AGE_H}시간 연설 {len(items)}건 (본문 포함)")
    return items
