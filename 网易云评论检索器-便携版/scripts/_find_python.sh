#!/bin/bash
# 共用的 Python 探测函数 —— 被「启动界面.command」和「环境自检.command」source。
# 输出：把可用的 python 可执行文件（可能带参数，如 "python3"）打印到 stdout。
# 返回：0 = 找到，1 = 没找到。

_find_python() {
  local cand p
  # 依次尝试常见命令名与常见安装路径
  for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9 \
              /opt/homebrew/bin/python3 /usr/local/bin/python3 \
              /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do

    if [ -x "$cand" ]; then
      p="$cand"
    else
      p="$(command -v "$cand" 2>/dev/null)" || continue
      [ -x "$p" ] || continue
    fi

    # macOS 上 /usr/bin/python3 是 Xcode 命令行工具的"壳"：
    # 工具没装时运行它会弹出一个安装对话框并卡住，这里直接跳过。
    if [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then
      continue
    fi

    # 版本需 >= 3.8
    if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
         >/dev/null 2>&1; then
      printf '%s' "$p"
      return 0
    fi
  done
  return 1
}

# 找不到 Python 时打印的友好指引
_python_missing_help() {
  cat <<'MSG'
──────────────────────────────────────────────────────────────
没有找到 Python（需要 3.8 或更高版本）。

本工具的界面由 Python 提供，所以必须先装一个。两种办法任选：

  方法一（推荐）：打开 https://www.python.org/downloads/
                  下载 macOS 安装包，一路点「继续 / 安装」即可。

  方法二：在「终端」里粘贴执行下面这行（安装 Xcode 命令行工具）：
              xcode-select --install

装好后，重新双击本文件即可。
──────────────────────────────────────────────────────────────
MSG
}
