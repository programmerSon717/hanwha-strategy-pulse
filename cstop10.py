"""📌 A팀 Top10 — 매일 KST 06:55.

☀️ Morning Brief(07:00)와 **선정 기준이 다르다.**
  Morning Brief : 모델이 매긴 strategic_score 순 + 추천 서칭 순서 티어
  A팀 Top10  : csfit.score() — A팀이 실제 공유한 125건의 주제 분포 가중치

같은 기사가 양쪽에 다 나올 수 있다. 둘 다 Aggregation Topic 이라 중복 허용이다.
"""
import html
import re
from datetime import datetime, timedelta, timezone

import brief
import csfit
import events
import gnews
import shared
import telegraph
import topics
from config import settings

KST = timezone(timedelta(hours=9))

# brief_candidates 컬럼 순서
K_KEY, K_HEAD, K_URL, K_PRI, K_SEC, K_SCORE = 0, 1, 2, 3, 4, 5
K_ISKEY, K_CLUSTER, K_ENT, K_LEDE, K_WHY, K_SENT = 6, 7, 8, 9, 10, 11
K_ETYPE, K_ORIGIN, K_TEXT = 12, 13, 14   # origin_at=기사 발행시각, text=발행 원문

# 홍보성 기사를 다시 실을 수 있게 되기까지의 간격 (2026-10-01 사용자 지정: 이틀)
PR_INTERVAL_SEC = 2 * 24 * 3600


def due(store, now: float | None = None) -> tuple[bool, str]:
    """지금 발행해야 하는가. (해야하나, 이유)

    **GitHub 의 schedule 에 기대지 않기 위해 있다.** 2026-10-02 06:55 에
    cstop10.yml 의 cron(21:55 UTC)이 아예 발화하지 않아 그날 Top10 이 누락됐다.
    실행 이력 0건. 리포 문서에도 적혀 있듯 GitHub 무료 티어의 예약 실행은
    best-effort 다(간격 중앙값 42분, 최대 11시간). 하루 한 번짜리는 통째로
    건너뛸 수 있다.

    그래서 **상시 도는 bot.yml 루프**가 매 회차 이것을 물어보고 띄운다.
    cron 워크플로는 백업으로 남겨 둔다 — 둘 다 와도 여기서 한 번만 나간다.
    """
    now = now or datetime.now(KST).timestamp()
    t = datetime.fromtimestamp(now, KST)
    hh, _, mm = settings.cs_top10_time.partition(":")
    sched = t.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    day0 = t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day1 = day0 + 24 * 3600

    if t < sched:
        return False, f"아직 {settings.cs_top10_time} 전"
    if store.agg_ran_on("cs_top10", day0, day1):
        return False, "오늘 이미 발행함"
    return True, "발행 시각 지남 · 오늘 미발행"


def window(now: float | None = None) -> tuple[float, float, str]:
    """후보를 뽑을 구간. 기본은 '하루 전 같은 시각 ~ 지금'.

    길이는 settings.cs_top10_window_hours 가 정한다. 봇이 며칠 멈췄다 살아난
    날은 24시간 창에 후보가 모자라 Top10 이 성립하지 않는다 — 그 설명은
    config.cs_top10_window_hours 주석에 있다.
    """
    now = now or datetime.now(KST).timestamp()
    t = datetime.fromtimestamp(now, KST)
    # 창의 끝은 **그날 06:00** 이다(settings.cs_top10_window_end). 발행 시각이
    # 아니라 그보다 50분 앞선다 — 사용자 지정 "전날 6:50am~당일 06:00am".
    hh, _, mm = settings.cs_top10_window_end.partition(":")
    end = t.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if end > t:                      # 아직 그 시각 전이면 전날 창이다
        end -= timedelta(days=1)
    until = end.timestamp()
    # 시작은 '전날 06:50'. 끝이 06:00 이므로 길이는 24시간이 아니라 23시간 10분이다.
    # (24시간을 그대로 빼면 전날 06:00 이 되어 06:00~06:50 구간이 두 번 실린다 —
    #  전날 판의 끝과 겹친다.)
    hh2, _, mm2 = settings.cs_top10_time.partition(":")
    start = (end - timedelta(days=1)).replace(hour=int(hh2), minute=int(mm2))
    since = start.timestamp()
    label = t.strftime("%Y.%m.%d %a")
    return since, until, label


def _stem_hits(ta: set, tb: set, minlen: int = 2, prefix: int = 3) -> int:
    """어간이 겹치는 낱말 수.

    **한국어 복합어는 앞부분이 같아도 서로의 접두사가 아니다.**
    "애큐온캐피탈" 과 "애큐온저축은행" 은 어느 쪽도 다른 쪽으로 시작하지 않아
    접두사 비교로는 안 걸렸고, 같은 딜 기사 두 건이 Top10 에 나란히 실렸다
    (2026-10-02). 앞 3글자가 같으면 같은 낱말로 본다.

    조사 때문에 정확히 일치하는 비교도 안 된다 — "자본확충" vs "자본확충으로".
    그래서 접두사 포함도 함께 본다.
    """
    n = 0
    for x in ta:
        if len(x) < minlen:
            continue
        for y in tb:
            if len(y) < minlen:
                continue
            if (x == y or x.startswith(y) or y.startswith(x)
                    or (len(x) >= prefix and len(y) >= prefix
                        and x[:prefix] == y[:prefix])):
                n += 1
                break
    return n


def _stem_overlap(ta: set, tb: set, minlen: int = 2) -> bool:
    return _stem_hits(ta, tb, minlen) > 0


# 기사 제목에 흔한 말들. 이게 겹치는 건 같은 사건이라는 증거가 못 된다.
# 반대로 이 목록 **밖**의 긴 낱말(스테이블코인·애큐온·포르테그라·타임월드…)이
# 겹치면 같은 사건일 가능성이 매우 높다.
_GENERIC_WORDS = {
    "금융", "사업", "전략", "시장", "추진", "확대", "강화", "검토", "논의",
    "도입", "관리", "서비스", "투자", "경쟁", "규제", "실적", "계획", "발표",
    "지원", "협력", "체계", "구조", "방안", "대응", "개선", "성장", "진출",
    "가능성", "본격화", "가시화", "전환", "부문", "기업", "국내", "해외",
    "보험", "증권", "은행", "그룹", "계열사", "업계", "당국", "정부",
    # 2026-10-05 감사: 아래가 "희귀 낱말"로 통과해 서로 다른 기사를 묶었다.
    "금융위", "금감원", "공정위", "증권사", "보험사", "과징금", "디지털자산",
    "포트폴리오", "에이전트", "개인정보", "정보유출", "하나은행", "신한은행",
    "국민은행", "우리은행", "카카오뱅크", "토스뱅크", "케이뱅크",
}


def _distinctive(words: set) -> set:
    """흔한 말을 뺀, 그 사건을 특정하는 낱말들."""
    return {w for w in words
            if len(w) >= 3 and w not in _GENERIC_WORDS and not w.isdigit()}


def _same(a_title, a_ents, a_type, b_title, b_ents, b_type,
          strict: bool = False) -> bool:
    """두 기사가 같은 사건인가. **신호를 합산해서** 판정한다.

    저장된 cluster_id 를 믿으면 안 된다 — 모델이 main_entities 를 기사마다
    다르게 적어 fingerprint 가 갈린다. 애큐온캐피탈 인수 한 건이 실제로
    5가지 cluster_id 로 저장돼 Top10 에 3건이 나란히 실렸다(2026-10-02).

    제목 유사도 단독으로도 안 된다. 같은 딜을 다룬 기사들의 Jaccard 가
    0.13~0.14 로 나와 events.SOFT_TITLE_SIMILARITY(0.25)에도 못 미쳤다.
    매체마다 제목을 전혀 다르게 뽑기 때문이다.

    그래서 네 신호를 합산한다. 엔티티가 하나도 안 겹치면 무조건 다른 사건이다.
        엔티티 2개 이상 겹침  +2      엔티티 1개 겹침       +1
        action 그룹 같음      +1      제목 유사도 0.25 이상 +1
        회사명 말고 겹치는 낱말이 있음  +1
    합이 3 이상이면 같은 사건으로 본다.

    틀렸을 때의 대가가 비대칭이라 이 정도로 과감하게 잡는다 — 잘못 묶으면
    Top10 에서 기사 하나가 빠질 뿐이고, 못 묶으면 같은 뉴스가 3~4건 실린다.
    """
    def ents_of(ents, title):
        return events._entity_set(
            {"main_entities": [e for e in (ents or "").split(",") if e]}, title)

    ea, eb = ents_of(a_ents, a_title), ents_of(b_ents, b_title)
    inter = ea & eb
    if not inter:
        # **교집합이 비어도 포기하지 않는다.** 모델이 같은 사건의 엔티티를
        # 기사마다 다르게 적는 것이 애초의 문제다. 실측: 같은 볼트온 기사가
        # 한쪽은 {한화생명}, 다른 쪽은 {도쿄해상} 으로 적혀 Top10 초안에
        # 둘 다 들어갔다(2026-10-05 발주자 지적).
        # 내용이 강하게 겹칠 때만 같은 사건으로 본다.
        if events.similarity(a_title, b_title) >= events.TITLE_SIMILARITY:
            return True
        return events._distinctive_overlap(a_title, b_title, ea | eb)

    score = 2 if len(inter) >= 2 else 1

    ga = events.ACTION_GROUPS.get(a_type or "other", "other")
    gb = events.ACTION_GROUPS.get(b_type or "other", "other")
    if ga == gb:
        score += 1

    if events.similarity(a_title, b_title) >= events.SOFT_TITLE_SIMILARITY:
        score += 1

    # 회사명을 뺀 낱말이 몇 개나 겹치는가 (인수 · 자본확충 · 애큐온 …)
    ta, tb = events._tokens(a_title), events._tokens(b_title)
    # **교집합에 든 회사명만 뺀다.** 그건 이미 엔티티 신호로 세었다.
    # 전부 빼면 "애큐온캐피탈" 과 "애큐온저축은행" 같은 **서로 다른** 회사명이
    # 사라져 어간 비교 기회를 잃는다. 같은 딜의 두 기사가 그렇게 갈렸다.
    names = {events._norm(x) for x in inter}

    # **교집합 회사명으로 시작하는 낱말까지 뺀다.**
    # 엔티티가 '카카오' 로 정규화되면 제목의 '카카오뱅크' 는 names 와 글자가
    # 달라 안 지워졌다. 그래서 같은 회사 이름이 어간 겹침(+1)과 희귀 낱말(+1)로
    # 두 번 계산돼, 서로 다른 카카오뱅크 기사 두 건이 같은 사건으로 묶였다
    # (2026-10-05). 회사명은 이미 엔티티 신호로 세었으니 여기서 또 세면 안 된다.
    #
    # '애큐온캐피탈' vs '애큐온저축은행' 은 여전히 남는다 — 그때 교집합은
    # '한화생명' 이고 애큐온* 은 거기서 시작하지 않는다.
    def _strip(toks):
        # **별칭까지 지운다.** 교집합이 '하나금융' 으로 정규화되면 제목의
        # '하나은행' 은 접두사가 달라 안 지워졌고, 회사명 하나로 어간겹침과
        # 희귀낱말을 두 번 받아 서로 다른 하나은행 기사가 묶였다(2026-10-05).
        alias = set(names)
        for n in list(names):
            alias.add(n.replace("금융", "은행"))
            alias.add(n.replace("금융", ""))
        alias = {a for a in alias if len(a) >= 2}
        return {w for w in toks
                if not any(w.startswith(a) for a in alias)}

    # 어간 겹침에는 회사명을 **남긴다**(기존 동작). '애큐온캐피탈' 과
    # '애큐온저축은행' 처럼 회사명 자체가 같은 딜을 가리키는 경우가 있고,
    # 틀렸을 때의 대가가 비대칭이라 묶는 쪽으로 기운다 — 잘못 묶으면 기사
    # 하나가 빠질 뿐이고, 못 묶으면 같은 뉴스가 3~4건 실린다.
    hits = _stem_hits(ta - names, tb - names)
    score += 2 if hits >= 2 else (1 if hits else 0)

    # **희귀하고 구체적인 낱말이 겹치면 그것만으로 강한 증거다.**
    # 교보생명 스테이블코인 건이 아주경제·신아일보 두 기사로 Top10 에 나란히
    # 실렸다(2026-10-05). 엔티티 1점 + 공통낱말 1점 = 2점으로 문턱(3)에 못
    # 미쳤는데, 겹친 낱말이 하필 '스테이블코인' 이었다. 모델이 사건 종류를
    # 서로 다르게(other / partnership) 적어 그 신호도 못 받았다.
    #
    # 흔한 말(금융·추진·강화…)은 _GENERIC_WORDS 로 빼므로 과잉 병합은 없다.
    # 희귀 낱말 가산점에서는 회사명을 뺀다 — 그건 이미 엔티티 신호로 세었다.
    # 안 빼면 '카카오뱅크' 하나로 어간 겹침과 희귀 낱말을 **두 번** 받아,
    # 서로 다른 카카오뱅크 기사가 같은 사건으로 묶인다(2026-10-05).
    if _distinctive(_strip(ta) & _strip(tb)):
        score += 1

    if strict:
        # **과거 14일치 전체와 비교할 때 쓰는 엄격 모드.**
        #
        # 기본 모드는 '하루치 후보 풀 안에서' 비교하라고 만든 것이라 과감하다
        # (docstring 의 비대칭 논리). 그걸 2주치 전체에 그대로 대면 회사명만
        # 같으면 걸린다 — "카카오뱅크 글로벌 디지털자산 확장" 이 2주 전
        # "카카오뱅크 개인사업자 대출 4조" 와 같은 사건으로 묶여 10/5 Top10 이
        # 0건이 됐다(2026-10-05).
        #
        # 그래서 **회사명 말고 겹치는 낱말**을 반드시 요구한다. 애큐온 인수
        # 반복 보도는 '애큐온'·'인수' 가 공통으로 남아 걸리고, 같은 회사의
        # 다른 사건은 걸리지 않는다.
        #
        # hits 1 로는 모자랐다. "카카오뱅크 글로벌 디지털자산 확장" 이 2주 전
        # "카카오뱅크 개인사업자 대출" 과 낱말 하나로 묶였다. 2개를 요구하면
        # 애큐온 반복 보도('애큐온'+'인수')는 그대로 걸리고 오탐은 빠진다.
        return score >= 3 and hits >= 2

    return score >= 3


