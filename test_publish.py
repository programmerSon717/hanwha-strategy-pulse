"""1회성 테스트 발행: 국내(블록미디어)+해외(CoinDesk) 각 1건 요약→발행.
seen DB에도 기록해서 이후 상시 실행 시 중복 발행되지 않게 한다."""
import asyncio
import httpx

from collectors import rss
from config import settings
from publisher import publish
from store import Store
from summarizer import summarize

store = Store(settings.db_path)

SOURCES = [
    ("블록미디어", "https://www.blockmedia.co.kr/feed", "국내"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "해외"),
]


async def main():
    async with httpx.AsyncClient() as client:
        for name, url, hint in SOURCES:
            items = await rss.fetch_feed(client, name, url, hint)
            print(f"\n[{name}] {len(items)}건 수집")
            published = False
            for item in items:
                key = Store.make_key(item.source, item.unique_id)
                if store.is_seen(key):
                    continue
                data = await summarize(item)
                if data is None:
                    print(f"  [skip 무관] {item.title[:50]}")
                    continue
                print(f"  요약됨 (중요도 {data.get('importance')}, {data.get('region')}): {item.title[:50]}")
                await publish(client, data, item.url)
                store.mark_seen(key, item.source, item.title)
                published = True
                break
            if not published:
                print(f"  [{name}] 발행할 관련 뉴스 없음")
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
