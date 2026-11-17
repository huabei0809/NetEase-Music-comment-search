#!/usr/bin/env python3
"""定向扫描一张专辑的全部曲目，只挑出指定 uid 的评论（不落库，纯内存过滤）。

用途：已知某个 uid，想知道 Ta 在这批歌里评论过哪些。
设计取舍：
  * 不写入 SQLite —— 目标只是"找一个人"，落库 144 万条既慢又占空间；
  * 歌与歌并发、单曲内串行 —— cursor 分页不能并发，否则会重复抓；
  * 只用 uid 精确比较，不走 matcher —— 避免匹配语义带来的歧义。
"""

from __future__ import annotations

import concurrent.futures as cf
import sys
from pathlib import Path
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让脚本能被放在任意目录

from netease_crawler.api import ORDER_NEWEST_FIRST, NeteaseAPI  # noqa: E402
from netease_crawler.client import WeapiClient  # noqa: E402

#: 目标 uid。默认是占位示例值，实际用时从命令行传入，避免把真实 uid 写进代码：
#:     python tools/scan_album_for_uid.py 345678901
TARGET_UID = int(sys.argv[1]) if len(sys.argv) > 1 else 123456789

#: 《希忘Hope》(专辑 id 156507145) 全部曲目；(song_id, 歌名)
ALBUM = [
    (2006471955, "飞行模式"),
    (2006472438, "小镇里的花"),
    (2006472448, "小镇里的花 (民乐版)"),
    (2006472449, "黑白艺术家"),
    (2006472464, "好想我回来啊"),
    (2006472465, "花落时相遇"),
    (2006471567, "走，一起去看日出吧"),
    (2006484446, "向阳而生"),
    (2006485344, "点燃银河尽头的篝火"),
    (2006485349, "当全世界忘了我"),
    (2006484469, "虚幻与现实"),
]

_print_lock = threading.Lock()


@dataclass
class SongScan:
    song_id: int
    name: str
    scanned: int = 0
    pages: int = 0
    hits: list = None
    error: str = ""

    def __post_init__(self):
        if self.hits is None:
            self.hits = []


def scan_song(song_id: int, name: str) -> SongScan:
    """扫完一首歌的全部评论，返回统计与命中。"""
    result = SongScan(song_id=song_id, name=name)
    try:
        client = WeapiClient(min_interval=0.35)
        api = NeteaseAPI(client)
        for page in api.iter_song_comments(
            song_id, page_size=1000, order_type=ORDER_NEWEST_FIRST
        ):
            result.pages += 1
            result.scanned += page.raw_comment_count
            for c in page.comments:
                if c.user_id == TARGET_UID:
                    result.hits.append(c)
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    with _print_lock:
        flag = f"★命中 {len(result.hits)} 条" if result.hits else "无"
        print(
            f"  {name:<24} 扫描 {result.scanned:>7} 条 / {result.pages:>3} 页  {flag}"
            f"{'  错误: ' + result.error if result.error else ''}",
            flush=True,
        )
    return result


def main() -> int:
    t0 = time.monotonic()
    print(f"目标 uid = {TARGET_UID}；专辑《希忘Hope》共 {len(ALBUM)} 首，开始定向扫描……", flush=True)
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda a: scan_song(*a), ALBUM))

    total = sum(r.scanned for r in results)
    all_hits = [c for r in results for c in r.hits]
    print(f"\n合计扫描 {total} 条评论，耗时 {time.monotonic() - t0:.0f} 秒", flush=True)
    print(f"==> uid={TARGET_UID} 命中 {len(all_hits)} 条\n", flush=True)

    for c in sorted(all_hits, key=lambda x: -x.time):
        song = next((n for i, n in ALBUM if i == c.song_id), str(c.song_id))
        print(f"[{c.time_str}] 《{song}》 ♥{c.liked_count} 昵称={c.nickname!r}")
        print(f"    {c.content}")

    failed = [r.name for r in results if r.error]
    if failed:
        print(f"\n注意：以下曲目抓取异常，结果可能不完整 —— {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