# 추천 서칭 순서를 **가산점**으로 반영한다.
#
# 처음엔 티어를 절대 1차 정렬키로 뒀다. 그랬더니 69점짜리 규제 기사
# ("보험사 GA 관리 평가 지표…K-ICS 반영")가 45점짜리 T2 기사 뒤로 밀려
# Top10 에서 아예 빠졌다. 추천 순서는 **어디부터 찾아볼지**의 우선순위지
# 품질 판단을 뒤집으라는 뜻이 아니다(2026-10-02).
# 가산점이면 티어가 낮아도 내용이 좋으면 올라온다.
TIER_BONUS = {1: 25, 2: 15, 3: 10, 4: 5, 5: 0}


# 한화 **금융**계열사. 발주 수칙의 4순위다 — 1순위(한화그룹 전반)와 가른다.
_HANWHA_FIN_RE = re.compile(
    r"한화생명|한화손해보험|한화손보|한화투자증권|한화증권|한화자산운용"
    r"|한화저축은행|한화금융|한화생명금융서비스|캐롯손해보험|캐롯손보|피플라이프")


def tier_of(r) -> int:
    """추천 서칭 순서상의 티어. **한화 여부는 제목으로 판단한다.**

        1 한화그룹 관련          2 진행중인 M&A · 보험
        3 규제 · 지배구조        4 한화 금융계열사(곁다리 언급)
        5 기타 (네이버 · 카카오 · 토스 · 메리츠 등)

    brief.tier 는 entities 로 판단한다. Morning Brief 에서는 그게 맞다 —
    사용자가 "한화면 무조건 1순위"로 확정했다. 다만 Top10 에 그대로 쓰면
    모델이 main_entities 에 한화생명을 폭넓게 적는 탓에 "퇴직연금 기금화"
    같은 기사까지 T1 이 돼 10건 중 7건이 1티어로 몰린다. 티어가 아무것도
    가르지 못한다. 그래서 여기서는 **기사의 핵심이 한화인지**를 제목으로 본다.
    """
    title = r[K_HEAD] or ""
    ents = r[K_ENT] or ""
    etype = r[K_ETYPE] if len(r) > K_ETYPE else ""
    pri = r[K_PRI] or ""

    group = events.ACTION_GROUPS.get(etype or "other", "other")

    # 1순위 — 기사의 핵심이 한화. **금융계열사도 여기다.**
    # (한때 금융계열사를 4순위로 내려 봤는데, 한화생명이 2·4순위로 흩어져
    #  오히려 나빠졌다. 발주 4순위는 "한화가 곁다리로만 언급된 것" 을 뜻한다.)
    if csfit.primary_category(title) == "한화":
        return 1
    # 2순위 — 진행중인 M&A · 보험
    if pri == "ma_governance" or group == "deal":
        return 2
    if pri == "insurance_finance":
        return 2
    # 3순위 — 규제 · 지배구조
    #
    # **primary_topic 만 보면 안 된다.** 2순위는 event_type 도 보는데 여기만
    # 안 봐서, "금융위, 토스뱅크 반값 엔화 적법성 검토" 같은 명백한 규제
    # 기사가 5순위로 추락했다(2026-10-05 감사, 실측 6건).
    # "지배구조" 는 발주 수칙 3번에 적혀 있는데 분기 자체가 없었다.
    if pri == "regulation_policy" or group in ("regulation", "governance"):
        return 3
    # 4순위 — 한화가 **곁다리로만** 언급된 기사. 제목에는 없고 entities 에만
    # 있는 경우다. 1순위가 제목으로 먼저 가져가므로 여기 남는 건 그것뿐이다.
    if "한화" in ents or _HANWHA_FIN_RE.search(ents):
        return 4
    return 5


def _acceptable(r, picked: list) -> bool:
    """대체까지 끝난 기사를 최종적으로 받아들일 수 있는가.

    **대체 뒤에 다시 봐야 한다.** 유료기사를 같은 사건의 다른 매체 기사로
    갈아타고 나면 그 기사가 이미 뽑힌 것과 같은 사건일 수도, A팀이 이미
    공유한 것일 수도 있다. 갈아타기 전에만 검사해서 한화투자증권 종투사 건이
    두 번 실렸다(2026-10-02).
    """
    if shared.already_shared(r[K_HEAD] or ""):
        return False
    return not any(
        _same(r[K_HEAD] or "", r[K_ENT] or "",
              r[K_ETYPE] if len(r) > K_ETYPE else "",
              q[K_HEAD] or "", q[K_ENT] or "",
              q[K_ETYPE] if len(q) > K_ETYPE else "")
        for _, q in picked)


def _coverage_of(r) -> int:
    """그 기사가 담은 **집합의 크기.**

    발주자 절대 수칙(2026-10-05): "집합개념으로 따지면 더 큰개념의 내용을
    담고있는거만 하나만 올려." 같은 사건을 접을 때 남길 쪽은 적합도가
    높은 쪽이 아니라 **내용을 더 많이 담은 쪽**이다. 적합도로 뽑았더니
    '해외 M&A 볼트온 전략 강화 추세'(더 큰 집합) 대신 '…도쿄해상 주목해야'
    가 남았다(2026-10-05 3차 감사).
    """
    h = r[K_HEAD] or ""
    d = {"main_entities": [x.strip() for x in (r[K_ENT] or "").split(",")
                           if x.strip()],
         "event_type": r[K_ETYPE] if len(r) > K_ETYPE else "",
         "title_ko": h}
    try:
        return int(events.coverage(d, h))
    except Exception:
        return 0


def _fetchable(group: list, store) -> tuple | None:
    """묶음에서 **전문을 가져올 수 있는** 기사를 고른다.

    유료기사는 Instant View 에 전문을 실을 수 없다. 같은 사건을 다룬 다른
    매체 기사가 묶음 안에 있으면 그걸 쓰고, 없으면 None 을 돌려준다
    (호출부가 그 사건을 통째로 건너뛴다). 2026-10-02 사용자 지정.

    **집합이 큰 순으로** 보고, 같으면 점수로 가린다. 점수 순으로 보았더니
    읽을 수 있는 큰 집합이 묶음에 있는데도 작은 집합을 집어 올렸다 —
    '신한은행 해킹 IP, 토스뱅크에도 접근'(집합 20) 대신 '토스뱅크·온투업체도
    타깃'(집합 14)이 나갔다(2026-10-05 3차 감사). 집합론이 점수보다 앞선다.
    유료 도메인은 네트워크를 타지 않고 바로 거른다.
    """
    for sc, r in sorted(group, key=lambda x: (-_coverage_of(x[1]), -x[0])):
        url = gnews.resolve(r[K_URL] or "", store)
        if telegraph.is_paywalled(url):
            continue
        paras, why = telegraph.fetch_article(url, title=r[K_HEAD] or "")
        if not why:
            return sc, r
    return None


def issue_blocked(r, issue_used: dict, prior_issues: set) -> str | None:
    """그 기사의 **사건 덩어리**가 이미 찼는가. 막히면 사유를 돌려준다.

    발주자 지정(2026-10-07): "AI사고, 금융권 잇단 AI 해킹사건은 어제 이미
    올라간 AI 프로파일링이랑 주제가 겹치잖아. 그리고 둘끼리도 겹치고.
    어제자에 이미 올린 집합론에 따른 내용은 중복으로 올리지 마라."

    same_event 로는 못 잡는다 — 사고 자체·당국 대응·관련 상품·인력 수요는
    서로 '다른 사건' 이기 때문이다. 하나의 사건 덩어리(csfit.ISSUE_THEMES)로
    묶어서 **판 안에서 한 건, 직전 판에 나갔으면 아예 제외** 한다.
    """
    themes = csfit.issue_themes_of(r[K_HEAD] or "", r[K_ENT] or "")
    if not themes:
        return None

    # **새로 터진 사건은 막지 않는다.**
    #
    # 발주자 보충(2026-10-07): "AI 해킹사건 보안사건 터진 거랑 완전 별개 또
    # 새로운 사건이 터진 거면 모르겠는데, 예를 들어 5일날 터진 사건을 계속
    # 언급하는 뉴스면 중복이니까 더 올릴 필요는 없단 거지."
    #
    # 그래서 막는 것은 **이미 터진 사건을 되짚는 보도**다 — 파장·전망·대응·
    # 해설·관련 상품·인력 수요 같은 것들. 반대로 확정된 새 사실(새 피해,
    # 제재, 수사 착수, 계약 체결 …)은 그 자체가 새 사건이므로 통과시킨다.
    _d = {"main_entities": [x.strip() for x in (r[K_ENT] or "").split(",")
                            if x.strip()],
          "event_type": r[K_ETYPE] if len(r) > K_ETYPE else "",
          "title_ko": r[K_HEAD] or ""}
    try:
        fresh = events.looks_material(r[K_HEAD] or "", _d)
    except Exception:
        fresh = False

    for th in themes:
        if th in prior_issues and not fresh:
            return f"직전 판에 나간 이슈({th})"
        if issue_used.get(th, 0) >= 1:
            return f"같은 판 이슈 중복({th})"
    return None


def mark_issue(r, issue_used: dict) -> None:
    for th in csfit.issue_themes_of(r[K_HEAD] or "", r[K_ENT] or ""):
        issue_used[th] = issue_used.get(th, 0) + 1


def prior_issue_themes(store, since: float, days: int = 2) -> set:
    """직전 판들에 실린 기사의 사건 덩어리 모음."""
    out: set = set()
    if store is None:
        return out
    try:
        rows = store.cstop10_recent_clusters(since - days * 24 * 3600)
    except Exception:
        return out
    for h, e, _t in rows:
        out.update(csfit.issue_themes_of(h or "", e or ""))
    return out


