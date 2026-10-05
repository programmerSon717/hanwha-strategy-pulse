"""Strategy Pulse 설정. 환경변수 기반(.env 또는 시스템 환경변수).

한화생명 A팀(Business Strategy & Planning) 전용 Strategic Intelligence Agent.
CryptoNews Bot 의 검증된 엔진을 그대로 쓰고 뉴스 universe / taxonomy / threshold 만 교체했다.
"""
import json
import os
from dataclasses import dataclass, field
from urllib.parse import quote

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ─────────────────────────────────────────────────────────────
# Google News 검색 피드 헬퍼
#
# 국내 금융·딜 전문지(더벨·인베스트조선·연합인포맥스 등)는 자체 RSS 가 없거나
# 막혀 있다. 크립토 봇에서 쓰던 것과 같은 방식으로 구글뉴스 검색 피드를 쓴다.
# (2026-09-23 실측: site: 검색 3종 모두 100건 정상 수신)
# ─────────────────────────────────────────────────────────────
def _int_or_none(v) -> int | None:
    """빈 문자열·미설정·0 을 전부 None 으로. 아직 안 채운 .env 로도 import 가 되어야 한다."""
    try:
        return int(str(v).strip()) or None
    except (TypeError, ValueError):
        return None


def gnews(query: str) -> str:
    return ("https://news.google.com/rss/search?q=" + quote(query)
            + "&hl=ko&gl=KR&ceid=KR%3Ako")


