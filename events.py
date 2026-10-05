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
        # **교집합이 비어도 포기하지 않는다.** 모델이 같은 사건의 엔티티를
        # 기사마다 다르게 적는 것이 애초의 문제인데, 교집합을 AND 조건으로
        # 요구하면 그 문제에 그대로 뚫린다. 실측: 같은 볼트온 기사가 한쪽은
        # {한화생명}, 다른 쪽은 {도쿄해상} 으로 적혀 갈렸다(2026-10-05).
        #
        # 대신 **내용이 강하게 겹칠 때만** 같은 사건으로 본다 —
        # 제목 유사도가 높거나, 변별력 있는 낱말을 공유할 때.
        if similarity(title_a, title_b) >= TITLE_SIMILARITY:
            return True
        # 변별어 **2개 이상**을 요구한다. 1개만으로 묶으면 "해킹" 하나로
        # 신한은행 IP 추적과 토스뱅크 환전 분쟁이 한 사건이 됐다
        # (2026-10-05 2차 감사).
        return _distinctive_count(title_a, title_b, ents_a | ents_b) >= 2
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
    # 2026-10-05 감사 2차. 아래는 해당 분야 기사 절반에 들어가는 말인데
    # '변별력 있는 낱말' 로 인정돼 무관한 기사를 이었다.
    #   '규제' → 보험광고 규제 × 저축은행 자본적정성 규제
    #   '증권사'·'과징금' → 서로 다른 세 정책이 한 사건으로
    #   'ai'·'보안' → KB 플랫폼 로드맵 × 5대은행 AI 보안
    #   '유출'·'지원'·'변화'·'진입'·'미국' 도 같은 유형이다.
    "ai", "보안", "유출", "침해", "지원", "변화", "규제", "과징금", "제재",
    "증권사", "은행권", "보험사", "진입", "미국", "일본", "중국", "유럽",
    "고객", "정보", "서비스", "플랫폼", "사업", "전략", "협력", "경쟁",
    # 2026-10-05 2차. 아래가 1개만 겹쳐도 무관한 기사가 묶였다.
    "해킹", "국정감사", "국감", "ga", "제한", "소집", "점검", "논의",
    "채널", "판매", "실손", "대책", "체계", "강화", "확산", "우려",
    "대형", "대표", "관행", "도마", "증인", "채택", "추세", "방안",
}


# 금액·수치 토큰은 변별력이 없다. "5000억" 하나로 서로 다른 딜이 묶였다.
_NUM_TOKEN = re.compile(r"^[0-9][0-9,.]*(억|조|만|원|%|%p|배|건|명|주|달러)?$")


# 사건의 '종류'만 말할 뿐 어느 사건인지는 못 가르는 낱말.
# action 그룹이 이미 같다는 전제에서 보므로 이 말들은 근거가 되지 못한다.
_ACTION_TOKENS = {
    "인수", "매각", "합병", "지분", "출자", "투자", "협약", "제휴", "체결",
    "추진", "확정", "검토", "발표", "출시", "진출", "설립", "제재", "개편",
}


def _aliases_of(canon: str) -> set:
    """정식명에 딸린 별칭 전부. 엔티티 사전에서 끌어온다."""
    try:
        from config import entity_groups
    except Exception:
        return set()
    out = set()
    for group in entity_groups().values():
        for c, al in group.items():
            if _norm(c) == _norm(canon):
                out.update(al)
                out.add(c)
    return out


def _distinctive_count(a: str, b: str, ents: frozenset = frozenset()) -> int:
    """두 제목이 공유하는 **변별력 있는 낱말의 개수.**"""
    return len(_distinctive_shared(a, b, ents))


