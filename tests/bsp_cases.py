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
    print("\n[§5] 토픽 10개")
    # 2026-10-01: 📌 경전실 Top10 추가. Primary 7개는 그대로다 —
    #              새 토픽은 집계 토픽이라 모델이 지정하지 않는다.
    check("토픽 수", len(topics.CATEGORIES), 10)
    check("Primary 지정 가능 토픽 7개", len(PRIMARY_TOPIC_IDS), 7)
    check("집계 토픽", topics.AGGREGATION,
          {"key_issues", "daily_brief", "cs_top10"})
    for tid, name in [
        ("hanwha_group", "🏢 한화그룹"), ("ma_governance", "🤝 M&A · 지배구조"),
        ("insurance_finance", "🏦 보험 · 금융"), ("regulation_policy", "⚖️ 규제 · 정책"),
        ("competitors_bigtech", "🔎 경쟁사 · Big Tech"),
        ("digital_newbiz", "💡 디지털 · 신사업"), ("global_finance", "🌐 Global"),
        ("key_issues", "🚨 주요이슈"), ("daily_brief", "☀️ Morning Brief"),
        ("cs_top10", "📌 경전실 Top10"),
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
    """📌 경전실 Top10 — 적합도·홍보성 판정 (2026-10-01)."""
    import csfit, cstop10, time
    print("\n[Top10] 경전실 적합도")

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

    # 홍보성 쿼터 — 이틀에 한 건
    def row(k, head, ent, sc):
        return (k, head, "http://x", "hanwha_group", "", sc, 0, None, ent,
                "요약", "왜", time.time(), "other", time.time())
    rows = [row("a", "한화손보, 최고령 설계사 헌정식", "한화손해보험", 60),
            row("b", "한화큐셀, 영농형태양광 업무협약", "한화큐셀", 55),
            row("c", "한화생명, 애큐온캐피탈 인수 의결", "한화생명", 92)]
    now = time.time()

    class S:
        def __init__(self, last): self._l = last
        def last_pr_pick(self): return self._l

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
    check("프롬프트가 BSP", "경영전략실" in prompts_bsp.SYSTEM_PROMPT, True)


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
