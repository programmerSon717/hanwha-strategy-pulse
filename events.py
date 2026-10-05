"""Event Deduplication / Clustering (스펙 §21).

같은 사건을 여러 언론사가 기사화한다. URL 단위가 아니라 **EVENT 단위** 로 묶는다.
("교보생명 SBI 지분 확대" 기사 8개를 8건으로 취급하면 피드가 통째로 오염된다.)

Event fingerprint = main_entity + action + target + 시간창

판정은 두 겹이다.
  1) fingerprint 완전일치 — 싸다. LLM 없이 대부분을 잡는다.
  2) 제목 토큰 유사도 — 표현이 달라 fingerprint 가 갈리는 경우를 받아낸다.

대표기사 선택 기준 (§21): Source quality > Information richness > Original reporting
                          > Latest material information
"""
import re
import time
import unicodedata

from config import canonical_entity, source_weight

# 같은 사건으로 볼 시간창. 딜 기사는 며칠에 걸쳐 후속보도가 나온다.
EVENT_WINDOW_SEC = 3 * 24 * 3600

# 제목 유사도가 이 값 이상이면 (그것만으로) 같은 사건으로 본다.
TITLE_SIMILARITY = 0.62

# 2차 판정용 약한 문턱. 단독으로는 쓰지 않는다 — 엔티티 교집합·action 일치와
# AND 로만 묶는다. 모델이 main_entities 를 기사마다 다르게 적는 탓에
# fingerprint 가 갈리는 경우를 받아낸다. (2026-10-01 애큐온캐피탈 3중 발행 사고)
SOFT_TITLE_SIMILARITY = 0.25

# event_type 을 거친 action 으로 묶는다. 같은 딜을 '인수'/'합병'으로
# 달리 쓴 기사가 갈리지 않게 한다.
ACTION_GROUPS = {
    "acquisition": "deal", "merger": "deal", "divestiture": "deal",
    "stake_change": "deal", "investment": "deal",
    "governance": "governance",
    "regulation": "regulation", "litigation": "regulation",
    "product": "product", "partnership": "partnership",
    "earnings": "earnings", "appointment": "appointment",
    "market": "market", "other": "other",
}

