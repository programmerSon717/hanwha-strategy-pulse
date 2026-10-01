"""Upbit 공지사항 수집. 웹 프론트가 쓰는 공개 API 사용."""
import httpx

from models import NewsItem

API = "https://api-manager.upbit.com/api/v1/announcements"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


async def fetch(client: httpx.AsyncClient) -> list[NewsItem]:
    try:
        r = await client.get(
            API,
            params={"os": "web", "page": 1, "per_page": 15, "category": "all"},
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        notices = (r.json().get("data") or {}).get("notices", [])
        return [
            NewsItem(
                source="Upbit 공지",
                unique_id=str(n["id"]),
                title=n.get("title", ""),
                url=f"https://upbit.com/service_center/notice?id={n['id']}",
                region_hint="국내",
            )
            for n in notices
        ]
    except Exception as e:
        print(f"[upbit] fetch 실패: {e}")
        return []
