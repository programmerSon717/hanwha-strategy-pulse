#!/usr/bin/env python
"""Strategy Pulse 회귀 검사 (스펙 §38 Test Case, §39 Negative Test).

    venv/bin/python tests/bsp_cases.py            # 오프라인 검사만 (모델 호출 없음)
    venv/bin/python tests/bsp_cases.py --llm      # 실제 모델로 A~H 판정까지

오프라인 검사는 API 키 없이 돌고 한도를 쓰지 않는다. CI 와 배포 전에 이걸 돌린다.
--llm 은 8건을 호출하므로 한도를 먹는다. 프롬프트를 고쳤을 때만 쓴다.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events
import prefilter
import prompts_bsp
import publisher
import topics
from config import PRIMARY_TOPIC_IDS, canonical_entity, settings, source_weight
from models import NewsItem

PASS, FAIL = "  ✅", "  ❌"
_fails = 0


def check(label: str, got, want):
    global _fails
    ok = got == want
    if not ok:
        _fails += 1
    print(f"{PASS if ok else FAIL} {label}")
    if not ok:
        print(f"       기대={want!r}  실제={got!r}")


def item(title: str, source: str = "연합뉴스") -> NewsItem:
    return NewsItem(source=source, unique_id=title, title=title, url="https://x/1")


# ══════════════════════════════════════════════════════════
def test_negative():
    """§39 — 이 유형이 Feed 를 오염시키지 않아야 한다."""
    print("\n[§39] Negative Test — 사전필터가 걸러야 하는 것")
    for title in [
        "삼성생명 새 건강보험 상품 출시 기념 이벤트",   # 보험상품 이벤트
        "비트코인 가격 5% 상승",                        # Bitcoin 가격
        "코스피 2600선 마감… 외국인 순매수",            # 일반 주식시장
        "원/달러 환율 상승 마감",                       # 단순 환율
        "한화이글스 새 감독 선임",                      # 연예/스포츠
        "한화생명, 사회공헌 기부금 전달",               # PR
        "이더리움 시세 전망 차트",                      # 크립토 시황
    ]:
        check(f"버림: {title[:34]}", prefilter.reject_reason(item(title)) is not None, True)

    print("\n[§39] 전략 신호가 있으면 살아남아야 한다")
    for title in [
        "한화생명, 미국 증권사 인수 추진",
        "금융위, 보험사 자본규제 개편",
        "교보생명 경영권 분쟁, SBI 지분 확대",
        "삼성화재 주가 급등, 자사주 소각 발표",         # 시황어 + 전략신호
        "코스피 상장 보험사 지분 매각 추진",
        "토스, 보험 라이선스 예비인가 신청",
        "카카오페이, 해외 결제사업 확대",
    ]:
        check(f"통과: {title[:34]}", prefilter.reject_reason(item(title)), None)


def test_entity_alias():
    """§30 — 같은 회사를 다른 이름으로 써도 인식해야 한다."""
    print("\n[§30] Entity Alias")
    for alias, want in [
        ("한화생명보험", "한화생명"), ("Hanwha Life", "한화생명"),
        ("네이버파이낸셜", "네이버"), ("NAVER Financial", "네이버"),
        ("금감원", "금융감독원"), ("공정위", "공정거래위원회"),
        ("카카오뱅크", "카카오"), ("캐롯", "캐롯손해보험"),
    ]:
        check(f"{alias} → {want}", canonical_entity(alias), want)
    check("모르는 이름은 None", canonical_entity("없는회사"), None)


def test_topic_routing():
    """§5·§7 — 토픽 키와 보정."""
    print("\n[§5] 토픽 11개")
    # 2026-10-01: 📌 A팀 Top10 추가.
    # 2026-10-05: 🔗 top10(링크용) 추가.
    # 둘 다 Primary 7개는 그대로다 — 집계 토픽이라 모델이 지정하지 않는다.
    check("토픽 수", len(topics.CATEGORIES), 11)
    check("Primary 지정 가능 토픽 7개", len(PRIMARY_TOPIC_IDS), 7)
    check("집계 토픽", topics.AGGREGATION,
          {"key_issues", "daily_brief", "cs_top10", "cs_top10_links"})
    for tid, name in [
        ("hanwha_group", "🏢 한화그룹"), ("ma_governance", "🤝 M&A · 지배구조"),
        ("insurance_finance", "🏦 보험 · 금융"), ("regulation_policy", "⚖️ 규제 · 정책"),
        ("competitors_bigtech", "🔎 경쟁사 · Big Tech"),
        ("digital_newbiz", "💡 디지털 · 신사업"), ("global_finance", "🌐 Global"),
        ("key_issues", "🚨 주요이슈"), ("daily_brief", "☀️ Morning Brief"),
        ("cs_top10", "📌 A팀 Top10"),
    ]:
        check(f"{tid} 이름", topics.CATEGORIES[tid], name)

    print("\n[§7] 라우팅 값 보정")
    check("표기 흔들림", topics.normalize_topic("Hanwha Group"), "hanwha_group")
    check("한글 표기", topics.normalize_topic("규제"), "regulation_policy")
    check("집계토픽을 primary 로 주면 보정",
          topics.normalize_topic("key_issues") in PRIMARY_TOPIC_IDS, True)
    check("알 수 없는 값도 유효 토픽으로",
          topics.normalize_topic("엉뚱한값") in PRIMARY_TOPIC_IDS, True)


def test_event_dedup():
    """§21 — 같은 사건은 하나로."""
    print("\n[§21] Event Deduplication")
    a = {"main_entities": ["교보생명"], "event_type": "stake_change", "strategic_score": 88}
    b = {"main_entities": ["Kyobo Life"], "event_type": "acquisition", "strategic_score": 80}
    check("별칭·유사 action 은 같은 fingerprint",
          events.fingerprint(a) == events.fingerprint(b), True)

    c = {"main_entities": ["금융위원회"], "event_type": "regulation", "strategic_score": 90}
    check("다른 사건은 다른 fingerprint",
          events.fingerprint(a) == events.fingerprint(c), False)

    idx = events.EventIndex()
    t1 = "교보생명 경영권 분쟁, SBI 지분 확대"
    t2 = "[단독] SBI, 교보생명 지분 추가 확대"
    fp = idx.add(a, t1, "연합뉴스", 1200)
    check("두 번째 기사가 같은 사건으로 잡힌다", idx.match(b, t2), fp)

    idx.add(b, t2, "더벨", 5000)
    check("대표기사는 매체등급 높은 쪽(더벨)", idx._by_fp[fp]["source"], "더벨")

    check("Material Update 인식", events.looks_material("SBI-교보생명 본계약 체결", {}), True)
    check("일반 후속보도는 아님", events.looks_material("교보생명 관련 업계 반응", {}), False)

    # ── 2026-10-01 회귀: 애큐온캐피탈 3중 발행 사고 ──────────────
    # 모델이 같은 사건의 main_entities 를 기사마다 다르게 적어 fingerprint 가
    # 셋으로 갈렸고, 제목 유사도(0.27~0.38)도 TITLE_SIMILARITY=0.62 에 못 미쳐
    # 세 건이 모두 발행됐다. 주요이슈 하루 상한 6건 중 3건을 한 사건이 먹었다.
    real = [
        ("한화생명, 애큐온캐피탈 지분 50.54% 인수 의결",
         ["한화생명", "애큐온캐피탈"], "acquisition"),
        ("한화생명, 애큐온캐피탈 인수 확정",
         ["한화생명"], "acquisition"),
        ("한화생명, 애큐온캐피탈 인수 통해 여신금융 진출 및 종합금융그룹 도약",
         ["한화생명", "한화저축은행"], "acquisition"),
    ]
    idx2 = events.EventIndex()
    cids = []
    for title, ents, et in real:
        d = {"main_entities": ents, "event_type": et, "strategic_score": 95}
        cids.append(idx2.add(d, title, "보험저널", 2000))
    check("엔티티가 달라도 같은 딜이면 한 클러스터", len(set(cids)), 1)

    # 같은 회사라도 action 이 다르면 섞이면 안 된다.
    other = {"main_entities": ["한화생명", "한화생명금융서비스"],
             "event_type": "product", "strategic_score": 75}
    check("같은 회사라도 다른 사건은 안 섞임",
          idx2.match(other, "700종신보험 판매 중단에 따른 GA업계의 대체 상품 전략 가동"),
          None)

    # 엔티티는 겹치고 action 도 같지만 제목이 전혀 다른 별개 딜.
    far = {"main_entities": ["한화생명", "교보생명"],
           "event_type": "acquisition", "strategic_score": 80}
    check("엔티티만 겹치는 무관한 딜은 안 섞임",
          idx2.match(far, "교보생명 지분 매각 협상 결렬"), None)


def test_cstop10():
    # 합성 데이터로 검사한다. 실제 공유 이력(groundtruth)이 끼어들면
    # 가짜 제목이 '기공유'로 걸려 결과가 달라진다. 비워 두고 본다.
    import shared as _sh
    _sh._cache = []
    # 합성 URL(http://x)은 실제로 받아올 수 없다. 전문 확보 검사를 우회해
    # 선정 로직만 본다. 유료기사 대체 자체는 test_iv_paywall 에서 검사한다.
    import cstop10 as _cs
    _cs._ORIG_FETCHABLE = getattr(_cs, "_ORIG_FETCHABLE", _cs._fetchable)
    _cs._fetchable = lambda group, store: max(group, key=lambda x: x[0])
    """📌 A팀 Top10 — 적합도·홍보성 판정 (2026-10-01)."""
    import csfit, cstop10, time
    print("\n[Top10] A팀 적합도")

    # 홍보성 판정. **여러 단어 패턴이 핵심 회귀 지점이다** — 예전에 re.X 가
    # 패턴 안 공백을 지워 "조기 지급"이 "조기지급"이 되는 바람에 전부 샜다.
    check("헌정식 = 홍보성", csfit.is_pr("한화손보, 최고령 설계사 헌정식"), True)
    check("두 단어 패턴(조기 지급)",
          csfit.is_pr("한화그룹, 협력사 대금 1850억 조기 지급"), True)
    check("업무협약 = 홍보성",
          csfit.is_pr("한화큐셀·포스코, 영농형태양광 업무협약"), True)
    check("딜이 섞이면 홍보성 아님",
          csfit.is_pr("한화생명, 애큐온캐피탈 지분 50.54% 인수 의결"), False)
    check("자본확충도 홍보성 아님",
          csfit.is_pr("한화투자증권, 신종자본증권으로 9000억 자본확충"), False)

    # 적합도 — 한화가 가장 큰 가중치
    hanwha = csfit.score("한화생명, 애큐온캐피탈 인수 의결", "한화생명")[0]
    other = csfit.score("핀다, 초대 CPO 선임", "핀다")[0]
    check("한화 기사가 더 높다", hanwha > other, True)

    # 주 범주가 결정한다 — 보조 범주를 더해 역전시키면 안 된다.
    # (2026-10-02: 단순 합산이던 때 GA·보험 기사가 네 범주를 먹어 100점이 되고
    #  글로벌 기사는 54점에 그쳐, Top10 분포가 실측과 119%p 어긋났다.)
    import csfit as _cf
    check("보조 범주 반영률이 1 미만", _cf.SECONDARY_WEIGHT < 1.0, True)
    multi = _cf.score("보험사 GA 채널 지급여력 규제 심사", "")[0]      # 네 범주
    single = _cf.score("한화생명 베트남 법인 설립", "한화생명")[0]        # 한화(40)
    check("범주 많다고 한화를 못 넘는다", single > multi, True)

    # 홍보성 쿼터 — 이틀에 한 건
    def row(k, head, ent, sc, text=""):
        return (k, head, "http://x", "hanwha_group", "", sc, 0, None, ent,
                "요약", "왜", time.time(), "other", time.time(), text)
    rows = [row("a", "한화손보, 최고령 설계사 헌정식", "한화손해보험", 60),
            row("b", "한화큐셀, 영농형태양광 업무협약", "한화큐셀", 55),
            row("c", "한화생명, 애큐온캐피탈 인수 의결", "한화생명", 92)]
    now = time.time()

    class S:
        def __init__(self, last): self._l = last
        def last_pr_pick(self): return self._l
        def cstop10_recent_clusters(self, since): return []
        # 2026-10-05: select() 가 '창 시작 전에 일반 탭으로 나간 사건'도 본다.
        def published_clusters_before(self, before, since=None): return []
        # 2026-10-05: select() 가 '다른 날짜 Top10 에 쓰인 기사'도 걸러낸다.
        def top10_article_keys(self, exclude_date=""): return set()

    picked = cstop10.select(rows, count=10, store=S(None), now=now)
    npr = sum(1 for _, r in picked if csfit.is_pr(r[1]))
    check("홍보성은 한 건만", npr, 1)

    picked = cstop10.select(rows, count=10, store=S(now - 86400), now=now)
    npr = sum(1 for _, r in picked if csfit.is_pr(r[1]))
    check("하루 전 실었으면 보류", npr, 0)

    picked = cstop10.select(rows, count=10, store=S(now - 3 * 86400), now=now)
    npr = sum(1 for _, r in picked if csfit.is_pr(r[1]))
    check("사흘 전이면 다시 허용", npr, 1)

    # 홍보성은 맨 아래
    picked = cstop10.select(rows, count=10, store=S(None), now=now)
    check("홍보성은 맨 아래", csfit.is_pr(picked[-1][1][1]), True)

    # 발행 원문을 **그대로** 쓴다 — 재조립하지 않는다.
    from config import settings
    orig = (f"<b>{settings.bot_name}</b>\n\n🏢 <b>제목</b>\n\n"
            "✅ <b>핵심</b>\n신용등급이 &#x27;긍정적 검토&#x27; 대상으로 상향\n\n"
            "📂 <b>주요 내용</b>\n"
            "<blockquote>• 불릿1\n• 불릿2\n• 불릿3\n• 불릿4\n• 불릿5</blockquote>\n\n"
            "💡 <b>Why it matters</b>\n왜 중요한가\n\n🕒 2026-10-01 15:45 KST\n\n"
            '<a href="http://x">기사 원문</a> - 보험저널\n\n#태그1 #태그2')
    r = row("z", "제목", "한화생명", 90, orig)
    out = cstop10.render_item(r)
    check("카테고리 라벨", "<b>[🏢 한화그룹]</b>" in out, True)
    # 제목을 눌러 기사로 갈 수 있어야 한다 (2026-10-02 사용자 지정).
    # 원문에는 링크가 맨 아래 "기사 원문" 에만 있다.
    check("제목이 링크", '<a href="http://x"><b>제목</b></a>' in out, True)
    check("아이콘은 링크 밖에", "🏢 <a href=" in out, True)

    # Instant View — 제목은 telegra.ph 로 링크한다. 원래 기사 주소로 링크하면
    # IV 템플릿이 등록된 도메인만 되고(글로벌이코노믹 ○) 나머지는
    # "Open this link?" 가 뜨며 브라우저로 나간다(newsis AMP ×).
    out_iv = cstop10.render_item(r, url="http://real", iv="https://telegra.ph/x")
    check("제목은 telegra.ph 로",
          '<a href="https://telegra.ph/x"><b>제목</b></a>' in out_iv, True)
    check('본문 "기사 원문" 은 실제 기사로', "http://real" in out_iv, True)
    check("봇 이름 줄 제거", settings.bot_name not in out, True)
    check("핵심 섹션 유지", "✅ <b>핵심</b>" in out, True)
    check("주요 내용 섹션 유지", "📂 <b>주요 내용</b>" in out, True)
    check("불릿 5개 그대로", out.count("• 불릿"), 5)
    check("영문 라벨은 펭귄으로", "Why it matters" in out, False)
    check("펭귄", "🐧 왜 중요한가" in out, True)
    check("발행시각 유지", "🕒 2026-10-01 15:45 KST" in out, True)

    # **이중 이스케이프 회귀** (2026-10-02: 화면에 &#x27; 가 그대로 찍혔다)
    # 원문은 이미 이스케이프돼 있다. 다시 escape 하면 &amp;#x27; 가 된다.
    check("이중 이스케이프 없음", "&amp;#x27;" in out, False)
    check("원문 엔티티는 그대로", "&#x27;" in out, True)

    # 원문이 없는 옛 행도 깨지지 않는다
    out2 = cstop10.render_item(row("y", "옛제목", "한화생명", 50))
    check("원문 없어도 렌더", "옛제목" in out2, True)

    # 길이가 넘치면 **내용을 깎지 말고 메시지를 나눈다**
    many = [(90, row(f"k{i}", f"제목{i}", "한화생명", 90, orig)) for i in range(10)]
    msgs, previews = cstop10.render_all(many, "2026.10.02 Fri")
    check("불릿을 깎지 않는다", all(m.count("• 불릿") % 5 == 0 for m in msgs), True)
    # 미리보기 카드는 끈다 — 크고 거슬린다는 사용자 지적(2026-10-02).
    # 대신 제목을 telegra.ph 로 링크해 Instant View 로 열리게 한다.
    check("미리보기 카드 없음", previews, [])
    check("조각마다 상한 이내",
          all(cstop10.visible_len(m) <= cstop10.SAFE_LIMIT for m in msgs), True)
    # 고르게 나뉘어야 한다 — 3,999 + 731 처럼 쏠리면 안 된다
    if len(msgs) > 1:
        lens = [cstop10.visible_len(m) for m in msgs]
        check("조각 길이가 고르다", max(lens) - min(lens) < cstop10.SAFE_LIMIT // 2, True)


def test_cstop10_dedup():
    # 합성 데이터로 검사한다. 실제 공유 이력(groundtruth)이 끼어들면
    # 가짜 제목이 '기공유'로 걸려 결과가 달라진다. 비워 두고 본다.
    import shared as _sh
    _sh._cache = []
    # 합성 URL(http://x)은 실제로 받아올 수 없다. 전문 확보 검사를 우회해
    # 선정 로직만 본다. 유료기사 대체 자체는 test_iv_paywall 에서 검사한다.
    import cstop10 as _cs
    _cs._ORIG_FETCHABLE = getattr(_cs, "_ORIG_FETCHABLE", _cs._fetchable)
    _cs._fetchable = lambda group, store: max(group, key=lambda x: x[0])
    """같은 사건 접기 + 기게재 제외 (2026-10-02 사용자 피드백 회귀).

    Top10 에 애큐온캐피탈 인수가 3건, 한화투자증권 자본확충이 2건 실렸다.
    저장된 cluster_id 가 5가지로 갈려 있어 1차 묶기로는 못 잡았다.
    """
    import cstop10, time
    print("\n[Top10] 같은 사건 접기")

    # 실제 사고 데이터 그대로
    A = ("한화투자증권 자본확충 및 캐피탈·저축은행 인수 추진, 한화 금융계열사 포트폴리오 다각화",
         "한화투자증권,한화생명,한화그룹,김동원", "acquisition")
    B = ("한화투자증권, 9천억 원 자본확충으로 종투사 도약 및 디지털자산 사업 탄력",
         "한화투자증권", "investment")
    C = ("한화생명, 애큐온캐피탈 인수 확정", "한화생명", "acquisition")
    D = ("한화생명, 애큐온캐피탈 지분 50.54% 인수 의결",
         "한화생명,애큐온캐피탈", "acquisition")
    X = ("교보생명그룹, 디지털자산 접목 시도", "교보생명", "product")

    check("자본확충 2건은 같은 사건", cstop10._same(*A, *B), True)
    check("애큐온 2건은 같은 사건", cstop10._same(*C, *D), True)
    check("무관한 기사는 안 묶임", cstop10._same(*A, *X), False)

    # **한국어 복합어 — 서로의 접두사가 아니어도 앞 3글자가 같으면 같은 낱말.**
    # "애큐온캐피탈" 과 "애큐온저축은행" 이 안 걸려 같은 딜 기사 2건이
    # Top10 에 나란히 실렸다(2026-10-02).
    R1 = ("한화생명, 애큐온저축은행 인수 관련 풋옵션 자본비율 리스크 점검",
          "한화생명,한화저축은행", "governance")
    R2 = ("한화생명, 애큐온캐피탈 지분 50.54% 인수 의결",
          "한화생명,애큐온캐피탈", "acquisition")
    check("애큐온캐피탈 ↔ 애큐온저축은행", cstop10._same(*R1, *R2), True)
    check("접두 3글자 규칙",
          cstop10._stem_hits({"애큐온캐피탈"}, {"애큐온저축은행"}), 1)
    check("앞글자만 같은 무관한 말은 안 걸림",
          cstop10._stem_hits({"한화생명"}, {"삼성생명"}), 0)

    # 같은 사건이면 **리스크를 짚은 쪽**을 대표로 올린다.
    import csfit as _cf
    check("리스크 기사에 가산",
          _cf.risk_bonus("풋옵션 자본비율 리스크 점검") > 0, True)
    check("호재 전달에는 가산 없음",
          _cf.risk_bonus("인수 본계약 체결…신용등급 상향"), 0)

    # **한국어 조사** — "자본확충" vs "자본확충으로". 공백 토큰 비교로는 못 잡는다.
    import events
    check("조사가 붙어도 어간이 겹치면 인식",
          cstop10._stem_overlap({"자본확충"}, {"자본확충으로"}), True)
    check("짧은 조각은 무시", cstop10._stem_overlap({"가"}, {"가나다"}), False)
    check("무관한 낱말은 안 겹침",
          cstop10._stem_overlap({"자본확충"}, {"지배구조"}), False)

    # select() 가 실제로 접는가
    def row(k, head, ent, sc, et, cid):
        return (k, head, "http://x", "hanwha_group", "", sc, 0, cid, ent,
                "요약", "왜", time.time(), et, time.time(), "")
    rows = [row("a", A[0], A[1], 90, A[2], "cid1"),
            row("b", B[0], B[1], 88, B[2], "cid2"),
            row("c", C[0], C[1], 95, C[2], "cid3"),
            row("d", D[0], D[1], 92, D[2], "cid4"),
            row("x", X[0], X[1], 70, X[2], "cid5")]
    picked = cstop10.select(rows, count=10, store=None)
    heads = [r[1] for _, r in picked]
    # A 의 제목이 "자본확충 **및** 캐피탈·저축은행 인수" 로 두 딜을 다 담고 있어
    # 애큐온 건과도 이어진다. 네 건이 한 묶음이 되는 게 맞다 — 실제로 같은 날
    # 같이 발표된 한 건의 Corporate Action 이다.
    check("한화 딜 4건이 한 건으로", len(picked), 2)
    check("무관한 건은 남음", X[0] in heads, True)
    # 뽑힌 것들끼리도 서로 다른 사건이어야 한다
    pairs = [(p[1], q[1]) for i, p in enumerate(picked) for q in picked[i + 1:]]
    check("선정분끼리 중복 없음",
          any(cstop10._same(a[1] or "", a[8] or "", a[12] or "",
                            b[1] or "", b[8] or "", b[12] or "")
              for a, b in pairs), False)
    check("한화 건은 가장 큰 것 하나만",
          sum(1 for h in heads if "한화" in h), 1)


def test_cstop10_criteria():
    # 합성 데이터로 검사한다. 실제 공유 이력(groundtruth)이 끼어들면
    # 가짜 제목이 '기공유'로 걸려 결과가 달라진다. 비워 두고 본다.
    import shared as _sh
    _sh._cache = []
    # 합성 URL(http://x)은 실제로 받아올 수 없다. 전문 확보 검사를 우회해
    # 선정 로직만 본다. 유료기사 대체 자체는 test_iv_paywall 에서 검사한다.
    import cstop10 as _cs
    _cs._ORIG_FETCHABLE = getattr(_cs, "_ORIG_FETCHABLE", _cs._fetchable)
    _cs._fetchable = lambda group, store: max(group, key=lambda x: x[0])
    """추천 서칭 순서 + 125건 가중치 + 크립토 선별 (2026-10-02 사용자 지정)."""
    import cstop10, csfit, time
    print("\n[Top10] 선정 기준")

    def row(k, head, ent, sc, pri="insurance_finance", et="other"):
        return (k, head, "http://x", pri, "", sc, 0, f"c{k}", ent,
                "요약", "왜", time.time(), et, time.time(), "")

    # 티어 — 한화 여부는 **제목**으로 본다.
    # entities 로 보면 모델이 한화생명을 폭넓게 적는 탓에 "퇴직연금 기금화"
    # 까지 T1 이 돼 10건 중 7건이 1티어로 몰린다.
    check("제목이 한화면 T1",
          cstop10.tier_of(row("a", "한화생명, 애큐온캐피탈 인수", "한화생명", 90)), 1)
    check("엔티티만 한화면 T1 아님",
          cstop10.tier_of(row("b", "퇴직연금 기금화 논의 본격화", "한화생명", 60)) != 1, True)
    check("규제는 T3",
          cstop10.tier_of(row("c", "금융위, 토큰증권 제도 시행", "금융위원회", 70,
                              pri="regulation_policy")), 3)

    # 티어는 **가산점**이다. 절대 우선키로 두면 좋은 기사가 낮은 티어라는
    # 이유로 통째로 빠진다(69점 규제 기사가 45점 T2 뒤로 밀려 탈락했다).
    check("티어가 높을수록 가산점 큼",
          cstop10.TIER_BONUS[1] > cstop10.TIER_BONUS[3] > cstop10.TIER_BONUS[5], True)
    check("가산점이 점수차를 뒤집을 만큼 크지 않다",
          cstop10.TIER_BONUS[1] - cstop10.TIER_BONUS[5] < 50, True)

    # 크립토 — 제도·사업은 싣고 체인 기술·시세는 뺀다
    check("STO 제도는 게재",
          csfit.is_crypto_tech("금융위, 토큰증권(STO) 제도 내년 2월 시행"), False)
    check("스테이블코인 사업도 게재",
          csfit.is_crypto_tech("카카오그룹, 원화 스테이블코인 사업 추진"), False)
    check("체인 업그레이드는 제외",
          csfit.is_crypto_tech("이더리움 덴쿤 업그레이드…레이어2 가스비 절감"), True)
    check("시세 기사도 제외",
          csfit.is_crypto_tech("비트코인 8만5500달러 터치 후 급락"), True)

    rows = [row("x", "이더리움 덴쿤 업그레이드 완료", "이더리움", 60),
            row("y", "한화생명, 애큐온캐피탈 인수 본계약", "한화생명", 90)]
    picked = cstop10.select(rows, count=10, store=None)
    heads = [r[1] for _, r in picked]
    check("기술 기사는 Top10 에 안 들어감",
          any("덴쿤" in h for h in heads), False)


def test_iv_paywall():
    """IV 페이지는 본문을 제대로 가져왔을 때만 만든다 (2026-10-02 회귀).

    더벨 유료기사에서 "이 콘텐츠는 자본시장 미디어 더벨 유료회원 전용입니다"
    라는 안내문을 본문이라고 페이지에 실었다. 뉴데일리 AMP 는 0자였는데도
    요약만 담긴 빈 페이지를 만들었다. 둘 다 안 만드는 게 맞다.
    """
    import cstop10 as _cs
    import telegraph
    # 다른 테스트가 _fetchable 을 스텁으로 바꿔 놨을 수 있다. 원본으로 되돌린다.
    if hasattr(_cs, "_ORIG_FETCHABLE"):
        _cs._fetchable = _cs._ORIG_FETCHABLE
    print("\n[IV] 페이월·추출실패 판정")

    check("더벨은 유료 매체 목록에",
          any("thebell" in d for d in telegraph.PAYWALL_DOMAINS), True)
    check("인베스트조선도",
          any("investchosun" in d for d in telegraph.PAYWALL_DOMAINS), True)
    check("페이월 문구 인식",
          bool(telegraph.PAYWALL_MARKERS.search(
              "이 콘텐츠는 자본시장 미디어 더벨 유료회원 전용입니다")), True)
    check("일반 본문은 페이월 아님",
          bool(telegraph.PAYWALL_MARKERS.search(
              "한화생명이 애큐온캐피탈 지분 50.54%를 인수하기로 했다")), False)
    check("최소 분량 기준이 있다", telegraph.MIN_BODY_CHARS >= 200, True)

    # **<br> 로 문단을 나누는 매체가 많다** (2026-10-02 회귀).
    # 비즈니스포스트는 <p> 1개에 <br> 47개, 뉴시스는 <p> 0개였다.
    # <p> 만 보던 때 이 매체들이 전부 "본문 부족(0자)" 로 빠졌다.
    check("상투 문구 필터 — IE 안내",
          bool(telegraph._BOILERPLATE.search(
              "잠깐! 현재 Internet Explorer 8이하 버전을 이용중이십니다")), True)
    check("상투 문구 필터 — 저작권",
          bool(telegraph._BOILERPLATE.search("무단 전재 및 재배포 금지")), True)
    check("일반 문장은 안 걸린다",
          bool(telegraph._BOILERPLATE.search(
              "한화생명이 애큐온캐피탈 지분 50.54%를 인수하기로 했다")), False)
    check("제목 비교 정규화", telegraph._key("한화생명, 애큐온 인수!"),
          telegraph._key("한화생명 애큐온 인수"))

    # 유료 도메인은 네트워크를 타지 않고 바로 거른다
    paras, why = telegraph.fetch_article("https://m.thebell.co.kr/m/newsview.asp?x=1")
    check("유료 매체는 즉시 생략", (paras, bool(why)), ([], True))
    check("도메인만으로 판정(네트워크 안 탐)",
          telegraph.is_paywalled("https://m.thebell.co.kr/x"), True)
    check("무료 매체는 통과",
          telegraph.is_paywalled("https://www.yna.co.kr/x"), False)

    # **유료기사는 같은 사건의 다른 매체로 갈아탄다.**
    # 전문을 Instant View 에 실을 수 없는 기사는 올리지 않는다.
    # 네트워크를 타지 않게 fetch_article 을 바꿔 끼운다 — 유료 도메인은
    # _fetchable 이 그 전에 거르므로 여기 오지 않는다.
    import cstop10, time
    real_fetch = telegraph.fetch_article
    telegraph.fetch_article = lambda url, timeout=15, title="": (["본문"], "")
    try:
        def row(k, head, url, ent="한화생명", sc=90):
            return (k, head, url, "hanwha_group", "", sc, 0, "c1", ent,
                    "요약", "왜", time.time(), "acquisition", time.time(), "")
        grp = [(95, row("a", "하나은행 싱가포르 디지털 금융",
                        "https://m.thebell.co.kr/x")),
               (80, row("b", "하나은행 싱가포르 디지털 금융 거점 확대",
                        "https://www.yna.co.kr/view/AKR1"))]
        got = cstop10._fetchable(grp, None)
        check("유료 대신 다른 매체를 고른다",
              got is not None and "thebell" not in (got[1][2] or ""), True)
        check("점수가 낮아도 전문 되는 쪽", got[1][0] if got else None, "b")

        only_paid = [(95, row("a", "제목", "https://m.thebell.co.kr/x"))]
        check("대체할 기사가 없으면 건너뛴다",
              cstop10._fetchable(only_paid, None), None)
    finally:
        telegraph.fetch_article = real_fetch

    # **페이지는 언제나 만든다.** 제목은 반드시 Instant View 로 열려야 한다는
    # 절대 규칙 때문이다. 본문이 없으면 긁어온 척하지 않고 우리 요약으로 채우고
    # 왜 전문이 없는지 밝힌다.
    c = telegraph.build_content("요약문", ["불릿1"], "왜중요", [],
                                "http://x", "매체",
                                note=telegraph.NOTE_PAYWALL)
    text = str(c)
    check("본문 없어도 페이지는 만든다", len(c) > 0, True)
    check("우리 요약으로 채운다", "요약문" in text, True)
    check("주요 내용도 싣는다", "불릿1" in text, True)
    check("왜 전문이 없는지 밝힌다", "유료회원 전용" in text, True)
    check("원문 링크는 항상", "http://x" in text, True)

    # 본문이 있으면 본문이 맨 위, 요약은 싣지 않는다
    c2 = telegraph.build_content("요약문", ["불릿1"], "왜중요",
                                 ["기사 본문 첫 문단"], "http://x", "매체")
    t2 = str(c2)
    check("본문이 있으면 본문만", "요약문" in t2, False)
    check("본문이 맨 위", c2[0]["children"], ["기사 본문 첫 문단"])


def test_shared_and_ads():
    """A팀 기공유 제외 · 광고성 문구 제거 (2026-10-02 사용자 지정)."""
    import shared, telegraph
    print("\n[Top10] 기공유 제외 · 광고 제거")

    # **토큰 Jaccard 만으로는 못 잡는다.** 매체가 다르면 제목이 전혀 다르다.
    #   "포스코그룹, 우리금융지주 보유 지분 전량 매각"
    #   "포스코, 우리금융 지분 6700억 블록딜…10년만에 엑시트"  → Jaccard 0.08
    # 어간 겹침(포스코 ⊂ 포스코그룹)으로 세야 잡힌다.
    a = shared._stems("포스코그룹, 우리금융지주 보유 지분 전량 매각")
    b = shared._stems("포스코, 우리금융 지분 6700억 블록딜…10년만에 엑시트")
    check("복합어가 달라도 어간으로 잡는다",
          shared._overlap(a, b) >= shared.STEM_HITS, True)

    c1 = shared._stems("금융위, 토큰증권 제도 내년 2월 시행 확정")
    c2 = shared._stems("한화생명, 애큐온캐피탈 인수 본계약 체결")
    check("무관한 기사는 안 걸린다",
          shared._overlap(c1, c2) >= shared.STEM_HITS, False)
    check("흔한 말은 신호로 안 센다", "업계" in shared._COMMON, True)

    # 광고·추천 기사 제목 — 한국어 본문은 종결어미로 끝나고 제목은 안 끝난다
    ads = ["550조 퇴직연금, DC·IRP 시장 확대…개인 자금 증권사 머니무브",
           "바비인형 제조사 마텔, 주가 18% 폭등...어센틱 브랜즈 인수설",
           "코스피, 반도체株 강세에 6900선 회복...코스닥은 4%대 급등"]
    body = ["한화생명이 애큐온캐피탈 지분 50.54%를 4400억원에 인수하기로 했다.",
            "금융감독원은 2일 보험업계와 간담회를 열고 심의 기준을 강화하기로 했다"]
    check("광고성 제목은 제외",
          all(telegraph._looks_like_headline(t) for t in ads), True)
    check("본문 문장은 유지",
          any(telegraph._looks_like_headline(t) for t in body), False)

    # 추천 기사 영역은 통째로 잘라낸다 — 컨테이너 정규식이 문서 끝까지 먹는다
    cut = telegraph._cut_tail("본문" * 200 + '<div class="related-news">추천</div>')
    check("추천 영역 절단", "related" in cut, False)


def test_due_gate():
    """--if-due 게이트 (2026-10-02 누락 사고 회귀).

    GitHub schedule 이 발화하지 않아 06:55 Top10 이 통째로 빠진 날이 있었다.
    상시 루프가 매 회차 물어보는 구조로 바꿨으므로, 그 판정이 정확해야 한다.
    """
    import cstop10
    from datetime import datetime, timedelta, timezone
    KST = timezone(timedelta(hours=9))
    print("\n[due] 발행 시점 판정")

    class S:
        def __init__(self, ran=False): self.ran = ran
        def agg_ran_on(self, scope, a, b): return self.ran
        # select() 가 쓰는 조회들. due() 검사에는 쓰이지 않지만,
        # 같은 가짜 store 가 다른 검사로 흘러가도 터지지 않게 둔다.
        def cstop10_recent_clusters(self, since): return []
        def published_clusters_before(self, before, since=None): return []
        # 2026-10-05: select() 가 '다른 날짜 Top10 에 쓰인 기사'도 걸러낸다.
        def top10_article_keys(self, exclude_date=""): return set()

    def at(h, m):
        return datetime(2026, 10, 2, h, m, tzinfo=KST).timestamp()

    # 2026-10-05: 06:55 → 06:50. 사용자가 지정한 시각이다. 바꾸지 마라.
    check("발행 시각이 06:50", settings.cs_top10_time, "06:50")
    ok, _ = cstop10.due(S(False), at(6, 30))
    check("06:30 아직 이름", ok, False)
    ok, _ = cstop10.due(S(False), at(6, 49))
    check("06:49 아직 이름", ok, False)
    ok, _ = cstop10.due(S(False), at(6, 50))
    check("06:50 정각이면 발행", ok, True)
    ok, _ = cstop10.due(S(False), at(9, 0))
    check("늦어도 그날 안이면 발행", ok, True)
    ok, _ = cstop10.due(S(True), at(9, 0))
    check("오늘 이미 나갔으면 안 함", ok, False)
    ok, _ = cstop10.due(S(False), at(23, 59))
    check("자정 직전에도 발행", ok, True)


def test_top10_links():
    """🔗 top10(링크용) — 기사 1건당 1메시지, 원문 주소 (2026-10-05 사용자 지정).

    왜 검사하나. 한 메시지에 10건을 몰아 담으면 텔레그램이 미리보기 카드를
    하나만 붙여 9건이 맨 주소로 남는다. 그래서 '건수 = 메시지 수' 가 규칙이다.
    주소도 telegra.ph 가 아니라 원문이어야 한다 — 복사해서 붙이는 용도다.
    """
    import cstop10
    print("\n[§5] 🔗 top10(링크용)")

    check("탭 정의 있음", "cs_top10_links" in topics.CATEGORIES, True)
    check("탭 이름", topics.display_name("cs_top10_links"), "🔗 top10(링크용)")
    check("집계 탭이다", "cs_top10_links" in topics.AGGREGATION, True)
    check("모델이 지정할 수 없다", "cs_top10_links" in PRIMARY_TOPIC_IDS, False)

    # K_* 인덱스에 맞춘 최소 행. 길이는 K_TEXT 까지 채운다.
    def row(head, url, pri):
        r = [None] * (cstop10.K_TEXT + 1)
        r[cstop10.K_HEAD], r[cstop10.K_URL], r[cstop10.K_PRI] = head, url, pri
        return r

    picked = [
        (0, row("한화생명, 애큐온캐피탈 인수", "https://ex.co/a", "hanwha_group")),
        (0, row("교보생명 스테이블코인 실증", "https://ex.co/b", "insurance_finance")),
    ]
    msgs = cstop10.render_links(picked, "2026.10.05 Sun", None)
    check("기사 1건당 1메시지", len(msgs), 2)
    check("1번에 번호", msgs[0][0].startswith("<b>1.</b>"), True)
    check("2번에 번호", msgs[1][0].startswith("<b>2.</b>"), True)
    check("카테고리 들어감", "🏢 한화그룹" in msgs[0][0], True)
    check("제목 들어감", "애큐온캐피탈" in msgs[0][0], True)
    check("원문 주소 들어감", "https://ex.co/a" in msgs[0][0], True)
    check("미리보기 주소 = 원문", msgs[0][1], "https://ex.co/a")
    check("telegra.ph 를 쓰지 않음", "telegra.ph" in msgs[0][0], False)
    # 본문·요약은 이쪽 역할이 아니다. 📌 A팀 Top10 과 겹치면 탭이 무의미해진다.
    check("🐧 해설이 섞이지 않음", "🐧" in msgs[0][0], False)
    # 주소가 없는 행은 조용히 건너뛴다 — 빈 링크를 올리면 안 된다.
    check("주소 없으면 제외",
          len(cstop10.render_links([(0, row("제목만", "", "hanwha_group"))],
                                   "x", None)), 0)


def test_secs_until_due():
    """발행 시각에 정확히 깨어나기 (2026-10-03 07:10 지연 회귀).

    20분 주기로만 물어보던 때는 06:49 회차를 놓치고 07:09 회차에 잡혀
    07:10 에 나갔다. bot.yml 의 대기 루프가 이 값을 보고 잠을 끊는다.
    """
    import subprocess
    import sys
    print("\n[타이밍] 발행 시각까지 남은 초")
    out = subprocess.run([sys.executable, "tools/secs_until_due.py"],
                         capture_output=True, text=True)
    check("실행 성공", out.returncode, 0)
    try:
        secs = int(out.stdout.strip())
    except ValueError:
        secs = -1
    check("정수 한 줄", secs >= 0, True)
    # 06:50·07:00 중 가까운 쪽까지. 둘 다 지났으면 FAR(86400).
    check("24시간 안", secs <= 86400, True)


def test_paywall_gate_everywhere():
    """유료 매체는 **일반 탭에도** 내보내지 않는다 (2026-10-05 회귀).

    Top10 에는 같은 사건의 다른 매체로 갈아타는 관문이 있었는데 일반 발행
    경로에는 없어서 딜사이트 기사가 그대로 나갔다(msg 384). 팀 입장에서는
    열어도 전문을 못 읽는 링크라 쓸모가 없다.
    """
    import telegraph
    print("\n[유료] 일반 탭 유료 차단")
    for d in ("dealsite.co.kr", "thebell.co.kr", "investchosun.com", "einfomax.co.kr"):
        check(f"{d} 는 유료", telegraph.is_paywalled(f"https://www.{d}/news/1"), True)
    check("무료 매체는 통과(ebn)", telegraph.is_paywalled("https://www.ebn.co.kr/news/1"), False)

    src = open("main.py", encoding="utf-8").read()
    gate = src.find("telegraph.is_paywalled")
    step5 = src.find("STEP 5: Event Deduplication")
    check("main.py 에 유료 관문이 있다", gate > 0, True)
    # 관문이 Event 등록 뒤에 있으면, 무료 매체 기사가 '중복 Event' 에 막혀
    # 영영 못 나간다. 반드시 앞이어야 한다.
    check("관문이 Event 등록보다 앞", 0 < gate < step5, True)
    # 2026-10-05: 막는 게 아니라 **같은 사건의 무료 기사로 갈아탄다.**
    # 딜 전문지는 A팀이 실제로 공유하는 Source 라 통째로 막으면 안 된다.
    check("무료 대체를 시도한다", "free_alternative" in src, True)
    check("대체 실패 시에만 건너뛴다", "유료·대체실패" in src, True)

    # 본문을 못 읽는 건은 ⚠️ 꼬리말을 달지 않고 아예 내보내지 않는다.
    check("본문불가 건을 거른다", "본문불가" in src, True)
    low = src.find('if (data.get("confidence") or 1.0) < 0.5:')
    check("본문불가 관문도 Event 등록보다 앞", 0 < low < step5, True)


def test_source_weight():
    """§16 — 매체 가중치."""
    print("\n[§16] Source Quality")
    check("공식 1차 출처", source_weight("금융위원회"), 5)
    check("딜 전문지", source_weight("더벨"), 4)
    check("종합 경제지", source_weight("연합뉴스"), 3)
    check("모르는 매체는 기본값", source_weight("어디뉴스"), 2)


def test_schema():
    """§28 — Structured Output 검증."""
    print("\n[§28] JSON Schema")
    ok, _ = prompts_bsp.validate({"relevant": False, "relevance_reason": "코인 시세"})
    check("제외 판정은 relevant/reason 만으로 통과", ok, True)

    ok, _ = prompts_bsp.validate({"relevant": True, "primary_topic": "key_issues",
                                  "strategic_score": 90, "title_ko": "a",
                                  "summary": "b", "why_it_matters": "c"})
    check("집계토픽을 primary 로 주면 거부", ok, False)

    ok, _ = prompts_bsp.validate({"relevant": True, "primary_topic": "hanwha_group",
                                  "strategic_score": 150, "title_ko": "a",
                                  "summary": "b", "why_it_matters": "c"})
    check("점수 범위 밖이면 거부", ok, False)

    ok, _ = prompts_bsp.validate({"relevant": True, "primary_topic": "hanwha_group",
                                  "strategic_score": 90, "title_ko": "a",
                                  "summary": "b", "why_it_matters": ""})
    check("why_it_matters 비면 거부", ok, False)

    d = prompts_bsp.coerce({
        "relevant": True, "primary_topic": "hanwha_group",
        "secondary_topics": ["ma_governance", "hanwha_group", "key_issues"],
        "strategic_score": 91.6, "event_type": "없는타입",
        "tags": ["#한화생명", "M&A"], "confidence": 5,
        "title_ko": "a", "summary": "b", "why_it_matters": "c"})
    check("secondary 에서 primary·집계토픽 제거", d["secondary_topics"], ["ma_governance"])
    check("점수 정수화", d["strategic_score"], 92)
    check("모르는 event_type 은 other", d["event_type"], "other")
    check("태그 # 제거", d["tags"], ["한화생명", "M&A"])
    check("confidence 범위 보정", d["confidence"], 1.0)


def test_render():
    """§26 — 개별 기사 발행 양식."""
    print("\n[§26] Telegram UI")
    d = {"category": "hanwha_group", "headline": "한화생명, 미국 증권사 인수 추진",
         "summary": "인수 실사에 착수했다.", "key_points": ["규모 5,000억원", "연내 본계약"],
         "why_it_matters": "해외 WM 확대와 연결될 가능성이 있음.",
         "tags": ["한화생명", "M&A", "한화그룹"], "_source_name": "더벨",
         "_posted_label": "2026-09-23 06:31 KST"}
    out = publisher.render(d, "https://thebell.co.kr/x")
    check("봇 이름 노출", settings.bot_name in out, True)
    check("토픽 아이콘", "🏢" in out, True)
    check("핵심 섹션", "✅ <b>핵심</b>" in out, True)
    check("주요 내용 섹션", "📂 <b>주요 내용</b>" in out, True)
    # 2026-10-01: 영문 라벨을 떼고 크립토 봇과 같은 펭귄 코멘트 형태로 바꿨다.
    check("펭귄 코멘트(why_it_matters) 섹션", "🐧 " in out, True)
    check("영문 라벨은 더 이상 쓰지 않음", "Why it matters" in out, False)
    check("매체명", "더벨" in out, True)
    check("탭 태그가 맨 앞", out.strip().split("\n")[-1].startswith("#한화그룹"), True)
    check("중복 태그 제거(한화그룹 1회)", out.count("#한화그룹"), 1)
    check("crypto wording 없음",
          any(w in out for w in ("크립토", "코인", "블록체인")), False)


def test_thresholds():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    """§19·§40 — 임계값이 config 로 조정 가능해야 한다."""
    print("\n[§19] Threshold 설정")
    check("discard", settings.discard_threshold, 50)
    check("general", settings.general_topic_threshold, 65)
    check("key issue", settings.key_issue_threshold, 85)
    check("브리프 시각", settings.daily_brief_time, "07:00")
    check("브리프 건수", settings.daily_brief_count, 10)
    # 운영에 들어가면 .env 의 DRY_RUN 은 false 가 된다. 현재 값이 아니라
    # **코드의 기본값**이 true 인지를 본다 — .env 가 없거나 키가 빠졌을 때
    # 실수로 발행되지 않게 하는 안전장치가 그것이다.
    src = open(os.path.join(here, "config.py"), encoding="utf-8").read()
    check("DRY_RUN 코드 기본값이 true",
          'os.getenv("DRY_RUN", "true")' in src, True)
    if settings.dry_run:
        print("     (현재 .env: DRY_RUN=true — 발행하지 않음)")
    else:
        print("     (현재 .env: DRY_RUN=false — 실발행 상태)")


def test_isolation():
    """불변의 법칙 — 크립토 봇 두 리포에 영향이 없어야 한다."""
    print("\n[격리] 크립토 봇과의 분리")
    import subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 2026-10-01 배포 후: remote 가 생겼다. 지켜야 할 불변식은 "remote 가 없다"가
    # 아니라 "크립토 리포를 가리키지 않는다" 다.
    r = subprocess.run(["git", "remote", "-v"], cwd=here, capture_output=True, text=True)
    remotes = r.stdout.strip()
    check("remote 가 크립토 리포가 아님",
          ("crypto-news-bot" in remotes), False)
    # 토큰이 "비어 있는가"가 아니라 "크립토 봇 것과 다른가"를 본다.
    # 셋업을 마치면 토큰은 당연히 채워진다. 지켜야 할 불변식은 재사용 금지다.
    inherited = {}
    danger = os.path.join(here, ".env.crypto-inherited.DANGEROUS")
    if os.path.exists(danger):
        with open(danger, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                inherited[k.strip()] = v.strip()
    crypto_token = inherited.get("TELEGRAM_BOT_TOKEN", "")
    crypto_chat = inherited.get("TELEGRAM_CHANNEL_ID", "")
    check("크립토 봇 토큰을 재사용하지 않음",
          bool(crypto_token) and settings.telegram_bot_token == crypto_token, False)
    check("크립토 봇 그룹으로 발행하지 않음",
          bool(crypto_chat) and str(settings.telegram_channel_id) == crypto_chat, False)
    check("프롬프트가 BSP", "A팀" in prompts_bsp.SYSTEM_PROMPT, True)
    # 2026-10-02: key_points 가 추상적으로만 나와 구체 예시를 프롬프트에 박았다.
    # 예시가 빠지면 다시 "포트폴리오 다변화" 류로 돌아간다.
    P = prompts_bsp.SYSTEM_PROMPT
    check("불릿에 구체 예시 있음", "4,400억 원에 인수" in P, True)
    check("나쁜 예도 함께 제시", "나쁜 예" in P, True)
    check("수치 조작 금지 명시", "지어내거나 반올림하지" in P, True)
    check("미확인 보도 표기 지시", "공식 확인하지 않음" in P, True)


# ══════════════════════════════════════════════════════════
# §38 — 실제 모델 판정 (--llm 일 때만)
# ══════════════════════════════════════════════════════════
LLM_CASES = [
    ("A", "한화생명, 미국 증권사 인수 추진",
     dict(relevant=True, primary="hanwha_group",
          secondary={"ma_governance", "global_finance"}, score_min=80)),
    ("B", "금융위, 보험사 자본규제 개편",
     dict(relevant=True, primary="regulation_policy",
          secondary={"insurance_finance"}, score_min=75)),
    ("C", "카카오페이, 해외 결제사업 확대",
     dict(relevant=True, primary="competitors_bigtech",
          secondary={"digital_newbiz", "global_finance"}, score_min=60)),
    ("D", "비트코인 가격 5% 상승",
     dict(relevant=False)),
    ("E", "교보생명 경영권 분쟁, SBI 지분 확대",
     dict(relevant=True, primary="ma_governance",
          secondary={"insurance_finance"}, score_min=75)),
    ("F", "한화그룹 관련 공정위 조사",
     dict(relevant=True, primary="hanwha_group",
          secondary={"regulation_policy"}, score_min=80, key_issue=True)),
    ("G", "삼성생명 새 건강보험 상품 출시",
     dict(relevant=False, allow_low=True)),
    ("H", "인도네시아 금융당국 외국계 은행 인수규제 개편",
     dict(relevant=True, primary={"global_finance", "regulation_policy"}, score_min=55)),
]


async def test_llm():
    from summarizer import summarize

    print("\n[§38] 실제 모델 판정 — Topic Routing Test")
    print(f"  {'':2} {'입력':32} {'Rel':5} {'Primary':20} {'Score':5} {'Key'}")
    print("  " + "─" * 76)

    for cid, title, want in LLM_CASES:
        it = item(title)
        pre = prefilter.reject_reason(it)
        if pre:
            print(f"  {cid}  {title[:32]:32} 사전필터에서 버림 — {pre}")
            check(f"{cid} 기대와 일치", want["relevant"], False)
            continue

        data = await summarize(it)
        if data is None:
            print(f"  {cid}  {title[:32]:32} 모델 호출 실패/무응답")
            continue

        rel = bool(data.get("relevant"))
        pri = data.get("primary_topic", "-")
        sc = data.get("strategic_score", "-")
        ki = data.get("is_key_issue", "-")
        print(f"  {cid}  {title[:32]:32} {str(rel):5} {str(pri):20} {str(sc):5} {ki}")

        if want.get("allow_low"):
            ok = (not rel) or (isinstance(sc, int) and sc < settings.general_topic_threshold)
            check(f"{cid} 무관하거나 낮은 점수", ok, True)
            continue
        check(f"{cid} relevant", rel, want["relevant"])
        if not want["relevant"]:
            continue
        exp = want["primary"]
        exp = exp if isinstance(exp, set) else {exp}
        check(f"{cid} primary ∈ {exp}", pri in exp, True)
        if "score_min" in want:
            check(f"{cid} score ≥ {want['score_min']}", sc >= want["score_min"], True)
        if want.get("key_issue"):
            check(f"{cid} key issue 후보", bool(ki), True)


def main():
    print("═" * 78)
    print("  Strategy Pulse 회귀 검사")
    print("═" * 78)
    test_negative()
    test_entity_alias()
    test_topic_routing()
    test_event_dedup()
    test_cstop10()
    test_cstop10_dedup()
    test_cstop10_criteria()
    test_iv_paywall()
    test_shared_and_ads()
    test_due_gate()
    test_top10_links()
    test_paywall_gate_everywhere()
    test_secs_until_due()
    test_source_weight()
    test_schema()
    test_render()
    test_thresholds()
    test_isolation()

    if "--llm" in sys.argv:
        asyncio.run(test_llm())
    else:
        print("\n[§38] 모델 판정 검사는 건너뜀 (--llm 을 붙이면 실행)")

    print("\n" + "═" * 78)
    if _fails:
        print(f"  실패 {_fails}건")
        sys.exit(1)
    print("  전부 통과")


if __name__ == "__main__":
    main()
