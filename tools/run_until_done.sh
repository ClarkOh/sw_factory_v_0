#!/bin/sh
# 파이프라인을 완주할 때까지 돌린다. 한도에 걸리면 풀릴 때까지 기다렸다 이어간다.
#
# exit 0  = COMPLETE   -> 끝
# exit 2  = 게이트/한도 정지 -> 워커가 살아나면 재개 (게이트 대기면 워커 탐침이
#           통과해도 run 이 즉시 다시 2로 나오므로, 같은 정지 지점이 반복되면 멈춘다)
CFG=$1
LOG=$2
prev=""
while :; do
    PYTHONUTF8=1 python -m factory.cli -c "$CFG" run >> "$LOG" 2>&1
    rc=$?
    [ $rc -eq 0 ] && { echo "[run_until_done] COMPLETE" >> "$LOG"; exit 0; }
    now=$(tail -3 "$LOG" | grep "정지 지점" | tail -1)
    if [ "$now" = "$prev" ] && ! tail -5 "$LOG" | grep -q "워커를 쓸 수 없습니다"; then
        echo "[run_until_done] 같은 지점 반복 -- 사람 판단 필요" >> "$LOG"; exit $rc
    fi
    prev="$now"
    # 한도가 풀릴 때까지 10분 간격 탐침 (한도 중엔 토큰을 안 쓴다)
    while echo ping | claude -p 2>&1 | grep -q "limit"; do sleep 600; done
done
