"""Binance 공지사항 수집.

공식 문서화된 엔드포인트는 아니지만 웹 프론트가 쓰는 CMS API를 그대로 사용.
차단(403/CAPTCHA)될 수 있으므로 User-Agent를 지정하고, 실패 시 조용히 스킵한다.
catalogId 참고: 48=신규상장, 49=최신공지, 161=상장폐지 등.
"""
import httpx

from models import NewsItem

API = "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
CATALOGS = [48, 49]  # 신규 상장 + 일반 공지


async def fetch(client: httpx.AsyncClient) -> list[NewsItem]:
    items: list[NewsItem] = []
    for catalog_id in CATALOGS:
        try:
            r = await client.get(
                API,
                params={"type": 1, "pageNo": 1, "pageSize": 10, "catalogId": catalog_id},
                headers=HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            articles = (data.get("data") or {}).get("catalogs", [])
            for cat in articles:
                for a in cat.get("articles", []):
                    code = a.get("code", "")
                    items.append(
                        NewsItem(
                            source="Binance 공지",
                            unique_id=code,
                            title=a.get("title", ""),
                            url=f"https://www.binance.com/en/support/announcement/{code}",
                            region_hint="해외",
                        )
                    )
        except Exception as e:
            print(f"[binance] fetch 실패 (catalog {catalog_id}): {e}")
    return items