def select(rows: list, count: int | None = None, store=None,
           now: float | None = None, prior_issues: set | None = None) -> list:
    """적합도 순 Top N.

    같은 사건은 **한 건만** 싣는다. 저장된 cluster_id 로 1차로 묶고,
    그것만으로는 갈리는 건들을 _same() 으로 2차로 묶는다.

    홍보성 기사는 이틀에 한 건, 가장 큰 것 하나만 넣는다.
    """
    import time
    count = count or settings.daily_brief_count
    now = now or time.time()

    # 1차: 저장된 cluster_id
    best: dict[str, tuple] = {}
    for r in rows:
        cid = r[K_CLUSTER] or f"_solo:{r[K_KEY]}"
        sc = (csfit.score(r[K_HEAD] or "", r[K_ENT] or "",
                          r[K_PRI] or "", r[K_SCORE])[0]
              + csfit.risk_bonus(r[K_HEAD] or ""))
        cur = best.get(cid)
        # **집합이 큰 쪽을 대표로 둔다** (집합론 절대 수칙). 집합 크기가
        # 같을 때만 적합도로 가린다.
        if cur is None or (_coverage_of(r), sc) > (_coverage_of(cur[1]), cur[0]):
            best[cid] = (sc, r)

    # 2차: 실제 값으로 같은 사건 재판정. 점수 높은 쪽을 남긴다.
    # **같은 사건 묶음의 멤버를 전부 들고 있는다.**
    # 대표가 유료기사면 같은 사건을 다룬 다른 매체 기사로 갈아타야 한다.
    # 예전엔 대표 하나만 남기고 버려서 대체할 후보가 없었다(2026-10-02).
    merged: list = []
    groups: dict[int, list] = {}
    # **집합이 큰 것부터** 넣는다. 먼저 들어간 쪽이 대표로 남으므로,
    # 이 순서가 곧 '더 큰 집합만 올린다' 는 수칙의 구현이다.
    for sc, r in sorted(best.values(),
                        key=lambda x: (-_coverage_of(x[1]), -x[0])):
        dup = -1
        for i, (sc2, r2) in enumerate(merged):
            if _same(r[K_HEAD] or "", r[K_ENT] or "", r[K_ETYPE] if len(r) > K_ETYPE else "",
                     r2[K_HEAD] or "", r2[K_ENT] or "",
                     r2[K_ETYPE] if len(r2) > K_ETYPE else ""):
                dup = i
                break
        if dup >= 0:
            groups[dup].append((sc, r))
        else:
            groups[len(merged)] = [(sc, r)]
            merged.append((sc, r))

    dropped = len(best) - len(merged)
    if dropped:
        print(f"[cstop10] 같은 사건 {dropped}건 접음")

    # 3차: 최근 Top10 에 이미 실린 사건 제외
    if store is not None:
        # **2일치를 엄격 모드로 본다.** 7일치를 느슨한 모드로 대면 회사명·
        # 분야가 같다는 이유로 걸려, 창 안 후보 20건 중 12건이 "기게재"로
        # 빠졌다 — 그 빈자리를 며칠 전 기사가 메웠다(2026-10-05 발주자 지적).
        # 막아야 하는 것은 **같은 사건의 반복**이지 같은 회사·같은 분야가
        # 아니다. strict=True 는 회사명 말고 겹치는 낱말을 반드시 요구한다.
        recent = store.cstop10_recent_clusters(now - 2 * 24 * 3600)
        if recent:
            keep = []
            for sc, r in merged:
                hit = any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                                r[K_ETYPE] if len(r) > K_ETYPE else "",
                                h or "", e or "", t or "", strict=True)
                          for h, e, t in recent)
                if hit:
                    print(f"[cstop10] 기게재 사건 제외: {(r[K_HEAD] or '')[:40]}")
                else:
                    keep.append((sc, r))
            merged = keep

    # 3.5차: **창 시작 전에 이미 일반 탭으로 나간 사건**은 뺀다.
    #
    # 3차는 '이전 Top10 에 실렸던 것'만 본다. 그런데 애큐온 인수 건처럼 일반
    # 탭에는 여러 번 나갔지만 Top10 에는 안 실린 사건이 있다. 팀은 이미 그
    # 사건을 봤는데 Top10 에서 또 보게 된다(2026-10-05 지적).
    #
    # 창 **안**에서 발행된 것은 빼지 않는다 — 그건 오늘 처음 전한 뉴스이고,
    # Top10 은 원래 그중에서 고르는 물건이다. 창 **밖**(그 전)에 나간 것만 뺀다.
    if store is not None:
        since, _until, _lab = window(now)
        # **창 직전 하루만 본다.** 기본 14일치를 보니 같은 분야 기사가
        # 줄줄이 걸려 창 안 후보 20건 중 8건이 빠졌다(2026-10-05 발주자
        # 지적: 10/6 판에 10/5 내용이 없다). 막아야 하는 것은 "어제 이미
        # 전한 사건을 오늘 또" 이지, 2주 전 비슷한 기사가 아니다.
        prior = store.published_clusters_before(since, since - 24 * 3600)
        if prior:
            keep = []
            for sc, r in merged:
                hit = any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                                r[K_ETYPE] if len(r) > K_ETYPE else "",
                                h or "", e or "", t or "", strict=True)
                          for h, e, t in prior)
                if hit:
                    print(f"[cstop10] 일반탭 기발행 사건 제외: {(r[K_HEAD] or '')[:38]}")
                else:
                    keep.append((sc, r))
            merged = keep

    # 3.7차: **다른 날짜 Top10 에 이미 실린 '그 기사' 는 뺀다.**
    #
    # _same() 은 엔티티가 비면 무조건 다른 사건으로 본다. 그래서 엔티티가 빈
    # 행(뱅크샐러드)이 10/4·10/5 양쪽에 실렸다. URL 문자열 비교도 통하지
    # 않는다 — 봇은 구글뉴스 리디렉터를, 큐레이션 행은 풀린 원문을 저장한다.
    # 기사 번호로 맞추면 그 둘을 다 피해 간다(store.top10_article_keys).
    if store is not None:
        used_keys = store.top10_article_keys()
        if used_keys:
            keep = []
            for sc, r in merged:
                k = article_key(gnews.resolve(r[K_URL] or "", store))
                if k and k in used_keys:
                    print(f"[cstop10] 타 날짜 Top10 기게재 제외: {(r[K_HEAD] or '')[:36]}")
                else:
                    keep.append((sc, r))
            merged = keep

    # A팀이 이미 공유한 건은 뺀다.
    # 담당자들이 아침에 올린 것을 봇이 또 올리면 중복이다. 비교는 제목 기준이다 —
    # 같은 사건을 다른 매체가 쓰면 URL 이 전혀 다르다(2026-10-02 사용자 지정).
    before = len(merged)
    kept = []
    for sc, r in merged:
        hit = shared.already_shared(r[K_HEAD] or "")
        if hit:
            print(f"[cstop10] A팀 기공유 제외: {(r[K_HEAD] or '')[:34]}")
        else:
            kept.append((sc, r))
    merged = kept

    # 경영 판단에 쓸 데 없는 체인 기술·시세 기사는 뺀다 (사용자 지정).
    before = len(merged)
    merged = [(sc, r) for sc, r in merged
              if not csfit.is_crypto_tech(r[K_HEAD] or "")]
    if before != len(merged):
        print(f"[cstop10] 크립토 기술·시세 {before - len(merged)}건 제외")

    # **1차 정렬키는 사용자가 지정한 추천 서칭 순서다.**
    #   1 한화  2 M&A·보험  3 규제·지배구조  4 한화 곁다리  5 기타
    # 125건 실측 가중치(csfit)는 **같은 티어 안에서의** 2차 정렬키다.
    # 기준의 위계가 그렇다 — 추천 순서가 틀이고, 125건은 그 안에서 담당자들이
    # 실제로 무엇을 골랐는지 보여주는 성향이다(2026-10-02 사용자 설명).
    def rank_key(x):
        return -(x[0] + TIER_BONUS.get(tier_of(x[1]), 0))

    # 순위를 매긴 뒤에도 묶음을 찾을 수 있게 원래 index 를 들고 다닌다.
    idx_of = {id(r): i for i, (_, r) in enumerate(merged)}
    ranked = sorted(merged, key=rank_key)

    pr = [(sc, r) for sc, r in ranked if csfit.is_pr(r[K_HEAD] or "")]
    normal = [(sc, r) for sc, r in ranked if not csfit.is_pr(r[K_HEAD] or "")]

    allow_pr = True
    if store is not None:
        last = store.last_pr_pick()
        if last and (now - last) < PR_INTERVAL_SEC:
            allow_pr = False
    pr_pick = pr[:1] if (allow_pr and pr) else []
    if pr and not allow_pr:
        print(f"[cstop10] 홍보성 {len(pr)}건 보류 — 직전 게재 후 이틀 미경과")

    cap = settings.daily_brief_max_per_entity
    used: dict[str, int] = {}
    cat_used: dict[str, int] = {}
    theme_used: dict[str, int] = {}
    # 사건 덩어리 — 판 안에서 한 건, 직전 판에 나간 것은 제외(2026-10-07).
    issue_used: dict[str, int] = {}
    _prior_issues = prior_issues or set()
    picked, deferred = [], []

    # **집합론 생존자만 쓴다.** 최소배정은 범주별로 도는데, 같은 사건의
    # 더 큰 집합이 다른 범주로 분류돼 있으면 작은 쪽이 먼저 자리를 잡고
    # 큰 쪽을 중복으로 막는다. 실측(2026-10-05 3차 감사): 'M&A·매각'
    # 최소배정이 '토스뱅크·온투업체도 타깃 AI 해킹'(집합 작음)을 넣어,
    # 같은 해킹 사태의 '신한은행 해킹 IP, 토스뱅크에도 접근'(집합 큼)이
    # 밀려났다. 접기를 먼저 해 큰 쪽만 남긴다.
    _alive = {r[K_KEY] for r in fold_by_event([r for _, r in normal])}
    normal = [(sc, r) for sc, r in normal if r[K_KEY] in _alive]

    # 범주별 최소 1자리을 **먼저** 채운다. 점수 경쟁에 맡기면 영영 못 들어온다.
    reserved_keys = set()
    for catg, floor in csfit.CATEGORY_FLOOR.items():
        got = 0
        # 그 범주에서 **점수가 높은 순**으로 본다. 최소배정은 한 자리뿐이라
        # 아무거나가 아니라 그 범주 최고점이 들어가야 한다.
        _cands = sorted(
            (x for x in normal
             if csfit.primary_category(x[1][K_HEAD] or "") == catg),
            key=lambda x: -x[0])
        for sc, r in _cands:
            if got >= floor:
                break
            # 느슨한 하한을 쓴다 — 자리를 비워 두느니 그 범주 최고점을 넣는다.
            # csfit.FLOOR_RELAXED_MIN 주석 참고.
            if sc < csfit.FLOOR_RELAXED_MIN:
                continue
            if csfit.primary_category(r[K_HEAD] or "") != catg:
                continue
            if any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                         r[K_ETYPE] if len(r) > K_ETYPE else "",
                         q[K_HEAD] or "", q[K_ENT] or "",
                         q[K_ETYPE] if len(q) > K_ETYPE else "")
                   for _, q in picked):
                continue
            _th = csfit.themes_of(r[K_HEAD] or "", r[K_ENT] or "")
            if any(theme_used.get(t, 0) >= csfit.THEME_CAP[t] for t in _th):
                continue
            _ib = issue_blocked(r, issue_used, _prior_issues)
            if _ib:
                print(f"[cstop10] 최소배정 제외({_ib}): {(r[K_HEAD] or '')[:34]}")
                continue
            got_alt = _fetchable(groups.get(idx_of.get(id(r), -1), [(sc, r)]), store)
            if got_alt is None or not _acceptable(got_alt[1], picked):
                continue
            sc, r = got_alt
            # **갈아탄 뒤 다시 본다.** _fetchable 은 같은 사건 묶음에서 다른
            # 매체 기사로 바꿔 끼우는데, 바뀐 기사가 막아야 할 이슈일 수 있다.
            # 실측(2026-10-07): 검사를 통과한 기사가 'AI 해킹 보안인재' 건으로
            # 갈아타 그대로 들어갔다.
            _ib = issue_blocked(r, issue_used, _prior_issues)
            if _ib:
                print(f"[cstop10] 최소배정 제외({_ib}): {(r[K_HEAD] or '')[:34]}")
                continue
            picked.append((sc, r))
            mark_issue(r, issue_used)
            reserved_keys.add(r[K_KEY])
            cat_used[catg] = cat_used.get(catg, 0) + 1
            for _t in csfit.themes_of(r[K_HEAD] or "", r[K_ENT] or ""):
                theme_used[_t] = theme_used.get(_t, 0) + 1
            ents = [e for e in (r[K_ENT] or "").split(",") if e]
            h = ents[0] if ents else (r[K_PRI] or "_")
            used[h] = used.get(h, 0) + 1
            got += 1
            print(f"[cstop10] {catg} 최소배정: {(r[K_HEAD] or '')[:40]}")

    for sc, r in pr_pick + normal:
        if r[K_KEY] in reserved_keys:
            continue
        ents = [e for e in (r[K_ENT] or "").split(",") if e]
        head = ents[0] if ents else (r[K_PRI] or "_")
        # 범주 상한 — 125건 실측 분포에 맞춘다. 안 걸면 국내 보험·GA 가 독식한다.
        catg = csfit.primary_category(r[K_HEAD] or "", r[K_ENT] or "")
        ccap = csfit.CATEGORY_CAP.get(catg, 2)
        if cat_used.get(catg, 0) >= ccap:
            deferred.append((sc, r))
            continue
        # 주제 상한 — 대표범주가 흩어져도 같은 주제가 몰리지 않게 한다.
        _themes = csfit.themes_of(r[K_HEAD] or "", r[K_ENT] or "")
        if any(theme_used.get(t, 0) >= csfit.THEME_CAP[t] for t in _themes):
            deferred.append((sc, r))
            continue
        if used.get(head, 0) >= cap:
            deferred.append((sc, r))
            continue
        # 사건 덩어리 — 판 안 한 건, 직전 판에 나갔으면 제외(2026-10-07).
        _ib = issue_blocked(r, issue_used, _prior_issues)
        if _ib:
            print(f"[cstop10] 제외({_ib}): {(r[K_HEAD] or '')[:38]}")
            continue
        # **최종 안전장치 — 이미 뽑은 것과 같은 사건이면 넣지 않는다.**
        # 앞 단계에서 접었더라도 최소배정·deferred 경로로 들어올 수 있다.
        # 10건은 서로 다른 사건이어야 한다(2026-10-02 사용자 지정).
        if any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                     r[K_ETYPE] if len(r) > K_ETYPE else "",
                     q[K_HEAD] or "", q[K_ENT] or "",
                     q[K_ETYPE] if len(q) > K_ETYPE else "")
               for _, q in picked):
            continue
        # **유료기사면 같은 사건의 다른 매체 기사로 갈아탄다.**
        # 전문을 Instant View 에 실을 수 없는 기사는 올리지 않는다. 대체할
        # 기사가 묶음에 없으면 그 사건을 통째로 건너뛴다(2026-10-02 사용자 지정).
        got_alt = _fetchable(groups.get(idx_of.get(id(r), -1), [(sc, r)]), store)
        if got_alt is None:
            print(f"[cstop10] 전문 확보 불가 — 건너뜀: {(r[K_HEAD] or '')[:34]}")
            continue
        if not _acceptable(got_alt[1], picked):
            continue
        sc, r = got_alt
        _ib = issue_blocked(r, issue_used, _prior_issues)     # 갈아탄 뒤 재검사
        if _ib:
            print(f"[cstop10] 제외({_ib}): {(r[K_HEAD] or '')[:38]}")
            continue

        cat_used[catg] = cat_used.get(catg, 0) + 1
        for _t in csfit.themes_of(r[K_HEAD] or "", r[K_ENT] or ""):
            theme_used[_t] = theme_used.get(_t, 0) + 1
        used[head] = used.get(head, 0) + 1
        mark_issue(r, issue_used)
        picked.append((sc, r))
        if len(picked) >= count:
            break
    for sc, r in deferred:
        if len(picked) >= count:
            break
        if any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                     r[K_ETYPE] if len(r) > K_ETYPE else "",
                     q[K_HEAD] or "", q[K_ENT] or "",
                     q[K_ETYPE] if len(q) > K_ETYPE else "")
               for _, q in picked):
            continue
        # **상한을 여기서도 지킨다.** deferred 는 정의상 범주·주제·엔티티
        # 상한에 걸려 밀린 것들인데, 재투입 루프가 아무 검사도 안 해서
        # 자리가 비면 상한이 통째로 무효가 됐다 — 상한 1인 채널·GA 가
        # 6건까지 들어갔다(2026-10-05 감사, 재현 확인).
        _catg = csfit.primary_category(r[K_HEAD] or "", r[K_ENT] or "")
        if cat_used.get(_catg, 0) >= csfit.CATEGORY_CAP.get(_catg, 2):
            continue
        _themes = csfit.themes_of(r[K_HEAD] or "", r[K_ENT] or "")
        if any(theme_used.get(t, 0) >= csfit.THEME_CAP[t] for t in _themes):
            continue
        _ents = [e for e in (r[K_ENT] or "").split(",") if e]
        _head = _ents[0] if _ents else (r[K_PRI] or "_")
        if used.get(_head, 0) >= settings.daily_brief_max_per_entity:
            continue
        if issue_blocked(r, issue_used, _prior_issues):
            continue
        got_alt = _fetchable(groups.get(idx_of.get(id(r), -1), [(sc, r)]), store)
        if got_alt is None or not _acceptable(got_alt[1], picked):
            continue
        if issue_blocked(got_alt[1], issue_used, _prior_issues):   # 갈아탄 뒤
            continue
        picked.append(got_alt)
        mark_issue(got_alt[1], issue_used)
        cat_used[_catg] = cat_used.get(_catg, 0) + 1
        for _t in _themes:
            theme_used[_t] = theme_used.get(_t, 0) + 1
        used[_head] = used.get(_head, 0) + 1

    # 최종 배열도 추천 순서를 따른다. 홍보성은 맨 아래.
    picked.sort(key=lambda x: (csfit.is_pr(x[1][K_HEAD] or ""), rank_key(x)))
    return picked


