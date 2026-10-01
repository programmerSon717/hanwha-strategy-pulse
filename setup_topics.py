"""그룹에 카테고리 탭(토픽)을 1회 생성한다.

사전 조건:
  1) 대상이 '주제(Topics)'가 켜진 그룹일 것 (채널은 불가)
  2) 봇이 관리자이고 '주제 관리(Manage Topics)' 권한을 가질 것
  3) .env 의 TELEGRAM_CHANNEL_ID 가 그 그룹을 가리킬 것

    python setup_topics.py
"""
import asyncio

import httpx

import topics
from config import settings

API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


async def main():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{API}/getChat", params={"chat_id": settings.telegram_channel_id}, timeout=15
        )
        info = r.json()
        if not info.get("ok"):
            print(f"[에러] 채팅 조회 실패: {info.get('description')}")
            return

        chat = info["result"]
        print(f"대상: {chat.get('title')} (type={chat.get('type')})")

        if chat.get("type") != "supergroup":
            print(
                "[에러] 토픽은 슈퍼그룹에서만 됩니다.\n"
                "       그룹을 만들고 설정 > 주제(Topics)를 켠 뒤 다시 실행하세요.\n"
                f"       현재 타입: {chat.get('type')}"
            )
            return

        if not chat.get("is_forum"):
            print("[에러] 이 그룹은 주제(Topics)가 꺼져 있습니다. 그룹 설정에서 켜주세요.")
            return

        created = await topics.ensure_topics(client)
        print("\n완료. 카테고리 → 탭 매핑:")
        for cat, tid in created.items():
            print(f"  {cat:8s} → thread_id {tid}")
        print(f"\n'{settings.topics_file}' 에 저장했습니다.")
        print("이제 .env 에 USE_TOPICS=true 를 넣고 main.py 를 실행하세요.")


if __name__ == "__main__":
    asyncio.run(main())