def _distinctive_shared(a: str, b: str, ents: frozenset = frozenset()) -> set:
    """두 제목이 공유하는 변별력 있는 낱말 집합."""
    drop = set(_GENERIC_TOKENS) | _ACTION_TOKENS
    names = set()
    for e in ents:
        drop |= _tokens(e)
        names.add(_norm(e))
        for _al in _aliases_of(e):
            names.add(_norm(_al))
            drop |= _tokens(_al)
    drop |= names

    def _is_entity(t: str) -> bool:
        return any(n and (n in t or t in n) for n in names)

    def _ok(t):
        return (t not in drop and not _is_entity(t)
                and not _NUM_TOKEN.match(t) and len(t) >= 2)

    return {t for t in _tokens(a) if _ok(t)} & {t for t in _tokens(b) if _ok(t)}


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
        # **별칭까지 넣는다.** canonical_entity 가 '신한은행'→'신한금융' 으로
        # 환원하므로 names 에는 정식명만 남고, 제목의 '신한은행' 은 걸러지지
        # 않아 회사 이름이 '변별력 있는 낱말' 로 통과했다. 오탐 14쌍 중 5쌍이
        # 이 한 줄 때문이었다(2026-10-05 감사).
        # 변수명 주의: 바깥 인자 a(제목)를 가리면 안 된다.
        for _al in _aliases_of(e):
            names.add(_norm(_al))
            drop |= _tokens(_al)
    drop |= names

    def _is_entity(t: str) -> bool:
        # 엔티티는 정식명으로 환원돼 들어온다("카카오뱅크" → "카카오").
        # 그래서 제목의 "카카오뱅크" 가 drop 에 걸리지 않고 변별력 있는
        # 낱말로 남아, 카카오뱅크 기사 둘을 아무렇게나 이었다(2026-10-05).
        # 엔티티 이름을 품은 낱말은 전부 엔티티 언급으로 본다.
        return any(n and (n in t or t in n) for n in names)

    def _ok(t):
        return t not in drop and not _is_entity(t) and not _NUM_TOKEN.match(t)

    ta = {t for t in _tokens(a) if _ok(t)}
    tb = {t for t in _tokens(b) if _ok(t)}
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
    # 엔티티 수 가중을 10 → 6 으로 낮춘다. 실측상 coverage 점수의 68% 가
    # 엔티티 개수였는데, 그건 모델이 기사마다 다르게 적는 값이라 '더 큰
    # 개념' 이 아니라 **모델의 작성 변덕**을 재고 있었다. 실제로 "애큐온 인수
    # + 한화투자증권 유상증자" 처럼 **두 사건을 섞은 기사**가 엔티티 3개로
    # 이겨, 지분율·본계약을 담은 기사를 눌렀다(2026-10-05 감사).
    ents = len(_entity_set(data, title))
    # 범위를 넓히는 표현. "전 금융사"·"전 금융권" 은 개별 회사보다 큰 집합이다.
    scope = len(re.findall(r"전\s*금융|금융권\s*전체|업계\s*전반|전\s*업권"
                           r"|잇따른|잇단|전반|일제히|동시", _norm(title)))
    # 확정 사실(본계약·확정·승인·제재 …)은 가장 큰 가점이다. 딜에서 가장
    # 중요한 진전이 '포괄성' 때문에 밀려 버려지던 문제를 막는다.
    material = 20 if looks_material(title, data) else 0
    return material + ents * 6 + scope * 5 + min(len(_tokens(title)), 12)


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
        # **각 사건의 대표하고만 견준다.** 예전엔 지금까지 들어온 제목 전부와
        # 비교해, A—B 가 닮고 B—C 가 닮으면 A 와 C 가 한 사건이 됐다
        # (단일연결 체인). 실측: 방카슈랑스 제휴 → 신한은행 유출 → 보안
        # 취약점 → 생보사 유동성이 한 덩어리가 돼 3건이 삭제 대상이 됐다
        # (2026-10-05 감사). 대표하고만 견주면 체인이 한 칸에서 끊긴다.
        mine = parts(data, title)
        best, best_sim = None, 0.0
        for known_fp, cur in self._by_fp.items():
            kt = cur["title"]
            sim = similarity(title, kt)
            if sim >= TITLE_SIMILARITY or same_event(mine, title,
                                                     cur["parts"], kt):
                # 여럿에 걸리면 **가장 닮은 쪽**에 붙인다. 먼저 온 것에
                # 붙이면 투입 순서에 따라 결과가 달라진다.
                if sim >= best_sim or best is None:
                    best, best_sim = known_fp, sim
        return best

    def add(self, data: dict, title: str, source: str, body_len: int) -> str:
        fp = self.match(data, title) or fingerprint(data, title)
        rep = representative_score(data, source, body_len)
        prev = self._by_fp.get(fp)
        if prev is None or rep > prev["rep"]:
            self._by_fp[fp] = {"rep": rep, "title": title, "source": source,
                               "parts": parts(data, title)}
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
    # 제재·과징금은 **처분을 받은 경우**만 사고다. "제재 수준 상향
    # 입법예고"·"과징금 부과기준율 하향" 같은 제도 변경 기사가 사고로
    # 잡혀 일일 상한 예외까지 탔다(2026-10-05 감사).
    # "과징금 부과기준율 하향" 처럼 제도 문구가 뒤에 붙는 경우를 제외한다.
    r"|제재\s*(조치|부과|결정)"
    r"|과징금\s*(?!부과기준|기준|제도|산정기준)(부과|처분|물)"
    r"|과태료\s*(?!부과기준|기준)(부과|처분)"
    r"|징계|적발|압수수색|고발|기소|검사\s*착수|제재를\s*받"
    r"|피해\s*확산|피해\s*규모|손실\s*발생|대규모\s*손실"
    r"|리콜|사고\s*발생"
    # '결함' 은 뺐다 — 이 봇 lede 는 "~함." 명사형 종결을 써서
    # "협약을 **체결함**" 이 전부 사고로 잡혔다(실측 3/3 오탐).
    r"|제품\s*결함|구조적\s*결함|설계\s*결함",
    re.I)


