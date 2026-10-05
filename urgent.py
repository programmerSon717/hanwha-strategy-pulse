"""긴급 레인 — 지표 발표·FOMC·잭슨홀처럼 늦으면 가치가 없어지는 건을 즉시 잡는다.

일반 폴링은 소스 60여 곳을 20분 주기로 훑는다. 그 주기로는 경제지표 발표가 최대
20분 늦고, 잭슨홀 연설처럼 시장이 실시간 반응하는 건은 이미 늦은 뒤에 나간다.
실제로 2026-08-28 워시 의장 잭슨홀 연설(23:00 KST)이 그날 폴링에 잡히지 않았다.

그래서 **소스를 몇 곳으로 좁혀 짧은 주기로 따로 돌린다.** 전체 스윕과 분리돼 있어
일반 파이프라인의 동작·비용에 영향을 주지 않는다.
"""
import re

import httpx

from collectors import fed_speech, rss, te_calendar, tradingeconomics
from config import settings
from models import NewsItem

# 지표·정책 이벤트가 갈 탭.
#   미국 건은 🇺🇸미국매크로(키 'US Rates'), 나머지 나라는 🌐글로벌매크로.
#   예전에는 전부 Global Macro 로 몰았는데, 사용자가 "미국 매크로는 미국매크로 탭에
#   모으고 싶다"고 해서 나눈다. country.enforce() 도 원래 이 방향으로 보정한다.
US_TAB = "US Rates"
TARGET_TAB = "Global Macro"

# FOMC·미국 거시·잭슨홀은 여기에도 한 부 더 올린다(사용자 지시).
# 채널에서 가장 많이 보는 탭이라 미국 관련 굵직한 건은 놓치면 안 된다.
MIRROR_TAB = "이슈"

# 미러 대상 판정에 쓰는 미국 관련 표현
_US = re.compile(
    r"미국|美|연준|연방준비|\bfed\b|\bfomc\b|united states|\bu\.s\.|\bUS\b|"
    r"워시|잭슨홀|jackson\s?hole")


def is_us(title: str, region_hint: str) -> bool:
    return bool(_US.search(f"{title} {region_hint}"))


def target_tab(title: str, region_hint: str) -> str:
    """미국 건은 미국매크로 탭, 나머지는 글로벌매크로 탭."""
    return US_TAB if is_us(title, region_hint) else TARGET_TAB


def should_mirror(label: str, title: str, region_hint: str) -> bool:
    """이슈 탭에도 보낼 것인가.

    대상: FOMC · 잭슨홀 · 미국 거시. 그 외 나라의 지표는 매크로 탭에만 둔다.
    """
    if label in ("FOMC", "잭슨홀"):
        return True
    return is_us(title, region_hint)

# 제목에 하나라도 걸리면 긴급으로 본다. **제목만 본다** — 본문까지 보면
# 크립토 기사에 스치듯 언급된 것까지 걸려 오탐이 급증한다.
#
# 단순 단어 매칭으로는 새는 게 많아 정규식을 쓴다. 실제로 놓쳤던 것들:
#   "연준 선호 인플레이션 지표, 7월에도 목표치 상회"  ← PCE 발표인데 'PCE'가 없다
#   "체코 경제, 2분기 0.4% 성장"                  ← GDP 발표인데 '성장률'이 아니다
# 반대로 '인플레이션'만 넣으면 논평 기사까지 다 걸리므로, 발표를 뜻하는 말
# (상승·둔화·지표·기록 등)과 함께 있을 때만 잡는다.
KEYWORDS: dict[str, str] = {
    "CPI":   r"소비자\s?물가|\bcpi\b|인플레이션.{0,25}(상승|하락|둔화|가속|지표|기록|예상|%)|기조적\s?인플레",
    "PCE":   r"\bpce\b|개인\s?소비\s?지출",
    "PPI":   r"생산자\s?물가|\bppi\b",
    "고용":   r"비농업|고용\s?(지표|보고서|동향|시장)|노동\s?시장|실업률|실업수당|nonfarm|non-farm|unemployment|payroll",
    "PMI":   r"\bism\b|\bpmi\b|구매관리자",
    "GDP":   r"\bgdp\b|국내총생산|성장률|\d\s?분기[^,]{0,30}성장",
    "FOMC":  r"\bfomc\b|연방공개시장위원회|금리\s?(결정|인상|인하|동결|경로)|기준금리|점도표|매파|비둘기파|긴축|완화\s?기조|양적\s?긴축|\bqt\b|\bqe\b",
    "유동성": r"신용\s?스프레드|대출\s?(기준|태도)|자금\s?조달|기업\s?자금|금융\s?여건|유동성",
    "잭슨홀": r"잭슨홀|jackson\s?hole",
    # 트럼프 발언은 크립토·거시 양쪽에 즉각 반영된다. 다만 관세·이민 같은
    # 무관한 트럼프 기사까지 다 걸면 탭이 도배되므로 **주제어와 함께** 있을 때만 잡는다.
    "트럼프": r"(트럼프|trump).{0,40}(암호화폐|가상자산|크립토|비트코인|블록체인|스테이블코인|crypto|bitcoin|"
             r"연준|\bfed\b|금리|인플레이션|관세.{0,10}물가|고용|실업|증시|달러)|"
             r"(암호화폐|가상자산|크립토|비트코인|블록체인|연준|금리|인플레이션).{0,40}(트럼프|trump)",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in KEYWORDS.items()}


