"""출력 언어 검사 — 가나·한자가 섞인 채 나가는 글을 발행 전에 잡는다.

RULES 9·10-5 는 "다국어 원문을 전부 한국어로 옮긴다"고 못박아 뒀고 프롬프트에도
같은 지시가 두 곳에 있다. 그런데도 **모델은 계속 어긴다.** 실측(발행 이력 5,474건
기준) 하루 351건 중 9건꼴로 가나·한자가 본문에 남았다. 실제로 나간 글들:

    UAE, 국가 ID 시스템 'UAE PASS'를 아바ランチ 네트워크로 전환   (CoinPost)
    오ンド·파이낸스, 미국 주식 무期限 선물 …                      (CoinPost)
    아서 헤イズ, 프랑스 은행 신용 불안으로 …                      (CoinPost)
    마이클 세일러 'CLARITY 법案 지연 시 …'                        (중국어 매체)

단어 **가운데**가 원문으로 남는 형태라 읽는 사람이 오타로도 못 본다.
그래서 country.enforce() 와 같은 방식을 쓴다 — **프롬프트는 권고, 이쪽이 강제다.**

처리 순서:
 1. 자주 나오는 낱말은 사전(GLOSSARY)으로 그 자리에서 바꾼다. 호출이 들지 않는다.
 2. 그래도 남으면 남은 조각만 모델에 넘겨 한국어 표기를 받아 바꾼다(summarizer).
    JSON 전체를 다시 쓰게 하지 않는다 — 사실이 바뀔 수 있어서다(RULES 10-2).
 3. 그래도 남으면 로그에 남기고 내보낸다. LANG_STRICT=1 이면 발행을 포기한다.

괄호 안은 검사하지 않는다. `금융청(FSA)`·`신화그룹(新火科技控股)` 처럼
**원어 병기는 규칙이 허용하는 예외**다(RULES 9).
"""
import os
import re

LOCALE = os.getenv("LOCALE", "ko").lower()

# 1 이면 끝내 안 고쳐진 글을 발행하지 않는다. 기본은 0 —
# 놓치는 쪽이 더 나쁘다는 것이 이 채널의 기존 판단이다(RULES 10-4).
STRICT = os.getenv("LANG_STRICT", "0") == "1"

# 남으면 안 되는 문자대. 한국어판은 가나·한자, 영문판은 거기에 한글까지.
_KANA = "぀-ヿㇰ-ㇿ"
_HAN = "㐀-䶿一-鿿豈-﫿"
_HANGUL = "가-힣㄰-㆏"
FOREIGN = re.compile(f"[{_KANA}{_HAN}]" if LOCALE == "ko"
                     else f"[{_KANA}{_HAN}{_HANGUL}]")

# 검사할 필드. `origin_text` 는 뺀다 — 퍼온 글의 원문을 그대로 인용해야 하고
# (RULES 2) 외국어면 번역을 병기하는 자리라 원문이 남아 있는 게 맞다.
# Strategy Pulse 스키마 키. 크립토 봇의 headline/lede/comment 도 남겨 둔다 —
# 사진 캡션 경로 등 일부 코드가 아직 그 이름으로 값을 넣는다.
FIELDS = ("title_ko", "summary", "why_it_matters", "update_note",
          "relevance_reason", "headline", "lede", "comment")
LIST_FIELDS = ("key_points", "tags", "main_entities", "bullets", "hashtags")

