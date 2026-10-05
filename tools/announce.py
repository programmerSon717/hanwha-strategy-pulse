"""각 토픽에 운영 전환 안내를 한 번씩 올린다.

사용법:  venv/bin/python tools/announce.py        # 미리보기
         venv/bin/python tools/announce.py --go   # 실제 게시
"""
import os, sys, json, asyncio
import httpx
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings

TOKEN = settings.telegram_bot_token
CHAT = settings.telegram_channel_id

# 토픽 id → 환경변수 이름
ENV = {
    "hanwha_group": "TG_TOPIC_HANWHA_GROUP",
    "ma_governance": "TG_TOPIC_MA_GOVERNANCE",
    "insurance_finance": "TG_TOPIC_INSURANCE_FINANCE",
    "regulation_policy": "TG_TOPIC_REGULATION_POLICY",
    "competitors_bigtech": "TG_TOPIC_COMPETITORS_BIGTECH",
    "digital_newbiz": "TG_TOPIC_DIGITAL_NEWBIZ",
    "global_finance": "TG_TOPIC_GLOBAL_FINANCE",
    "key_issues": "TG_TOPIC_KEY_ISSUES",
    "cs_top10": "TG_TOPIC_CS_TOP10",
    "cs_top10_links": "TG_TOPIC_CS_TOP10_LINKS",
    "daily_brief": "TG_TOPIC_DAILY_BRIEF",
}

BODY = (
    "<b>🛠 Strategy Pulse v1.0 — 정식 운영 전환 안내</b>\n"
    "\n"
    "테스트 운영을 마치고 오늘부터 정식 운영에 들어갑니다.\n"
    "\n"
    "<b>이번에 달라진 것</b>\n"
    "• <b>선정 기준 정비</b> — 키워드·우선순위 수칙을 선별 로직에 직접 반영했습니다.\n"
    "• <b>중복 정리</b> — 같은 사건은 <b>가장 넓은 내용을 담은 기사 한 건만</b> 올립니다.\n"
    "• <b>🚨 주요이슈</b> — 해킹·정보유출·제재 등 사고·피해는 점수와 무관하게 바로 올립니다.\n"
    "• <b>📌 Top10</b> — 매일 <b>06:50</b>에 발행합니다. 전날 16·18·22시와 당일 04시에 "
    "미리 뽑아 검증하므로, 건수가 모자라거나 날짜가 어긋난 채로 나가지 않습니다.\n"
    "• <b>원문 확보</b> — 유료·로그인 벽에 막힌 기사는 읽을 수 있는 곳으로 바꿔 싣습니다.\n"
    "\n"
    "이제 <b>24시간 중단 없이</b> 수집·발행됩니다.\n"
    "기준에 어긋난 기사나 중복이 보이면 알려주시면 바로 반영하겠습니다."
)


async def main():
    go = "--go" in sys.argv
    targets = []
    for tid, env in ENV.items():
        v = os.getenv(env)
        if v and str(v).strip().isdigit():
            targets.append((tid, int(v)))
        else:
            print(f"  [건너뜀] {tid} — {env} 없음")
    print(f"\n대상 토픽 {len(targets)}개: {', '.join(t for t, _ in targets)}")
    print("\n--- 게시될 내용 ---")
    print(BODY.replace("<b>", "").replace("</b>", ""))
    if not go:
        print("\n실제로 올리려면 --go 를 붙이세요.")
        return
    if not (TOKEN and CHAT):
        print("\n[중단] 토큰/채널을 읽지 못했습니다.")
        return
    ok = fail = 0
    async with httpx.AsyncClient(timeout=30) as cl:
        for tid, thread in targets:
            r = await cl.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                              json={"chat_id": CHAT, "message_thread_id": thread,
                                    "text": BODY, "parse_mode": "HTML",
                                    "link_preview_options": {"is_disabled": True}})
            d = r.json()
            if d.get("ok"):
                ok += 1
                print(f"  올림 {tid} → msg={d['result']['message_id']}")
            else:
                fail += 1
                print(f"  실패 {tid}: {d.get('description')}")
            await asyncio.sleep(1.2)
    print(f"\n성공 {ok}건 · 실패 {fail}건")

asyncio.run(main())
