"""Telethon 최초 로그인 (1회만 실행).

전화번호 → 텔레그램 앱으로 온 인증코드 입력 → tg_session.session 생성.
이후에는 이 세션 파일로 자동 로그인되므로 다시 실행할 필요 없다.

    python tg_login.py
"""
import asyncio

from telethon import TelegramClient

from config import settings


async def main():
    if not (settings.tg_api_id and settings.tg_api_hash):
        print("[에러] .env 에 TG_API_ID / TG_API_HASH 를 먼저 넣으세요.")
        return

    client = TelegramClient("tg_session", int(settings.tg_api_id), settings.tg_api_hash)
    await client.start()  # 전화번호/코드 입력 프롬프트

    me = await client.get_me()
    print(f"\n[로그인 성공] {me.first_name} (@{me.username or me.id})")

    # 설정된 소스 채널을 실제로 읽을 수 있는지 확인
    for ch in settings.tg_source_channels:
        try:
            entity = await client.get_entity(ch)
            msgs = [m async for m in client.iter_messages(entity, limit=3)]
            print(f"  {ch}: OK — 최근 {len(msgs)}건 읽기 성공")
            for m in msgs:
                first = (m.message or "").strip().split("\n")[0][:60]
                if first:
                    print(f"      - {first}")
        except Exception as e:
            print(f"  {ch}: 실패 — {e}")

    await client.disconnect()
    print("\n세션 저장됨(tg_session.session). 이제 main.py 가 자동으로 채널을 읽습니다.")


if __name__ == "__main__":
    asyncio.run(main())
