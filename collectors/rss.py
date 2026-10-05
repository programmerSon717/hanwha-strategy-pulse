"""RSS 피드 수집기. 국내/해외 뉴스, 금융당국 보도자료 등."""
import asyncio
import calendar

import feedparser
import httpx

from models import NewsItem

# 일부 매체(Arabian Business, Tech in Asia 등)는 짧은 UA 를 403 으로 막는다.
# 브라우저와 같은 전체 UA 를 써야 통과한다.
UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _entry_epoch(entry) -> float | None:
    """RSS 엔트리의 발행시각을 epoch(UTC)로. 없거나 파싱 실패면 None."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return calendar.timegm(t)  # feedparser 는 UTC 기준 struct_time 을 준다
            except (TypeError, ValueError):
                continue
    return None


# 일시적 실패에 대한 재시도. Cloudflare 뒤에 있는 매체(CryptoBriefing 등)는
# 연결을 그냥 끊어버리는 경우가 있어, 한 번 실패했다고 버리면 그 실행에서 소스 하나를
# 통째로 놓친다. 실제로 그렇게 놓친 걸 '죽은 피드'로 오진한 적이 있다.
RETRIES = 3
RETRY_WAIT = (1, 3)          # 시도 사이 대기(초)
RETRYABLE_STATUS = {403, 408, 429, 500, 502, 503, 504}


def _retryable(e: Exception) -> bool:
    """다시 시도해볼 만한 실패인가. 404 같은 영구 실패는 재시도하지 않는다."""
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in RETRYABLE_STATUS
    # 연결 끊김·타임아웃 등 네트워크 계층 오류는 전부 일시적으로 본다
    return isinstance(e, (httpx.TransportError, asyncio.TimeoutError))


async def fetch_feed(client: httpx.AsyncClient, name: str, url: str, region_hint: str) -> list[NewsItem]:
    for attempt in range(RETRIES):
        try:
            return await _fetch_once(client, name, url, region_hint)
        except Exception as e:
            if attempt < RETRIES - 1 and _retryable(e):
                await asyncio.sleep(RETRY_WAIT[attempt])
                continue
            # 예외 메시지가 비어 있는 경우가 많다(httpx 의 연결 오류). 타입까지 찍어야
            # 나중에 원인을 알아볼 수 있다.
            detail = str(e) or "(메시지 없음)"
            print(f"[rss:{name}] fetch 실패 ({type(e).__name__}): {detail}")
            return []


async def _fetch_once(client: httpx.AsyncClient, name: str, url: str, region_hint: str) -> list[NewsItem]:
    r = await client.get(url, timeout=20, follow_redirects=True, headers=UA)
    r.raise_for_status()
    parsed = await asyncio.to_thread(feedparser.parse, r.content)
    items = []
    for e in parsed.entries[:15]:
        link = getattr(e, "link", "")
        if not link:
            continue
        summary = getattr(e, "summary", "") or ""
        items.append(
            NewsItem(
                source=name,
                unique_id=link,
                title=getattr(e, "title", ""),
                url=link,
                body=summary[:2000],
                region_hint=region_hint,
                published_at=_entry_epoch(e),
            )
        )
    return items


async def fetch_all(client: httpx.AsyncClient, sources: list) -> list[NewsItem]:
    results = await asyncio.gather(
        *[fetch_feed(client, name, url, hint) for name, url, hint in sources]
    )
    return [item for sub in results for item in sub]
