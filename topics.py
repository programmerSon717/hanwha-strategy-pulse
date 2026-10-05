"""Strategy Pulse — 포럼 토픽(탭) 라우팅.

텔레그램 Topics 는 슈퍼그룹 전용이다. 대상 채팅이 '주제(Topics)' 가 켜진 그룹이어야 한다.

스펙 §33: message_thread_id 를 코드에 하드코딩하지 않는다.
  1순위  .env 의 TG_TOPIC_* (config.settings.topic_thread_ids)
  2순위  topics.json 캐시 (scripts/setup_topics.py 가 만들어 준다)

**production group 에 자동으로 토픽을 만들지 않는다.** 생성은 scripts/setup_topics.py
를 명시적으로 실행했을 때만 일어난다.
"""
import json
import os

from config import settings, TOPIC_DEFS, TOPIC_BY_ID, PRIMARY_TOPIC_IDS

API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

# 토픽 id → 표시 이름.
#
# **키(왼쪽)는 LLM 이 primary_topic 으로 뱉는 값이자 라우팅 키다. 절대 바꾸지 마라.**
# 바꾸면 prompts_bsp 의 STEP B·main 라우팅·brief 가 전부 어긋난다.
# 표시 이름만 바꾸는 것은 안전하다 — 라우팅은 키로만 한다.
# 이미 만들어진 탭 이름은 이 값을 고쳐도 자동으로 안 바뀐다(editForumTopic 필요).
#
# 아이콘은 스펙 §5 지정값이다. 색깔별 원형 이모지로 바꾸지 마라 —
# 아이콘 자체가 토픽의 의미를 설명해야 한다.
CATEGORIES = {t["id"]: t["telegram_topic_name"] for t in TOPIC_DEFS}

# 집계 토픽. primary 로 지정되지 않고, 다른 토픽과 중복 게시가 허용된다 (§6).
AGGREGATION = {t["id"] for t in TOPIC_DEFS if t.get("aggregation")}


def _load_cache() -> dict:
    if os.path.exists(settings.topics_file):
        try:
            with open(settings.topics_file, encoding="utf-8") as f:
                data = json.load(f)
            # 크립토 봇 시절 캐시가 남아 있을 수 있다. BSP 키만 취한다.
            return {k: v for k, v in data.items() if k in CATEGORIES}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(data: dict):
    with open(settings.topics_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def thread_id_for(topic_id: str) -> int | None:
    """토픽 id 에 해당하는 message_thread_id. 없으면 None (→ General 로 나간다)."""
    if not settings.use_topics:
        return None
    env_val = settings.topic_thread_ids.get(topic_id)
    if env_val:
        return env_val
    return _load_cache().get(topic_id)


def display_name(topic_id: str) -> str:
    return CATEGORIES.get(topic_id, topic_id)


def normalize_topic(value: str | None) -> str:
    """모델이 뱉은 topic 값을 실제 라우팅 키로 보정한다.

    표기가 흔들리면(예: 'Hanwha Group', '한화그룹') 라우팅이 조용히 실패해
    엉뚱한 탭으로 나가므로 여기서 한 번 걸러준다.
    """
    if not value:
        return "insurance_finance"
    v = str(value).strip()
    if v in PRIMARY_TOPIC_IDS:
        return v

    low = v.lower().replace(" ", "_").replace("-", "_")
    if low in PRIMARY_TOPIC_IDS:
        return low

    aliases = {
        "한화": "hanwha_group", "한화그룹": "hanwha_group", "hanwha": "hanwha_group",
        "hanwha_group": "hanwha_group",
        "m&a": "ma_governance", "ma": "ma_governance", "지배구조": "ma_governance",
        "m&a_지배구조": "ma_governance", "governance": "ma_governance",
        "보험": "insurance_finance", "금융": "insurance_finance",
        "보험_금융": "insurance_finance", "insurance": "insurance_finance",
        "finance": "insurance_finance",
        "규제": "regulation_policy", "정책": "regulation_policy",
        "규제_정책": "regulation_policy", "regulation": "regulation_policy",
        "policy": "regulation_policy",
        "경쟁사": "competitors_bigtech", "빅테크": "competitors_bigtech",
        "경쟁사_big_tech": "competitors_bigtech", "bigtech": "competitors_bigtech",
        "big_tech": "competitors_bigtech", "competitors": "competitors_bigtech",
        "디지털": "digital_newbiz", "신사업": "digital_newbiz",
        "디지털_신사업": "digital_newbiz", "digital": "digital_newbiz",
        "newbiz": "digital_newbiz",
        "글로벌": "global_finance", "global": "global_finance",
        "해외": "global_finance", "global_finance": "global_finance",
    }
    if low in aliases:
        return aliases[low]

    # 집계 토픽을 primary 로 준 경우 — 스펙상 금지다. 성격이 가장 가까운 곳으로 보낸다.
    if low in ("key_issues", "주요이슈", "daily_brief", "morning_brief"):
        print(f"[라우팅] 집계 토픽 '{value}' 을 primary 로 받음 → insurance_finance 로 보정")
        return "insurance_finance"

    print(f"[라우팅] 알 수 없는 토픽 '{value}' → insurance_finance 로 처리")
    return "insurance_finance"


async def create_topics(client, only_missing: bool = True) -> dict:
    """포럼 토픽을 생성하고 {topic_id: thread_id} 를 반환한다.

    **자동으로 불리지 않는다.** scripts/setup_topics.py 에서만 명시적으로 호출한다 (§33).
    """
    cache = _load_cache()
    for topic_id, name in CATEGORIES.items():
        if only_missing and topic_id in cache:
            continue
        r = await client.post(
            f"{API}/createForumTopic",
            json={"chat_id": settings.telegram_channel_id, "name": name},
            timeout=15,
        )
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"토픽 생성 실패({name}): {data.get('description')}")
        cache[topic_id] = data["result"]["message_thread_id"]
        print(f"[topics] 생성됨: {name} (thread_id={cache[topic_id]})")
    save_cache(cache)
    return cache