@dataclass
class Settings:
    # ── 정체성 (§40) ──
    bot_name: str = os.getenv("BOT_NAME", "Strategy Pulse")
    timezone: str = os.getenv("TIMEZONE", "Asia/Seoul")

    # ── Telegram ──
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_channel_id: str = os.getenv("TELEGRAM_CHANNEL_ID", "")

    # ── LLM (Gemini 무료 티어) ──
    # flash-lite 가 일일 한도 500건으로 가장 크다. 나머지 flash 계열은 20건/일이라
    # 주 모델로 쓰면 하루 20건 만에 멈춘다. (summarizer.FALLBACK_MODELS 참고)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    # ── 폴링 주기(초) ──
    poll_exchange_sec: int = int(os.getenv("POLL_EXCHANGE_SEC", "300"))
    poll_rss_sec: int = int(os.getenv("POLL_RSS_SEC", "420"))

    # ── DRY RUN (§36) ──
    # true 면 텔레그램으로 보내지 않고 판정 결과만 출력한다.
    # 실운영 전환 전 검증용. 기본값을 true 로 둔다 — 실수로 발행되지 않게.
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    # ── Strategic Relevance Threshold (§19) ──
    # score < discard_threshold           → 버린다
    # discard ~ general 미만               → DB 에만 저장, 텔레그램 게시 안 함
    # general_topic_threshold 이상         → 일반 Topic 게시
    # important_threshold 이상             → 중요 기사
    # key_issue_threshold 이상             → 🚨 주요이슈 후보 (is_key_issue 별도 판정)
    discard_threshold: int = int(os.getenv("DISCARD_THRESHOLD", "50"))
    general_topic_threshold: int = int(os.getenv("GENERAL_TOPIC_THRESHOLD", "65"))
    important_threshold: int = int(os.getenv("IMPORTANT_THRESHOLD", "80"))
    key_issue_threshold: int = int(os.getenv("KEY_ISSUE_THRESHOLD", "85"))
    # 하루에 주요이슈가 과도하게 많아지는 것을 방지한다(§20).
    key_issue_daily_cap: int = int(os.getenv("KEY_ISSUE_DAILY_CAP", "6"))

    # ── Morning Brief (§23) ──
    daily_brief_time: str = os.getenv("DAILY_BRIEF_TIME", "07:00")
    # 📌 A팀 Top10 발행 시각. Morning Brief 보다 10분 앞선다.
    #
    # 06:55 → 06:50 (2026-10-05 사용자 지정). **이 값만 바꿔선 그 시각에 안 나간다.**
    # 실제 발행은 bot.yml 의 상시 루프가 --if-due 로 물어볼 때 일어나므로,
    # 물어보는 주기가 곧 오차다. 20분 주기였을 때 06:49 회차를 놓치고
    # 다음 07:09 회차에 잡혀 07:10 에 나갔다(2026-10-03 실측).
    # 그래서 bot.yml 이 이 시각에 **정확히 깨어나도록** 함께 고쳤다.
    cs_top10_time: str = os.getenv("CS_TOP10_TIME", "06:50")
    # 발행문에 찍히는 꼬리표. **기본값은 중립이고, 실제 표기는 환경변수로 넣는다.**
    # 이 저장소는 공개돼 있어 조직 이름을 코드에 박으면 그대로 드러난다.
    # 로컬은 .env(커밋 안 됨), Actions 는 Secret 으로 준다.
    # 없어도 발행은 정상이다 — 꼬리표만 중립으로 나간다.
    # 일반 탭 게시의 **발주 기준 하한**(csfit 적합도).
    #
    # 20 인 이유: 실측 144건에 대보면 20 미만은 csfit RULES 11개 범주 중
    # **어느 하나도 안 걸리는** 14건과 정확히 일치한다(점수 17~18, 전부
    # strategic_score×0.25 만으로 만들어진 값). 퇴직연금·은행 해킹보안처럼
    # 발주 키워드에 근거가 없는 기사들이다.
    #
    # 30(FLOOR_MIN_SCORE)으로 올리면 28건이 더 잘리는데, 그중에는 메트라이프
    # K-ICS 자본관리·하나금융 자사주 소각·최태원 지배력 방어처럼 **기준에 맞는**
    # 기사가 섞여 있다. 그래서 Top10 의 최소배정 하한(30)과 일부러 다르게 둔다 —
    # 일반 탭은 넓게 담고, Top10 에서 좁히는 구조다.
    general_fit_threshold: int = int(os.getenv("GENERAL_FIT_THRESHOLD", "20"))

    cs_top10_label: str = os.getenv("CS_TOP10_LABEL", "Top10")
    cs_links_label: str = os.getenv("CS_LINKS_LABEL", "top10(링크용)")
    # Top10 을 뽑는 시간 창(시간). 평소에는 24 가 맞다 — '어제 아침부터 지금까지'.
    #
    # **봇이 며칠 멈췄다 살아났을 때는 넓혀야 한다.** 멈춘 동안 수집·발행이
    # 없었으니 24시간 창에 후보가 10여 건밖에 안 들어오고, 같은 사건 접기와
    # 기게재 제외를 거치면 4건으로 줄어 Top10 이 성립하지 않는다(2026-10-05 실측:
    # 24h 창 16건 → 선정 4건 / 72h 창 57건). 그때만 CS_TOP10_WINDOW_HOURS=72 로
    # 한 번 돌리면 된다. 멈춘 기간의 뉴스는 팀에 공유된 적이 없으니 중복도 아니다.
    cs_top10_window_hours: int = int(os.getenv("CS_TOP10_WINDOW_HOURS", "24"))
    # 창의 **끝** 시각. 발행(06:50)보다 50분 앞선다 (2026-10-05 사용자 지정:
    # "전날 6:50am~당일 06:00am"). 그 50분은 기사를 고르고 Instant View 를
    # 만드는 시간이다 — 발행 직전까지 들어오는 기사를 쫓으면 06:50 을 놓친다.
    cs_top10_window_end: str = os.getenv("CS_TOP10_WINDOW_END", "06:00")

    # 사전 생성 시각. 다음날 06:50 에 내보낼 것을 미리 만들어 둔다.
    # 뒤에 만든 것이 더 기준에 맞으면 앞서 만든 것을 **대체**한다.
    # 목적은 품질 향상과, 06:50 에 "이상하게 만들어지거나·전날자를 못 가져오거나
    # ·10건이 안 되는" 사고를 미리 잡는 것이다(2026-10-05 사용자 지정).
    cs_top10_draft_times: list = field(default_factory=lambda: [
        t.strip() for t in
        os.getenv("CS_TOP10_DRAFT_TIMES", "18:00,22:00,04:00").split(",")
        if t.strip()
    ])
    # 10건이 안 차면 창을 **뒤로** 넓혀 가며 다시 뽑는다 (2026-10-05 사용자 지정).
    #
    #   "월요일 06:50 에 나가야 하는 게 10건인데(일요일 기사 수집) 10건이 안 되면
    #    토요일 저녁~오후 것을 가져와서 10건 맞춰야 해. 단 기준은 항상 유지."
    #
    # 주말·연휴는 기사도 적지만 **사건 수**가 더 적다. 10/4 분이 후보 15건에서
    # 선정 6건에 그친 것도 기사가 없어서가 아니라 서로 다른 사건이 6개뿐이어서였다.
    # 창을 넓히면 앞선 날의 다른 사건이 들어와 10건이 찬다.
    #
    # **기준은 그대로다.** 넓어진 창에도 같은 사건 접기·기게재 제외·범주 상한·
    # 유료 차단·본문 확보 검사가 전부 그대로 걸린다. 넓히는 것은 '어디까지 볼지'
    # 뿐이고, '무엇을 고를지'는 건드리지 않는다.
    # 모자라면 **하루씩 앞으로** 가며 부족분만 채운다. 창을 넓혀 다시 뽑지 않는다
    # (다시 뽑으면 범주 상한 경쟁이 달라져 오히려 줄었다 — cstop10.topup 주석).
    # 3 → 1 (2026-10-05 지적). 3일을 열어 두니 10/4 판에 10/1 기사가 실렸다.
    #
    # 사용자가 허용한 범위는 "월요일 분이 일요일 기사로 안 차면 **토요일**
    # 저녁·오후 것" 즉 **바로 전날 하루**까지다. 그 이상 거슬러 가면 이미
    # 팀이 며칠 전에 본 뉴스가 오늘 Top10 에 올라온다.
    cs_top10_fill_days: int = int(os.getenv("CS_TOP10_FILL_DAYS", "1"))
    daily_brief_count: int = int(os.getenv("DAILY_BRIEF_COUNT", "10"))
    # 같은 회사/같은 사건이 브리프를 독식하지 않게 하는 상한 (§24-6)
    daily_brief_max_per_entity: int = int(os.getenv("DAILY_BRIEF_MAX_PER_ENTITY", "4"))

    # ── 구(舊) 크립토 봇 호환 필드 ──
    # summarizer/main 이 아직 참조한다. BSP 에서는 strategic_score 가 실제 게이트이고
    # 이 값들은 사실상 통과용이다. 제거하면 엔진 코드를 건드려야 해 그대로 둔다.
    min_importance: int = int(os.getenv("MIN_IMPORTANCE", "1"))
    policy_min_importance: int = int(os.getenv("POLICY_MIN_IMPORTANCE", "1"))

    # ── 기사 시간 Window (§22) ──
    # 금융·딜 기사는 크립토보다 수명이 길다. 오전 브리프가 전날 저녁 기사를
    # 담아야 하므로 24시간으로 잡는다.
    max_age_hours: int = int(os.getenv("MAX_AGE_HOURS", "24"))
    # 규제·정책은 더 길게. 시행령·감독규정은 며칠에 걸쳐 후속보도가 나온다.
    regulation_max_age_hours: int = int(os.getenv("REGULATION_MAX_AGE_HOURS", "72"))

    run_budget_sec: int = int(os.getenv("RUN_BUDGET_SEC", "0"))
    db_path: str = os.getenv("DB_PATH", "botstate.sqlite3")

    # telegra.ph access_token.
    #
    # **DB(botstate.sqlite3) 에 넣지 않는다.** 그 파일은 커밋되므로
    # 레포가 퍼블릭이 되는 순간 토큰이 그대로 공개된다(2026-10-05 전환).
    # 비어 있어도 봇은 돈다 — telegraph.get_token 이 1회용 계정을 만든다.
    # 다만 그러면 매 실행마다 계정이 새로 생기므로 넣어 두는 게 낫다.
    telegraph_token: str = os.getenv("TELEGRAPH_TOKEN", "")

    # ── Forum Topic (§33) ──
    # message_thread_id 는 코드에 하드코딩하지 않는다. .env 에서 읽는다.
    use_topics: bool = os.getenv("USE_TOPICS", "false").lower() == "true"
    topics_file: str = os.getenv("TOPICS_FILE", "topics.json")
    topics_def_file: str = os.getenv("TOPICS_DEF_FILE", "bsp_topics_def.json")
    entities_file: str = os.getenv("ENTITIES_FILE", "bsp_entities.json")

    # 토픽 id → .env 변수명. setup_topics.py 가 출력한 값을 여기에 넣는다.
    topic_thread_ids: dict = field(default_factory=lambda: {
        tid: _int_or_none(os.getenv(env))
        for tid, env in (
            ("hanwha_group",        "TG_TOPIC_HANWHA_GROUP"),
            ("ma_governance",       "TG_TOPIC_MA_GOVERNANCE"),
            ("insurance_finance",   "TG_TOPIC_INSURANCE_FINANCE"),
            ("regulation_policy",   "TG_TOPIC_REGULATION_POLICY"),
            ("competitors_bigtech", "TG_TOPIC_COMPETITORS_BIGTECH"),
            ("digital_newbiz",      "TG_TOPIC_DIGITAL_NEWBIZ"),
            ("global_finance",      "TG_TOPIC_GLOBAL_FINANCE"),
            ("key_issues",          "TG_TOPIC_KEY_ISSUES"),
            ("daily_brief",         "TG_TOPIC_DAILY_BRIEF"),
            ("cs_top10",            "TG_TOPIC_CS_TOP10"),
            ("cs_top10_links",      "TG_TOPIC_CS_TOP10_LINKS"),
        )
    })

    # ── 텔레그램 소스 채널 (선택) ──
    tg_web_channels: list = field(default_factory=lambda: [
        c.strip() for c in os.getenv(
            "TG_WEB_CHANNELS", os.getenv("TG_SOURCE_CHANNELS", "")
        ).split(",") if c.strip()
    ])
    tg_api_id: str = os.getenv("TG_API_ID", "")
    tg_api_hash: str = os.getenv("TG_API_HASH", "")
    tg_source_channels: list = field(default_factory=lambda: [
        c.strip() for c in os.getenv("TG_SOURCE_CHANNELS", "").split(",") if c.strip()
    ])

    # ─────────────────────────────────────────────────────────
    # Source Quality Weight (§16)
    #
    # 절대적 진실도가 아니라 금융 전문성·Deal 전문성·원문 정보량·공식 여부·
    # 재보도 여부를 반영한 상대 가중치다. Strategic Score 의 Source Quality
    # 항목(0~5)을 계산할 때 쓴다.
    #   5 = 공식 1차 출처 (당국 보도자료·공시)
    #   4 = 딜/금융 전문지 원문 리포팅
    #   3 = 종합 경제지
    #   2 = 일반 매체·재보도
    # ─────────────────────────────────────────────────────────
    source_weights: dict = field(default_factory=lambda: {
        "금융위원회": 5, "금융감독원": 5, "공정거래위원회": 5, "한국은행": 5,
        "DART 공시": 5, "회사 IR": 5,
        "더벨": 4, "인베스트조선": 4, "연합인포맥스": 4, "보험저널": 4,
        "연합뉴스": 3, "한국경제": 3, "이데일리": 3, "매일경제": 3,
        "서울경제": 3, "파이낸셜뉴스": 3, "조선비즈": 3, "머니투데이": 3,
        "Reuters": 4, "Bloomberg": 4, "Financial Times": 4, "Nikkei": 3,
        "_default": 2,
    })

    # ─────────────────────────────────────────────────────────
    # STEP 1 — Article Collection: 직접 RSS
    # (이름, URL, 기본 분류 힌트)
    # 2026-09-23 수신 검증 완료된 피드만 등록했다.
    # 죽은 피드(매일경제·서울경제·파이낸셜뉴스·금감원·공정위 직접 RSS)는
    # 아래 regulation_sources 의 구글뉴스 site: 검색으로 대체했다.
    # ─────────────────────────────────────────────────────────
    rss_sources: list = field(default_factory=lambda: [
        # 공식 1차 출처
        ("금융위원회", "http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111", "규제"),
        # 종합 경제지
        ("연합뉴스", "https://www.yna.co.kr/rss/economy.xml", "경제"),
        # 한국경제·이데일리 직접 RSS 는 뺐다 (2026-09-23 실측).
        #   한국경제  → httpx 로는 403. curl 로는 200 이라 UA 문제가 아니라
        #              TLS 지문 차단으로 보인다. Accept 헤더를 붙여도 그대로 403.
        #   이데일리  → httpx 에서 ConnectError (호스트 연결 자체가 안 됨)
        # 둘 다 아래 regulation_sources 의 구글뉴스 site: 검색으로 대체했다.
        # 보험 전문지
        ("보험저널", "https://www.insjournal.co.kr/rss/allArticle.xml", "보험"),
    ])

    # ─────────────────────────────────────────────────────────
    # STEP 1 — Candidate Retrieval: 구글뉴스 검색 피드 (§15)
    #
    # 원칙: Keyword 검색 = 기사 후보 확보일 뿐, Keyword Match = 게시가 아니다.
    # 최종 Inclusion 은 STEP 4 Strategic Relevance Evaluation 에서 LLM 이 정한다.
    #
    # 단일 키워드보다 Entity + Event 조합이 훨씬 정확하다.
    # (실측: "한화생명" 단독 81건 vs "한화 인수 OR 매각" 33건 — 후자가 밀도가 높다)
    #
    # 형식은 크립토 봇의 regulation_sources 와 같다:
    #   (이름, 구글뉴스 검색어, 언어, 국가, ceid, 분류 힌트)
    # ─────────────────────────────────────────────────────────
    regulation_sources: list = field(default_factory=lambda: [
        # ── 딜·금융 전문지 (자체 RSS 없음 → site: 검색) ──
        ("더벨", "site:thebell.co.kr when:3d", "ko", "KR", "KR:ko", "딜"),
        ("인베스트조선", "site:investchosun.com when:3d", "ko", "KR", "KR:ko", "딜"),
        ("연합인포맥스", "site:einfomax.co.kr when:2d", "ko", "KR", "KR:ko", "금융"),
        # 직접 RSS 가 죽은 종합지 대체
        ("매일경제", "site:mk.co.kr 금융 OR 보험 OR 인수 when:2d", "ko", "KR", "KR:ko", "경제"),
        ("서울경제", "site:sedaily.com 금융 OR 보험 OR M&A when:2d", "ko", "KR", "KR:ko", "경제"),
        ("조선비즈", "site:biz.chosun.com 금융 OR 보험 OR 지배구조 when:2d", "ko", "KR", "KR:ko", "경제"),
        # 직접 RSS 가 막힌 두 매체 (위 rss_sources 주석 참고)
        ("한국경제", "site:hankyung.com 금융 OR 보험 OR 인수 OR 지배구조 when:2d",
         "ko", "KR", "KR:ko", "경제"),
        ("이데일리", "site:edaily.co.kr 금융 OR 보험 OR 인수 OR 지배구조 when:2d",
         "ko", "KR", "KR:ko", "경제"),

        # ── 🏢 한화그룹 (Entity + Event 조합) ──
        ("한화생명", "한화생명 when:2d", "ko", "KR", "KR:ko", "한화"),
        ("한화 금융계열사",
         '"한화손해보험" OR "한화투자증권" OR "한화자산운용" OR "캐롯손해보험" when:3d',
         "ko", "KR", "KR:ko", "한화"),
        ("한화 딜", "한화 인수 OR 매각 OR 합병 OR 지분 when:3d", "ko", "KR", "KR:ko", "한화"),
        ("한화 지배구조",
         "한화 지배구조 OR 승계 OR 계열분리 OR 지주사 OR 자사주 when:7d",
         "ko", "KR", "KR:ko", "한화"),
        ("한화 당국", "한화 공정위 OR 금융위 OR 금감원 OR 제재 when:7d", "ko", "KR", "KR:ko", "한화"),
        ("한화 해외", "한화 해외진출 OR 베트남 OR 인도네시아 OR 미국법인 when:7d",
         "ko", "KR", "KR:ko", "한화"),

        # ── 🤝 M&A · 지배구조 ──
        ("보험사 M&A", "보험사 인수 OR 매각 OR 실사 OR 우선협상 when:3d", "ko", "KR", "KR:ko", "딜"),
        ("금융사 M&A", "증권사 OR 저축은행 OR 자산운용사 인수 OR 매각 when:3d",
         "ko", "KR", "KR:ko", "딜"),
        ("지배구조", "지주사 전환 OR 계열분리 OR 공개매수 OR 경영권 분쟁 when:5d",
         "ko", "KR", "KR:ko", "지배구조"),
        ("주주환원", "자사주 소각 OR 주주환원 OR 밸류업 when:5d", "ko", "KR", "KR:ko", "지배구조"),
        ("PEF", "PEF OR 사모펀드 인수 OR 엑시트 OR 블라인드펀드 when:3d", "ko", "KR", "KR:ko", "딜"),

        # ── 🏦 보험 · 금융 ──
        ("보험 자본규제", "보험사 K-ICS OR 킥스 OR 지급여력 OR IFRS17 when:5d",
         "ko", "KR", "KR:ko", "보험"),
        ("보험 채널", "GA OR 보험대리점 OR 법인보험대리점 OR 설계사 when:3d",
         "ko", "KR", "KR:ko", "보험"),
        ("보험 자산운용", "보험사 자산운용 OR 대체투자 OR 해외투자 when:5d",
         "ko", "KR", "KR:ko", "보험"),
        ("퇴직연금", "퇴직연금 제도 OR 기금형 OR 디폴트옵션 when:5d", "ko", "KR", "KR:ko", "금융"),
        ("WM", "WM OR PB OR 패밀리오피스 OR 고액자산가 when:5d", "ko", "KR", "KR:ko", "금융"),

        # ── ⚖️ 규제 · 정책 ──
        ("금융위 정책", "금융위원회 보험 OR 자본시장 OR 지배구조 when:3d", "ko", "KR", "KR:ko", "규제"),
        ("금감원", "금융감독원 검사 OR 제재 OR 감독규정 when:3d", "ko", "KR", "KR:ko", "규제"),
        ("공정위", "공정거래위원회 대기업집단 OR 일감몰아주기 OR 부당지원 when:5d",
         "ko", "KR", "KR:ko", "규제"),
        ("금융법", "보험업법 OR 자본시장법 OR 금산분리 OR 의무공개매수 개정 when:7d",
         "ko", "KR", "KR:ko", "규제"),
        ("상법 개정", "상법 개정 이사 충실의무 OR 집중투표 when:7d", "ko", "KR", "KR:ko", "규제"),

        # ── 🔎 경쟁사 · Big Tech ──
        ("경쟁 보험사",
         '"삼성생명" OR "교보생명" OR "신한라이프" OR "KB라이프" OR "메리츠화재" when:2d',
         "ko", "KR", "KR:ko", "경쟁사"),
        ("금융지주", '"KB금융" OR "신한금융" OR "하나금융" OR "우리금융" 전략 OR 인수 when:3d',
         "ko", "KR", "KR:ko", "경쟁사"),
        ("빅테크 금융",
         "네이버 OR 카카오 OR 토스 금융 OR 보험 OR 라이선스 when:3d",
         "ko", "KR", "KR:ko", "빅테크"),

        # ── 💡 디지털 · 신사업 ──
        ("금융 AI", "보험 OR 금융 AI 도입 OR AI 에이전트 when:5d", "ko", "KR", "KR:ko", "디지털"),
        ("토큰증권", "토큰증권 OR STO OR 조각투자 제도 when:7d", "ko", "KR", "KR:ko", "디지털"),
        ("스테이블코인", "스테이블코인 은행 OR 금융회사 OR 제도 when:7d", "ko", "KR", "KR:ko", "디지털"),

        # ── 🌐 Global ──
        ("해외 보험 M&A",
         "insurance acquisition OR merger OR stake deal when:3d", "en-US", "US", "US:en", "글로벌"),
        ("베트남 금융", "Vietnam bank OR insurance acquisition OR license when:7d",
         "en-US", "US", "US:en", "글로벌"),
        ("인도네시아 금융", "Indonesia OJK bank OR insurance acquisition when:7d",
         "en-US", "US", "US:en", "글로벌"),
        ("동남아 금융", "Southeast Asia insurance OR bank expansion when:7d",
         "en-US", "US", "US:en", "글로벌"),
        ("한국계 해외진출", "한국 금융사 해외 진출 OR 현지법인 OR 인수 when:7d",
         "ko", "KR", "KR:ko", "글로벌"),

        # ── 2026-10-01 A팀 키워드 기준 보강 ──────────────────
        # 기존 쿼리가 덮지 못하던 것만 추가한다. 중복 쿼리는 수집량만 늘린다.
        # site:n.news.naver.com 은 쓰지 마라. 구글뉴스가 네이버 미러에서 제목을
        # 추출하지 못해 전부 빈 제목(" - n.news.naver.com")으로 온다. 제목이 없으면
        # 사전필터도 모델도 판단할 수 없다. (2026-10-01 실측)
        # A팀이 실제 공유하는 롱테일 매체(뉴스핌·뉴스토마토·더구루·경제타임스…)는
        # **site: 를 걸지 않은 키워드 쿼리**가 훨씬 잘 잡는다. 아래가 그 쿼리다.
        ("생보 3사", '"한화생명" OR "삼성생명" OR "교보생명" when:2d',
         "ko", "KR", "KR:ko", "보험"),
        ("보험 지배구조", "보험사 지분 OR 매각 OR 인수 OR 지주사 OR 자본확충 when:2d",
         "ko", "KR", "KR:ko", "보험"),
        ("빅테크 자회사",
         '"카카오페이" OR "카카오뱅크" OR "네이버페이" OR "네이버파이낸셜" OR "토스뱅크" when:3d',
         "ko", "KR", "KR:ko", "빅테크"),
        ("한화 오너", "김동관 OR 김동원 when:7d", "ko", "KR", "KR:ko", "한화"),
        ("삼성생명법", "삼성생명법 OR 보험업법 자산운용 비율 when:7d",
         "ko", "KR", "KR:ko", "규제"),
        ("주주행동", "경제개혁연대 OR 주주행동주의 OR 스튜어드십코드 when:7d",
         "ko", "KR", "KR:ko", "지배구조"),
        ("감사원", "감사원 금융 OR 보험 감사 OR 처분요구 when:7d",
         "ko", "KR", "KR:ko", "규제"),
        ("초고액자산가", "UHNW OR 초고액자산가 OR 멀티부티크 OR 패밀리오피스 when:7d",
         "ko", "KR", "KR:ko", "금융"),
    ])

    # 긴급 레인은 BSP 에서 쓰지 않는다. 크립토 봇의 경제지표 속보 레인이었고
    # A팀 브리핑과는 성격이 다르다. 엔진 코드가 참조하므로 빈 리스트로 둔다.
    urgent_sources: list = field(default_factory=list)