# 실제 발행 이력에서 반복해 나온 것들. 호출 없이 여기서 끝내는 게 이득이다.
GLOSSARY: dict[str, str] = {
    # 해외 기사에서 그대로 새어 나오는 일본어·중국어 금융 용어.
    # 실제 발행 이력에서 반복해 나온 것을 여기서 끝낸다 — 호출 없이 처리하는 게 이득이다.
    # 일본어
    "金融庁": "금융청", "保険": "보험", "生命保険": "생명보험", "損害保険": "손해보험",
    "証券": "증권", "銀行": "은행", "持株会社": "지주회사", "買収": "인수",
    "合併": "합병", "出資": "출자", "子会社": "자회사", "決済": "결제",
    "経営": "경영", "経済": "경제", "規制": "규제", "監督": "감독",
    "株主": "주주", "議決権": "의결권", "資本": "자본", "運用": "운용",
    "提携": "제휴", "統合": "통합", "上場": "상장", "金利": "금리",
    # 중국어(간체·번체)
    "保险": "보험", "寿险": "생명보험", "财险": "손해보험", "证券": "증권",
    "银行": "은행", "控股": "지주", "收购": "인수", "并购": "M&A",
    "合并": "합병", "股权": "지분", "股东": "주주", "监管": "규제",
    "资本": "자본", "资产管理": "자산운용", "牌照": "라이선스",
    "美联储": "연준", "稳定币": "스테이블코인", "加息": "금리 인상",
    "降息": "금리 인하", "流动性": "유동성", "托管": "수탁",
}

_PARENS = re.compile(r"[\(（][^)）]{0,60}[\)）]")
_TAG = re.compile(r"<[^>]+>")
_TRIM = re.compile(r"^[\s\"'“”‘’·,.…()\[\]<>]+|[\s\"'“”‘’·,.…()\[\]<>]+$")


def _values(data: dict) -> list[str]:
    out = [str(data.get(f) or "") for f in FIELDS]
    for f in LIST_FIELDS:
        out += [str(v) for v in (data.get(f) or []) if isinstance(v, str)]
    return out


def leftovers(data: dict) -> list[str]:
    """한국어로 안 옮겨진 조각들. 괄호 안(원어 병기)은 세지 않는다."""
    found: list[str] = []
    for value in _values(data):
        text = _PARENS.sub(" ", _TAG.sub(" ", value))
        for token in text.split():
            token = _TRIM.sub("", token)
            if token and FOREIGN.search(token) and token not in found:
                found.append(token)
    return found


def _replace_all(data: dict, mapping: dict[str, str]) -> None:
    """모든 필드에서 mapping 을 그대로 치환한다. 긴 낱말부터 바꾼다."""
    pairs = sorted(mapping.items(), key=lambda kv: -len(kv[0]))

    def fix(s: str) -> str:
        for src, dst in pairs:
            if src and dst and src in s:
                s = s.replace(src, dst)
        return s

    for f in FIELDS:
        if isinstance(data.get(f), str):
            data[f] = fix(data[f])
    for f in LIST_FIELDS:
        if isinstance(data.get(f), list):
            data[f] = [fix(v) if isinstance(v, str) else v for v in data[f]]


def apply_glossary(data: dict) -> None:
    _replace_all(data, GLOSSARY)


async def ensure(data: dict, translate=None) -> bool:
    """발행 전 언어 보정. 깨끗하면(또는 깨끗해졌으면) True.

    translate 는 `[조각] -> {조각: 한국어}` 를 돌려주는 async 함수다(기본은
    summarizer.translate_terms). 한도가 소진돼 호출을 못 하면 None 을 돌려주는데,
    그때는 **버리지 않는다** — 뉴스를 통째로 놓치는 쪽이 더 나쁘다.
    """
    if not data:
        return True
    apply_glossary(data)
    stuck = leftovers(data)
    if not stuck:
        return True

    if translate is None:
        from summarizer import translate_terms as translate
    mapping = await translate(stuck)
    if mapping:
        _replace_all(data, mapping)
        stuck = leftovers(data)
        if not stuck:
            return True

    print(f"[언어] 한국어로 안 옮겨진 조각: {' '.join(stuck[:6])}"
          + (" — 발행 보류" if STRICT and mapping is not None else ""))
    # 호출조차 못 한 경우(한도 소진)는 버리지 않는다.
    return not (STRICT and mapping is not None)