TG_LIMIT = 4096          # 텔레그램 한 메시지 상한. **보이는 텍스트** 기준이다
SAFE_LIMIT = 4060        # 여유분 36자. visible_len 이 정확해 더 줄일 이유가 없다

# 기사와 기사 사이. 섹션 사이가 한 줄이라 기사 경계는 더 벌려야 구분된다.
ITEM_GAP = "\n\n━━━━━\n\n"


def visible_len(s: str) -> int:
    """텔레그램이 세는 길이 — HTML 태그는 빼고 센다."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))


def _sections(text: str) -> dict:
    """발행 원문에서 섹션을 뜯어낸다. 없으면 빈 값."""
    out = {"lede": "", "bullets": [], "why": "", "when": "", "source": ""}
    if not text:
        return out
    m = re.search(r"✅ <b>핵심</b>\n(.+?)(?:\n\n|$)", text, re.S)
    if m: out["lede"] = m.group(1).strip()
    m = re.search(r"<blockquote>(.*?)</blockquote>", text, re.S)
    if m:
        out["bullets"] = [b.strip(" •").strip()
                          for b in m.group(1).split("\n") if b.strip()]
    m = re.search(r"(?:🐧|💡 <b>Why it matters</b>\n)\s*(.+?)(?:\n\n|$)", text, re.S)
    if m: out["why"] = m.group(1).strip()
    m = re.search(r"🕒 (.+?)(?:\n|$)", text)
    if m: out["when"] = m.group(1).strip()
    m = re.search(r"</a>\s*-\s*(.+?)(?:\n|$)", text)
    if m:
        src = m.group(1).strip()
        # "한화 금융계열사(비즈니스포스트)" 처럼 내부 수집기 이름이 앞에 붙는다.
        # 독자에게 필요한 건 매체명뿐이다.
        mm = re.match(r"^.+?\((.+)\)$", src)
        out["source"] = (mm.group(1) if mm else src).strip()
    return out


def _ts(v) -> float | None:
    """epoch 로 쓸 수 있는 값이면 float, 아니면 None. DB 에 TEXT 로 들어온 행이 있다."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _cut(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _posted_label(r) -> str:
    """기사 **원문 입력시각** 라벨. 없으면 빈 문자열.

    발행(sent_at)이 아니라 origin_at 이다 — 봇이 언제 내보냈는지가 아니라
    매체가 언제 쓴 기사인지를 보여줘야 한다(2026-10-05 지적).
    """
    ts = r[K_ORIGIN] if len(r) > K_ORIGIN else None
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d %H:%M KST")


def _iv_url(r, real_url: str, store) -> str | None:
    """이 기사의 telegra.ph 페이지 주소. 없으면 만든다.

    제목을 여기로 링크하면 **눌렀을 때 텔레그램 안에서 Instant View 로 열린다.**
    원래 기사 주소로 링크하면 매체마다 복불복이다 — IV 템플릿이 등록된
    도메인만 되고(글로벌이코노믹 ○), 아닌 곳은 "Open this link?" 가 뜨면서
    브라우저로 나간다(newsis AMP ×). telegra.ph 는 항상 된다.
    """
    if store is None:
        return None
    key = r[K_KEY]
    hit = store.get_iv_url(key)
    if hit:
        return hit

    body = (r[K_TEXT] or "") if len(r) > K_TEXT else ""
    bullets = []
    m = re.search(r"<blockquote>(.*?)</blockquote>", body, re.S)
    if m:
        bullets = [html.unescape(re.sub(r"<[^>]+>", "", b)).strip(" •").strip()
                   for b in m.group(1).split("\n") if b.strip()]
    src_name = ""
    m = re.search(r"</a>\s*-\s*(.+?)(?:\n|$)", body)
    if m:
        nm = html.unescape(m.group(1)).strip()
        mm = re.match(r"^.+?\((.+)\)$", nm)
        src_name = (mm.group(1) if mm else nm).strip()

    # **페이지는 언제나 만든다 — 제목은 반드시 Instant View 로 열려야 한다.**
    # 절대 규칙이다(2026-10-02). 본문을 못 가져왔다고 페이지를 안 만들면
    # 그 기사만 브라우저로 튕겨 나간다.
    # 본문이 없으면(유료기사·추출실패) 긁어온 척하지 않고, 우리가 가진
    # 핵심·주요 내용으로 채우고 왜 전문이 없는지 밝힌 뒤 원문으로 보낸다.
    paras, why_fail = telegraph.fetch_article(real_url,
                                              title=r[K_HEAD] or "")
    note = ""
    if why_fail:
        note = (telegraph.NOTE_PAYWALL
                if ("유료" in why_fail or "페이월" in why_fail)
                else telegraph.NOTE_FAILED)
        print(f"[cstop10] 본문 없음({why_fail}) — 요약으로 IV 구성: "
              f"{(r[K_HEAD] or '')[:32]}")

    content = telegraph.build_content(
        summary=(r[K_LEDE] or "").strip(),
        bullets=bullets,
        why=(r[K_WHY] or "").strip(),
        excerpt=paras,
        source_url=real_url,
        source_name=src_name,
        note=note,
        posted_label=_posted_label(r),
    )
    # 바이라인에도 입력시각을 넣는다 — IV 머리의 날짜는 페이지 생성 시각이라
    # 바꿀 수 없다(telegraph.build_content 주석). 바이라인은 우리가 정한다.
    # 바이라인에도 입력시각을 적는다. telegra.ph 가 그 뒤에 자기 생성시각(UTC)을
    # 붙이는데 지울 수 없어서, **바로 앞에 진짜 시각을 놓아** 먼저 읽히게 한다.
    _pl = _posted_label(r)
    _author = (f"{src_name} · 기사발행 {_pl}" if (src_name and _pl)
               else (src_name or settings.bot_name))
    url = telegraph.create_page(store, r[K_HEAD] or "", content,
                                author=_author)
    if url:
        store.put_iv_url(key, url)
    return url


def _link_headline(body: str, url: str) -> str:
    """제목 줄을 눌러 기사로 갈 수 있게 링크로 감싼다.

    원문의 제목 줄은 `🏢 <b>제목</b>` 형태라 눌러도 아무 일이 없다. 링크는
    맨 아래 "기사 원문" 에만 있어서 Top10 처럼 여러 건이 이어진 글에서는
    제목에서 바로 넘어가는 게 자연스럽다(2026-10-02 사용자 지정).

    이미 <a> 로 감싸여 있으면 건드리지 않는다.
    """
    if not url:
        return body
    lines = body.split("\n")
    for i, ln in enumerate(lines):
        if "<a " in ln:
            break
        m = re.match(r"^(\S+)\s+<b>(.+)</b>\s*$", ln)
        if m:
            icon, title = m.group(1), m.group(2)
            lines[i] = f'{icon} <a href="{html.escape(url, quote=True)}"><b>{title}</b></a>'
            break
    return "\n".join(lines)


def render_item(r, url: str | None = None, iv: str | None = None,
                **_ignored) -> str:
    """기사 1건. **발행 당시 원문을 그대로 쓴다.**

    원문(published.text)에는 ✅ 핵심 · 📂 주요 내용(불릿) · 🐧 · 🕒 · 기사 원문 ·
    해시태그가 이미 완성된 형태로 들어 있다. 실시간 탭에 나간 바로 그 글이다.

    **다시 조립하지 않는다.** 두 번 데였다.
      1) 섹션을 뜯어 재조립하면서 html.escape 를 한 번 더 걸었다. 원문은 이미
         이스케이프돼 있어서 `&#x27;` 가 화면에 그대로 찍혔다(2026-10-02).
      2) 한 메시지에 맞추려고 불릿을 2개로 깎고 문장에 상한을 걸었더니
         내용 품질이 눈에 띄게 떨어졌다.

    여기서는 맨 윗줄(봇 이름)만 떼고 카테고리 줄로 바꿔 끼운다. 그뿐이다.
    길이가 넘치면 render_all 이 **메시지를 나눈다** — 내용을 깎지 않는다.
    """
    e = html.escape
    body = (r[K_TEXT] or "").strip() if len(r) > K_TEXT else ""
    cat = topics.display_name(r[K_PRI] or "") or ""
    tag = " · 홍보" if csfit.is_pr(r[K_HEAD] or "") else ""
    head = f"<b>[{e(cat)}]</b>{tag}"

    if body:
        lines = body.split("\n")
        if lines and settings.bot_name in lines[0]:
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        body = "\n".join(lines)
        # 옛 메시지에 남은 영문 라벨만 펭귄으로 맞춘다.
        body = body.replace("💡 <b>Why it matters</b>\n", "🐧 ")
        # 본문 안의 구글뉴스 주소를 전부 실제 기사 주소로 바꾼다.
        # 제목뿐 아니라 맨 아래 "기사 원문" 도 바로 가야 한다.
        src = r[K_URL] or ""
        if url and src and url != src:
            body = body.replace(html.escape(src, quote=True), html.escape(url, quote=True))
            body = body.replace(src, url)
        # 제목은 Instant View 되는 telegra.ph 로, 본문 속 "기사 원문" 은 실제 기사로.
        body = _link_headline(body, iv or url or src)
        return head + "\n\n" + body

    # 원문이 없는 옛 행은 가진 필드로 최소 형태를 만든다. 여기서만 escape 한다.
    parts = [head, "", f'<a href="{e(r[K_URL] or "")}"><b>{e(r[K_HEAD] or "")}</b></a>', ""]
    if (r[K_LEDE] or "").strip():
        parts += ["✅ <b>핵심</b>", e(r[K_LEDE].strip()), ""]
    if (r[K_WHY] or "").strip():
        parts += [f"🐧 {e(r[K_WHY].strip())}", ""]
    ts = _ts(r[K_ORIGIN] if len(r) > K_ORIGIN else None) or _ts(r[K_SENT])
    if ts:
        parts.append(f"🕒 {datetime.fromtimestamp(ts, KST):%Y-%m-%d %H:%M} KST")
    return "\n".join(parts)


def render_links(picked: list, label: str, _store=None) -> list[tuple[str, str]]:
    """🔗 top10(링크용) 탭에 나갈 [(본문, 미리보기주소)] — **기사 1건당 1메시지**.

    2026-10-05 사용자 지정. 📌 A팀 Top10 과 번호가 1:1로 맞고, 그 번호로
    본문 쪽 해설을 찾아갈 수 있어야 한다. 그래서 번호·제목·원문 주소만 넣는다.

    **한 메시지에 몰아 담지 않는다.** 텔레그램은 메시지당 미리보기 카드를
    하나만 붙이므로(publisher.send_raw 주석), 10건을 묶으면 9건은 카드가 없는
    맨 주소로 남는다. 한 건씩 보내야 10건 전부 카드가 뜬다.

    주소는 **원문 기사**다 — 인스턴트뷰(telegra.ph)가 아니다. 이 탭은 복사해서
    메일·보고서에 붙이는 용도라 telegra.ph 중계 주소는 쓸모가 없다.
    인스턴트뷰로 읽는 건 📌 A팀 Top10 쪽 제목 링크가 한다.
    """
    e = html.escape
    out = []
    for i, (_, r) in enumerate(picked, 1):
        url = gnews.resolve(r[K_URL] or "", _store)
        if not url:
            continue
        cat = topics.display_name(r[K_PRI] or "") or ""
        head = (r[K_HEAD] or "").strip()
        text = (f"<b>{i}.</b> <b>[{e(cat)}]</b>\n"
                f"{e(head)}\n"
                f'<a href="{e(url, quote=True)}">{e(url)}</a>')
        out.append((text, url))
    return out


def _first_urls(msgs: list[str], items: list[str], urls: list[str]) -> list[str]:
    """조각마다 **맨 앞 기사**의 주소. 미리보기는 메시지당 하나뿐이라 대표를 고른다."""
    out, idx = [], 0
    for m in msgs:
        taken = sum(1 for it in items if it in m)
        out.append(urls[idx] if idx < len(urls) else "")
        idx += max(taken, 1)
    return out


