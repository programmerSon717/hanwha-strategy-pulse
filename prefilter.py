"""STEP 2 — Basic Filtering. 모델을 부르기 전에 명백한 쓰레기를 버린다.

**왜 LLM 앞에 두는가:** 무료 티어 한도가 빡빡하다. 크립토 봇에서 실측된 사고가 있다 —
발행 78건에 요약 호출 500건이 나가 하루 한도를 통째로 소진했다. 연예·시황·PR 기사는
어차피 relevant=false 가 될 것이므로 여기서 미리 버려 한도를 아낀다.

**여기서 하는 것은 Basic Filtering 이다. Inclusion 판정이 아니다 (§15).**
애매하면 통과시킨다. 최종 판정은 STEP 4 에서 LLM 이 한다.
지나치게 공격적으로 거르면 진짜 기사를 잃는다 — 크립토 봇에서 이슈 탭 문턱을
올렸다가 유효 20건 중 1건만 남은 전례가 있다.
"""
import re
import unicodedata


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


# ── 명백한 비(非)전략 영역 (§18 -30점, §39) ──────────────────
_JUNK = re.compile(
    r"연예|아이돌|가수|배우|드라마|예능|영화\s*개봉|시상식|열애|결혼설|"
    r"프로야구|야구단|한화이글스|축구|올림픽|월드컵|"
    r"맛집|레시피|여행\s*추천|날씨|미세먼지|로또|운세|별자리|"
    r"부고|인사말|신년사|골프대회|마라톤|기부\s*행사|봉사활동|"
    r"채용\s*공고|신입사원\s*모집|공모전|서포터즈"
)

# ── 단순 시황·주가 (§18 -20점) ────────────────────────────────
# '급등/급락' 자체보다 **그것만 다루는** 기사를 겨냥한다.
_MARKET_NOISE = re.compile(
    # 지수명과 동사 사이에 "2600선", "3,100포인트" 같은 수치가 끼는 일이 흔하다.
    # 사이를 넉넉히 허용하지 않으면 "코스피 2600선 마감" 이 그대로 새어나간다.
    r"(코스피|코스닥|나스닥|다우|s&p|닛케이|항셍)[^,.·]{0,20}?"
    r"(마감|출발|개장|장중|상승|하락|보합|약보합|강보합|순매수|순매도)|"
    r"(원/달러|원달러)\s*환율\s*(마감|상승|하락|출발)|"
    r"장\s*마감|마감\s*시황|개장\s*시황|주간\s*증시|증시\s*전망|"
    r"(주가|주식)\s*(급등|급락|강세|약세)\s*(이유|배경|분석)?$|"
    r"52주\s*신고가|상한가|하한가|공매도\s*잔고|수급\s*동향|"
    r"국제\s*유가|금값\s*(상승|하락)"
)

# ── 상품 홍보·이벤트·PR (§18 -20점) ──────────────────────────
_PROMO = re.compile(
    r"출시\s*기념|이벤트\s*진행|경품|사은품|추첨|할인\s*행사|무료\s*제공|"
    r"캠페인\s*전개|사회공헌|기부금\s*전달|장학금\s*수여|헌혈|연탄|김장|"
    r"수상|대상\s*수상|1위\s*선정|인증\s*획득|명예의\s*전당|"
    r"고객\s*감사|가입\s*혜택|신규\s*가입자|보험료\s*할인|"
    r"광고\s*모델|브랜드\s*모델|CF\s*공개"
)

# ── 의례·행사·동정 (§18 -20점) ──
# 당국·기업 수장의 방문·축사·시상 기사. 엔티티(금융위 등)가 들어 있어 양성 게이트를
# 통과해 버리므로 여기서 따로 잡는다. 실측(2026-09-23): 금융위 보도자료 10건 중
# 4건이 이 유형이었다. 전략 신호가 있으면 아래 _RESCUE 가 살려 보낸다.
_CEREMONY = re.compile(
    r"방문|참석|축사|격려|위촉|시상|수여|표창|개소식|기념식|간담회\s*개최|"
    r"포럼\s*참석|세미나\s*개최|캠페인|안내해\s*드립니다|"
    r"현장\s*점검|명절|추석|설\s*맞이|신년\s*인사|시구|시축|"
    r"^\[인사\]|^\[부고\]|^\[동정\]|^\[포토\]"
)

# ── 크립토 가격·시황 (§13, §18 -20점) ────────────────────────
# BSP 는 디지털자산을 '금융회사 관점의 사업기회/위협' 으로만 본다.
# 시세 기사가 피드를 지배하면 실패다.
_CRYPTO_PRICE = re.compile(
    r"비트코인|이더리움|리플|도지|알트코인|밈\s*코인|김치\s*프리미엄|"
    r"btc|eth|xrp|altcoin|memecoin"
)
_CRYPTO_PRICE_CTX = re.compile(
    r"시세|가격|급등|급락|상승세|하락세|돌파|붕괴|전망|차트|"
    r"고래|온체인|채굴|반감기|에어드랍|상장\s*폐지|거래량|"
    r"달러\s*선|만\s*달러|억\s*원\s*돌파"
)

