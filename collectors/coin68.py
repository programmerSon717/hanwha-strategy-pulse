"""Coin68(베트남) 법률·정책 섹션 수집기.

RSS 를 제공하지 않아 페이지의 __NEXT_DATA__(Next.js 서버 데이터)에서 글 목록을 뽑는다.
HTML 구조를 긁는 것보다 이쪽이 훨씬 덜 깨진다.

베트남 정책 뉴스는 한국·영어권 매체가 거의 다루지 않아, 이 소스가 사실상 유일한 창구다.
"""
import json
import re
from datetime import datetime

import httpx

from models import NewsItem

URL = "https://coin68.com/article/phap-ly/"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _walk(obj):
    """중첩 구조 어디에 있든 글처럼 보이는 dict 를 찾아낸다."""
    if isinstance(obj, dict):
        if isinstance(obj.get("title"), str) and obj.get("slug"):
            yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _epoch(post: dict) -> float | None:
    for k in ("published_at", "publishedAt", "created_at", "createdAt", "date"):
        v = post.get(k)
        if not isinstance(v, str):
            continue
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


async def fetch(client: httpx.AsyncClient, limit: int = 15) -> list[NewsItem]:
    try:
        r = await client.get(URL, headers=UA, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        print(f"[coin68] 요청 실패: {type(e).__name__} {str(e)[:60]}")
        return []

    m = _NEXT.search(r.text)
    if not m:
        print("[coin68] __NEXT_DATA__ 를 찾지 못함 — 사이트 구조가 바뀐 듯")
        return []

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"[coin68] JSON 파싱 실패: {e}")
        return []

    items, seen = [], set()
    for post in _walk(data):
        slug = post["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        items.append(
            NewsItem(
                source="Coin68(베트남)",
                unique_id=f"coin68:{slug}",
                title=post["title"],
                url=f"https://coin68.com/{slug}/",
                body=(post.get("description") or post.get("excerpt") or "")[:2000],
                # 베트남어 기사다. 분류·요약 시 베트남 정책일 가능성을 높게 보라는 힌트.
                region_hint="베트남",
                published_at=_epoch(post),
            )
        )
        if len(items) >= limit:
            break

    print(f"[coin68] {len(items)}건 수집")
    return items
