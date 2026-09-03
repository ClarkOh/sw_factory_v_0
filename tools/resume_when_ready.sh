#!/bin/sh
# 사용량 한도가 풀리면 파이프라인을 이어 돌린다.
#
# 왜 탐침을 먼저 두는가. 한도에 걸린 상태로 `run` 을 호출하면 그 단계의 생성이
# 한 번 실패로 기록되고 멈춘다. 해롭진 않지만 로그가 지저분해지고, 무엇보다
# "몇 번 시도했는가"가 실제 결함과 섞인다. 탐침은 한도에 걸려 있을 때
# 즉시 돌아오고 토큰을 쓰지 않는다.

WS=/c/work/sw_factory
LOG=$WS/projects/vending_machine/.factory/v8.log
CFG=$WS/projects/vending_machine/factory.yaml

while :; do
    if ! echo ping | claude -p 2>&1 | grep -q "session limit"; then
        break
    fi
    sleep 600
done

echo "[$(date +%H:%M:%S)] 한도 해제 확인 -- 파이프라인 재개" >> "$LOG"
cd "$WS" || exit 1
PYTHONUTF8=1 python -m factory.cli -c "$CFG" run >> "$LOG" 2>&1