# ── 전략 신호 — 위 필터에 걸려도 살려 보낸다 ─────────────────
# "삼성생명 주가 급등" 은 버리지만 "삼성생명 주가 급등, 자사주 소각 발표" 는 살린다.
_RESCUE = re.compile(
    r"인수|매각|합병|분할|지분|경영권|공개매수|지주사|계열분리|"
    r"자사주|주주환원|승계|대주주|출자|증자|"
    r"금융위|금감원|공정위|한국은행|제재|과징금|검사\s*착수|"
    r"보험업법|자본시장법|상법\s*개정|금산분리|감독규정|시행령|"
    r"K-?ICS|킥스|IFRS\s*17|지급여력|"
    r"라이선스|인가|예비인가|본인가|진출|철수|법인\s*설립|"
    r"토큰증권|STO|스테이블코인\s*(제도|법|규제|은행)"
)


def is_junk(item) -> bool:
    """연예·스포츠·생활 등 전략과 무관한 영역 (§39)."""
    t = _norm(item.title)
    return bool(_JUNK.search(t))


def is_market_noise(item) -> bool:
    """단순 시황·주가·환율 기사 (§39)."""
    t = _norm(item.title)
    if _RESCUE.search(t):
        return False
    return bool(_MARKET_NOISE.search(t))


def is_promo(item) -> bool:
    """상품 홍보·이벤트·수상·사회공헌 PR (§39)."""
    t = _norm(item.title)
    if _RESCUE.search(t):
        return False
    return bool(_PROMO.search(t))


def is_crypto_price(item) -> bool:
    """코인 시세·시황 기사 (§13)."""
    t = _norm(item.title)
    if _RESCUE.search(t):
        return False
    return bool(_CRYPTO_PRICE.search(t) and _CRYPTO_PRICE_CTX.search(t))


# 의례 판정에서 구제하는 신호는 **행위** 뿐이다.
# _RESCUE 를 그대로 쓰면 안 된다 — 거기엔 기관명(금융위·금감원·공정위)이 들어 있어
# "금융위원장 추석 전통시장 방문" 같은 기사가 전부 구제되어 버린다(실측).
# 기관이 '무엇을 했는가'가 있어야 구제한다.
_CEREMONY_RESCUE = re.compile(
    r"인수|매각|합병|분할|지분|경영권|공개매수|지주사|계열분리|"
    r"자사주|주주환원|승계|대주주|출자|증자|"
    r"제재|과징금|검사\s*착수|인가|라이선스|"
    r"개정|시행령|감독규정|개편|도입|시행|발표|의결|확정|"
    r"규제|정책\s*방향|로드맵|논의|추진"
)


def is_ceremony(item) -> bool:
    """수장 동정·행사·시상 등 의례성 기사 (§39)."""
    t = _norm(item.title)
    if _CEREMONY_RESCUE.search(t):
        return False
    return bool(_CEREMONY.search(t))


def reject_reason(item) -> str | None:
    """버릴 이유. 통과면 None. 로깅(§35)에 그대로 쓴다."""
    if is_junk(item):
        return "비전략영역(연예·스포츠·생활)"
    if is_market_noise(item):
        return "단순시황·주가"
    if is_promo(item):
        return "홍보·이벤트·수상PR"
    if is_crypto_price(item):
        return "크립토 시세"
    if is_ceremony(item):
        return "의례·행사·동정"
    return None


# ── 구(舊) 크립토 봇 엔진 호환 ───────────────────────────────
# main.py 가 이 두 이름을 부른다. 시그니처를 유지한다.
def is_offtopic(item) -> bool:
    return is_junk(item) or is_promo(item)


def is_price_story(item) -> bool:
    return is_market_noise(item) or is_crypto_price(item)