settings = Settings()


# ── 외부 정의 파일 로더 ────────────────────────────────────────
def _load_json(path: str) -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    full = path if os.path.isabs(path) else os.path.join(here, path)
    with open(full, encoding="utf-8") as f:
        return json.load(f)


TOPIC_DEFS = _load_json(settings.topics_def_file)["topics"]
TOPIC_BY_ID = {t["id"]: t for t in TOPIC_DEFS}
# Primary 로 지정 가능한 토픽 (aggregation 제외)
PRIMARY_TOPIC_IDS = [t["id"] for t in TOPIC_DEFS if not t.get("aggregation")]

_ENTITIES = _load_json(settings.entities_file)


def entity_groups() -> dict:
    """{그룹키: {정식명: [별칭...]}} — _comment/_label 은 제외."""
    out = {}
    for gkey, group in _ENTITIES.items():
        if gkey.startswith("_"):
            continue
        out[gkey] = {k: v for k, v in group.items() if not k.startswith("_")}
    return out


def canonical_entity(name: str) -> str | None:
    """별칭을 정식 엔티티명으로 되돌린다. 모르면 None (§30)."""
    n = (name or "").strip()
    if not n:
        return None
    low = n.lower()
    for group in entity_groups().values():
        for canon, aliases in group.items():
            if low == canon.lower():
                return canon
            for a in aliases:
                if low == a.lower():
                    return canon
    return None


def source_weight(source: str) -> int:
    """매체 가중치 (§16). 모르는 매체는 _default."""
    w = settings.source_weights
    s = source or ""
    for name, val in w.items():
        if name != "_default" and name in s:
            return val
    return w["_default"]
