"""표시 문구.

발행문에 들어가는 고정 문구를 한곳에 모아 둔 것이다. 영문판은 별도 리포
(programmerSon717/crypto-news-bot-en)에 있고 코드를 공유하지 않는다.

**한국어 값은 이 파일이 생기기 전 코드에 박혀 있던 문자열 그대로다.**
한 글자라도 다르면 독자가 보는 글이 달라진다. tests/ko_snapshot.py 가 매번 대조한다.
"""
import os

LOCALE = os.getenv("LOCALE", "ko").lower()

# 탭 표시 이름. 키(왼쪽)는 모델이 뱉는 분류값이라 언어와 무관하게 고정이다.
# 영문판도 같은 키를 쓰고 보여주는 이름만 바꾼다 — 라우팅·분류 코드를 건드리지 않기 위해서.
# 탭 표시 이름은 topics.py 에 박혀 있다. 여기는 비어 있다 —
# 영문판이 별도 리포로 갈라져 나가면서 다른 언어의 이름표가 필요 없어졌다.
TAB_NAMES: dict[str, dict[str, str]] = {}

STRINGS = {
    "ko": {
        "section_default": "주요내용",
        "update": "업데이트",
        "source_link": "기사 원문",
        "posted": "게시",
        "insight_context": "배경",
        "insight_impact": "영향",
        "insight_watch": "지켜볼 포인트",
        "author_original": "원문",
        "digest_title": "정리",
        "digest_count": "이 시간대 발행 {n}건",
        "overview_title": "브리핑",
        "overview_by_cat": "카테고리별",
        "overview_count": "이 시간대 총 {n}건",
    },
    "en": {
        "section_default": "Key points",
        "update": "Update",
        "source_link": "Read the original",
        "posted": "published",
        "insight_context": "Context",
        "insight_impact": "Impact",
        "insight_watch": "What to watch",
        "author_original": "original post",
        "digest_title": "recap",
        "digest_count": "{n} published this hour",
        "overview_title": "briefing",
        "overview_by_cat": "By tab",
        "overview_count": "{n} in total this hour",
    },
}


def T(key: str) -> str:
    """표시 문구 하나. 없는 키는 한국어로 되돌려 빈 화면이 나가지 않게 한다."""
    return STRINGS.get(LOCALE, STRINGS["ko"]).get(key) or STRINGS["ko"][key]


def tab_names() -> dict | None:
    """이 언어의 탭 이름표. 한국어면 None — topics.py 에 박힌 값을 그대로 쓴다."""
    return TAB_NAMES.get(LOCALE)