# ═══════════════════════════════════════════════════════════
# 양성 게이트 — Candidate Retrieval 의 마지막 관문 (§15)
#
# **왜 필요한가:** 직접 RSS(연합뉴스 경제 전체, 금융위 보도자료 전체)는 주제가
# 넓어 위의 음성 필터로는 거의 안 걸린다. 실측(2026-09-23): 수집 208건 중
# 음성 필터가 버린 것은 2건뿐이었고, 나머지 206건이 전부 모델로 갔다.
# Gemini 무료 티어 일일 한도가 500건이므로 스윕 2.5회면 하루치가 사라진다.
# ("금융위원장 추석 전통시장 방문", "유한양행 프로바이오틱스 출시" 같은 것에
#  한도를 쓰는 셈이다)
#
# 그래서 **전략 신호가 하나도 없는 기사는 모델을 부르지 않는다.**
#
# 이것은 Inclusion 판정이 아니다 (§15). 후보 확보의 마지막 단계일 뿐이고,
# 통과한 것 중 무엇을 실을지는 STEP 4 에서 모델이 정한다.
# 신호 목록은 bsp_entities.json 과 bsp_topics_def.json 에서 자동으로 만든다 —
# 코드에 키워드를 흩뿌리지 않는다 (§30).
# ═══════════════════════════════════════════════════════════
def _build_signals() -> tuple:
    """엔티티 신호와 주제어 신호를 **따로** 만든다.

    둘을 한 덩어리로 합치면 "금융" 한 단어만 걸려도 통과해 버린다.
    실측(2026-09-23): 합쳐 썼을 때 460건 중 327건이 모델로 갔다(절감 29%).
    """
    from config import TOPIC_DEFS, entity_groups

    ents: set[str] = set()
    for group in entity_groups().values():
        for canon, aliases in group.items():
            ents.add(canon)
            ents.update(aliases)

    kws: set[str] = set()
    for t in TOPIC_DEFS:
        kws.update(t.get("keywords") or [])

    def split(words):
        # 2~3글자 영문 약어는 단어 경계를 요구한다("AI" 가 "AIR" 에 걸리지 않게).
        short = {w for w in words if len(w) <= 3 and w.isascii()}
        plain = {w.lower() for w in words if w not in short and len(w) >= 2}
        rx = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(short)) + r")\b",
                        re.I) if short else None
        return plain, rx

    return split(ents), split(kws)


(_ENT_WORDS, _ENT_SHORT), (_KW_WORDS, _KW_SHORT) = _build_signals()


# 엔티티가 없어도 그 자체로 후보가 되는 **전략 사건** 표현.
# 이게 있으면 다른 조건 없이 통과시킨다 — 사건이 곧 신호다.
_EVENT_SIGNAL = re.compile(
    r"인수|매각|합병|분할|공개매수|경영권|지주사\s*전환|계열분리|"
    r"지분\s*(인수|매각|취득|확대|처분|투자)|대주주\s*변경|"
    r"자사주\s*(매입|소각)|주주환원|승계|출자|유상증자|무상증자|"
    r"실사|우선협상|본계약|양해각서\s*체결|딜|바이아웃|"
    # **"품다" 계열.** 한국 금융 기사에서 인수를 가리키는 가장 흔한 동사인데
    # 빠져 있었다(2026-10-05 감사). 실측 누락: "리졸루션라이프 100% 품은
    # 닛폰생명", "도쿄해상, 영국 MGA 품어 상용차보험 빈칸 채웠다" — 둘 다
    # 모델을 보지도 못하고 죽었다. groundtruth 137건에는 같은 성격의 해외
    # 보험사 M&A 가 실제로 올라가 있다.
    r"품은|품어|품었|품는|품기로|손에\s*넣|거머쥐|낙점|인수설|피인수|"
    r"제재|과징금|검사\s*착수|시정명령|고발|"
    r"인가|예비인가|본인가|라이선스|허가\s*취득|"
    r"개정안|시행령|감독규정|제도\s*개편|규제\s*완화|규제\s*강화|"
    r"해외\s*진출|현지법인\s*설립|합작법인|jv\s*설립|철수|매물|"
    # 해외사업 확대는 그 자체가 전략 사건이다. "해외\s*진출" 만 있어서
    # "한화생명, 인도네시아서 미국·중동까지 해외사업 확대" (groundtruth 수록분)
    # 가 탈락했다 — 적합도 55 점, 티어1 한화 기사였다.
    r"해외\s*(사업|시장|거점|법인|네트워크)\s*(확대|확장|진출|강화)|"
    r"글로벌\s*(확장|진출|공략)|현지\s*진출|해외\s*인수전|"
    # 사명·상호 변경은 대개 지분구조 변화의 후행 신호다.
    # (실측: "교보악사자산운용 → 교보자산운용" = AXA 의 JV 철수)
    r"사명\s*변경|상호\s*변경|브랜드\s*변경|간판\s*교체"
)

# 명백한 지면 코너·공지성 말머리. 본문을 볼 것도 없다.
_BRACKET_JUNK = re.compile(
    r"^\s*\[(게시판|인사|부고|동정|포토|영상|사진|알림|공지|신간|"
    r"오늘의|주간|표|그래픽|일지|bnk|기고|칼럼|사설|특징주|"
    r"내일의\s*날씨|주요\s*일정)\]"
)

