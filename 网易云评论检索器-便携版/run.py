#!/usr/bin/env python3
"""网易云音乐歌单评论爬虫 —— 启动脚本。

    python run.py --help
    python run.py --playlist 3778678 --user 123456789 --max-songs 3 --max-pages 1
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netease_crawler.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
