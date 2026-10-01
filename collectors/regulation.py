"""국가별 규제(Regulation) 뉴스 수집기.

크립토 매체 대부분은 Regulation 섹션 RSS 를 제공하지 않는다(확인 결과 대부분 404).
그래서 구글뉴스 검색 RSS 로 "그 나라 + 규제" 를 직접 겨냥해 긁는다.
여러 매체를 한꺼번에 훑는 효과가 있어, 사이트를 하나씩 등록하는 것보다 커버리지가 넓다.

구글뉴스 항목의 제목은 "기사제목 - 매체명" 형식이므로 매체명을 분리해 출처로 쓴다.
"""
import asyncio
import calendar
import urllib.parse

import feedparser
import httpx

from models import NewsItem

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
BASE = "https://news.google.com/rss/search"

# 광고·백과사전·강좌·도박 스팸이 섞여 들어온다.
# 특히 한국어 검색에는 카지노/토토 스팸 사이트가 구글뉴스를 파고들어 자주 잡힌다.
NOISE = ("Encyclopedia", "Guide to U.S.", "Moomoo", "Empower Your Portfolio",
         "講座", "Khóa học", "광고",
         "카지노", "바카라", "토토", "슬롯", "먹튀", "베팅사이트", "casino", "betting site")


def _url(query: str, hl: str, gl: str, ceid: str) -> str:
    return (f"{BASE}?q={urllib.parse.quote(query)}"
            f"&hl={hl}&gl={gl}&ceid={urllib.parse.quote(ceid)}")


def _split_source(title: str) -> tuple[str, str]:
    """'제목 - 매체명' → (제목, 매체명). 구분자가 없으면 매체명은 빈 문자열."""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and len(tail) < 40:
            return head.strip(), tail.strip()
    return title, ""


async def _fetch_one(client: httpx.AsyncClient, name: str, query: str, hl: str,
                     gl: str, ceid: str, hint: str, limit: int) -> list[NewsItem]:
    try:
        r = await client.get(_url(query, hl, gl, ceid), headers=UA, timeout=30,
                             follow_redirects=True)
        r.raise_for_status()
        parsed = await asyncio.to_thread(feedparser.parse, r.content)
    except Exception as e:
        print(f"[{name}] 수집 실패: {type(e).__name__} {str(e)[:60]}")
        return []

    items = []
    for e in parsed.entries[:limit]:
        raw_title = getattr(e, "title", "")
        if any(w in raw_title for w in NOISE):
            continue
        link = getattr(e, "link", "")
        if not link:
            continue

        title, outlet = _split_source(raw_title)
        ts = None
        for attr in ("published_parsed", "updated_parsed"):
            t = getattr(e, attr, None)
            if t:
                try:
                    ts = calendar.timegm(t)
                    break
                except (TypeError, ValueError):
                    pass

        items.append(
            NewsItem(
                source=f"{name}" + (f"({outlet})" if outlet else ""),
                unique_id=link,
                title=title,
                url=link,
                body=(getattr(e, "summary", "") or "")[:1500],
                region_hint=hint,
                published_at=ts,
            )
        )
    return items


async def fetch_all(client: httpx.AsyncClient, sources: list,
                    limit_each: int = 12) -> list[NewsItem]:
    results = await asyncio.gather(
        *[_fetch_one(client, name, q, hl, gl, ceid, hint, limit_each)
          for name, q, hl, gl, ceid, hint in sources]
    )
    items = [i for sub in results for i in sub]
    print(f"[규제] {len(sources)}개 국가에서 {len(items)}건 수집")
    return items
