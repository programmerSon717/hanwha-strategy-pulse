"""빗썸 공지 수집. 공개 API 사용.

왜 넣었나: 업비트 공지는 클라우드(데이터센터 IP)에서 403 으로 막힌다.
국내 거래소 소식이 통째로 비는데, 빗썸은 열려 있어 그 자리를 메운다.
(2026-08-29 확인. 빗썸도 언젠가 막을 수 있으니 실패하면 조용히 건너뛴다)

`categories` 로 공지 성격이 그대로 온다 — 거래유의·거래지원종료·마켓추가 등.
그걸 제목에 붙여 모델이 분류하기 쉽게 만든다.
"""
import datetime

import httpx

from models import NewsItem

API = "https://api.bithumb.com/v1/notices"
SOURCE = "빗썸 공지"
KST = datetime.timezone(datetime.timedelta(hours=9))
UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0 Safari/537.36"),
    "Accept": "application/json",
}


def _epoch(raw: str) -> float | None:
    """'2026-08-28 16:30:00' — 표기가 없지만 KST 다."""
    try:
        return (datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=KST).timestamp())
    except (TypeError, ValueError):
        return None


async def fetch(client: httpx.AsyncClient, count: int = 20) -> list[NewsItem]:
    try:
        r = await client.get(API, params={"count": count}, headers=UA,
                             timeout=15, follow_redirects=True)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"[bithumb] 공지 실패 ({type(e).__name__}): {e or '(메시지 없음)'}")
        return []

    items = []
    for n in rows:
        url = (n.get("pc_url") or "").strip()
        title = (n.get("title") or "").strip()
        if not url or not title:
            continue
        cats = " · ".join(n.get("categories") or [])
        items.append(NewsItem(
            source=SOURCE,
            unique_id=url,
            title=f"[빗썸] {title}" + (f" ({cats})" if cats else ""),
            url=url,
            body=f"빗썸 공지사항. 분류: {cats or '일반'}. 제목: {title}",
            region_hint="국내/거래소",
            published_at=_epoch(n.get("published_at", "")),
        ))
    if rows:
        print(f"[bithumb] 공지 {len(items)}건")
    return items
