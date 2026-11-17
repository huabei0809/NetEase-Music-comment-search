#!/bin/bash
# ==============================================================
#  网易云评论检索器 —— 一键启动（macOS：双击本文件即可）
#
#  · 不需要安装任何第三方 Python 库
#  · 服务只监听本机（127.0.0.1），不对局域网/外网开放
#  · 关闭这个终端窗口，或在窗口里按 Ctrl+C，即可停止服务
#
#  ⚠ 双击后如果提示「已损坏，无法打开」「来自身份不明的开发者」：
#     这不是文件坏了，而是它经微信 / 网盘 / 邮件传递后，被 macOS 打上了
#     「隔离标记」(com.apple.quarantine)，Gatekeeper 因此拒绝运行脚本。
#     任选一种方式放行，只需做一次：
#       · 在「启动界面.command」上点右键 → 打开 → 再点「打开」
#       · 或打开「终端」执行（把本文件夹拖进去即可补全路径）：
#           xattr -dr com.apple.quarantine <本文件夹路径>
#     放行一次后，启动器会自动把整个文件夹的隔离标记一并清除。
# ==============================================================

cd "$(dirname "$0")" || exit 1

if [ ! -f "scripts/_find_python.sh" ]; then
  echo "文件不完整：缺少 scripts/_find_python.sh"
  echo "请确认整个文件夹是完整解压出来的（不要只复制单个文件）。"
  read -r -p "按回车键关闭…" _
  exit 1
fi
# shellcheck source=scripts/_find_python.sh
. "scripts/_find_python.sh"

PY="$(_find_python)"
if [ -z "$PY" ]; then
  _python_missing_help
  read -r -p "按回车键关闭…" _
  exit 1
fi

"$PY" scripts/launch.py

echo
read -r -p "按回车键关闭窗口…" _
