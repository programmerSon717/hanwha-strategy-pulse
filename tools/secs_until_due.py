#!/usr/bin/env python
"""다음 '하루 한 번' 발행 시각까지 남은 초. 없으면 아주 큰 수.

**bot.yml 의 대기 루프가 이걸 보고 잠을 자른다.** 20분마다 물어보던 구조에서는
06:50 에 발행해야 할 것이 06:49 회차를 놓치고 07:09 회차에 잡혀 07:10 에 나갔다
(2026-10-03 실측). 발행 시각에 정확히 깨어나야 정시에 나간다.

출력은 정수 한 줄. 이미 지났거나 오늘 발행이 끝난 시각은 세지 않는다.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings   # noqa: E402

KST = timezone(timedelta(hours=9))
FAR = 86400


def main() -> int:
    now = datetime.now(KST)
    best = FAR
    for hhmm in (settings.cs_top10_time, settings.daily_brief_time):
        try:
            hh, _, mm = str(hhmm).partition(":")
            sched = now.replace(hour=int(hh), minute=int(mm),
                                second=0, microsecond=0)
        except ValueError:
            continue
        # 이미 지난 시각은 다음 회차가 알아서 --if-due 로 잡는다. 여기선 '앞으로'만 본다.
        if sched <= now:
            continue
        best = min(best, int((sched - now).total_seconds()))
    print(best)
    return 0


if __name__ == "__main__":
    sys.exit(main())