def render_all(picked: list, label: str, _store=None) -> tuple[list[str], list[str]]:
    """메시지 목록. **내용을 깎지 않는다 — 넘치면 나눈다.**

    한 판으로 보내려고 불릿을 줄이고 문장을 자르던 것을 그만뒀다. 기사 하나가
    약 460자(보이는 길이)라 10건이면 4,600자다. 텔레그램 상한이 4,096자이니
    2개로 나뉜다. 11개로 쪼개던 때와는 다르다.
    """
    e = html.escape
    n_pr = sum(1 for _, r in picked if csfit.is_pr(r[K_HEAD] or ""))
    head = (f"📌 <b>{e(settings.bot_name)} | {e(settings.cs_top10_label)}</b>"
            f"\n{e(label)}")
    if n_pr:
        head += f"  ·  홍보 {n_pr}건 포함"

    # 구글뉴스 리디렉터를 실제 기사 주소로 바꾼다. 발행 링크의 86% 가 그것이다.
    # 미리보기 카드가 붙으려면 메타태그가 있는 실제 기사 주소여야 한다.
    urls = [gnews.resolve(r[K_URL] or "", _store) for _, r in picked]
    ivs = [_iv_url(r, u, _store) for (_, r), u in zip(picked, urls)]
    items = [render_item(r, url=u, iv=iv)
             for (_, r), u, iv in zip(picked, urls, ivs)]

    # 몇 조각이 필요한지 먼저 센 뒤, 그 수에 맞춰 **고르게** 나눈다.
    # 그냥 채우면 3,999자 + 731자 처럼 한쪽으로 쏠려 보기 나쁘다.
    def pack(limit: int) -> list[str] | None:
        out, cur = [], head
        for it in items:
            cand = cur + ITEM_GAP + it
            if visible_len(cand) > limit and cur != head:
                out.append(cur)
                cur = it
            else:
                cur = cand
        out.append(cur)
        return out if all(visible_len(m) <= SAFE_LIMIT for m in out) else None

    base = pack(SAFE_LIMIT)
    n = len(base)
    if n == 1:
        return base, []
    # 조각 수를 늘리지 않는 선에서 한도를 조여 균등하게 만든다.
    total = visible_len(head) + sum(visible_len(i) + len(ITEM_GAP) for i in items)
    for limit in range(total // n + 60, SAFE_LIMIT + 1, 40):
        trial = pack(limit)
        if trial and len(trial) == n:
            return trial, []
    return base, []


# 한국 매체가 쓰는 기사번호 자리들. **빠진 패턴이 많아 Top10 의 69%가 키를
# 못 뽑았고, 그래서 같은 기사가 다른 날 Top10 에 다시 실렸다**(2026-10-05 감사).
_ART_ID = re.compile(
    r"idxno=(\d+)|newsId=(\w+)|ncode=(\w+)|ar_id=(\d+)|\bno=(\d+)"
    r"|AKR(\d+)|key=(\w+)"
    r"|/v/(\d+)|articles?/(\d+)|/page/view/(\d+)|/news/view/(\d+)"
    r"|/article/(\d+)|/amp/(\d+)|/view\.php\?ud=(\w+)")


def article_key(url: str) -> str:
    """"호스트#기사번호". 같은 기사면 주소 표기가 달라도 같은 값이 나온다.

    봇은 구글뉴스 리디렉터를, 사람이 고른 행은 풀린 원문을 저장한다. 그래서
    URL 문자열 비교로는 같은 기사를 못 잡는다. 엔티티 비교(_same)도
    main_entities 가 빈 행에서는 통하지 않는다(2026-10-05 뱅크샐러드 사고).
    """
    u = url or ""
    m = _ART_ID.search(u)
    if not m:
        return ""
    num = next((g for g in m.groups() if g), "")
    host = re.sub(r"^https?://", "", u).split("/")[0]
    host = re.sub(r"^(www\.|m\.|view\.|news\.|biz\.)", "", host)
    if not num or "google" in host:
        return ""
    return f"{host}#{num}"


def fold_by_event(rows: list) -> list:
    """같은 사건끼리 접고 **집합이 큰 쪽만** 남긴다.

    보충 경로(topup/fill_in_window)는 select() 가 만든 접기 그룹을 모른 채
    원본 후보를 다시 돈다. 그래서 본선에서 '해외 M&A 플랫폼+볼트온 추세'
    (집합 14)가 대표로 접혔는데도, 보충이 같은 사건의 '…도쿄해상 주목해야'
    (집합 13)를 집어 올렸다(2026-10-05 3차 감사). 집합론 절대 수칙은 보충
    경로에도 걸려야 한다.
    """
    kept: list = []
    for r in sorted(rows, key=lambda x: -_coverage_of(x)):
        if any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                     r[K_ETYPE] if len(r) > K_ETYPE else "",
                     q[K_HEAD] or "", q[K_ENT] or "",
                     q[K_ETYPE] if len(q) > K_ETYPE else "")
               for q in kept):
            continue
        kept.append(r)
    return kept


def topup(picked: list, rows: list, store, now: float, want: int,
          prior=None, ignore_cat_cap: bool = False,
          min_fit: int | None = None, prior_issues: set | None = None) -> list:
    """부족분을 **전날 기사에서 한 건씩 채운다.** 이미 뽑은 건 건드리지 않는다.

    2026-10-05 사용자 지정: "72시간까지 뽑지 말고, 그 전날 기사 중에 그나마
    Top10 에 들어갈 만한 걸로 부족한 걸 채워 넣어."

    **창을 넓혀 다시 뽑는 것과 다르다.** 다시 뽑으면 후보가 늘면서 범주 상한·
    최소배정 경쟁이 달라져 오히려 줄어든다(실측: 24h 9건 → 60h 6건). 여기서는
    오늘 뽑은 것을 그대로 두고 **모자란 수만큼만** 앞날에서 더한다.

    기준은 그대로다 — 같은 사건 금지, A팀 기공유 제외, 범주·엔티티 상한,
    유료 교체, 본문 확보 검사를 전부 통과한 것만 더한다.
    """
    if len(picked) >= want:
        return picked

    used_art = store.top10_article_keys() if store is not None else set()
    for sc, r in picked:
        _k = article_key(r[K_URL] or "")
        if _k:
            used_art.add(_k)

    cap = settings.daily_brief_max_per_entity
    used: dict[str, int] = {}
    cat_used: dict[str, int] = {}
    theme_used: dict[str, int] = {}
    issue_used: dict[str, int] = {}
    _prior_issues = prior_issues or set()
    for sc, r in picked:
        mark_issue(r, issue_used)
        ents = [e for e in (r[K_ENT] or "").split(",") if e]
        h = ents[0] if ents else (r[K_PRI] or "_")
        used[h] = used.get(h, 0) + 1
        catg = csfit.primary_category(r[K_HEAD] or "", r[K_ENT] or "")
        cat_used[catg] = cat_used.get(catg, 0) + 1
        for _t in csfit.themes_of(r[K_HEAD] or "", r[K_ENT] or ""):
            theme_used[_t] = theme_used.get(_t, 0) + 1

    # 적합도 + 추천 서칭 순서 가산점으로 '그나마 들어갈 만한' 순서를 만든다.
    # **먼저 같은 사건을 접는다** — 집합이 작은 쪽을 집어 올리면 안 된다.
    scored = []
    for r in fold_by_event(rows):
        fit, _ = csfit.score(r[K_HEAD] or "", r[K_ENT] or "",
                             r[K_PRI] or "", r[K_SCORE])
        scored.append((fit + TIER_BONUS.get(tier_of(r), 0), fit, r))
    scored.sort(key=lambda x: -x[0])

    added = 0
    for _, fit, r in scored:
        if len(picked) >= want:
            break
        floor = csfit.FLOOR_MIN_SCORE if min_fit is None else min_fit
        if fit < floor:
            continue
        if shared.already_shared(r[K_HEAD] or ""):
            continue
        # **다른 날짜 Top10 에 이미 실린 그 기사는 뺀다.** select() 에만 이
        # 검사를 두었더니 보충 경로로 같은 기사가 되들어왔다(2026-10-05).
        _ak = article_key(gnews.resolve(r[K_URL] or "", store))
        if _ak and _ak in (used_art or set()):
            print(f"[cstop10] 보충 제외(타 날짜 Top10): {(r[K_HEAD] or '')[:36]}")
            continue
        # select() 가 거르는 것들이 보충 경로로 새어 들어오면 안 된다.
        if csfit.is_crypto_tech(r[K_HEAD] or ""):
            continue
        if csfit.is_pr(r[K_HEAD] or ""):
            continue          # 홍보성은 select() 의 2일 간격 규칙으로만 넣는다
        # **이전 Top10 에 실렸거나 일반 탭으로 이미 나간 사건도 뺀다.**
        # 보충 경로에 이 검사가 빠져 있어서, 이미 일반 탭에 다섯 번 나간
        # 애큐온 인수 건이 보충으로 다시 들어왔다(2026-10-05).
        if prior and any(
                _same(r[K_HEAD] or "", r[K_ENT] or "",
                      r[K_ETYPE] if len(r) > K_ETYPE else "",
                      h or "", e or "", t or "", strict=True)
                for h, e, t in prior):
            print(f"[cstop10] 보충 제외(기발행): {(r[K_HEAD] or '')[:38]}")
            continue
        # **같은 사건은 절대 두 번 싣지 않는다** (사용자 지정).
        if any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                     r[K_ETYPE] if len(r) > K_ETYPE else "",
                     q[K_HEAD] or "", q[K_ENT] or "",
                     q[K_ETYPE] if len(q) > K_ETYPE else "")
               for _, q in picked):
            continue
        ents = [e for e in (r[K_ENT] or "").split(",") if e]
        head = ents[0] if ents else (r[K_PRI] or "_")
        if used.get(head, 0) >= cap:
            continue
        catg = csfit.primary_category(r[K_HEAD] or "", r[K_ENT] or "")
        if not ignore_cat_cap and \
                cat_used.get(catg, 0) >= csfit.CATEGORY_CAP.get(catg, 2):
            continue
        # **주제 상한은 범주 상한을 풀어도 지킨다.** 자리를 채우려고
        # ignore_cat_cap 을 켜는 바람에 GA 기사가 4건 들어왔다(2026-10-05).
        _themes = csfit.themes_of(r[K_HEAD] or "", r[K_ENT] or "")
        if any(theme_used.get(t, 0) >= csfit.THEME_CAP[t] for t in _themes):
            continue
        _ib = issue_blocked(r, issue_used, _prior_issues)
        if _ib:
            print(f"[cstop10] 보충 제외({_ib}): {(r[K_HEAD] or '')[:34]}")
            continue
        got = _fetchable([(fit, r)], store)
        if got is None:
            continue
        picked.append(got)
        mark_issue(got[1], issue_used)
        used[head] = used.get(head, 0) + 1
        cat_used[catg] = cat_used.get(catg, 0) + 1
        for _t in csfit.themes_of(r[K_HEAD] or "", r[K_ENT] or ""):
            theme_used[_t] = theme_used.get(_t, 0) + 1
        added += 1
        print(f"[cstop10] 보충: 적합{fit} {(r[K_HEAD] or '')[:40]}")
    if added:
        print(f"[cstop10] {added}건 보충 → {len(picked)}건")
    return picked


def origin_floor_day(since: float) -> str:
    """후보로 받아줄 **기사 발행일의 하한**(YYYY-MM-DD).

    창은 sent_at(봇이 내보낸 시각)으로 자른다 — 봇이 처리한 것만 후보가 되므로
    운영상 그래야 한다. 그런데 그러면 **며칠 전 기사**가 어제 수집됐다는
    이유로 오늘 판에 들어온다. 실측(2026-10-05, 10/6 발행분 초안): 창이
    10/5 06:50~10/6 06:00 인데 10/3 21:15 기사가 선정됐다.

    사용자가 창을 정의한 말은 "10월4일 오전 6시50분**뉴스**~10월5일 오전6시까지
    발행된거" 이고, 따로 "10/4 판에 10/1 기사가 실렸다"고 지적했다. 즉 기준은
    **기사가 보도된 날**이다. 그래서 sent_at 창에 더해 기사 발행일 하한을 건다.

    하한은 **날짜 단위**다. 시각으로 자르면 발행시각을 날짜만 주는 매체의
    기사(origin_at 이 00:00 으로 들어온다)가 같은 날인데도 떨어진다.
    허용 폭은 창 시작일에서 cs_top10_fill_days 만큼 거슬러 간 날까지 —
    보충이 허용된 범위와 같게 둔다.
    """
    # fill_days 가 0 이면 **창 시작일 그대로**다. 발주자 지정대로
    # "10/5 06:50 이후 기사"만 남긴다.
    base = datetime.fromtimestamp(since, KST) - timedelta(
        days=max(0, settings.cs_top10_fill_days))
    return base.strftime("%Y-%m-%d")


def drop_stale(rows: list, floor_day: str) -> list:
    """기사 발행일이 하한보다 이른 행을 버린다. 발행일이 없는 행은 남긴다."""
    out = []
    for r in rows:
        # **예외를 넓게 잡는다.** 예전엔 float() 만 감쌌는데, origin_at 이
        # 밀리초 epoch 로 들어오면 fromtimestamp 가 OverflowError/ValueError
        # 를 던져 drop_stale 이 통째로 터지고 run() 이 죽었다 — 삼중 cron 이
        # 전부 같은 코드라 **네 경로가 동시에 죽는** 유일한 유형이었다
        # (2026-10-05 감사, 재현 확인).
        try:
            o = float(r[K_ORIGIN])
            day = datetime.fromtimestamp(o, KST).strftime("%Y-%m-%d")
        except Exception:
            out.append(r)        # 판단 못 하면 살린다 — 버리는 쪽이 더 위험하다
            continue
        if day >= floor_day:
            out.append(r)
    return out


def in_window_origin(r, since: float) -> bool:
    """기사 **원문 발행시각**이 창 시작 이후인가.

    cstop10_candidates 는 봇 수집시각으로 자르므로, 원문이 창보다 앞선
    기사가 후보에 남는다. 발주자 지정(2026-10-05)은 원문시각 기준이다.
    원문시각을 모르면 통과시킨다 — 모른다고 버리면 수집 지연분이 전부
    사라진다.
    """
    v = r[K_ORIGIN] if len(r) > K_ORIGIN else None
    ts = _ts(v)
    if ts is None:
        return True
    if ts > 1e11:       # ms epoch 로 들어온 행이 있다
        ts /= 1000.0
    return ts >= since