# 제목에서 의미를 갖지 않는 토큰. 유사도 계산에서 뺀다.
_STOP = {
    "단독", "속보", "종합", "포토", "영상", "인터뷰", "기자", "뉴스", "오늘",
    "내년", "올해", "관련", "가능성", "예정", "전망", "검토", "추진", "밝혀",
    "것으로", "위해", "대해", "통해", "따르면", "이라고", "하는", "한다", "했다",
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)     # [단독] (종합) 제거
    text = re.sub(r"[^\w가-힣A-Za-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _tokens(title: str) -> set[str]:
    return {t for t in _norm(title).split()
            if len(t) > 1 and t not in _STOP}


def similarity(a: str, b: str) -> float:
    """제목 토큰 Jaccard. 0~1."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def fingerprint(data: dict, title: str = "") -> str:
    """main_entity + action + target + 시간창 (§21).

    엔티티는 별칭을 정식명으로 되돌려 쓴다 — '한화생명'/'Hanwha Life' 가
    다른 사건으로 갈리지 않게.
    """
    ents = []
    for e in (data.get("main_entities") or []):
        ents.append(canonical_entity(e) or _norm(e))
    if not ents:
        ents = sorted(_tokens(title or data.get("title_ko", "")))[:2]
    main = "+".join(sorted(set(ents))[:2])

    action = ACTION_GROUPS.get(data.get("event_type") or "other", "other")
    bucket = int(time.time() // EVENT_WINDOW_SEC)
    return f"{main}|{action}|{bucket}"


def _entity_set(data: dict, title: str = "") -> frozenset:
    """fingerprint 가 쓰는 것과 같은 엔티티 집합. 단 2개로 자르지 않는다."""
    ents = []
    for e in (data.get("main_entities") or []):
        ents.append(canonical_entity(e) or _norm(e))
    if not ents:
        ents = sorted(_tokens(title or data.get("title_ko", "")))[:2]
    return frozenset(ents)


def parts(data: dict, title: str = "") -> tuple:
    """(엔티티 집합, action 그룹, 시간창) — 2차 판정용."""
    action = ACTION_GROUPS.get(data.get("event_type") or "other", "other")
    bucket = int(time.time() // EVENT_WINDOW_SEC)
    return _entity_set(data, title), action, bucket


def same_event(a: tuple, title_a: str, b: tuple, title_b: str) -> bool:
    """두 기사가 같은 사건인가 (2차 판정).

    fingerprint 완전일치가 실패했을 때만 부른다. 세 신호를 AND 로 묶는다.
      · 엔티티 교집합이 비어 있지 않을 것
      · action 그룹과 시간창이 같을 것
      · 제목이 최소한으로라도 겹칠 것 (SOFT_TITLE_SIMILARITY)

    셋 다 요구하는 이유: 엔티티 교집합만 보면 같은 회사의 서로 다른 딜이
    한 사건으로 뭉개진다. 제목 유사도만 보면 표현이 달라 놓친다.
    """
    ents_a, action_a, bucket_a = a
    ents_b, action_b, bucket_b = b
    if action_a != action_b or bucket_a != bucket_b:
        return False
    if not (ents_a & ents_b):
        return False
    return similarity(title_a, title_b) >= SOFT_TITLE_SIMILARITY


def representative_score(data: dict, source: str, body_len: int) -> tuple:
    """대표기사 선택용 정렬 키 (§21). 클수록 대표에 가깝다."""
    return (
        source_weight(source),                      # 1. Source quality
        min(body_len, 8000),                        # 2. Information richness
        int(bool(data.get("material_update"))),     # 4. Latest material info
        data.get("strategic_score", 0),
    )


class EventIndex:
    """이번 실행 안에서 본 사건들. store 의 영속 중복방지와 별개로 동작한다."""

    def __init__(self):
        self._by_fp: dict[str, dict] = {}
        self._titles: list[tuple[str, str]] = []     # (fingerprint, title)
        self._parts: list[tuple[str, tuple, str]] = []   # (fp, parts, title)

    def match(self, data: dict, title: str) -> str | None:
        """이미 본 사건이면 그 fingerprint, 처음이면 None.

        판정 순서 — 싼 것부터.
          1) fingerprint 완전일치
          2) 제목 유사도 단독 (TITLE_SIMILARITY)
          3) 엔티티 교집합 + action + 약한 제목 유사도 (same_event)
        """
        fp = fingerprint(data, title)
        if fp in self._by_fp:
            return fp
        for known_fp, known_title in self._titles:
            if similarity(title, known_title) >= TITLE_SIMILARITY:
                return known_fp
        mine = parts(data, title)
        for known_fp, known_parts, known_title in self._parts:
            if same_event(mine, title, known_parts, known_title):
                return known_fp
        return None

    def add(self, data: dict, title: str, source: str, body_len: int) -> str:
        fp = self.match(data, title) or fingerprint(data, title)
        rep = representative_score(data, source, body_len)
        prev = self._by_fp.get(fp)
        if prev is None or rep > prev["rep"]:
            self._by_fp[fp] = {"rep": rep, "title": title, "source": source}
        self._titles.append((fp, title))
        self._parts.append((fp, parts(data, title), title))
        return fp

    def is_representative(self, fp: str, title: str) -> bool:
        cur = self._by_fp.get(fp)
        return bool(cur and cur["title"] == title)


# ── Material Update 판정 (§21) ────────────────────────────────
# 같은 사건이라도 아래 변화가 있으면 별도 Event Update 로 게시할 수 있다.
MATERIAL_MARKERS = (
    "성사", "무산", "철회", "확정", "체결", "승인", "불허", "반려", "부결",
    "판결", "결정", "제재", "과징금", "본계약", "우선협상", "실사 착수",
    "지분율", "인수가", "매각가", "공식 발표", "공시",
)


def looks_material(title: str, data: dict) -> bool:
    """제목·판정에 Material Update 신호가 있는가."""
    if data.get("material_update"):
        return True
    t = _norm(title)
    return any(_norm(m) in t for m in MATERIAL_MARKERS)
