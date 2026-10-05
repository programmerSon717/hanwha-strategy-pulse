"""다른 텔레그램 채널을 소스로 수집(Telethon).

봇 API로는 남의 채널을 읽을 수 없어 개인 계정(User API)을 쓴다.
my.telegram.org 에서 API_ID / API_HASH 를 발급받고 최초 1회 로그인(전화번호+코드)이 필요하다.
세션은 tg_session.session 파일에 저장되어 이후 자동 로그인된다.

주의: 개인 계정 자동화는 텔레그램 정책상 과도한 사용 시 제한될 수 있으므로
읽기 전용 + 낮은 빈도로만 사용한다.
"""
import os

from models import NewsItem

_client = None


async def _get_client():
    """로그인된 Telethon 클라이언트를 반환. 설정/세션이 없으면 None.

    start() 는 미인증 상태에서 stdin 으로 전화번호를 물어보며 무한 대기한다.
    백그라운드·CI 에서는 그대로 멈춰버리므로 connect() 후 인증 여부만 확인하고,
    미인증이면 조용히 건너뛴다. (로그인은 tg_login.py 로 사람이 1회 수행)
    """
    global _client
    from config import settings

    if not (settings.tg_api_id and settings.tg_api_hash):
        return None
    if not os.path.exists("tg_session.session"):
        return None

    if _client is None:
        from telethon import TelegramClient

        c = TelegramClient("tg_session", int(settings.tg_api_id), settings.tg_api_hash)
        await c.connect()
        if not await c.is_user_authorized():
            print("[telegram] 세션 미인증 — python tg_login.py 로 로그인하세요. 이번엔 건너뜁니다.")
            await c.disconnect()
            return None
        _client = c
    return _client


async def _to_item(client, entity, ch, msg, with_image: bool) -> NewsItem | None:
    """텔레그램 메시지 1건 → NewsItem. 대상이 아니면 None.

    이 채널은 트위터 캡처 + 한 줄 코멘트 형태가 대부분이라 본문이 이미지 안에 있다.
    따라서 사진이 붙은 글은 캡션이 짧아도 버리지 않고, 이미지를 실어 보낸다.
    """
    text = (msg.message or "").strip()
    has_photo = msg.photo is not None

    # 사진도 없고 캡션도 짧으면 정보가 없는 글(이모지, 인사 등)
    if not has_photo and len(text) < 40:
        return None

    uname = getattr(entity, "username", None) or str(entity.id)
    title = (text.split("\n")[0][:120] if text else f"[트위터 캡처] {uname}:{msg.id}")

    image = None
    if has_photo and with_image:
        try:
            image = await client.download_media(msg, file=bytes)
        except Exception as e:
            print(f"[telegram:{ch}] 이미지 다운로드 실패(msg {msg.id}): {e}")

    return NewsItem(
        source=f"TG:{ch}",
        unique_id=f"{uname}:{msg.id}",
        title=title,
        url=f"https://t.me/{uname}/{msg.id}",
        body=text[:2000],
        region_hint="",
        published_at=msg.date.timestamp() if msg.date else None,
        image=image,
        image_mime="image/jpeg",
    )


async def fetch(limit_per_channel: int = 10, with_image: bool = True) -> list[NewsItem]:
    from config import settings

    channels = settings.tg_source_channels
    if not channels:
        return []

    try:
        client = await _get_client()
        if client is None:
            return []
    except Exception as e:
        print(f"[telegram] 로그인 실패: {e}")
        return []

    items: list[NewsItem] = []
    for ch in channels:
        try:
            entity = await client.get_entity(ch)
            async for msg in client.iter_messages(entity, limit=limit_per_channel):
                item = await _to_item(client, entity, ch, msg, with_image)
                if item:
                    items.append(item)
        except Exception as e:
            print(f"[telegram:{ch}] 수집 실패: {e}")
    return items


async def fetch_since(since_epoch: float, with_image: bool = True) -> list[NewsItem]:
    """지정 시각 이후의 글을 전부 가져온다(백필용).

    iter_messages 는 최신순이므로 since 이전 글을 만나면 멈춘다.
    """
    from config import settings

    channels = settings.tg_source_channels
    if not channels:
        return []

    try:
        client = await _get_client()
        if client is None:
            print("[telegram] 세션 미인증 — python tg_login.py 로 먼저 로그인하세요.")
            return []
    except Exception as e:
        print(f"[telegram] 로그인 실패: {e}")
        return []

    items: list[NewsItem] = []
    for ch in channels:
        try:
            entity = await client.get_entity(ch)
            scanned = 0
            async for msg in client.iter_messages(entity, limit=None):
                if msg.date and msg.date.timestamp() < since_epoch:
                    break
                scanned += 1
                item = await _to_item(client, entity, ch, msg, with_image)
                if item:
                    items.append(item)
            print(f"[telegram:{ch}] {scanned}건 스캔 → {len(items)}건 수집")
        except Exception as e:
            print(f"[telegram:{ch}] 수집 실패: {e}")
    return items