def drop_before_window(rows: list, since: float) -> list:
    """원문 발행시각이 창 시작 이전인 후보를 버린다.

    후보 질의는 **봇 수집시각**으로 자른다(수집 지연을 포용하려고). 그래서
    원문이 창보다 앞선 기사가 남는다. 발주자 지정(2026-10-05): "무조건
    10/6 06:50 에 올라가는 완성본은 10/05 기사들과 10/06 새벽 기사여야
    한다. 10/05 06:50am 이후 시점의." 그래서 원문시각으로 한 번 더 자른다.
    원문시각 미상은 통과시킨다.
    """
    keep = [r for r in rows if in_window_origin(r, since)]
    if len(keep) != len(rows):
        print(f"[cstop10] 원문시각 창 밖 {len(rows) - len(keep)}건 제외 "
              f"(하한 {datetime.fromtimestamp(since, KST):%m-%d %H:%M})")
    return keep


def fill_in_window(picked: list, rows: list, store, want: int,
                   prior=None, since: float | None = None,
                   prior_issues: set | None = None) -> list:
    """**창 안에서** 남은 자리를 채운다. 마지막 수단이다.

    발주자 지정(2026-10-05): "무조건 10/6 06:50 에 올라가는 완성본은 10/05
    기사들과 10/06 새벽 기사여야 한다. 10/05 06:50am 이후 시점의." 그래서
    전날 보충을 닫았고(fill_days=0), 모자란 자리는 **창 밖으로 나가는 대신
    창 안에서 상한을 풀어** 채운다.

    푸는 것: 범주 상한·주제 상한·엔티티 상한·적합도 하한.
    푸지 않는 것 — 이건 절대 수칙이라 자리를 비우더라도 지킨다:
      · 같은 사건 두 번 금지(집합론)
      · 이전 Top10·A팀 기공유 제외
      · 유료/본문 미확보 기사 금지
    """
    if len(picked) >= want:
        return picked
    used_art = store.top10_article_keys() if store is not None else set()
    issue_used: dict[str, int] = {}
    _prior_issues = prior_issues or set()
    for _sc, q in picked:
        mark_issue(q, issue_used)
        _k = article_key(q[K_URL] or "")
        if _k:
            used_art.add(_k)
    scored = []
    for r in fold_by_event(rows):
        if any(r is q for _, q in picked):
            continue
        fit, _ = csfit.score(r[K_HEAD] or "", r[K_ENT] or "",
                             r[K_PRI] or "", r[K_SCORE])
        scored.append((fit + TIER_BONUS.get(tier_of(r), 0), fit, r))
    scored.sort(key=lambda x: -x[0])
    added = 0
    for _rank, fit, r in scored:
        if len(picked) >= want:
            break
        if shared.already_shared(r[K_HEAD] or ""):
            continue
        if csfit.is_crypto_tech(r[K_HEAD] or "") or csfit.is_pr(r[K_HEAD] or ""):
            continue
        # **기사 원문시각도 창 안이어야 한다.** 후보 질의는 봇 수집시각으로
        # 자르므로 원문이 창 앞인 기사가 섞인다. 발주자 지정(2026-10-05):
        # "10/05 06:50am 이후 시점의" 기사여야 한다. 상한을 풀어 자리를
        # 채우는 경로에서 이게 빠져 05:46·06:00 기사가 들어왔다.
        if since is not None and not in_window_origin(r, since):
            print(f"[cstop10] 창 안 보충 제외(원문시각 창 밖): "
                  f"{(r[K_HEAD] or '')[:36]}")
            continue
        _ak = article_key(gnews.resolve(r[K_URL] or "", store))
        if _ak and _ak in used_art:
            continue
        # **이전 Top10 에 나간 사건은 끝까지 막는다.** 집합론 절대 수칙이라
        # 자리를 비우는 것이 같은 사건을 두 번 싣는 것보다 낫다.
        if prior and any(
                _same(r[K_HEAD] or "", r[K_ENT] or "",
                      r[K_ETYPE] if len(r) > K_ETYPE else "",
                      h or "", e or "", t or "", strict=True)
                for h, e, t in prior):
            print(f"[cstop10] 창 안 보충 제외(기발행 사건): "
                  f"{(r[K_HEAD] or '')[:36]}")
            continue
        if any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                     r[K_ETYPE] if len(r) > K_ETYPE else "",
                     q[K_HEAD] or "", q[K_ENT] or "",
                     q[K_ETYPE] if len(q) > K_ETYPE else "")
               for _, q in picked):
            continue
        # 사건 덩어리는 상한을 풀어도 지킨다 — 어제 나간 이슈가 되돌아오면
        # 자리를 채운 의미가 없다(2026-10-07 발주자 지적).
        _ib = issue_blocked(r, issue_used, _prior_issues)
        if _ib:
            print(f"[cstop10] 창 안 보충 제외({_ib}): {(r[K_HEAD] or '')[:32]}")
            continue
        got = _fetchable([(fit, r)], store)
        if got is None:
            continue
        picked.append(got)
        mark_issue(got[1], issue_used)
        if _ak:
            used_art.add(_ak)
        added += 1
        print(f"[cstop10] 창 안 보충(상한 해제): 적합{fit} "
              f"{(r[K_HEAD] or '')[:40]}")
    if added:
        print(f"[cstop10] 창 안에서 {added}건 보충 → {len(picked)}건")
    return picked


def quality_of(picked: list) -> float:
    """초안끼리 견주는 점수. **건수가 먼저, 그다음이 적합도 평균**이다.

    10건을 채우는 것이 1순위다(사용자가 거듭 지정). 같은 건수면 적합도 평균이
    높은 쪽을 쓴다. 건수에 큰 가중치를 둬서 9건·평균 60 보다 10건·평균 40 이
    이기도록 한다 — 자리가 빈 Top10 은 그 자체로 사고다.
    """
    if not picked:
        return 0.0
    fits = []
    for sc, r in picked:
        fit, _ = csfit.score(r[K_HEAD] or "", r[K_ENT] or "",
                             r[K_PRI] or "", r[K_SCORE])
        fits.append(fit)
    # 세 번째 항: **기사가 얼마나 최신인가**. 건수·적합도가 같으면 더 최신인
    # 쪽을 쓴다. 이게 없으면 18:00 초안이 전날 기사로 10건을 채운 순간,
    # 밤새 들어온 창내 신선 기사로 만든 04:00 초안이 영영 기각된다
    # (2026-10-05 감사). 최대 1점이라 건수·적합도 순위는 뒤집지 못한다.
    now = datetime.now(KST).timestamp()
    ages = []
    for _sc, r in picked:
        try:
            ages.append((now - float(r[K_ORIGIN])) / 3600)
        except (TypeError, ValueError):
            ages.append(48.0)
    fresh = max(0.0, 1.0 - (sum(ages) / len(ages)) / 48.0)
    return len(picked) * 1000 + sum(fits) / len(fits) + fresh


def select_for(store, asof: float, by_origin: bool = False) -> list:
    """그 발행 시각 기준으로 뽑은 결과. run() 과 같은 길을 쓴다.

    by_origin 기본값은 **False** 다 — run() 의 평시 경로와 같아야 한다.
    예전엔 True 여서 초안은 원문 발행시각으로, 실발행은 봇 발행시각으로
    후보를 잘랐다. 기준이 갈리면 초안에 있던 기사가 06:50 에 사라진다
    (2026-10-05 감사).
    """
    since, until, _label = window(asof)
    want = settings.daily_brief_count
    rows = store.cstop10_candidates(since, until, settings.discard_threshold,
                                    by_origin=by_origin)
    rows = drop_stale(rows, origin_floor_day(since))
    rows = drop_before_window(rows, since)
    _pi = prior_issue_themes(store, since)
    if _pi:
        print(f"[cstop10] 직전 판 이슈 제외 대상: {', '.join(sorted(_pi))}")
    picked = select(rows, store=store, now=asof, prior_issues=_pi)
    if len(picked) < want:
        # **일반 탭에 나간 것은 제외 근거가 아니다.** Top10 은 원래 "그날
        # 팀에 나간 것 중의 Top10" 이다. 일반탭 발행을 기게재로 치니 창 안
        # 후보가 거의 전부 빠지고, 보충이 며칠 전 기사를 끌어왔다
        # (2026-10-05 발주자 지적: 10/6 판에 10/1·10/2 기사가 섞였다).
        # 막아야 하는 것은 **같은 사건이 Top10 에 반복되는 것**이고,
        # 그건 cstop10_recent_clusters(이전 Top10)가 담당한다.
        _prior = store.cstop10_recent_clusters(since - 2 * 24 * 3600)
        picked = topup(picked, rows, store, asof, want, prior=_prior,
                       ignore_cat_cap=True, prior_issues=_pi)
    day = 24 * 3600
    back = 0
    while len(picked) < want and back < settings.cs_top10_fill_days:
        back += 1
        lo, hi = since - day * back, since - day * (back - 1)
        extra = store.cstop10_candidates(lo, hi, settings.discard_threshold,
                                         by_origin=by_origin)
        # 기사 발행일 하한은 여기서도 건다 — 빠져 있어 10/1 기사가
        # 10/6 판에 들어왔다(2026-10-05 감사).
        # **하한은 창 기준으로 고정한다.** lo 기준으로 다시 계산했더니
        # 뒤로 갈수록 느슨해져, 창 안 10/3 기사는 버리면서 보충으로 10/1
        # 기사를 끌어오는 역전이 났다(2026-10-05 발주자 지적).
        extra = drop_stale(extra, origin_floor_day(since))
        if not extra:
            continue
        prior = store.cstop10_recent_clusters(lo - 2 * day)
        picked = topup(picked, extra, store, asof, want, prior=prior)
    # run() 과 같은 최후 보충. 초안과 실발행이 어긋나면 미리 검증한 의미가 없다.
    while len(picked) < want and back < settings.cs_top10_max_fill_days:
        back += 1
        lo, hi = since - day * back, since - day * (back - 1)
        extra = store.cstop10_candidates(lo, hi, settings.discard_threshold,
                                         by_origin=by_origin)
        # **하한은 창 기준으로 고정한다.** lo 기준으로 다시 계산했더니
        # 뒤로 갈수록 느슨해져, 창 안 10/3 기사는 버리면서 보충으로 10/1
        # 기사를 끌어오는 역전이 났다(2026-10-05 발주자 지적).
        extra = drop_stale(extra, origin_floor_day(since))
        if not extra:
            continue
        prior = store.cstop10_recent_clusters(lo - 2 * day)
        picked = topup(picked, extra, store, asof, want, prior=prior)
    # 창 밖으로 나가지 않는 대신, 창 안에서 상한을 풀어 마저 채운다.
    picked = fill_in_window(picked, rows, store, want,
                            prior=store.cstop10_recent_clusters(
                                since - 2 * day), since=since,
                            prior_issues=_pi)
    return picked


# ── 초안을 토픽에 실제로 올린다 ────────────────────────────────
# 발주자 지정(2026-10-05): "초안도 경전실 top10링크랑 경전실top10으로
# 올려야지. 초안 갈아끼울 때마다 갈아끼우기 전 초안은 삭제 후 갈아끼운
# 버전으로 올리는 방식으로."
#
# 그래서 초안도 실발행과 **같은 두 탭**에 올린다. 다만 머리말을 '초안'
# 으로 달아 06:50 실발행과 구분한다. 더 나은 초안이 나오면 앞서 올린
# 초안 메시지를 전부 지우고 새로 올린다. 06:50 실발행 직전에도 지운다 —
# 초안과 실발행이 나란히 남으면 어느 것이 최종인지 알 수 없다.
_DRAFT_MSG_KEY = "draft_msgs"


def _draft_msg_ids(store, pub: str) -> list:
    raw = store.get_setting(f"{_DRAFT_MSG_KEY}:{pub}") or ""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                t, m = part.split(":")
                out.append((t, int(m)))
            except ValueError:
                continue
    return out


def _save_draft_msg_ids(store, pub: str, ids: list) -> None:
    store.put_setting(f"{_DRAFT_MSG_KEY}:{pub}",
                      ",".join(f"{t}:{m}" for t, m in ids))


async def clear_draft_posts(client, store, pub: str) -> int:
    """그 발행일의 초안 메시지를 **전부 지운다.** 지운 수를 돌려준다."""
    import asyncio
    import publisher
    ids = _draft_msg_ids(store, pub)
    gone = 0
    for thread_name, mid in ids:
        try:
            if await publisher.delete_message(client, mid):
                gone += 1
        except Exception:                                   # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
    if ids:
        _save_draft_msg_ids(store, pub, [])
        print(f"[draft] 이전 초안 {gone}/{len(ids)}건 삭제")
    return gone


async def post_draft(client, store, pub: str, picked: list,
                     asof: float) -> int:
    """초안을 두 탭에 올린다. 앞서 올린 초안은 먼저 지운다."""
    import asyncio          # 모듈 최상단에 없다 — run() 도 함수 안에서 임포트한다
    import publisher
    await clear_draft_posts(client, store, pub)
    if not picked:
        return 0

    _, until, label = window(asof)
    d = datetime.fromtimestamp(asof, KST)
    stamp = datetime.now(KST).strftime("%H:%M")
    ids = []

    thread = topics.thread_id_for("cs_top10")
    if thread:
        head = (f"📝 <b>{d.month}월 {d.day}일자 {settings.cs_top10_label} 초안</b>"
                f" ({stamp} 기준 · {len(picked)}건)\n"
                f"<i>확정본은 {settings.cs_top10_time} 에 올라갑니다. "
                f"더 나은 기사가 들어오면 이 초안은 교체됩니다.</i>")
        try:
            mid = await publisher.send_raw(client, head, thread)
            if mid:
                ids.append(("cs_top10", mid))
            await asyncio.sleep(0.5)
        except Exception as exc:                            # noqa: BLE001
            print(f"[draft] 머리말 실패 — {exc}")
        msgs, preview_urls = render_all(picked, label, store)
        for i, m in enumerate(msgs):
            try:
                mid = await publisher.send_raw(
                    client, m, thread,
                    preview_url=preview_urls[i] if i < len(preview_urls) else None)
                if mid:
                    ids.append(("cs_top10", mid))
            except Exception as exc:                        # noqa: BLE001
                print(f"[draft] 본문 {i + 1} 실패 — {exc}")
            await asyncio.sleep(0.6)

    links_thread = topics.thread_id_for("cs_top10_links")
    if links_thread:
        head = (f"📝 <b>{d.month}월 {d.day}일자 {settings.cs_links_label} 초안</b>"
                f" ({stamp} 기준 · {len(picked)}건)")
        try:
            mid = await publisher.send_raw(client, head, links_thread)
            if mid:
                ids.append(("cs_top10_links", mid))
            await asyncio.sleep(0.5)
        except Exception as exc:                            # noqa: BLE001
            print(f"[draft] 링크 머리말 실패 — {exc}")
        for text, url in render_links(picked, label, store):
            try:
                mid = await publisher.send_raw(client, text, links_thread,
                                               preview_url=url)
                if mid:
                    ids.append(("cs_top10_links", mid))
            except Exception as exc:                        # noqa: BLE001
                print(f"[draft] 링크 실패 — {exc}")
            await asyncio.sleep(0.6)

    _save_draft_msg_ids(store, pub, ids)
    print(f"[draft] 초안 {len(ids)}건 게시 ({stamp})")
    return len(ids)


