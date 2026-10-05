"""오늘 Top10 이 아직 안 나갔는지 확인한다. 안 나갔으면 MISSED 를 찍는다.

워크플로 안에 파이썬을 히어독으로 끼워 넣었더니 `$( )` 안에서 히어독이
닫혀 셸 문법 오류가 났고, 그 스텝이 매 런 실패했다(2026-10-05 감사).
별도 파일로 뺀다.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings                                  # noqa: E402
from store import Store                                      # noqa: E402

KST = datetime.timezone(datetime.timedelta(hours=9))


def main() -> None:
    now = datetime.datetime.now(KST)
    hh, _, mm = settings.cs_top10_time.partition(":")
    due = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    # 발행 시각이 40분 넘게 지났는데 기록이 없으면 경보
    if now <= due + datetime.timedelta(minutes=40):
        return
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day1 = day0 + 24 * 3600
    if not Store(settings.db_path).agg_ran_on("cs_top10", day0, day1):
        print("MISSED")


if __name__ == "__main__":
    main()
