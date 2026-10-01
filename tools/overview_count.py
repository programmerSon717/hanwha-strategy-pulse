"""총괄 브리핑(overview)이 지금까지 몇 번 발행됐는지 센다.

워크플로가 다이제스트 실행 전후로 이 값을 비교해 '이번 회차에 브리핑이 실제로
나갔는지'를 판정한다. 로그 문구를 grep 하는 방식은 문구가 바뀌면 조용히 깨진다.

셸에서 쓰기 좋게 숫자 한 줄만 찍는다. 실패해도 0 을 찍어 루프를 세우지 않는다.
"""
import os
import sqlite3

try:
    db = os.getenv("DB_PATH", "botstate.sqlite3")
    con = sqlite3.connect(db)
    print(con.execute(
        "SELECT count(*) FROM digest_log WHERE scope='overview'").fetchone()[0])
except Exception:
    print(0)
