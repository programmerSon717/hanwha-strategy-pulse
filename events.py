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
    # 제목 유사도를 **내용 낱말로만** 재도록 좁혀 봤으나 되돌렸다(2026-10-05).
    # 그러면 "한화생명, 애큐온 인수 확정" × "…인수 통해 여신금융 진출" 이
    # 0.25 에서 0.125 로 떨어져 갈라진다 — 2026-10-01 애큐온 3중 발행 사고를
    # 막으려고 넣은 회귀 검사가 깨진다(tests/bsp_cases.py). 중복이 누락보다
    # 나쁘다는 것이 발주자의 일관된 지시이므로 원래 기준을 유지한다.
    if similarity(title_a, title_b) >= SOFT_TITLE_SIMILARITY:
        return True
    # **집합 포함 관계.** 한쪽 엔티티 집합이 다른 쪽을 품으면 제목 표현이
    # 달라도 같은 사건으로 본다. 같은 긴급소집 건이 주체를 하나만 적었는지
    # 둘 다 적었는지로 갈려 네 번 나간 사고가 있었다(2026-10-05).
    #
    # 포함 관계만으로는 넓다 — 시간창이 3일이라 그 사이 금융위 규제 기사가
    # 전부 한 덩어리가 된다. 그래서 변별력 있는 낱말이 겹칠 때만 묶는다.
    if ents_a <= ents_b or ents_b <= ents_a:
        return _distinctive_overlap(title_a, title_b, ents_a | ents_b)
    return False


# 변별력이 없어 두 기사를 잇는 근거가 못 되는 낱말.
# "금융권"·"당국"·"추진" 은 규제 기사 절반에 들어 있어, 이것만 겹쳤다고
# 같은 사건으로 보면 서로 다른 정책이 한 덩어리가 된다.
_GENERIC_TOKENS = {
    "금융", "금융권", "금융사", "금융위", "금감원", "당국", "금융당국",
    "은행", "보험", "증권", "업계", "시장", "추진", "검토", "확대", "강화",
    "방안", "대응", "논의", "회의", "개최", "예정", "전망", "계획", "관련",
    "따른", "위해", "대한", "오늘", "내일", "올해", "내년", "정부", "국내",
    # 아래는 2026-10-05 추가. "확장" 하나로 "카카오뱅크 글로벌 영토
    # 디지털자산으로 확장" 과 "카카오페이증권, 흑자 전환 이후 사업 확장"
    # 이 같은 사건으로 묶였다 — 주체도 내용도 다른 기사다.
    "확장", "본격화", "가시화", "전환", "개편", "점검", "부각", "대두",
    "나서", "속도", "박차", "모색", "제고", "개선", "성장", "실적",
}


# 사건의 '종류'만 말할 뿐 어느 사건인지는 못 가르는 낱말.
# action 그룹이 이미 같다는 전제에서 보므로 이 말들은 근거가 되지 못한다.
_ACTION_TOKENS = {
    "인수", "매각", "합병", "지분", "출자", "투자", "협약", "제휴", "체결",
    "추진", "확정", "검토", "발표", "출시", "진출", "설립", "제재", "개편",
}


def _distinctive_overlap(a: str, b: str, ents: frozenset = frozenset()) -> bool:
    """두 제목이 **변별력 있는 낱말**을 공유하는가.

    엔티티 이름은 근거에서 뺀다. 엔티티가 겹치는 것은 포함 관계 검사가 이미
    확인했고, 여기서 또 세면 **같은 회사의 서로 다른 딜**이 한 사건으로
    뭉개진다("한화생명, 애큐온 인수 확정" × "한화생명·교보생명, 저축은행
    인수 추진" — 겹치는 낱말이 회사 이름과 '인수' 뿐인데도 묶였다).
    """
    drop = set(_GENERIC_TOKENS) | _ACTION_TOKENS
    names = set()
    for e in ents:
        drop |= _tokens(e)
        names.add(_norm(e))
    drop |= names

    def _is_entity(t: str) -> bool:
        # 엔티티는 정식명으로 환원돼 들어온다("카카오뱅크" → "카카오").
        # 그래서 제목의 "카카오뱅크" 가 drop 에 걸리지 않고 변별력 있는
        # 낱말로 남아, 카카오뱅크 기사 둘을 아무렇게나 이었다(2026-10-05).
        # 엔티티 이름을 품은 낱말은 전부 엔티티 언급으로 본다.
        return any(n and (n in t or t in n) for n in names)

    ta = {t for t in _tokens(a) if t not in drop and not _is_entity(t)}
    tb = {t for t in _tokens(b) if t not in drop and not _is_entity(t)}
    return bool(ta & tb)


