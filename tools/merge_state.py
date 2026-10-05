"""원격 상태 DB 를 로컬에 **병합**한다 (git rebase 대신).

`botstate.sqlite3` 는 바이너리라 git 이 병합하지 못한다. 그래서 러너가
`git pull --rebase` 를 하면 반드시 충돌하고, 워크플로는 `rebase --abort`
후 push 실패를 echo 만 남긴다 — **그 회차에 수집·발행한 기록이 통째로
사라진다.** 로컬에서 사람이 푸시하는 동안에는 이게 매번 일어난다
(2026-10-05 실측: 봇의 마지막 상태 커밋 17:01, 이후 2시간 유실).

여기서는 git 에게 병합을 맡기지 않는다. 원격 DB 를 내려받아 **SQL 수준에서
행을 합친다.** 양쪽 모두의 수집·발행 기록이 남는다.

사용법:  python tools/merge_state.py          # origin/main 과 병합
"""
import os, subprocess, sqlite3, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "botstate.sqlite3")
TABLES = ("digest_log", "iv_page", "judgment", "published",
          "resolved_url", "seen", "seen_urls", "top10_draft",
          # setting 이 빠져 있어 draft_slot·draft_heal 마커가
          # 유실됐다(2026-10-05 감사).
          "setting")


def cols(c, t, db="main"):
    return [x[1] for x in c.execute(f"PRAGMA {db}.table_info({t})")]


def main():
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    try:
        blob = subprocess.run(["git", "show", f"{ref}:botstate.sqlite3"],
                              cwd=ROOT, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError:
        print("[merge_state] 원격 DB 를 읽지 못했습니다 — 병합 생략")
        return
    if not blob:
        print("[merge_state] 원격 DB 가 비어 있습니다 — 병합 생략")
        return

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        tmp.write(blob)
        remote = tmp.name

    c = sqlite3.connect(DB)
    try:
        c.execute("ATTACH ? AS r", (remote,))
    except sqlite3.Error as exc:
        print(f"[merge_state] 원격 DB 열기 실패 — {exc}")
        return

    added = 0
    for t in TABLES:
        shared = [x for x in cols(c, t) if x in cols(c, t, "r")]
        if not shared:
            continue
        before = c.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
        cl = ",".join(f'"{x}"' for x in shared)
        c.execute(f"INSERT OR IGNORE INTO main.{t} ({cl}) SELECT {cl} FROM r.{t}")
        after = c.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
        if after != before:
            print(f"[merge_state] {t}: +{after - before}")
            added += after - before

    # published 의 빈 칸은 원격 값으로 채운다 (로컬에서 쓴 값은 보존)
    filled = 0
    for col in [x for x in cols(c, "published")
                if x in cols(c, "published", "r") and x != "key"]:
        filled += c.execute(
            f'UPDATE main.published SET "{col}" ='
            f' (SELECT r2."{col}" FROM r.published r2 WHERE r2.key = main.published.key)'
            f' WHERE "{col}" IS NULL AND EXISTS'
            f' (SELECT 1 FROM r.published r2 WHERE r2.key = main.published.key'
            f'  AND r2."{col}" IS NOT NULL)').rowcount
    c.commit()
    os.unlink(remote)
    print(f"[merge_state] 병합 완료 — 행 {added}건 추가, 빈 칸 {filled}개 보충")


if __name__ == "__main__":
    main()
