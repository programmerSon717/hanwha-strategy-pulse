"""Trading Economics 경제 캘린더 — 발표된 지표의 실제/예상/이전 값을 그대로 가져온다.

뉴스 기사는 발표 뒤 몇 분~수십 분 늦게 나온다. 캘린더는 발표 즉시 값이 채워지므로
**숫자를 가장 빨리 전할 수 있는 경로**다. 기사 요약(스트림 API 경로)과는 성격이 다르다.

시간대: 캘린더 시각은 **UTC** 다. 실증 — 캐나다 GDP 가 캘린더에 12:30 PM,
스트림 API 에는 12:38 UTC 로 찍혔고 실제 발표 시각(08:30 ET = 12:30 UTC)과 맞는다.
**이 전제가 틀리면 '방금 발표된 것' 판정이 통째로 어긋난다.**

HTML 주의: 국가 칸에 표가 중첩돼 있어 평범한 <tr>...</tr> 매칭은 첫 </tr> 에서 끊긴다.
그래서 data-id 위치로 블록을 자르고, 중첩 <table> 을 지운 뒤 셀을 읽는다.
컬럼 순서는 [시각, (국가), 지표명, 실제, 이전, 예측치, 예상] 이다.
"""
import datetime
import html
import os
import re

import httpx

from models import NewsItem

URL = "https://ko.tradingeconomics.com/calendar"
SOURCE = "TE 캘린더"
UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0 Safari/537.36"),
}

# 사용자가 지정한 지표만. 캘린더 전체는 하루 74건이라 그대로 받으면 탭이 도배된다.
WANTED = re.compile(
    r"소비자물가|CPI|인플레이션|PCE|생산자물가|PPI|비농업|실업률|고용|"
    r"구매관리자|PMI|ISM|GDP|성장률|국내총생산|금리|FOMC", re.I)

# 중요도(calendar-date-N, 1~3). 2 이상이면 평일 약 10건. 1까지 받으면 30건 넘는다.
MIN_IMPORTANCE = int(os.getenv("TE_CAL_MIN_IMPORTANCE", "2"))

# 발표 직후만 잡는다. 이 창을 넘긴 과거 이벤트는 아예 보지 않는다 —
# 사용자 지시: 지난 발표를 뒤늦게 긁어오지 말 것.
FRESH_MIN = int(os.getenv("TE_CAL_FRESH_MIN", "45"))

_US = ("US",)


def _cells(block: str) -> list[str]:
    no_nested = re.sub(r"<table.*?</table>", "<td></td>", block, flags=re.S)
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split())
            for c in re.findall(r"<td[^>]*>(.*?)(?=<td|</tr>|$)", no_nested, re.S)]


def _dt(date_str: str, time_str: str) -> datetime.datetime | None:
    """'2026-08-28' + '12:30 PM' → UTC datetime. 시각이 없으면(종일 이벤트) None."""
    if not date_str or not time_str:
        return None
    try:
        t = datetime.datetime.strptime(time_str.strip(), "%I:%M %p").time()
    except ValueError:
        return None
    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        return None
    return datetime.datetime.combine(d, t, tzinfo=datetime.timezone.utc)


def _num(v: str) -> str:
    """'12.6% ®' 처럼 붙는 개정 표시(®)를 떼어낸다."""
    return re.sub(r"\s*®\s*", "", v or "").strip()


def parse(page: str) -> list[dict]:
    starts = [m.start() for m in re.finditer(r'<tr[^>]*\bdata-id="\d+"', page)]
    starts.append(len(page))
    out = []
    for i in range(len(starts) - 1):
        b = page[starts[i]:starts[i + 1]]
        rid = re.search(r'data-id="(\d+)"', b)
        date = re.search(r"class='\s*(\d{4}-\d{2}-\d{2})'", b)
        iso = re.search(r'calendar-iso"[^>]*>([^<]+)', b)
        if not (rid and date):
            continue
        c = _cells(b)
        if len(c) < 5:
            continue
        lv = re.findall(r"calendar-date-(\d)", b)
        out.append({
            "id": rid.group(1),
            "iso": (iso.group(1).strip() if iso else ""),
            "when": _dt(date.group(1), c[0]),
            "event": c[3],
            "actual": _num(c[4]),
            "previous": _num(c[5]) if len(c) > 5 else "",
            "forecast": _num(c[7]) if len(c) > 7 else "",
            "importance": int(lv[0]) if lv else 1,
            "url": "https://tradingeconomics.com" + (
                re.search(r'data-url="([^"]*)"', b).group(1)
                if re.search(r'data-url="([^"]*)"', b) else ""),
        })
    return out


async def fetch(client: httpx.AsyncClient) -> list[NewsItem]:
    """방금 값이 채워진 지표만 NewsItem 으로. 예정·과거 이벤트는 내보내지 않는다."""
    try:
        r = await client.get(URL, headers=UA, timeout=25, follow_redirects=True)
        r.raise_for_status()
        rows = parse(r.text)
    except Exception as e:
        print(f"[te-calendar] fetch 실패 ({type(e).__name__}): {e or '(메시지 없음)'}")
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    items, ready = [], 0
    for row in rows:
        if row["importance"] < MIN_IMPORTANCE or not WANTED.search(row["event"]):
            continue
        if not row["actual"] or row["actual"] == "-":
            continue          # 아직 발표 전이거나 숫자가 없는 이벤트(연설 등)
        when = row["when"]
        if when is None:
            continue
        age_min = (now - when).total_seconds() / 60
        if not (-5 <= age_min <= FRESH_MIN):
            continue          # 방금 발표된 것만. 과거는 뒤늦게 긁어오지 않는다
        ready += 1
        head = f"[{row['iso']}] {row['event']} — 실제 {row['actual']}"
        detail = [f"지표: {row['event']}", f"국가: {row['iso']}",
                  f"실제: {row['actual']}"]
        if row["forecast"]:
            head += f" (예상 {row['forecast']})"
            detail.append(f"시장 예상: {row['forecast']}")
        if row["previous"]:
            detail.append(f"직전: {row['previous']}")
        detail.append("출처: Trading Economics 경제 캘린더 (발표 즉시 집계된 확정치)")
        items.append(NewsItem(
            source=SOURCE,
            unique_id=row["id"],
            title=head,
            url=row["url"],
            body="\n".join(detail),
            region_hint=f"지표:캘린더/{row['iso']}",
            published_at=when.timestamp(),
        ))
    if rows:
        print(f"[te-calendar] 행 {len(rows)}건 중 방금 발표 {ready}건")
    return items