# 홍보·행사·사회공헌 — 엔티티는 있지만 전략이 없는 유형.
_SOFT_PR = re.compile(
    r"행사\s*개최|교육\s*(실시|진행|지원)|육성|공모|서포터즈|"
    r"체험단|해피|나눔|동행|봉사|후원|협약식|출범식|발대식|"
    r"원팀|화합|소통\s*행사|임직원|직원\s*(대상|참여)|"
    r"이벤트|프로모션|경품|사은|할인|무료\s*상담|"
    r"캠페인|공익|사회공헌|esg\s*활동|봉사활동"
)


# 주제어 중 **그 자체로는 변별력이 없는 것들.**
# "금융"·"보험"·"증권" 은 종합 경제지 기사 절반에 들어 있다. 이것만으로 통과시키면
# 게이트가 사실상 없는 것과 같다(실측: 절감 29%). 그래서 이 말들은
# '엔티티와 함께 있을 때만' 신호로 친다.
_GENERIC_KW = {
    "생명보험", "손해보험", "재보험", "증권", "은행", "금융지주", "금융그룹",
    "자산운용", "저축은행", "ai", "신사업", "제휴", "통합", "투자", "보험",
    "디지털 금융", "금융 플랫폼", "블록체인", "승계", "기업가치",
}


def _hit(text: str, words: set, rx) -> bool:
    if rx and rx.search(text):
        return True
    return any(w in text for w in words)


def has_signal(item) -> bool:
    """전략 신호가 있는가. 없으면 모델을 부르지 않는다 (§15).

    통과 조건은 셋 중 하나다. (위에서부터 싼 순서)

      1) 전략 **사건** 표현        인수·제재·개정안 …      그 자체로 신호다
      2) **변별력 있는 주제어**    K-ICS·자본규제·토큰증권  엔티티가 없어도 신호다
      3) **엔티티 + 아무 주제어**  카카오 + 결제사업        둘이 함께라야 신호다

    엔티티만 있는 것(한화 한 번 언급)이나, 변별력 없는 주제어만 있는 것
    ("금융" 한 단어)은 통과시키지 않는다.

    **Recall 이 Precision 보다 먼저다** (§37). 여기서 버린 기사는 모델이 볼
    기회조차 없으므로, 애매하면 통과시킨다. 최종 판정은 STEP 4 의 몫이다.
    """
    text = _norm(f"{item.title} {item.body or ''}")
    if _EVENT_SIGNAL.search(text):
        return True

    strong = {w for w in _KW_WORDS if w not in _GENERIC_KW}
    if _hit(text, strong, _KW_SHORT):
        return True

    return (_hit(text, _ENT_WORDS, _ENT_SHORT)
            and _hit(text, _KW_WORDS, _KW_SHORT))


def is_soft_pr(item) -> bool:
    """엔티티는 있으나 전략이 없는 홍보·행사·사회공헌 기사."""
    t = _norm(item.title)
    if _EVENT_SIGNAL.search(t):
        return False
    return bool(_SOFT_PR.search(t))


# 소급 수집(--backfill)에서 신호 게이트를 느슨하게 할지.
#
# has_signal 은 **엔티티 AND 주제어**를 둘 다 요구한다. 상시 수집에서는 맞다 —
# 하루 수천 건이 들어오므로 모델 호출을 아껴야 하고, 놓친 기사는 다음 회차에
# 다시 들어온다.
#
# 그런데 소급 수집은 다르다. 날짜가 고정돼 있어 **다음 기회가 없고**, 검색으로
# 이미 A팀 키워드를 걸어 가져온 집합이다. 여기에 AND 게이트를 또 걸면
# 656건 중 561건이 모델을 보지도 못하고 죽는다(2026-10-05: 10/4 Top10 이 6건에서
# 막힌 실제 원인).
#
# 느슨 모드는 **엔티티만 있으면 모델에게 넘긴다.** 품질은 그대로다 — 기각 판단은
# 어차피 모델과 csfit 점수가 한다. 잡음(말머리·홍보·연예스포츠) 필터는 그대로 건다.
RELAXED = False


def gate(item) -> str | None:
    """STEP 2 종합 판정. 버릴 이유 또는 None.

    main.py 는 이것 하나만 부르면 된다.
    """
    if _BRACKET_JUNK.match(item.title or ""):
        return "지면코너·공지 말머리"
    r = reject_reason(item)
    if r:
        return r
    if is_soft_pr(item):
        return "홍보·행사(전략 없음)"
    if RELAXED:
        # 엔티티만 있으면 통과시킨다. 위 주석 참고.
        text = _norm(f"{item.title} {item.body[:300]}")
        if not _hit(text, _ENT_WORDS, _ENT_SHORT):
            return "A팀 엔티티 없음"
        return None
    if not has_signal(item):
        return "전략 신호 없음(모델 호출 안 함)"
    return None
