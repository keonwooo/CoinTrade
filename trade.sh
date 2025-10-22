#!/bin/bash
# ============================
# coinTrade 자동 실행 스크립트 (무로그 + 무PID)
# ============================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

VENV_PATH="${SCRIPT_DIR}/venv"
PYTHON_BIN="${VENV_PATH}/bin/python"
SCRIPT_PATH="${SCRIPT_DIR}/src"
SCRIPT_NAME="coinTrade2.py"
SCRIPT_FULL="${SCRIPT_PATH}/${SCRIPT_NAME}"

MODES=("basic" "volatility" "rsi" "volume" "rsi_trend")

source "${VENV_PATH}/bin/activate" 2>/dev/null

is_valid_mode() {
  local target="$1"
  for m in "${MODES[@]}"; do
    [ "$m" = "$target" ] && return 0
  done
  return 1
}

start_bot() {
  local MODE="$1"
  if ! is_valid_mode "$MODE"; then
    echo "알 수 없는 모드: $MODE"; return 1
  fi

  # 이미 같은 모드 실행 중인지 확인
  local pid
  pid=$(pgrep -f "${SCRIPT_FULL} --mode ${MODE}")
  if [ -n "$pid" ]; then
    echo "${MODE} 이미 실행 중 (PID: $pid)"
    return 0
  fi

  echo "${MODE} 모드 시작"
  nohup "$PYTHON_BIN" "$SCRIPT_FULL" --mode "$MODE" --logs-root "$SCRIPT_DIR" >/dev/null 2>&1 &
  echo "${MODE} 실행 중 (PID: $!)"
}

stop_bot() {
  local MODE="$1"
  if ! is_valid_mode "$MODE"; then
    echo "알 수 없는 모드: $MODE"; return 1
  fi

  local pids
  pids=$(pgrep -f "${SCRIPT_FULL} --mode ${MODE}")
  if [ -z "$pids" ]; then
    echo "${MODE} 실행 중 아님"
    return 0
  fi

  echo "${MODE} 종료 중..."
  pkill -f "${SCRIPT_FULL} --mode ${MODE}"
  sleep 1
  echo "${MODE} 종료 완료"
}

status_bot() {
  local MODE="$1"
  if ! is_valid_mode "$MODE"; then
    echo "알 수 없는 모드: $MODE"; return 1
  fi

  local pid
  pid=$(pgrep -f "${SCRIPT_FULL} --mode ${MODE}")
  if [ -n "$pid" ]; then
    echo "${MODE} 실행 중 (PID: $pid)"
  else
    echo "${MODE} 중지됨"
  fi
}

usage() {
  local MODES_STR
  MODES_STR=$(IFS=,; echo "${MODES[*]}")
  echo "사용법:"
  echo "  $0 start [mode|all]  - 모드 시작 (${MODES_STR})"
  echo "  $0 stop [mode|all]   - 모드 종료"
  echo "  $0 status [mode]     - 상태 확인"
}

case "$1" in
  start)
    if [ "$2" = "all" ]; then
      for m in "${MODES[@]}"; do start_bot "$m"; done
    else
      start_bot "$2"
    fi
    ;;
  stop)
    if [ "$2" = "all" ]; then
      for m in "${MODES[@]}"; do stop_bot "$m"; done
    else
      stop_bot "$2"
    fi
    ;;
  status)
    if [ -n "$2" ]; then
      status_bot "$2"
    else
      for m in "${MODES[@]}"; do status_bot "$m"; done
    fi
    ;;
  *)
    usage
    ;;
esac