def coverage(data: dict, title: str = "") -> int:
    """기사가 **얼마나 큰 개념을 담고 있는가**. 클수록 포괄적이다.

    발주자 지시(2026-10-05): "집합개념으로 따지면 더 큰 개념의 내용을
    담고있는거만 하나만 올려."

    실제 사고: 같은 긴급소집 건이 네 번 나갔다.
      · "금융위, 금융권 해킹 피해 확산에 전 금융권 긴급 소집"      엔티티 1
      · "금융당국, 2금융권 해킹 피해 확산에 따른 긴급 소집"         엔티티 2
      · "은행·2금융권 잇따른 AI 해킹 우려에 금융당국, 전 금융사
         CEO 긴급대응 회의 소집"                                엔티티 3  ← 이것만 남겨야 한다
    세 번째가 주체(금융위+금감원)도, 대상(은행+2금융권+전 금융사 CEO)도,
    맥락(AI 해킹)도 모두 포함한다. 나머지는 그 부분집합이다.
    """
    ents = len(_entity_set(data, title))
    # 범위를 넓히는 표현. "전 금융사"·"전 금융권" 은 개별 회사보다 큰 집합이다.
    scope = len(re.findall(r"전\s*금융|금융권\s*전체|업계\s*전반|전\s*업권"
                           r"|잇따른|잇단|전반|일제히|동시", _norm(title)))
    return ents * 10 + scope * 5 + min(len(_tokens(title)), 12)


def representative_score(data: dict, source: str, body_len: int) -> tuple:
    """대표기사 선택용 정렬 키 (§21). 클수록 대표에 가깝다."""
    return (
        # 0. **포괄성이 먼저다.** 같은 사건이면 더 큰 개념을 담은 것만 낸다
        #    (2026-10-05 발주자 지시). 아래 항목들은 포괄성이 같을 때의 순서다.
        coverage(data, data.get("title_ko") or ""),
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


# ── 사고·피해 기사 (🚨 주요이슈 강제) ──────────────────────────
# 발주자 지시(2026-10-05): "이런 해킹피해는 주요이슈로 넣어야지. 이거뿐만이
# 아니야. 피해입고 뭐 이런것들 등등은 주요이슈로 넣어."
#
# 실측 배경: AI 에이전트 해킹 건 14건이 **전부** is_key_issue=false 였다.
# 모델 점수가 68~78 로 주요이슈 임계값(85)에 못 미쳤기 때문이다. 그 결과
# 같은 사건이 디지털·신사업 / 규제·정책 / 경쟁사 / 보험·금융 네 토픽에
# 흩어져 들어갔고, 정작 🚨 주요이슈 에는 한 건도 없었다.
#
# 사고·피해는 전략성 점수로 재면 낮게 나온다 — 딜도 아니고 제도도 아니다.
# 그러나 팀이 가장 먼저 봐야 하는 종류다. 그래서 점수와 무관하게 올린다.
INCIDENT_RE = re.compile(
    r"해킹|크래킹|랜섬|피싱|스미싱|디도스|ddos|침해|유출|탈취|도용"
    r"|보안\s*사고|보안\s*허점|취약점|악용"
    r"|전산\s*장애|시스템\s*장애|먹통|접속\s*불가|서비스\s*중단"
    r"|횡령|배임|사기|부당\s*대출|불완전판매|불법\s*계좌"
    r"|제재|과징금|과태료|징계|적발|압수수색|고발|기소|검사\s*착수"
    r"|피해\s*확산|피해\s*규모|손실\s*발생|대규모\s*손실"
    r"|리콜|결함|사고\s*발생",
    re.I)


def looks_incident(title: str, data: dict | None = None) -> bool:
    """사고·피해 기사인가. 점수와 무관하게 🚨 주요이슈로 올린다."""
    text = title or ""
    if data:
        text = f"{text} {data.get('lede') or ''}"
    return bool(INCIDENT_RE.search(_norm(text)))