def health_check(store, hours: int = 48) -> dict:
    """초안을 갈아끼울 때마다 도는 **자가 점검.**

    발주자 지정(2026-10-05): "갈아끼울 때마다 수집기사나 발행기사 중복된 거,
    토픽 잘못 들어간 거 있으면 확인도 동시에 하면서 버그도 동시에 수정해."

    여기서는 **찾아서 알린다.** 지우는 것은 사람이 tools/delete_dups.py 로
    한다 — 자동 삭제는 오판했을 때 되돌릴 수 없다(실측으로 판정 기준을 세 번
    고쳤다).
    """
    import events as _ev
    import topics as _tp
    now = datetime.now(KST).timestamp()
    out = {"dup": [], "topic": [], "both": []}
    with store._conn() as c:                                # noqa: SLF001
        rows = c.execute(
            "SELECT message_id, headline, main_entities, event_type,"
            "       sent_at, primary_topic, category, mirror_ids"
            "  FROM published"
            " WHERE message_id IS NOT NULL AND headline IS NOT NULL"
            "   AND sent_at > ?"
            "   AND (cs_top10_date IS NULL OR cs_top10_date != 'DELETED-DUP')",
            (now - hours * 3600,)).fetchall()

    items = []
    for mid, h, e, et, sa, pt, cat, mir in rows:
        d = {"main_entities": [x.strip() for x in (e or "").split(",") if x.strip()],
             "event_type": et, "title_ko": h}
        items.append(dict(mid=mid, h=h, d=d, sa=sa, pt=pt, cat=cat,
                          mir=(mir or "").strip(),
                          cov=_ev.coverage(d, h)))

    # 1) 같은 사건이 두 번 이상 발행됐나 (직접 쌍, 전이 없음)
    for a in items:
        for b in items:
            if a is b or a["cov"] >= b["cov"]:
                continue
            if abs(float(a["sa"]) - float(b["sa"])) > 36 * 3600:
                continue
            if _ev.same_event(_ev.parts(a["d"], a["h"]), a["h"],
                              _ev.parts(b["d"], b["h"]), b["h"]):
                out["dup"].append((a["mid"], a["h"], b["mid"], b["h"]))
                break

    # 2) primary_topic 과 실제 게시 탭(category)이 어긋났나
    for r in items:
        if r["pt"] and r["cat"] and r["pt"] != r["cat"]:
            out["topic"].append((r["mid"], r["pt"], r["cat"], r["h"]))

    # 3) 한 기사가 두 탭에 (미러 잔재)
    for r in items:
        if r["mir"]:
            out["both"].append((r["mid"], r["mir"], r["h"]))

    if out["dup"]:
        print(f"[점검] ⚠️ 같은 사건 중복 발행 {len(out['dup'])}건")
        for mid, h, wm, wh in out["dup"][:5]:
            print(f"   {mid} {h[:38]}")
            print(f"     ↔ {wm} {wh[:38]}")
    if out["topic"]:
        print(f"[점검] ⚠️ 토픽 어긋남 {len(out['topic'])}건")
        for mid, pt, cat, h in out["topic"][:5]:
            print(f"   {mid} {pt} → {cat}  {h[:36]}")
    if out["both"]:
        print(f"[점검] ⚠️ 한 기사가 두 탭에 {len(out['both'])}건")
    if not any(out.values()):
        print("[점검] 중복·토픽 어긋남 없음")
    return out


