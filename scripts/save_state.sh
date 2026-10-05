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

# ── 아래 두 가드는 **사람이 로컬에서 돌릴 때만** 건다 ──────────────
#
# 러너에서는 걸지 않는다. 러너는 체크아웃 직후라 '원격이 진실' 이 항상
# 참이고, 거기서 중단되면 그 회차의 수집 기록이 통째로 날아간다 — 호출부가
# `|| true` 로 감싸고 있어 **봇은 멀쩡히 도는데 기록만 안 남는다.** 가장
# 알아채기 어려운 고장이다. 발주자 지정(2026-10-05): "봇에는 절대 영향
# 미치면 안 돼."
if [ -z "${CI:-}" ]; then

# **코드가 미커밋이면 손대지 않는다.** 아래 루프는 git reset --hard 를
# 쓴다 — 상태 DB 만 바꿔 올리는 것이 목적이고 코드는 원격이 진실이라는
# 전제다. 사람이 로컬에서 고치던 중에 이걸 돌리면 그 수정이 날아간다.
_dirty="$(git status --porcelain -- . ':(exclude)botstate.sqlite3')"
if [ -n "$_dirty" ]; then
  echo "  [상태] ✗ 커밋되지 않은 코드 변경이 있습니다 — 중단합니다"
  echo "$_dirty"
  echo "  먼저 커밋하거나 되돌린 뒤 다시 실행하세요."
  exit 1
fi
# **푸시되지 않은 커밋도 막는다.** 미커밋 변경만 막았더니, 커밋은 했으나
# 아직 올리지 않은 수정이 reset --hard 로 날아갔다 — 이 가드를 넣은
# 커밋 자신이 그렇게 사라졌다(2026-10-05). reflog 로 되찾았다.
git fetch -q origin main || true
_ahead="$(git rev-list --count origin/main..HEAD -- . ':(exclude)botstate.sqlite3' 2>/dev/null || echo 0)"
if [ "${_ahead:-0}" != "0" ]; then
  echo "  [상태] ✗ 올리지 않은 코드 커밋이 ${_ahead}개 있습니다 — 중단합니다"
  git log --oneline origin/main..HEAD -- . ':(exclude)botstate.sqlite3'
  echo "  먼저 git push 한 뒤 다시 실행하세요."
  exit 1
fi

fi   # ← 로컬 전용 가드 끝

TMP="$(mktemp -t state.XXXXXX.sqlite3)"
for i in 1 2 3 4 5; do
  cp -f botstate.sqlite3 "$TMP"
  git fetch -q origin main || true
  # --hard 다. --soft 로 하면 인덱스에 남은 옛 코드가 그대로 커밋돼
  # 사람이 방금 올린 수정을 되돌린다(2026-10-05 감사).
  git reset -q --hard origin/main || true
  cp -f "$TMP" botstate.sqlite3
  # **인터프리터를 찾아 쓴다.** `python` 을 박아 두었더니 로컬 zsh 에는
  # 그 이름이 없어 "command not found" 로 떨어졌고, `|| true` 가 그걸
  # 삼켜 **병합 없이** 로컬 DB 를 그대로 커밋했다. 원격 회차의 수집분이
  # 통째로 사라졌다(2026-10-05 실측: seen 19건·published 1건 유실).
  # 병합은 이 스크립트의 존재 이유이므로, 실패하면 **푸시하지 않는다.**
  PY=""
  for cand in ./venv/bin/python ./venv/bin/python3 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "  [상태] ✗ 파이썬을 찾지 못했습니다 — 병합 없이는 푸시하지 않습니다"
    cp -f "$TMP" botstate.sqlite3
    exit 1
  fi
  if ! "$PY" tools/merge_state.py origin/main; then
    echo "  [상태] ✗ 병합 실패 — 원격 기록이 사라질 수 있어 푸시를 중단합니다"
    cp -f "$TMP" botstate.sqlite3
    exit 1
  fi
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
