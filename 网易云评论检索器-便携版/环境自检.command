#!/bin/bash
# ==============================================================
#  网易云评论检索器 —— 运行环境自检（macOS：双击本文件）
#
#  会检查：Python 版本 / 加密与网络回退通道 / 前端文件是否齐全
#          / 能否连上网易云
#  排查"双击了没反应""点了检索没动静"之类问题时，先跑这个。
#
#  ⚠ 若双击后提示「已损坏，无法打开」：不是文件坏了，是 macOS 的隔离标记
#     在起作用（文件经微信/网盘传递后都会带），先用下面任一方式放行：
#       · 右键 → 打开 → 再点「打开」
#       · 终端执行：xattr -dr com.apple.quarantine <本文件夹路径>
# ==============================================================

cd "$(dirname "$0")" || exit 1

if [ ! -f "scripts/_find_python.sh" ]; then
  echo "文件不完整：缺少 scripts/_find_python.sh"
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

"$PY" scripts/launch.py --check

echo
read -r -p "按回车键关闭窗口…" _
