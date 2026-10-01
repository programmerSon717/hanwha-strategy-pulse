"""Strategy Pulse — 메인 루프.

    STEP 1 수집 → STEP 2 사전필터 → STEP 3~4 이해·전략관련성 판정(LLM)
    → STEP 5 Event 중복제거 → STEP 6 Topic 분류 → STEP 7 Ranking
    → STEP 8 텔레그램 발행 → (별도) STEP 9 Morning Brief

    python main.py --brief                   # ☀️ Morning Brief 생성 (06:55 KST)
    python main.py --brief --dry-run         # 브리프 내용만 확인, 발행 안 함


첫 실행 시에는 기존 뉴스가 전부 새 뉴스로 잡히므로,
--warm 옵션으로 현재 뉴스를 발행 없이 '이미 본 것'으로 등록하고 시작하는 걸 권장.

    python main.py --warm                    # 최초 1회
    python main.py                           # 상시 실행
    python main.py --once                    # 1회만 (GitHub Actions 등 스케줄러용)
    python main.py --since 2026-08-16        # 해당 날짜 00시(KST) 이후 뉴스 백필
    python main.py --since 2026-08-16 --dry-run   # 발행 없이 대상만 출력
    python main.py --tg-since 2026-08-10     # 텔레그램 소스 채널만 백필(트위터 캡처 인사이트)
    python main.py --digest                  # (구) 시간별 다이제스트 — BSP 미사용
    python main.py --resort                  # 탭 안 글을 최신순으로 재정렬
    python main.py --resort --only 이슈       # 특정 탭만
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

from collectors import (binance, bithumb, upbit, rss, telegram_channels, tg_web,
                        blockmedia_archive, blockmedia_research, coin68,
                        regulation)
from config import settings
import events
from models import NewsItem
import prefilter
import publisher
from publisher import publish
import topics as _topics
from store import Store, normalize_url
import summarizer
import lang
from summarizer import summarize, summarize_briefing, summarize_insight

store = Store(settings.db_path)

KST = timezone(timedelta(hours=9))


def _arg_value(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def filter_since(items: list[NewsItem], date_str: str) -> list[NewsItem]:
    """date_str(YYYY-MM-DD) 00:00 KST 이후 항목만. 날짜 불명(None)은 제외한다.

    백필에서 날짜 불명을 통과시키면 오래된 공지까지 무제한 유입되므로 의도적으로 버린다.
    """
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST).timestamp()
    kept, undated = [], 0
    for it in items:
        if it.published_at is None:
            undated += 1
            continue
        if it.published_at >= start:
            kept.append(it)
    if undated:
        print(f"[since] 날짜 불명 {undated}건 제외")
    return kept


def normalize_category(cat: str | None) -> str:
    """모델이 뱉은 카테고리를 실제 탭이 존재하는 값으로 보정한다.

    표기가 흔들리면(예: 'US policy', '미국정책') 탭 라우팅이 조용히 실패해
    본문에 섞여 발행되므로, 여기서 한 번 걸러준다.
    """
    import topics as _topics

    if not cat:
        return "이슈"
    if cat in _topics.CATEGORIES:
        return cat
    lowered = cat.strip().lower()
    for known in _topics.CATEGORIES:
        if known.lower() == lowered:
            return known
    aliases = {
        "미국정책": "US Policy", "일본정책": "Japan Policy",
        "홍콩정책": "Hong Kong Policy", "싱가포르정책": "Singapore Policy",
        "싱가폴정책": "Singapore Policy", "uae정책": "UAE Policy",
        "베트남정책": "Vietnam Policy", "국내": "국내정책", "해외": "해외정책",
        "한국금리": "Korea Rates", "미국금리": "US Rates",
        "한국증시": "Korea Equities", "미국증시": "US Equities",
        "korea rate": "Korea Rates", "us rate": "US Rates",
        "korea equity": "Korea Equities", "us equity": "US Equities",
        "global macro": "Global Macro", "글로벌거시": "Global Macro",
        "중국": "China", "china": "China", "china policy": "China",
        # 모델이 띄어쓰기를 넣거나 영어로 쓰는 일이 있다
        "거래소 이슈": "거래소이슈", "거래소": "거래소이슈",
        "exchange": "거래소이슈", "exchange issue": "거래소이슈",
    }
    if lowered in aliases:
        return aliases[lowered]
    print(f"[분류] 알 수 없는 카테고리 '{cat}' → 이슈로 처리")
    return "이슈"


# 이보다 오래된 글은 '실시간'이 아니라고 보고 게시 시각을 함께 표기한다.
FRESH_SEC = 3 * 3600

# 크립토 봇의 나라별 탭 보정(country.py)은 BSP 에서 쓰지 않는다 —
# BSP 토픽은 나라가 아니라 전략 주제로 갈린다(§5). 모듈은 지우지 않고 남겨 둔다.
MARKET_TABS: set = set()


def is_repost(item: NewsItem) -> bool:
    """다른 텔레그램 채널에서 퍼온 글인가. 이 글들은 수집처가 출처가 아니다."""
    return item.source.startswith("TG:")


def topics_thread_id(category: str) -> int | None:
    import topics as _topics
    return _topics.thread_id_for(category)


def annotate_origin(data: dict, item: NewsItem):
    """발행 직전에 출처·게시시각 정보를 보강한다.

    - 채널 캡션에 원문 링크가 붙어 있으면 그게 가장 확실하므로 모델 판단보다 우선한다.
    - 실시간이 아닌 글이면 게시 시각을 표기하도록 표시를 남긴다.
    """
    if is_repost(item):
        # 렌더러가 수집처 링크를 출처로 쓰지 않게 하는 표시
        data["_repost"] = True
    if item.origin_url:
        data["origin_url"] = item.origin_url

    # 발행 하단 '기사 원문' 옆에 매체명을 적기 위해 수집처를 넘긴다.
    data["_source_name"] = item.source

    # 기사·문서의 발행 시각은 **항상** 표기한다(사용자 요청).
    # 실시간이든 백필이든 언제 나온 뉴스인지 알 수 있어야 한다.
    if item.published_at is None:
        return
    dt = datetime.fromtimestamp(item.published_at, KST)
    data["_posted_label"] = dt.strftime("%Y-%m-%d %H:%M KST")


def age_limit_hours(item: NewsItem) -> int:
    """이 항목에 적용할 나이 상한.

    국가별 규제 기사만 따로 넉넉하게 본다. 구글뉴스 검색 결과라 색인이 늦고
    when:7d 로 긁어오는데, 일반 기사와 같은 12시간을 적용하면 195건 중 1건만
    남아 나라별 정책 탭이 통째로 빈다. 게시 시각은 본문에 항상 표기되므로
    독자는 오래된 기사임을 알 수 있고, 중복 판정이 이미 나간 사건의 반복을 막는다.
    """
    if (item.region_hint or "").endswith("/규제"):
        return settings.regulation_max_age_hours
    return settings.max_age_hours


# 정책·규제 탭. 여기는 문턱을 낮춰야 나라별 탭이 채워진다.
POLICY_TABS = {"국내정책", "US Policy", "Japan Policy", "Hong Kong Policy",
               "Singapore Policy", "UAE Policy", "Vietnam Policy",
               "해외정책", "China"}


def importance_floor(category: str) -> int:
    """이 탭에 실으려면 몇 점 이상이어야 하는가.

    규제 기사는 모델이 3점을 주는 일이 많다. 문턱을 4로 걸면 나라별 정책 탭이
    통째로 빈다. 그래서 문턱을 3으로 내렸더니, 이번에는 프로젝트 홍보성 글이
    이슈 탭으로 새어 나왔다(예측시장 펀드 유입, AI 에이전트 수익 공유 실험).
    한쪽을 채우려고 내린 문턱이 다른 쪽을 흐린 것이라, 탭 성격별로 나눠 건다.
    """
    if category in POLICY_TABS:
        return settings.policy_min_importance
    return settings.min_importance


def is_stale(item: NewsItem) -> bool:
    """설정한 시간보다 오래된 기사인가. 날짜 불명은 최신으로 본다(공지 등)."""
    limit = age_limit_hours(item)
    if not limit or item.published_at is None:
        return False
    return datetime.now(tz=KST).timestamp() - item.published_at > limit * 3600


def summarize_priority(item: NewsItem) -> tuple:
    """요약 대기열의 순번. 작을수록 먼저 처리한다.

    1) 국가별 규제·정책이 맨 앞. 물량이 많은 일반 기사에 밀리면 국가 탭이
       계속 비어 보인다. (기존 동작을 그대로 유지한다)
    2) 그다음 일반 뉴스.
    3) 리서치·분석은 맨 뒤. 시의성이 없어 몇 시간 늦어도 값이 안 떨어진다.

    같은 단계 안에서는 **최신순**이다. 예전에는 오래된 것부터 처리했는데,
    회당 상한이 7건으로 줄면서 5분 전 속보가 11시간 된 기사 뒤에 밀려
    12시간 제한에 걸려 버려지는 일이 생긴다. 뉴스 채널에는 최신이 먼저다.
    """
    hint = item.region_hint or ""
    if "리서치" in hint:
        tier = 2
    elif "/규제" in hint or "정책" in hint:
        tier = 0
    else:
        tier = 1
    return (tier, -(item.published_at or 0))


def dedupe_items(items: list[NewsItem]) -> list[NewsItem]:
    """같은 중복제거 키를 가진 항목이 여러 수집기에서 들어오면 첫 번째만 남긴다.

    블록미디어 리서치 글은 일반 피드에도 실린다. 키를 공유시켜 두 번 발행되는 것은
    막았지만, 어느 쪽이 먼저 처리되느냐에 따라 탭이 달라진다.
    수집 순서(리서치 먼저)를 그대로 존중해 여기서 잘라낸다.
    """
    seen_keys, seen_urls, out = set(), set(), []
    for it in items:
        key = Store.make_key(it.source, it.unique_id)
        if key in seen_keys:
            continue
        # 같은 매체를 여러 피드로 등록하면(일반 + 규제 등) 같은 기사가 두 번 들어온다.
        # 키는 소스이름이 섞여 있어 못 잡으므로 URL 로 한 번 더 거른다.
        # 여기서 안 거르면 두 건 다 요약돼 무료 한도를 두 배로 태운다.
        url_key = normalize_url(it.url)
        if url_key and url_key in seen_urls:
            continue
        seen_keys.add(key)
        if url_key:
            seen_urls.add(url_key)
        out.append(it)
    dropped = len(items) - len(out)
    if dropped:
        print(f"[중복] 수집 단계에서 같은 글 {dropped}건 정리")
    return out


def publish_order(items: list[NewsItem]) -> list[NewsItem]:
    """발행 순서 = 과거 → 최신.

    텔레그램은 탭을 열면 맨 아래로 이동한다. 그러므로 **최신이 맨 아래**에
    있어야 열자마자 최신 기사가 보인다. 그래서 오래된 것부터 보낸다.

    실시간으로 들어오는 새 글도 자동으로 맨 아래에 붙으므로 이 순서가 유지된다.
    날짜 불명은 맨 뒤(최신 자리)로 보낸다.
    """
    return sorted(items, key=lambda i: i.published_at or float("inf"))


# 동시 요약 수.
# 예전엔 2였다 — 스로틀이 전역이라 늘려도 처리량이 안 늘고 429만 늘었기 때문이다.
# 이제 스로틀이 **모델별**이라(분당 15건/모델, 실측) 모델 수만큼 병렬이 의미가 있다.
# 모델 7개 × 분당 12건 = 이론상 84건/분. 그중 안전하게 일부만 쓴다.
SUMMARY_CONCURRENCY = int(os.getenv("SUMMARY_CONCURRENCY", "6"))

# 실행 예산 중 요약 단계에 쓸 비율. 나머지는 발행에 남긴다.
# 요약이 예산을 전부 먹으면 발행 루프가 통째로 밀려, 애써 요약한 결과를 버리게 된다.
SUMMARIZE_BUDGET_RATIO = 0.6

# 예산 초과·모델 소진으로 **손도 대지 않은** 항목. '요약 실패'(None)와 구분해야 한다.
# 실패는 '본 것'으로 찍고 넘어가지만, 이건 다음 실행이 다시 잡아야 한다.
SKIPPED = object()


def _judge(item: NewsItem, key: str, *, relevant: bool, score, topic,
           reason: str, dup: str | None = None, sent: bool = False,
           dry: bool = False):
    """판정 결과를 감사 로그에 남긴다 (§35).

    DRY_RUN 이어도 남긴다 — 실제 운영 전 검증이 이 표의 목적이다.
    LLM 원문 프롬프트나 시크릿은 절대 넣지 않는다 (§41).
    """
    try:
        store.log_judgment(
            key, title=item.title, source=item.source, url=item.url,
            relevant=relevant, score=score, primary_topic=topic,
            reason=reason, duplicate_event=dup, sent=sent)
    except Exception as e:                      # 로깅 실패가 발행을 막으면 안 된다
        print(f"[로그] 판정 기록 실패: {e}")


def _dry_print(data: dict, item: NewsItem, score: int):
    """DRY_RUN 출력 (§36)."""
    print("─" * 66)
    print(f"[DRY] {data.get('headline', '')[:60]}")
    print(f"      relevant=true  score={score}  "
          f"primary={data.get('primary_topic')}  "
          f"secondary={data.get('secondary_topics')}  "
          f"key_issue={data.get('is_key_issue')}")
    print(f"      요약: {(data.get('summary') or '')[:90]}")
    print(f"      why : {(data.get('why_it_matters') or '')[:90]}")
    print(f"      매체: {item.source}  |  {item.url[:60]}")


async def _summarize_one(item: NewsItem, recent: list | None = None) -> dict | None:
    """항목 1건을 알맞은 경로로 요약한다."""
    if item.deep:
        # 최상위 정책 이벤트(FOMC·연준 연설)는 bullet 10~16개짜리 심층 요약으로.
        data = await summarize_briefing(item)
    elif is_repost(item):
        posted = (
            datetime.fromtimestamp(item.published_at, KST).strftime("%Y-%m-%d %H:%M")
            if item.published_at else "불명"
        )
        data = await summarize_insight(item, posted)
    else:
        data = await summarize(item, recent)
    # 일본어·중국어 기사가 반쯤만 옮겨져 나가는 일이 잦다(`아바ランチ`, `무期限`).
    # 프롬프트로는 계속 새므로 코드로 한 번 더 강제한다.
    if data and not await lang.ensure(data):
        print(f"[skip] 한국어 표기 보정 실패: {item.title[:50]}")
        return None
    return data


async def _summarize_ahead(items: list[NewsItem],
                           deadline: float | None = None,
                           recent: list | None = None) -> list:
    """요약을 미리 병렬로 돌린다. 발행은 순서대로 해야 하므로 결과 순서는 유지한다.

    한 건씩 처리하면 건당 15~20초(이미지 읽기)가 그대로 쌓여 수십 건에 수십 분이 걸린다.
    발행 자체는 텔레그램 레이트리밋 때문에 순차로 남겨둔다.
    """
    sem = asyncio.Semaphore(SUMMARY_CONCURRENCY)
    skipped = 0

    async def one(it: NewsItem):
        nonlocal skipped
        async with sem:
            # 예산을 넘겼거나 오늘 쓸 모델이 없으면 손대지 않고 다음 실행으로 넘긴다.
            # 이 검사가 없어서 소진된 날 요약 단계가 40분씩 돌다 job 타임아웃(20분)에
            # 통째로 잘렸고, 그 바람에 발행도 이력 커밋도 못 한 채 9일을 헛돌았다.
            if (deadline and time.monotonic() > deadline) or summarizer.all_exhausted():
                skipped += 1
                return SKIPPED
            try:
                return await _summarize_one(it, recent)
            except Exception as e:
                print(f"[요약] 실패({it.title[:40]}): {e}")
                return None

    out = await asyncio.gather(*[one(i) for i in items])
    if skipped:
        print(f"[요약] {skipped}건은 시간/한도 부족 — 손대지 않고 다음 실행으로 넘김")
    return out


async def process_items(client: httpx.AsyncClient, items: list[NewsItem], warm: bool,
                        dry_run: bool = False):
    stats: dict[str, int] = {}
    # BSP 판정 카운터 (§35 집계 로깅)
    rejected = below = stored_only = clustered = 0
    event_index = events.EventIndex()
    key_issue_count = store.key_issues_since(time.time() - 24 * 3600)
    budget = settings.run_budget_sec
    started = time.monotonic()
    deferred = 0
    dup = 0
    dropped = 0
    covered = 0

    ordered = publish_order(dedupe_items(items))
    stale = 0

    # 아직 안 본 것만 추려 미리 요약해둔다(순서 유지). 오래된 건 요약도 하지 않는다.
    # URL 중복도 여기서 걸러야 한다. 발행 루프에서만 막으면 이미 요약이 끝난 뒤라
    # 무료 한도를 그냥 버리게 된다.
    pending = [i for i in ordered
               if not store.is_seen(Store.make_key(i.source, i.unique_id))
               and not store.is_url_seen(i.url)
               and not is_stale(i)
               and not prefilter.gate(i)]
    # ── 요약 순번(우선순위) ──
    # 무료 한도가 빡빡해 한 번에 몇 건밖에 못 돌린다. 그래서 **이 순서가 곧
    # 채널에 나가는 내용**이 된다. 발행 순서(과거→최신, RULES 1)와는 다른 얘기다.
    # 여기서 정하는 건 '무엇을 요약할 것인가'이고, 발행 순서는 아래 루프가 따로 지킨다.
    pending.sort(key=summarize_priority)
    if pending:
        head = pending[0]
        print(f"[우선순위] {len(pending)}건 대기 — 선두: {head.title[:40]}")
    if budget:
        # 시간 상한이 걸린 실행(CI)에서는 어차피 처리 못 할 분량까지 요약하면 낭비다.
        # 병목은 발행이 아니라 요약이다. summarizer 의 전역 스로틀(RATE_LIMIT_RPM)이
        # 호출 하나당 60/RPM 초를 강제하므로, 그 처리량으로 상한을 계산한다.
        # 처리량 = 동시 실행 수 × (분당 한도 / 60). 스로틀이 모델별이 된 뒤로는
        # 동시 실행이 실제로 처리량을 늘린다. 예전 식은 직렬 가정이라 과소평가했다.
        per_item = 60.0 / (summarizer.RATE_LIMIT_RPM * SUMMARY_CONCURRENCY)
        cap = max(1, int(budget * SUMMARIZE_BUDGET_RATIO / per_item))
        if len(pending) > cap:
            print(f"[요약] 시간 상한 고려해 {len(pending)}건 중 {cap}건만 처리")
            pending = pending[:cap]

    summaries: dict[str, dict | None] = {}
    # 발행 단계의 마감시각. 요약이 끝난 뒤 다시 세팅된다(아래 참고).
    publish_until = (started + budget) if budget else None
    if pending and not warm:
        print(f"[요약] {len(pending)}건 병렬 처리 시작 (동시 {SUMMARY_CONCURRENCY})")
        deadline = (started + budget * SUMMARIZE_BUDGET_RATIO) if budget else None
        # 이미 발행한 글 목록. 모델이 "이거 이미 나갔다"를 판정하는 근거다.
        recent = store.recent_for_dedup(hours=12, limit=10)
        t0 = time.monotonic()
        results = await _summarize_ahead(pending, deadline, recent)
        took = time.monotonic() - t0
        # 손대지 않은 항목은 아예 담지 않는다 → 아래에서 '다음 실행으로' 처리된다.
        summaries = {Store.make_key(i.source, i.unique_id): d
                     for i, d in zip(pending, results) if d is not SKIPPED}
        print(f"[요약] 완료 — 유효 {sum(1 for d in results if d and d is not SKIPPED)}건"
              f" ({took:.0f}초)")
        # 요약이 예산을 넘겨 끝나는 일이 잦다 — 모델이 쉬는 동안 진행 중인 호출이 남는다.
        # 그때 발행 루프가 곧바로 시간 초과가 되면 **요약에 쓴 한도가 통째로 버려진다.**
        # (실측: 유효 18건을 만들어놓고 발행 0건.) 그래서 발행에는 별도 시계를 준다.
        if budget:
            publish_until = time.monotonic() + budget * (1 - SUMMARIZE_BUDGET_RATIO)
            if took > budget:
                print(f"[요약] 예산({budget}초) 초과 — 발행에 "
                      f"{budget * (1 - SUMMARIZE_BUDGET_RATIO):.0f}초 따로 배정")

    for item in ordered:
        # 시간 상한을 넘으면 남은 항목은 손대지 않고 다음 실행으로 넘긴다.
        # '본 것으로 표시'를 하기 전에 끊어야 발행 없이 유실되는 항목이 생기지 않는다.
        if publish_until and time.monotonic() > publish_until:
            deferred += 1
            continue

        key = Store.make_key(item.source, item.unique_id)
        if store.is_seen(key):
            continue

        # 소스 이름이 달라 키는 새것이지만 이미 처리한 기사일 수 있다
        # (같은 매체의 일반 피드 / 규제 피드에 같은 글이 실리는 경우).
        # '본 것'으로만 찍고 넘어가 다시 수집되지 않게 한다.
        if store.is_url_seen(item.url):
            if not dry_run:
                store.mark_seen(key, item.source, item.title)
            dup += 1
            continue

        # 이번 실행에서 요약하지 못한 항목(시간 예산 초과·모델 소진·처리량 상한)은
        # 손대지 않는다. '본 것'으로 찍어버리면 발행되지 않은 채 영영 사라진다.
        # 오래된 기사는 해당 없다 — 그건 아래에서 기록하고 버리는 게 맞다.
        outdated = is_stale(item)
        # 요약하지 않고 '본 것'으로만 기록하는 것들. 모델을 부르지 않는다.
        #   종합지에서 온 크립토 무관 기사
        #   가격·시황 기사 — 이 채널의 주제가 아닌데 요약 한도를 먹는다
        #     (실측 2026-09-03: 발행 78건에 요약 호출 500건이 나가 한도를 소진했다)
        gate_reason = prefilter.gate(item)
        offtopic = gate_reason is not None
        if budget and not warm and not outdated and not offtopic and key not in summaries:
            deferred += 1
            continue

        if not dry_run:
            store.mark_seen(key, item.source, item.title)
            store.mark_url_seen(item.url, item.source, item.title)
        if warm:
            continue

        # 오래된 기사는 발행하지 않는다(위에서 '본 것'으로 기록했으니 다시 잡히지 않는다).
        if outdated:
            stale += 1
            continue
        if offtopic:
            _judge(item, key, relevant=False, score=None, topic=None,
                   reason=gate_reason, dry=dry_run)
            dropped += 1
            continue

        # 미리 병렬로 돌려둔 결과를 쓴다. 없으면(단건 경로) 그 자리에서 처리.
        data = summaries[key] if key in summaries else await _summarize_one(item)
        if data is None:
            _judge(item, key, relevant=False, score=None, topic=None,
                   reason="요약 실패 또는 파싱 불가", dry=dry_run)
            print(f"[skip] 요약 실패: {item.title[:60]}")
            continue

        # ── STEP 4 결과: 관련성 (§4) ──
        if not data.get("relevant"):
            why = data.get("_reject_reason") or data.get("relevance_reason") or "무관"
            _judge(item, key, relevant=False, score=None, topic=None,
                   reason=why, dry=dry_run)
            rejected += 1
            print(f"[REJECTED] {item.title[:52]} — {why[:40]}")
            continue

        score = data.get("strategic_score", 0)
        topic = _topics.normalize_topic(data.get("primary_topic"))
        data["primary_topic"] = topic

        # ── STEP 7 결과: Threshold (§19) ──
        if score < settings.discard_threshold:
            _judge(item, key, relevant=True, score=score, topic=topic,
                   reason=f"점수 미달(<{settings.discard_threshold})", dry=dry_run)
            below += 1
            print(f"[REJECTED] score {score} {item.title[:48]}")
            continue
        if score < settings.general_topic_threshold:
            # DB 에는 남기되 텔레그램에는 게시하지 않는다(§19).
            _judge(item, key, relevant=True, score=score, topic=topic,
                   reason=f"게시 문턱 미달(<{settings.general_topic_threshold}) — 저장만",
                   dry=dry_run)
            stored_only += 1
            print(f"[저장만] score {score} {item.title[:48]}")
            continue

        # ── STEP 5: Event Deduplication / Clustering (§21) ──
        title_for_event = data.get("title_ko") or item.title
        dup_fp = event_index.match(data, title_for_event)
        cluster_id = event_index.add(data, title_for_event, item.source,
                                     len(item.body or ""))
        material = events.looks_material(title_for_event, data)

        if dup_fp and not material:
            # 같은 사건을 이번 실행에서 이미 잡았다. 대표기사만 내보낸다.
            if not event_index.is_representative(cluster_id, title_for_event):
                _judge(item, key, relevant=True, score=score, topic=topic,
                       reason="동일 Event — 대표기사 아님", dup=cluster_id, dry=dry_run)
                clustered += 1
                print(f"[중복Event] {item.title[:48]}")
                continue

        prior = store.cluster_seen(cluster_id, events.EVENT_WINDOW_SEC)
        if prior and not material:
            # 이미 발행한 사건이고 새 사실도 없다.
            _judge(item, key, relevant=True, score=score, topic=topic,
                   reason=f"이미 발행한 Event({prior[0][:30]})", dup=cluster_id,
                   dry=dry_run)
            clustered += 1
            print(f"[중복Event] 기발행: {item.title[:44]}")
            continue
        if prior and material:
            data["update_note"] = data.get("update_note") or \
                f"앞서 전한 '{prior[0][:40]}' 건의 후속입니다."

        # ── STEP 6: Topic Routing (§6) ──
        # 일반 기사는 Primary Topic 하나에만 게시한다. 여러 탭에 복제하지 않는다.
        data["category"] = topic
        data["headline"] = data.get("title_ko") or item.title
        data["lede"] = data.get("summary") or ""
        if (data.get("confidence") or 1.0) < 0.5:
            data["_low_confidence"] = True      # §31

        stats[topic] = stats.get(topic, 0) + 1
        annotate_origin(data, item)

        # ── STEP 8: Telegram Delivery ──
        if dry_run or settings.dry_run:
            _dry_print(data, item, score)
            _judge(item, key, relevant=True, score=score, topic=topic,
                   reason="DRY_RUN", dup=cluster_id, dry=True)
            continue

        msg_id = await publish(client, data, item.url,
                               image_url=item.image_url, image=item.image)
        if msg_id:
            _, origin = publisher.origin_of(data)
            ids = [i for i in data.get("_message_ids", []) if i != msg_id]

            # 🚨 주요이슈 — 집계 토픽이므로 원 토픽과 중복 게시가 허용된다 (§6, §20).
            # 이미 렌더된 본문을 그대로 재사용하므로 모델을 다시 부르지 않는다.
            mirror_ids = []
            if data.get("is_key_issue") and score >= settings.key_issue_threshold:
                if key_issue_count < settings.key_issue_daily_cap:
                    ktid = topics_thread_id("key_issues")
                    body = data.get("_rendered", "")
                    if ktid and body:
                        mid2 = await publisher.send_raw(client, body, ktid)
                        if mid2:
                            mirror_ids.append(mid2)
                            key_issue_count += 1
                            print(f"[주요이슈] 추가 게시 ({key_issue_count}/"
                                  f"{settings.key_issue_daily_cap}) "
                                  f"{data['headline'][:34]}")
                else:
                    print(f"[주요이슈] 일일 상한 {settings.key_issue_daily_cap} 도달 — "
                          f"추가 게시 생략: {data['headline'][:34]}")

            store.record_published(key, msg_id, topics_thread_id(topic),
                                   origin or item.url, data["headline"],
                                   category=topic, lede=data.get("lede", ""),
                                   text=data.get("_rendered", ""), extra_ids=ids,
                                   photo_file_id=data.get("_photo_file_id", ""),
                                   origin_at=item.published_at,
                                   mirror_ids=mirror_ids)
            store.save_bsp_fields(key, data, cluster_id=cluster_id,
                                  canonical=normalize_url(item.url),
                                  collected_at=time.time())
            _judge(item, key, relevant=True, score=score, topic=topic,
                   reason=data.get("relevance_reason", ""), dup=cluster_id,
                   sent=True, dry=False)
        await asyncio.sleep(5)  # 항목당 사진+본문 2건이 나가므로 여유를 둔다

    total = sum(stats.values())
    if total:
        dist = "  ".join(f"{k}:{v}" for k, v in stats.items())
        print(f"\n[집계] 발행대상 {total}건 — {dist}")
    if deferred:
        print(f"[집계] 시간 상한({budget}초) 도달 — {deferred}건은 다음 실행으로 미룸")
    if stale:
        print(f"[집계] 과거 기사 {stale}건 제외 "
              f"(일반 {settings.max_age_hours}시간 / 규제 {settings.regulation_max_age_hours}시간)")
    if dup:
        print(f"[집계] 다른 피드로 이미 처리한 같은 기사 {dup}건 제외")
    if dropped:
        print(f"[집계] STEP2 사전필터로 걸러낸 기사 {dropped}건 (모델 호출 안 함)")
    if covered:
        print(f"[집계] 이미 발행한 글에 내용이 다 들어 있어 제외 {covered}건")
    if rejected:
        print(f"[집계] 관련 없음(relevant=false) {rejected}건")
    if below:
        print(f"[집계] 점수 미달(<{settings.discard_threshold}) {below}건")
    if stored_only:
        print(f"[집계] 저장만 하고 게시 안 함"
              f"(<{settings.general_topic_threshold}) {stored_only}건")
    if clustered:
        print(f"[집계] 동일 Event 로 묶여 제외 {clustered}건")


async def recent_tg_web(client: httpx.AsyncClient, hours: int = 6) -> list[NewsItem]:
    """공개 채널의 최근 글. 상시 수집용이라 짧은 구간만 본다."""
    if not settings.tg_web_channels:
        return []
    since = datetime.now(tz=KST).timestamp() - hours * 3600
    items: list[NewsItem] = []
    for ch in settings.tg_web_channels:
        items += await tg_web.fetch_since(client, ch, since, max_pages=2)
    return items


async def collect_all(client: httpx.AsyncClient) -> list[NewsItem]:
    """STEP 1 — Article Collection.

    크립토 봇의 거래소 공지 수집기(binance·upbit·bithumb·coin68·blockmedia)는
    BSP 와 무관하므로 부르지 않는다. 모듈은 지우지 않고 남겨 둔다 —
    되돌릴 때 필요하고, 부르지 않으면 비용이 0이다.
    """
    items: list[NewsItem] = []
    # 직접 RSS — 공식 1차 출처와 종합 경제지
    items += await rss.fetch_all(client, settings.rss_sources)
    # Candidate Retrieval — 구글뉴스 검색 피드 (Entity + Event 조합, §15)
    items += await regulation.fetch_all(client, settings.regulation_sources)
    # 텔레그램 소스 채널(설정했을 때만)
    if settings.tg_web_channels:
        items += await recent_tg_web(client)
    if settings.tg_source_channels and settings.tg_api_id:
        items += await telegram_channels.fetch()
    return items


async def exchange_loop(client: httpx.AsyncClient):
    """크립토 거래소 공지 전용 레인. BSP 에서는 쓰지 않는다 (호출되지 않음)."""
    return
    while True:
        items = []
        items += await binance.fetch(client)
        items += await upbit.fetch(client)
        await process_items(client, items, warm=False)
        await asyncio.sleep(settings.poll_exchange_sec)


async def rss_loop(client: httpx.AsyncClient):
    while True:
        items = await blockmedia_research.fetch(client)
        items += await rss.fetch_all(client, settings.rss_sources)
        items += await telegram_channels.fetch()
        await process_items(client, items, warm=False)
        await asyncio.sleep(settings.poll_rss_sec)


async def main():
    warm_only = "--warm" in sys.argv
    once = "--once" in sys.argv
    since = _arg_value("--since")
    dry_run = "--dry-run" in sys.argv

    tg_since = _arg_value("--tg-since")
    do_digest = "--digest" in sys.argv
    digest_hours = int(_arg_value("--hours") or 1)

    async with httpx.AsyncClient() as client:
        # ☀️ Morning Brief (§23). 06:55 KST 에 스케줄러가 부른다.
        if "--brief" in sys.argv:
            import brief
            await brief.run(client, store,
                            dry_run=True if dry_run else None)
            return

        if do_digest:
            # 크립토 봇의 시간별 다이제스트. BSP 에서는 Morning Brief 가 그 자리를
            # 대신한다. 모듈은 남겨 두되 기본 경로에서는 부르지 않는다.
            import digest
            await digest.run(client, store, hours=digest_hours, dry_run=dry_run)
            return

        if "--purge" in sys.argv:
            import purge
            await purge.run(client, store, only=_arg_value("--only"), dry_run=dry_run)
            return

        if "--resort" in sys.argv:
            import resort
            await resort.run(client, store, only=_arg_value("--only"), dry_run=dry_run)
            return

        if "--reroute" in sys.argv:
            import reroute
            only = _arg_value("--only")
            await reroute.run(client, store, dry_run=dry_run,
                              only={c.strip() for c in only.split(",")} if only else None)
            return

        if tg_since:
            # 텔레그램 소스 채널만 백필. 트위터 캡처를 비전 모델로 읽어 인사이트로 발행한다.
            start = datetime.strptime(tg_since, "%Y-%m-%d").replace(tzinfo=KST).timestamp()
            print(f"[TG백필] {tg_since} 00:00 KST 이후 채널 글 수집")
            items = []
            for ch in settings.tg_web_channels:
                items += await tg_web.fetch_since(client, ch, start)
            if not items:
                # 공개 채널이 아니면 웹 미리보기가 비어 나온다 → 개인 계정 경로로 재시도
                items = await telegram_channels.fetch_since(start)
            items = publish_order(items)   # 과거 → 최신 순 발행
            withpic = sum(1 for i in items if i.image)
            print(f"[TG백필] 대상 {len(items)}건 (이미지 포함 {withpic}건)"
                  f"{' — dry-run: 발행하지 않음' if dry_run else ''}")
            await process_items(client, items, warm=False, dry_run=dry_run)
            return

        if since:
            print(f"[백필] {since} 00:00 KST 이후 뉴스 수집")
            items = await collect_all(client)
            # RSS는 최신 몇 건만 주므로 블록미디어는 사이트맵으로 당일 전체를 보강
            items += await blockmedia_archive.fetch_date(client, since)
            items = filter_since(items, since)
            items = publish_order(items)   # 과거 → 최신 순 발행
            print(f"[백필] 대상 {len(items)}건"
                  f"{' (dry-run: 발행하지 않음)' if dry_run else ''}")
            await process_items(client, items, warm=False, dry_run=dry_run)
            return

        if warm_only:
            items = await collect_all(client)
            await process_items(client, items, warm=True)
            print(f"[warm] {len(items)}건 등록 완료. 이제 python main.py 로 실행하세요.")
            return

        if "--urgent" in sys.argv:
            # 긴급 레인: 지표 발표·FOMC·잭슨홀 등 늦으면 가치가 없어지는 건만
            # 짧은 주기로 잡는다. 전체 스윕(--once)과 분리돼 있다.
            import urgent
            items = await urgent.collect(client)
            if items:
                for i in items:
                    print(f"  · {i.title[:58]}  [{i.region_hint}]")
            await process_items(client, items, warm=False, dry_run=dry_run)
            return

        if once:
            # GitHub Actions 등 스케줄러에서 주기적으로 호출하는 모드: 1회 수집 후 종료
            items = await collect_all(client)
            print(f"[once] {len(items)}건 수집"
                  f"{' — dry-run: 발행하지 않음' if dry_run else ''}")
            # dry_run 을 넘기지 않아 `--once --dry-run` 이 실제로 발행해버렸다.
            # 진단하려고 돌린 명령이 채널에 글을 올리면 안 된다.
            await process_items(client, items, warm=False, dry_run=dry_run)
            print("[once] 완료")
            return

        if settings.use_topics:
            import topics

            mapping = topics._load()
            if not mapping:
                print("[경고] USE_TOPICS=true 인데 topics.json 이 없습니다. "
                      "먼저 python setup_topics.py 를 실행하세요.")
            else:
                print(f"[topics] 탭 라우팅 활성: {mapping}")

        await asyncio.gather(
            exchange_loop(client),
            rss_loop(client),
        )


if __name__ == "__main__":
    asyncio.run(main())
