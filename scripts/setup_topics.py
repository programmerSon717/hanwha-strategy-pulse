#!/usr/bin/env python
"""Strategy Pulse — 텔레그램 Forum Topic 생성 유틸 (스펙 §33).

**production group 에 자동으로 토픽을 만들지 않는다.** 이 스크립트를 명시적으로
실행했을 때만 동작한다. 봇 루프는 절대 토픽을 만들지 않는다.

    # 무엇이 만들어질지만 본다 (아무것도 만들지 않음)
    venv/bin/python scripts/setup_topics.py --plan

    # 실제로 만든다
    venv/bin/python scripts/setup_topics.py --create

    # 이미 있는 그룹에서 thread_id 만 다시 출력한다
    venv/bin/python scripts/setup_topics.py --show

만들어진 message_thread_id 를 .env 의 TG_TOPIC_* 에 붙여 넣으면 된다.
출력 마지막에 붙여 넣을 형태 그대로 찍어 준다.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import topics
from config import settings

ENV_NAMES = {
    "hanwha_group":        "TG_TOPIC_HANWHA_GROUP",
    "ma_governance":       "TG_TOPIC_MA_GOVERNANCE",
    "insurance_finance":   "TG_TOPIC_INSURANCE_FINANCE",
    "regulation_policy":   "TG_TOPIC_REGULATION_POLICY",
    "competitors_bigtech": "TG_TOPIC_COMPETITORS_BIGTECH",
    "digital_newbiz":      "TG_TOPIC_DIGITAL_NEWBIZ",
    "global_finance":      "TG_TOPIC_GLOBAL_FINANCE",
    "key_issues":          "TG_TOPIC_KEY_ISSUES",
    "daily_brief":         "TG_TOPIC_DAILY_BRIEF",
    "cs_top10":            "TG_TOPIC_CS_TOP10",
}


def print_env_block(cache: dict):
    print("\n" + "=" * 62)
    print(".env 에 아래를 붙여 넣으세요")
    print("=" * 62)
    for tid, env in ENV_NAMES.items():
        print(f"{env}={cache.get(tid, '')}")
    print("=" * 62)


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--plan"

    if not settings.telegram_bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN 이 없습니다. .env 를 먼저 채우세요.")
    if not settings.telegram_channel_id:
        sys.exit("TELEGRAM_CHANNEL_ID 가 없습니다. .env 를 먼저 채우세요.")

    if mode == "--plan":
        print("아래 9개 토픽을 만듭니다. (지금은 아무것도 만들지 않았습니다)\n")
        for tid, name in topics.CATEGORIES.items():
            print(f"  {name}    → {ENV_NAMES[tid]}")
        print(f"\n대상 그룹: {settings.telegram_channel_id}")
        print("\n실제로 만들려면:  venv/bin/python scripts/setup_topics.py --create")
        return

    if mode == "--show":
        cache = topics._load_cache()
        if not cache:
            print("topics.json 에 캐시된 thread_id 가 없습니다.")
        print_env_block(cache)
        return

    if mode != "--create":
        sys.exit(f"알 수 없는 옵션: {mode}  (--plan | --create | --show)")

    print(f"대상 그룹: {settings.telegram_channel_id}")
    ans = input("이 그룹에 토픽 9개를 만듭니다. 계속할까요? [y/N] ").strip().lower()
    if ans != "y":
        print("취소했습니다.")
        return

    async with httpx.AsyncClient() as client:
        cache = await topics.create_topics(client, only_missing=True)
    print_env_block(cache)


if __name__ == "__main__":
    asyncio.run(main())
