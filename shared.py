"""A팀이 이미 공유한 기사 — Top10 에서 뺀다.

**왜.** 담당자들이 아침에 이미 올린 건을 봇이 또 올리면 중복이다.
애큐온캐피탈 인수처럼 여러 날에 걸쳐 보도되는 건은 특히 겹치기 쉽다.

목록은 groundtruth/titles.json 이다(커밋하지 않는다 — 동료 실명이 섞여 있다).
비교는 제목 기준이다. URL 로는 못 잡는다 — 같은 사건을 다른 매체가 쓴 기사는
주소가 전혀 다르다.
"""
import json
import os

import events

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "groundtruth", "titles.json")

# 제목이 이만큼 겹치면 같은 사건으로 본다.
SIMILARITY = 0.30

# 어간이 이만큼 겹치면 같은 사건으로 본다.
# **토큰 Jaccard 만으로는 못 잡는다.** A팀이 고른 기사와 우리가 고른 기사는
# 매체가 달라 제목이 전혀 다르게 쓰인다. 실측(2026-10-02):
#   "포스코그룹, 우리금융지주 보유 지분 전량 매각"
#   "포스코, 우리금융 지분 6700억 블록딜…10년만에 엑시트"
#   → 같은 사건인데 Jaccard 0.08. 복합어(포스코 vs 포스코그룹)가 다른 토큰이 된다.
# 어간 겹침(한쪽이 다른 쪽의 앞부분)으로 세면 포스코·우리금융·지분 3개가 잡힌다.
STEM_HITS = 3

# 어느 기사에나 나오는 말. 겹쳐도 신호가 아니다.
_COMMON = {
    "기사", "뉴스", "오늘", "올해", "내년", "지난해", "업계", "시장", "전망",
    "추진", "검토", "확대", "강화", "진행", "발표", "계획", "방안", "관련",
    "한국", "국내", "해외", "금융", "경제", "기업", "회사", "사업",
}

_cache: list[str] | None = None


def titles() -> list[str]:
    global _cache
    if _cache is not None:
        return _cache
    out = []
    try:
        with open(_PATH, encoding="utf-8") as f:
            for row in json.load(f):
                t = (row.get("title") or "").strip()
                if not t or t.startswith("__FAIL__"):
                    continue
                for sep in (" - ", " | ", " :: "):      # 매체명 꼬리 제거
                    if sep in t:
                        t = t.rsplit(sep, 1)[0]
                out.append(t.strip())
    except (OSError, json.JSONDecodeError):
        pass
    _cache = out
    return out


def _stems(title: str) -> set:
    return {t for t in events._tokens(title) if len(t) >= 2 and t not in _COMMON}


def _overlap(a: set, b: set) -> int:
    """어간이 겹치는 낱말 수. 한쪽이 다른 쪽의 앞부분이면 같은 낱말로 본다."""
    n = 0
    for x in a:
        if any(x == y or x.startswith(y) or y.startswith(x) for y in b):
            n += 1
    return n


def already_shared(title: str) -> str | None:
    """A팀이 이미 올린 기사면 그 제목, 아니면 None."""
    if not title:
        return None
    mine = _stems(title)
    if not mine:
        return None
    for t in titles():
        if events.similarity(title, t) >= SIMILARITY:
            return t
        if _overlap(mine, _stems(t)) >= STEM_HITS:
            return t
    return None
