#!/bin/bash
# ==============================================================
#  网易云评论检索器 —— 命令行启动（Linux / macOS）
#
#    ./start.sh              启动网页界面（默认 http://127.0.0.1:8765/）
#    ./start.sh check        只做环境自检
#    ./start.sh 9000         指定端口启动
#    ./start.sh 9000 --no-browser
#
#  不需要安装任何第三方 Python 库。
# ==============================================================

cd "$(dirname "$0")" || exit 1

ARGS=()
for arg in "$@"; do
  case "$arg" in
    check) ARGS+=("--check") ;;
    -*)    ARGS+=("$arg") ;;
    ''|*[!0-9]*)
      echo "无法识别的参数：$arg"
      echo "用法：./start.sh [端口] [check] [--no-browser]"
      exit 2 ;;
    *)     ARGS+=("--port" "$arg") ;;
  esac
done

_find_py() {
  local cand p
  for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9 \
              /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if [ -x "$cand" ]; then p="$cand"
    else p="$(command -v "$cand" 2>/dev/null)" || continue; [ -x "$p" ] || continue
    fi
    if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
         >/dev/null 2>&1; then printf '%s' "$p"; return 0; fi
  done
  return 1
}

PY="$(_find_py)"
if [ -z "$PY" ]; then
  echo "没有找到 Python（需要 3.8 或更高版本）。"
  echo "  Debian/Ubuntu： sudo apt install python3"
  echo "  CentOS/RHEL  ： sudo yum install python3"
  echo "  macOS        ： https://www.python.org/downloads/"
  exit 1
fi

exec "$PY" scripts/launch.py "${ARGS[@]}"
