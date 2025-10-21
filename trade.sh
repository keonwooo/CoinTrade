#!/bin/bash
# ============================
# coinTrade 자동 실행 스크립트
# ============================

VENV_PATH="/home/kw/coinTrade/venv"
PYTHON_BIN="${VENV_PATH}/bin/python"
SCRIPT="coinTrade2.py"
LOG_DIR="/home/kw/coinTrade/logs_sh"

MODES=("basic" "volatility" "rsi" "volume")

source ${VENV_PATH}/bin/activate

mkdir -p "$LOG_DIR"

start_bot() {
    MODE=$1
    LOG_FILE=${LOG_DIR}/run_{MODE}.log

    echo "${MODE} 모드 시작.."
    nohup $PYTHON_BIN "$SCRIPT" --mode "$MODE" > "$LOG_FILE" 2>&1 &
    PID=$!
    echo $PID > "${LOG_DIR}/${MODE}.pid"
    echo "${MODE} 실행 중 (PID: $PID)"
}

stop_bot() {
    MODE=$1
    PID_FILE="${LOG_DIR}/${MODE}.pid"

    if [ ! -f "$PID_FILE" ]; then
        echo "${MODE} PID 파일 없음 (중지 상태)"
        return
    fi

    PID=$(cat "$PID_FILE")
    if ps -p $PID > /dev/null 2>&1; then
        echo "${MODE} 종료 중 (PID: $PID)..."
        kill $PID
        sleep 1
        if ps -p $PID > /dev/null 2>&1; then
            echo "강제 종료 시도"
            kill -9 $PID
        fi
    else
        echo "${MODE} PID(${PID}) 프로세스가 이미 종료됨"
    fi

    rm -f "$PID_FILE"
    echo "${MODE} 종료 완료"
}

status_bot() {
    MODE=$1
    PID_FILE="${LOG_DIR}/${MODE}.pid"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p $PID > /dev/null 2>&1; then
            echo "${MODE} 실행 중 (PID: $PID)"
        else
            echo "${MODE} 프로세스 없음 (PID 파일만 존재)"
        fi
    else
        echo "${MODE} 중지됨"
    fi
}

usage() {
    MODES_STR=$(IFS=,; echo "${MODES[*]}")
    echo "사용법:"
    echo "  $0 start [mode]     - 해당 모드 시작 (${MODES_STR})"
    echo "  $0 stop [mode]      - 해당 모드 종료"
    echo "  $0 status [mode]    - 해당 모드 상태 확인"
    echo "  $0 stop all         - 모든 모드 종료"
}

case "$1" in
    start)
        if [ "$2" = "all" ]; then
            for m in "${MODES[@]}"; do
                start_bot "$m"
            done
        else
            start_bot "$2"
        fi
        ;;
    stop)
        if [ "$2" = "all" ]; then
            for m in "${MODES[@]}"; do
                stop_bot "$m"
            done
        else
            stop_bot "$2"
        fi
        ;;
    status)
        if [ -z "$2" ]; then
            for m in "${MODES[@]}"; do
                status_bot "$m"
            done
        else
            status_bot "$2"
        fi
        ;;
    *)
    usage
    ;;
esac