def looks_incident(title: str, data: dict | None = None) -> bool:
    """사고·피해 기사인가. 점수와 무관하게 🚨 주요이슈로 올린다."""
    text = title or ""
    if data:
        text = f"{text} {data.get('lede') or ''}"
    return bool(INCIDENT_RE.search(_norm(text)))


# ── 🚨 주요이슈로 올릴 '사건성' 판정 ───────────────────────────
# 발주자 지적(2026-10-05): "주요이슈로 들어가는거 기준이 뭔데 지금 한화내용
# 죄다 주요이슈로 들어가는거같은데..?"
#
# 실측으로 맞는 말이었다 — 주요이슈 미러 9건 중 8건이 한화 기사였다.
# 기준이 프롬프트(STEP D)에만 있어 모델이 느슨하게 적용했고, 한화 기사는
# 전략성 점수가 88~98 로 나와 임계값(85)을 거의 항상 넘었다. 그래서
# "한화 + 고득점" 이 사실상 주요이슈의 기준이 돼 있었다.
#
# 주요이슈는 **오늘 경영진에게 바로 보고할 사건**이다. 분석·전망·협력 논의·
# 인사는 중요해도 그 자리가 아니다. 그래서 사건 종류로 코드에서 한 번 더
# 거른다. 모델 판정(is_key_issue)과 점수는 그대로 요구하되, 여기에 더해
# 아래 중 하나여야 한다.
DECISIVE_GROUPS = {"deal", "regulation", "governance"}


def decisive(title: str, data: dict | None = None) -> bool:
    """주요이슈에 올릴 '사건'인가.

    딜·규제·지배구조이거나, 제목에 확정 신호(체결·확정·승인·제재 …)가 있으면
    사건으로 본다. 'other'·'market'·'partnership'·'earnings'·'appointment'·
    'product' 는 그 자체로는 아니다.

    실측 적용 결과 (주요이슈 미러 9건)
      유지: 애큐온 인수 의결·확정·본계약, 한화 KAI 지분 확보,
            한화갤러리아 매각 검토            ← 전부 deal
      제외: 김동원 사장 포트폴리오 재편 주도   ← other(분석)
            한화금융 아부다비 협력 추진       ← partnership(논의)
    """
    group = ACTION_GROUPS.get((data or {}).get("event_type") or "other", "other")
    if group in DECISIVE_GROUPS:
        return True
    return looks_material(title, data or {})
