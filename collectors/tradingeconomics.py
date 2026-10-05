"""Trading Economics 뉴스 스트림 — 나라별 경제지표 발표를 구조화된 형태로 받는다.

investing.com 은 제목 문구로 지표 종류를 추측해야 해서 새는 게 많았다
(`연준 선호 인플레이션 지표`가 PCE 발표인 걸 문자열로는 못 알아본다).
Trading Economics 의 스트림 API 는 `category` 로 지표 종류를,
`country` 로 국가를 직접 준다. 그래서 지표 판정을 문자열 추측이 아니라
필드 값으로 한다.

참고: 사이트 RSS(`/rss/news.aspx`)는 403 이고, 한국어 페이지는 열리지만
표를 긁어야 한다. 이 스트림 엔드포인트가 웹 프론트가 실제로 쓰는 것이고
가장 안정적이다.
"""
import datetime
import os

import httpx

from models import NewsItem

API = "https://tradingeconomics.com/ws/stream.ashx"
SITE = "https://tradingeconomics.com"
SOURCE = "Trading Economics"

UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

# 사용자가 지정한 지표만 받는다. category 값을 소문자로 만든 뒤 부분일치로 본다.
# (TE 는 같은 지표를 나라마다 조금씩 다르게 적는다:
#  "Producer Prices Change" / "Producer Price Inflation" 등)
WANTED = {
    "CPI":  ("inflation rate", "core inflation", "cpi", "consumer price"),
    "PCE":  ("pce", "personal spending", "personal income"),
    "PPI":  ("producer price", "producer prices"),
    "고용":  ("non farm payrolls", "nonfarm", "unemployment rate",
            "employment change", "jobless claims"),
    "PMI":  ("pmi", "ism "),
    "GDP":  ("gdp growth", "gdp annual", "monthly gdp", "gdp"),
    "금리":  ("interest rate",),
}


def _label(category: str | None) -> str | None:
    c = (category or "").lower()
    if not c:
        return None
    for label, keys in WANTED.items():
        if any(k in c for k in keys):
            return label
    return None


def _epoch(raw: str) -> float | None:
    """'2026-08-28T15:31:10.627' — 시간대 표기가 없는 UTC."""
    try:
        return (datetime.datetime.fromisoformat(raw)
                .replace(tzinfo=datetime.timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


# TE 는 항목마다 중요도 1~3 을 준다. 실측(2026-08-28): 9.5시간에 지표 21건
# = 하루 약 53건, 그중 16건이 중요도 1(마카오 실업률·불가리아 PPI 같은 것).
# 기본값 1 은 '전부 받는다'. 탭이 시끄러우면 리포 Variables 에서 2 로 올리면
# 하루 10건 안팎만 남는다. 코드 수정 없이 조절하라고 환경변수로 뺐다.
MIN_IMPORTANCE = int(os.getenv("TE_MIN_IMPORTANCE", "1"))


async def fetch(client: httpx.AsyncClient, size: int = 60) -> list[NewsItem]:
    try:
        r = await client.get(API, params={"start": 0, "size": size},
                             headers=UA, timeout=20, follow_redirects=True)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"[tradingeconomics] fetch 실패 ({type(e).__name__}): {e or '(메시지 없음)'}")
        return []

    items: list[NewsItem] = []
    for row in rows:
        label = _label(row.get("category"))
        if not label:
            continue
        try:
            if int(row.get("importance") or 1) < MIN_IMPORTANCE:
                continue
        except (TypeError, ValueError):
            pass
        rid, title = row.get("ID"), (row.get("title") or "").strip()
        if not rid or not title:
            continue
        country = (row.get("country") or "").strip()
        page = f"{SITE}{row.get('url') or ''}"
        # 지표 페이지 주소는 발표 때마다 같다. 그대로 쓰면 URL 중복제거에 걸려
        # 다음 달 발표가 통째로 막힌다. 발표 id 를 붙여 건마다 구분한다.
        url = f"{page}?i={rid}"
        items.append(
            NewsItem(
                source=SOURCE,
                unique_id=str(rid),
                title=f"[{country}] {title}" if country else title,
                url=url,
                body=(row.get("description") or "")[:2000],
                region_hint=f"지표:{label}/{country}",
                published_at=_epoch(row.get("date")),
            )
        )
    if rows:
        print(f"[tradingeconomics] {len(rows)}건 중 지표 {len(items)}건")
    return items
