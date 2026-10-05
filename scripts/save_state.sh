#!/usr/bin/env bash
# 상태 DB 를 원격에 올린다. **rebase 를 쓰지 않는다.**
#
# 왜: `git rebase -X ours` 의 ours 는 rebase 대상(= origin/main)을 뜻한다.
# 그래서 SQL 로 합쳐 놓은 DB 를 원격 것으로 되돌리고, 트리가 같아지니 git 이
# 커밋을 빈 커밋으로 보고 버린다 — **exit 0 으로 "완료"를 찍으면서 그 회차
# 기록이 통째로 사라졌다**(2026-10-05 실측). 게다가 작업트리 DB 까지 되감겨
# 다음 회차가 같은 기사를 다시 발행했다.
#
# 대신 여기서는: 내 DB 를 따로 보관 → 베이스를 원격으로 맞춤(--hard) →
# 내 DB 복원 → 원격 행 흡수 → 커밋 → push. 경합하면 지터를 두고 재시도한다.
set -u
MSG="${1:-chore: 발행 이력 갱신 [skip ci]}"

if [[ -z "$(git status --porcelain botstate.sqlite3)" ]]; then
  echo "  [상태] 새 발행 없음 — 커밋 생략"
  exit 0
fi

TMP="$(mktemp -t state.XXXXXX.sqlite3)"
for i in 1 2 3 4 5; do
  cp -f botstate.sqlite3 "$TMP"
  git fetch -q origin main || true
  # --hard 다. --soft 로 하면 인덱스에 남은 옛 코드가 그대로 커밋돼
  # 사람이 방금 올린 수정을 되돌린다(2026-10-05 감사).
  git reset -q --hard origin/main || true
  cp -f "$TMP" botstate.sqlite3
  python tools/merge_state.py origin/main || true
  git add botstate.sqlite3 || true
  if git diff --cached --quiet; then
    echo "  [상태] 합친 뒤 변경 없음 — 생략"
    rm -f "$TMP"; exit 0
  fi
  git commit -q -m "$MSG" || true
  if git push -q origin HEAD:main; then
    echo "  [상태] 발행 이력 커밋·푸시 완료"
    rm -f "$TMP"; exit 0
  fi
  echo "  [상태] push 경합 — 재시도 $i"
  sleep $(( (RANDOM % 5) + i * 3 ))
done
rm -f "$TMP"
echo "::warning::상태 푸시 5회 실패 — 이 회차 기록이 유실될 수 있습니다"
