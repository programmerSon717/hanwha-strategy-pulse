"""블록미디어 특정 날짜 기사 수집(백필 전용).

RSS는 최신 10건만 주므로 하루치를 다 담지 못한다. robots.txt 에 공개적으로 명시된
사이트맵(sitemap_index.xml)에서 해당 날짜 URL을 뽑아 기사 페이지에서 제목/설명을 읽는다.

주의: post-sitemap.xml(번호 없음)은 2018년치다. 번호가 큰 파일일수록 최신이므로
번호 없는 것을 0으로 두고 정렬해야 최신 파일을 고를 수 있다.
"""
import asyncio
import calendar
import re
import time

import httpx

from models import NewsItem

INDEX = "https://www.blockmedia.co.kr/sitemap_index.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; crypto-news-bot/1.0)"}

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
    re.DOTALL | re.IGNORECASE,
)


def _sitemap_num(url: str) -> int:
    m = re.search(r"post-sitemap(\d*)\.xml", url)
    return int(m.group(1)) if m and m.group(1) else 0


async def _newest_sitemaps(client: httpx.AsyncClient, count: int = 2) -> list[str]:
    r = await client.get(INDEX, headers=HEADERS, timeout=30)
    r.raise_for_status()
    posts = [u for u in re.findall(r"<loc>(.*?)</loc>", r.text) if "post-sitemap" in u]
    posts.sort(key=_sitemap_num)
    return posts[-count:]


async def _article(client: httpx.AsyncClient, url: str, epoch: float) -> NewsItem | None:
    try:
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
        html = r.text
        tm = _TITLE_RE.search(html)
        title = tm.group(1).strip() if tm else ""
        # WordPress 제목은 보통 "제목 - 블록미디어" 형태
        title = re.sub(r"\s*[-|]\s*블록미디어\s*$", "", title)
        if not title:
            return None
        dm = _DESC_RE.search(html)
        body = dm.group(1).strip() if dm else ""
        return NewsItem(
            source="블록미디어",
            unique_id=url,          # RSS와 동일한 키(URL)라 중복 발행되지 않는다
            title=title,
            url=url,
            body=body[:2000],
            region_hint="국내",
            published_at=epoch,
        )
    except Exception as e:
        print(f"[blockmedia_archive] 기사 실패 {url}: {e}")
        return None


async def fetch_date(client: httpx.AsyncClient, date_str: str) -> list[NewsItem]:
    """date_str: 'YYYY-MM-DD' (사이트맵 lastmod 기준)."""
    try:
        sitemaps = await _newest_sitemaps(client)
    except Exception as e:
        print(f"[blockmedia_archive] 사이트맵 실패: {e}")
        return []

    targets: list[tuple[str, float]] = []
    for sm in sitemaps:
        try:
            r = await client.get(sm, headers=HEADERS, timeout=30)
            r.raise_for_status()
            for loc, mod in re.findall(
                r"<url>\s*<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", r.text, re.DOTALL
            ):
                if mod.startswith(date_str):
                    epoch = calendar.timegm(time.strptime(mod[:19], "%Y-%m-%dT%H:%M:%S"))
                    targets.append((loc, epoch))
        except Exception as e:
            print(f"[blockmedia_archive] {sm} 실패: {e}")

    print(f"[blockmedia_archive] {date_str} 대상 {len(targets)}건 — 본문 수집 중")
    items = []
    for url, epoch in targets:
        it = await _article(client, url, epoch)
        if it:
            items.append(it)
        await asyncio.sleep(1)  # 상대 서버 배려
    return items