async def build_draft(store, asof: float, dry_run: bool = False,
                      client=None) -> dict:
    """다음 발행분 초안을 만들고, **기존 초안보다 나을 때만** 갈아치운다.

    2026-10-05 사용자 지정: 전날 18:00·22:00·당일 04:00 에 세 번 만들고,
    뒤에 만든 것이 더 기준에 맞으면 앞서 만든 것을 지우고 대체한다.
    """
    import time as _t
    pub = datetime.fromtimestamp(asof, KST).strftime("%Y-%m-%d")
    # 갈아끼울 때마다 중복·토픽 어긋남을 함께 본다(2026-10-05 발주자 지정).
    try:
        health_check(store)
    except Exception:                                       # noqa: BLE001
        import traceback
        print("[점검] 실패 —")
        traceback.print_exc()
    picked = select_for(store, asof)
    q = quality_of(picked)
    prev = store.get_top10_draft(pub)
    n = len(picked)
    avg = (q - n * 1000) if n else 0
    if prev and prev[1] >= q:
        pn = int(prev[1] // 1000)
        print(f"[draft] {pub} 유지 — 기존 {pn}건(품질 {prev[1]:.1f})이 "
              f"이번 {n}건(품질 {q:.1f})보다 낫거나 같다")
        return {"replaced": False, "count": pn, "quality": prev[1]}
    if not dry_run:
        store.save_top10_draft(pub, [r[K_KEY] for _, r in picked], q, _t.time())
    was = f"{int(prev[1]//1000)}건" if prev else "없음"
    print(f"[draft] {pub} 갱신 — {was} → {n}건 (적합도 평균 {avg:.1f})")
    for i, (_, r) in enumerate(picked, 1):
        print(f"   {i:>2}. {_posted_label(r)[:16]} {(r[K_HEAD] or '')[:44]}")
    # **초안을 탭에 올린다.** 앞서 올린 초안은 post_draft 가 먼저 지운다.
    if client is not None and not dry_run:
        try:
            await post_draft(client, store, pub, picked, asof)
        except Exception:                                   # noqa: BLE001
            # **사유를 통째로 남긴다.** 한 줄만 찍었더니 asyncio 임포트
            # 누락(NameError)이 조용히 묻혀, 초안이 왜 안 올라가는지
            # 한참 못 찾았다(2026-10-05).
            import traceback
            print("[draft] 게시 실패 —")
            traceback.print_exc()
    return {"replaced": True, "count": n, "quality": q}


def draft_due(store, now: float | None = None) -> tuple[bool, str]:
    """지금이 초안을 만들 시각인가. (해야하나, 이유)

    설정한 시각(기본 18:00·22:00·04:00)을 **지난 지 1시간 안**이면 만든다.
    상시 루프가 20분마다 물어보므로 시각마다 한 번은 반드시 걸린다.
    """
    now = now or datetime.now(KST).timestamp()
    t = datetime.fromtimestamp(now, KST)

    # 슬롯을 **분 단위로 정렬**해 두고 늦은 것부터 본다. 창은 '다음 슬롯까지'
    # 와 1시간 중 **짧은 쪽**이다. 예전엔 무조건 1시간이라, 18:50 과 19:00
    # 처럼 가까운 슬롯이 겹쳐 19:00 회차가 18:50 에 먹혔다(2026-10-05).
    slots = list(settings.cs_top10_draft_times)
    # 하루짜리 임시 회차를 날짜가 맞을 때만 더한다. config 주석 참고.
    today = t.strftime("%Y-%m-%d")
    if today == settings.cs_top10_draft_extra_date:
        slots += settings.cs_top10_draft_extra_times
    if today == getattr(settings, "cs_top10_draft_extra_date2", ""):
        slots += settings.cs_top10_draft_extra_times2

    # **앞당겨 시작한다.** 지정한 시각은 "그때 교체를 시작하라"가 아니라
    # "그때는 이미 교체가 끝나 있어야 한다"는 뜻이다(2026-10-05 발주자 지정).
    lead = max(0, int(getattr(settings, "cs_top10_draft_lead_min", 0)))
    marks = []
    for hhmm in slots:
        hh, _, mm = hhmm.partition(":")
        try:
            at = (int(hh) * 60 + int(mm) - lead) % (24 * 60)
            marks.append((at, hhmm))
        except ValueError:
            continue
    marks.sort()
    for i in range(len(marks) - 1, -1, -1):
        minute, hhmm = marks[i]
        nxt = marks[i + 1][0] if i + 1 < len(marks) else minute + 60
        span = min(3600, max(60, (nxt - minute) * 60))
        hh, mm = divmod(minute, 60)
        mark = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
        gap = (t - mark).total_seconds()
        if 0 <= gap < span:
            # **그 회차를 이미 돌았으면 다시 돌지 않는다.** 상시 루프가 20분·
            # 긴급 레인이 5분마다 물어봐서, 한 시간 창에 최대 12번 재생성됐다.
            # 사용자가 말한 "총 3번"과 어긋나고 모델 호출만 낭비된다.
            slot = f"{t:%Y-%m-%d}/{hhmm}"
            if store is not None and store.get_setting("draft_slot") == slot:
                return False, f"{hhmm} 회차는 이미 돌았다"
            if store is not None:
                store.put_setting("draft_slot", slot)
            return True, (f"{hhmm} 회차 — {lead}분 앞당겨 시작"
                          if lead else f"{hhmm} 초안 생성 시각")

    # **자가복구.** 위 슬롯은 "시각을 지난 지 1시간 안" 에만 걸린다. 그 한 시간
    # 동안 루프가 죽어 있거나 cron 이 발화하지 않으면 그 회차는 영영 날아간다
    # (2026-10-05 실측: 16:00 슬롯이 통째로 날아갔다. 가동 중인 런이 초안을
    # 모르는 옛 코드였고, 새로 추가한 cron 은 아직 발화하지 않았다).
    #
    # 발행 시각이 가까운데 쓸 만한 초안이 없으면 슬롯과 무관하게 만든다.
    # 06:50 에 쓰이는 하한(daily_brief_count 의 80%)과 같은 기준으로 본다 —
    # 그보다 적은 초안은 어차피 버려지므로 없는 것과 같다.
    if store is not None:
        pub = datetime.fromtimestamp(next_publish_ts(now), KST).strftime("%Y-%m-%d")
        d = store.get_top10_draft(pub)
        need = max(1, int(settings.daily_brief_count * 0.8))
        if not d or not d[0] or len(d[0]) < need:
            # 한 시간에 한 번으로 묶는다. 20분마다 다시 뽑으면 모델 호출만 탄다.
            stamp = f"{pub}/{t:%H}"
            if store.get_setting("draft_heal") != stamp:
                store.put_setting("draft_heal", stamp)
                have = len(d[0]) if d and d[0] else 0
                return True, f"초안 {have}건뿐({need}건 필요) — 슬롯 밖 보충 생성"
    return False, "초안 생성 시각 아님"


def next_publish_ts(now: float | None = None) -> float:
    """지금 기준으로 **다음 06:50**. 초안은 그 시각 기준으로 만든다."""
    now = now or datetime.now(KST).timestamp()
    t = datetime.fromtimestamp(now, KST)
    hh, _, mm = settings.cs_top10_time.partition(":")
    nxt = t.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if nxt <= t:
        nxt += timedelta(days=1)
    return nxt.timestamp()


async def run(client, store, dry_run: bool | None = None,
              asof: float | None = None) -> int | None:
    """asof 를 주면 **그 시각 기준**으로 뽑는다(과거분 소급 생성용).

    봇이 멈춰 있던 날의 Top10 을 뒤늦게 만들 때 쓴다. 창·라벨·중복 판정이
    전부 그 시각을 기준으로 돌아가므로, 그날 아침에 돌았을 때와 같은 결과가
    나온다. 평소 운영에서는 쓰지 않는다(asof=None → 지금).
    """
    import asyncio
    import publisher
    dry = settings.dry_run if dry_run is None else dry_run
    since, until, label = window(asof)
    by_origin = asof is not None
    want = settings.daily_brief_count

    def _pick(since_ts):
        # 소급 생성(asof)은 원문 발행일로 자른다 — 그날 봇이 멈춰 있었으면
        # sent_at 기준 후보가 0건이기 때문이다. store.cstop10_candidates 주석 참고.
        rows = store.cstop10_candidates(since_ts, until,
                                        settings.discard_threshold,
                                        by_origin=by_origin)
        rows = drop_stale(rows, origin_floor_day(since))
        rows = drop_before_window(rows, since)
        _pi = prior_issue_themes(store, since)
        if _pi:
            print(f"[cstop10] 직전 판 이슈 제외 대상: {', '.join(sorted(_pi))}")
        return rows, select(rows, store=store, now=asof, prior_issues=_pi)

    # **확정된 초안이 있으면 그걸 그대로 낸다.**
    # 전날 18:00·22:00·당일 04:00 에 미리 만들어 검증해 둔 것이다. 06:50 에
    # 즉석에서 다시 뽑으면 그 사이 들어온 기사로 결과가 달라져, 미리 확인한
    # 의미가 없어진다(2026-10-05 사용자 지정).
    _pub = datetime.fromtimestamp(asof or until, KST).strftime("%Y-%m-%d")
    _draft = store.get_top10_draft(_pub)
    if _draft and _draft[0]:
        _rows = store.rows_by_keys(_draft[0])
        _min_ok = max(1, int(settings.daily_brief_count * 0.8))
        if len(_rows) < _min_ok:
            # **모자란 초안은 쓰지 않는다.** 1건짜리 초안이 그대로 나가는 것이
            # 사용자가 막으려던 바로 그 사고다(2026-10-05 감사).
            print(f"[cstop10] ⚠️ 초안이 {len(_rows)}건뿐({_min_ok}건 미만) — "
                  f"버리고 새로 뽑는다")
            _draft = None
        elif len(_rows) == len(_draft[0]):
            _built = datetime.fromtimestamp(_draft[2], KST)
            print(f"[cstop10] 확정 초안 사용 — {len(_rows)}건 "
                  f"({_built:%m-%d %H:%M} 생성)")
            picked = [(0, r) for r in _rows]
            rows = _rows
        else:
            print(f"[cstop10] 초안 {len(_draft[0])}건 중 {len(_rows)}건만 복원 — "
                  f"새로 뽑는다")
            _draft = None
    if not _draft or not _draft[0]:
        rows, picked = _pick(since)

    # 10건이 안 차면 **전날 기사에서 부족분만 채운다** (2026-10-05 사용자 지정).
    #
    # 창을 넓혀 통째로 다시 뽑던 방식은 버렸다. 후보가 늘면 범주 상한·최소배정
    # 경쟁이 달라져 오히려 줄었다(실측: 24h 9건 → 60h 6건). 오늘 뽑은 것은
    # 그대로 두고 모자란 수만큼만 하루씩 앞으로 가며 더한다.
    #
    # 기준은 그대로다 — 같은 사건은 절대 두 번 싣지 않고, 범주·엔티티 상한과
    # 유료 교체·본문 확보 검사를 전부 통과한 것만 더한다. topup() 주석 참고.
    _used_draft = bool(_draft and _draft[0] and picked)

    # **창 안에 남은 것을 먼저 다 쓴다** (2026-10-05 지적).
    #
    # 범주 상한에 막혀 창 안 기사가 7건이나 남았는데 전날로 넘어가 10/1~10/2
    # 기사를 가져왔다. 상한은 "한 범주가 독식하지 않게" 하려는 것이지 "창 밖에서
    # 가져오라"는 뜻이 아니다. 10 건이 안 차면 상한을 풀어서라도 **그날 창 안을
    # 먼저 비운다.** 그래도 모자랄 때만 전날로 간다.
    if len(picked) < want and not _used_draft:
        print(f"[cstop10] {len(picked)}건 — 범주 상한을 풀고 창 안에서 먼저 채운다")
        # **기게재 검사를 빠뜨리면 안 된다.** 예전엔 published_clusters_before 만
        # 넘겨서, 어제 Top10 에 실린 사건의 다른 기사가 오늘 다시 들어왔다
        # (2026-10-05 감사). 전날 보충 경로와 같은 두 목록을 넘긴다.
        #
        # 범주 상한만 푼다. **적합도 하한은 그대로 둔다** — 사용자 지시는
        # "범주 상한을 풀어서라도"였지 품질 하한을 풀라는 뜻이 아니었다.
        # **일반 탭에 나간 것은 제외 근거가 아니다.** Top10 은 원래 "그날
        # 팀에 나간 것 중의 Top10" 이다. 일반탭 발행을 기게재로 치니 창 안
        # 후보가 거의 전부 빠지고, 보충이 며칠 전 기사를 끌어왔다
        # (2026-10-05 발주자 지적: 10/6 판에 10/1·10/2 기사가 섞였다).
        # 막아야 하는 것은 **같은 사건이 Top10 에 반복되는 것**이고,
        # 그건 cstop10_recent_clusters(이전 Top10)가 담당한다.
        _prior = store.cstop10_recent_clusters(since - 2 * 24 * 3600)
        picked = topup(picked, rows, store, asof or datetime.now(KST).timestamp(),
                       want, prior=_prior, ignore_cat_cap=True)

    day = 24 * 3600
    back = 0
    while len(picked) < want and back < settings.cs_top10_fill_days:
        back += 1
        lo, hi = since - day * back, since - day * (back - 1)
        extra = store.cstop10_candidates(lo, hi, settings.discard_threshold,
                                         by_origin=by_origin)
        if not extra:
            continue
        print(f"[cstop10] {len(picked)}건 — {back}일 전 기사 {len(extra)}건에서 보충한다")
        # 보충 대상도 '이미 나간 사건' 검사를 받아야 한다. 기준 시점은
        # **그 기사들이 속한 날의 시작** 이다 — 그보다 전에 나간 것만 기발행이다.
        prior = store.cstop10_recent_clusters(lo - 7 * 24 * 3600)
        picked = topup(picked, extra, store,
                       asof or datetime.now(KST).timestamp(), want, prior=prior)

    # ── 최후 보충 ──────────────────────────────────────────────
    # 여기까지 와서도 모자라면 **더 거슬러 간다.** "무조건 10건" 이 수칙이고,
    # 2건짜리 Top10 을 내는 것이 오래된 기사를 한둘 섞는 것보다 나쁘다
    # (2026-10-05 발주자 지정, 10/6 발행분이 2건에서 막힌 뒤 추가).
    # 기준은 그대로 간다 — topup 안의 같은 사건 접기·기게재 제외·범주 상한·
    # 유료 차단·본문 확보 검사가 전부 그대로 걸린다.
    while len(picked) < want and back < settings.cs_top10_max_fill_days:
        back += 1
        lo, hi = since - day * back, since - day * (back - 1)
        extra = store.cstop10_candidates(lo, hi, settings.discard_threshold,
                                         by_origin=by_origin)
        # **하한은 창 기준으로 고정한다.** lo 기준으로 다시 계산했더니
        # 뒤로 갈수록 느슨해져, 창 안 10/3 기사는 버리면서 보충으로 10/1
        # 기사를 끌어오는 역전이 났다(2026-10-05 발주자 지적).
        extra = drop_stale(extra, origin_floor_day(since))
        if not extra:
            continue
        prior = store.cstop10_recent_clusters(lo - 2 * day)
        before = len(picked)
        picked = topup(picked, extra, store,
                       asof or datetime.now(KST).timestamp(), want, prior=prior)
        if len(picked) > before:
            print(f"[cstop10] 최후 보충 — {back}일 전에서 {len(picked) - before}건 "
                  f"(총 {len(picked)}건)")

    # 창 밖으로 나가지 않는 대신, 창 안에서 상한을 풀어 마저 채운다.
    picked = fill_in_window(
        picked, rows, store, want,
        prior=store.cstop10_recent_clusters(since - 2 * 24 * 3600),
        since=since, prior_issues=prior_issue_themes(store, since))

    if len(picked) < want:
        print(f"[cstop10] ⚠️ 경고 — {settings.cs_top10_max_fill_days}일 전까지 "
              f"뒤졌으나 {len(picked)}/{want}건. 기준을 지키면 서로 다른 사건이 "
              f"이만큼뿐이다. 수집량·중복 제외 범위를 확인하라.")

    span = (f"{datetime.fromtimestamp(since, KST):%m-%d %H:%M}"
            f" ~ {datetime.fromtimestamp(until, KST):%m-%d %H:%M}")
    print(f"[cstop10] 구간 {span} · 후보 {len(rows)}건 → 선정 {len(picked)}건")
    if not picked:
        # 0건이면 낼 것이 없다. 다만 **조용히 넘어가지 않는다** — due() 가
        # 계속 참이라 긴급 레인이 5분마다 종일 재시도하는데, 아무도 그걸
        # 모르는 것이 가장 나쁘다(2026-10-05 감사).
        print("[cstop10] ⚠️ 후보 0건 — 게시할 것이 없다. "
              "수집이 멈췄거나 중복 제외가 과한지 확인하라.")
        return None

    msgs, preview_urls = render_all(picked, label, store)
    print(f"[cstop10] 메시지 {len(msgs)}건 "
          f"(보이는 길이 {[visible_len(m) for m in msgs]})")

    if dry:
        for m in msgs:
            print("─" * 60)
            print(m)
        print("─" * 60)
        print("[cstop10] 🔗 링크용 탭에 나갈 것 (기사 1건당 1메시지):")
        for text, _ in render_links(picked, label, store):
            print("  · " + text.replace("\n", " / "))
        print("[cstop10] DRY_RUN — 발행하지 않았습니다")
        return None

    # **초안을 먼저 지운다.** 초안과 확정본이 나란히 남으면 어느 것이
    # 최종인지 알 수 없다(2026-10-05 발주자 지정).
    try:
        await clear_draft_posts(client, store,
                                datetime.fromtimestamp(until, KST).strftime("%Y-%m-%d"))
    except Exception as exc:                                # noqa: BLE001
        print(f"[cstop10] 초안 정리 실패 — {exc}")

    thread = topics.thread_id_for("cs_top10")
    # 📌 A팀 Top10 도 링크용 탭처럼 머리말을 먼저 띄운다
    # (2026-10-05 사용자 지정). 본문이 2개로 나뉘는 날이 많아, 어디서
    # 그날 묶음이 시작하는지 날짜로 알려 줘야 한다.
    _d = datetime.fromtimestamp(until, KST)
    _header_id = None
    try:
        hid = await publisher.send_raw(
            client,
            f"📌 <b>{_d.month}월 {_d.day}일자 {settings.cs_top10_label} 발행 시작합니다</b>",
            thread)
        # **여기서 발행 기록을 남기지 않는다.** 머리말만 나가고 본문 전송이
        # 실패하면 due() 가 "오늘 이미 발행함"을 돌려주어 재시도가 영영 막힌다
        # (2026-10-05 감사). 기록은 본문이 실제로 나간 뒤에 남긴다.
        _header_id = hid
        await asyncio.sleep(0.5)
    except Exception as exc:                              # noqa: BLE001
        print(f"[cstop10] 머리말 실패 — {exc}")
    # **본문이 전부 나간 뒤에 기록한다.** 예전엔 한 건 보낼 때마다 기록해서,
    # 2개 중 1개만 나가고 실패하면 agg_ran_on 이 참이 되고 due() 가 "오늘
    # 이미 발행함"을 돌려줘 **반쪽 발행이 영구 고정**됐다(2026-10-05 감사).
    first = None
    sent_ids = []
    for i, m in enumerate(msgs):
        try:
            mid = await publisher.send_raw(
                client, m, thread,
                preview_url=preview_urls[i] if i < len(preview_urls) else None)
        except Exception as exc:                            # noqa: BLE001
            print(f"[cstop10] ⚠️ 본문 {i + 1}/{len(msgs)} 전송 실패 — {exc}")
            mid = None
        if mid is None:
            # 하나라도 못 보내면 기록을 남기지 않는다 — 다음 회차가 재시도한다.
            for _ts, _id in sent_ids:
                try:
                    await publisher.delete_message(client, _id)
                except Exception:                           # noqa: BLE001
                    pass
            if _header_id:
                try:
                    await publisher.delete_message(client, _header_id)
                except Exception:                           # noqa: BLE001
                    pass
            print("[cstop10] ⚠️ 반쪽 발행을 거둬들였다 — 다음 회차에 재시도")
            return None
        sent_ids.append((until + i, mid))
        first = first or mid
        if i < len(msgs) - 1:
            await asyncio.sleep(0.6)
    for _ts, _id in sent_ids:
        store.record_agg_message("cs_top10", _ts, _id)
    if _header_id:
        store.record_agg_message("cs_top10", until - 1, _header_id)
    # 🔗 top10(링크용) — 같은 10건의 원문 주소를 한 건씩 따로 보낸다.
    # 본문 발행이 끝난 뒤에 한다. 이쪽이 실패해도 Top10 은 이미 나가 있어야 한다.
    links_thread = topics.thread_id_for("cs_top10_links")
    if links_thread:
        # 머리말을 먼저 띄운다 (2026-10-05 사용자 지정).
        # 링크만 10건이 연달아 올라오면 어느 날짜 묶음인지, 어디서 시작하는지
        # 알 수 없다. 날짜를 박아 묶음의 시작을 알린다.
        _d = datetime.fromtimestamp(until, KST)
        try:
            await publisher.send_raw(
                client, f"📌 <b>{_d.month}월 {_d.day}일자 {settings.cs_links_label} 발행 시작합니다</b>",
                links_thread)
            await asyncio.sleep(0.5)
        except Exception as exc:                          # noqa: BLE001
            print(f"[cstop10] 링크용 머리말 실패 — {exc}")
        sent = 0
        for i, (text, url) in enumerate(render_links(picked, label, store)):
            try:
                mid = await publisher.send_raw(client, text, links_thread,
                                               preview_url=url)
            except Exception as exc:                      # noqa: BLE001
                print(f"[cstop10] 링크 {i + 1}번 발행 실패 — {exc}")
                continue
            # window_end 가 (scope, window_end) 유일키다. 본문 쪽과 겹치지 않게 비켜 둔다.
            store.record_agg_message("cs_top10_links", until + 100 + i, mid)
            sent += 1
            await asyncio.sleep(0.6)   # 텔레그램 초당 제한을 피한다
        print(f"[cstop10] 🔗 링크용 {sent}건 발행")
    else:
        print("[cstop10] 🔗 링크용 탭 thread_id 없음 — 건너뜀 "
              "(scripts/setup_topics.py --create 로 탭을 만들어라)")

    # 실린 기사를 표시해 둔다 — 다음 회차에서 다시 뽑히지 않게.
    store.mark_cstop10([r[K_KEY] for _, r in picked],
                       datetime.fromtimestamp(until, KST).strftime("%Y-%m-%d"))
    if any(csfit.is_pr(r[K_HEAD] or "") for _, r in picked):
        store.record_pr_pick(until, first)
        print("[cstop10] 홍보성 1건 게재 — 이틀간 보류")
    return first
