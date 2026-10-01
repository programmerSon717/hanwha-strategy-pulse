"""블록미디어 '리서치' 카테고리 수집기 — 지정 키워드 글만 이슈 탭으로.

RSS 로는 카테고리를 못 거른다. `/archives/category/research/feed/` 는 본문 HTML 로
리다이렉트되고, `/feed/?category_name=research` 는 필터가 무시된 전체 피드가 온다.
그래서 워드프레스 REST API 로 리서치 카테고리만 직접 가져온다.

중복 주의: 리서치 글은 일반 피드(`/feed`)에도 같이 실린다. 그래서 unique_id 를
일반 피드가 만드는 링크 형태(utm 쿼리 포함)와 **똑같이** 맞춰 중복제거 키를
공유한다. 이렇게 해야 같은 글이 두 번 나가지 않는다.
"""
import html
import re
from datetime import datetime, timezone

import httpx

from models import NewsItem

API = "https://www.blockmedia.co.kr/wp-json/wp/v2/posts"
CATEGORY_ID = 56530          # 'research' 슬러그의 카테고리 id
SOURCE = "블록미디어"          # 일반 피드와 같은 이름 = 중복제거 키 공유
TARGET_TAB = "이슈"           # 사용자 지정: 이 소스는 무조건 이슈 탭

# 사용자가 지정한 키워드. 제목+본문에서 하나라도 걸리면 수집한다.
# '토큰'이 '토큰화'를 포함하지만, 로그에 어떤 말이 걸렸는지 남기려고 둘 다 둔다.
KEYWORDS = ["토큰화", "토큰", "DeFi", "디파이", "가상자산", "Digital asset"]

UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0 Safari/537.36"),
    "Accept": "application/json",
}


def _text(raw: str) -> str:
    """워드프레스가 주는 HTML 조각을 평문으로."""
    return html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).strip()


def _epoch(date_gmt: str) -> float | None:
    """REST 의 date_gmt('2026-08-26T05:13:35')는 타임존 표기가 없는 UTC 다."""
    try:
        return datetime.fromisoformat(date_gmt).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _matched(title: str, body: str) -> list[str]:
    hay = f"{title} {body}".lower()
    return [k for k in KEYWORDS if k.lower() in hay]


def _rss_style_id(link: str) -> str:
    """일반 피드가 내보내는 링크와 동일한 형태. 중복제거 키를 맞추기 위함."""
    return f"{link}?utm_source=general&utm_medium=rss"


async def fetch(client: httpx.AsyncClient, limit: int = 20,
                since: float | None = None) -> list[NewsItem]:
    try:
        r = await client.get(
            API, timeout=20, follow_redirects=True, headers=UA,
            params={"categories": CATEGORY_ID, "per_page": limit,
                    "_fields": "id,date_gmt,link,title,content"},
        )
        r.raise_for_status()
        posts = r.json()
    except Exception as e:
        print(f"[blockmedia:research] fetch 실패: {e}")
        return []

    items, skipped = [], 0
    for p in posts:
        link = p.get("link") or ""
        if not link:
            continue
        published = _epoch(p.get("date_gmt", ""))
        if since is not None and (published is None or published < since):
            continue
        title = _text(p.get("title", {}).get("rendered", ""))
        body = _text(p.get("content", {}).get("rendered", ""))
        hits = _matched(title, body)
        if not hits:
            skipped += 1
            continue
        items.append(
            NewsItem(
                source=SOURCE,
                unique_id=_rss_style_id(link),
                title=title,
                url=link,
                body=body[:2000],
                region_hint="국내/리서치",
                published_at=published,
                force_category=TARGET_TAB,
            )
        )
    if items or skipped:
        print(f"[blockmedia:research] 키워드 매치 {len(items)}건 / 제외 {skipped}건")
    return items