def urgency_of(title: str) -> str | None:
    """제목이 어떤 긴급 항목에 해당하는가. 아니면 None."""
    t = title or ""
    for label, pat in _COMPILED.items():
        if pat.search(t):
            return label
    return None


async def collect(client: httpx.AsyncClient) -> list[NewsItem]:
    """긴급 소스를 훑어 지표·정책 이벤트만 돌려준다.

    두 경로를 쓴다:
      - Trading Economics: `category` 필드로 지표 종류를 **정확히** 판정한다.
        나라별 지표 발표의 주 공급원.
      - RSS(투자·연준): 제목 정규식으로 판정한다. FOMC 성명·연준 발언·잭슨홀처럼
        지표가 아닌 정책 이벤트를 잡는 경로.
    """
    hits: list[NewsItem] = []

    # ── 1. 지표 발표 '숫자' (캘린더) ──
    # 기사보다 빠르다. 발표 즉시 실제/예상/직전 값이 채워지므로 그대로 전한다.
    # 발표 직후 창(기본 45분)을 벗어난 과거 이벤트는 아예 내보내지 않는다.
    for it in await te_calendar.fetch(client):
        it.force_category = target_tab(it.title, it.region_hint)
        if should_mirror("지표", it.title, it.region_hint):
            it.mirror_to = MIRROR_TAB
        hits.append(it)

    # ── 2. 지표 발표 '기사' (구조화된 판정) ──
    for it in await tradingeconomics.fetch(client):
        label = it.region_hint.split("/")[0].removeprefix("지표:") or "지표"
        it.force_category = target_tab(it.title, it.region_hint)
        if should_mirror(label, it.title, it.region_hint):
            it.mirror_to = MIRROR_TAB
        hits.append(it)

    # ── 3. 연준 연설 본문 ──
    # press_all.xml 에는 연설이 안 들어간다. 그래서 잭슨홀 연설 원문이 채널에
    # 도달한 적이 없고 "연설 시작"류 속보만 나갔다. 전용 피드 + 본문 수집으로 해결.
    for it in await fed_speech.fetch(client):
        it.force_category = US_TAB     # 연준은 언제나 미국
        it.mirror_to = MIRROR_TAB      # 연준 의장 발언은 언제나 이슈 탭에도
        it.deep = True                 # bullet 10~16개짜리 심층 요약
        hits.append(it)

    # ── 4. 정책 이벤트 (제목 판정) ──
    rss_items = await rss.fetch_all(client, settings.urgent_sources)
    for it in rss_items:
        label = urgency_of(it.title)
        if not label:
            continue
        it.region_hint = f"{it.region_hint}/긴급:{label}".lstrip("/")
        it.force_category = target_tab(it.title, it.region_hint)
        if should_mirror(label, it.title, it.region_hint):
            it.mirror_to = MIRROR_TAB
        # FOMC 성명·잭슨홀은 시장이 문장 하나까지 뜯어본다 → 심층 요약
        if label in ("FOMC", "잭슨홀"):
            it.deep = True
        hits.append(it)

    mirrored = sum(1 for i in hits if i.mirror_to)
    if hits or rss_items:
        print(f"[긴급] 지표·정책 이벤트 {len(hits)}건 "
              f"(그중 {mirrored}건은 {MIRROR_TAB} 탭에도 발행)")
    return hits
