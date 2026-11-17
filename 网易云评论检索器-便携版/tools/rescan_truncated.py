#!/usr/bin/env python3
"""重扫并发时被截断的曲目（末页恰好 1000 条 = 满页处中断），并查找目标 uid。"""
import sys, concurrent.futures as cf
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让脚本能被放在任意目录
from netease_crawler.client import WeapiClient
from netease_crawler.api import NeteaseAPI, ORDER_NEWEST_FIRST

#: 目标 uid。默认是占位示例值，实际用时从命令行传入，避免把真实 uid 写进代码：
#:     python tools/rescan_truncated.py 345678901
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 123456789
CASES = [(2006472449, "黑白艺术家"), (2006472448, "小镇里的花 (民乐版)")]

def scan(sid, name):
    client = WeapiClient(min_interval=0.3); api = NeteaseAPI(client)
    total = 0; pages = 0; last = 0; hits = []; err = ""
    try:
        for page in api.iter_song_comments(sid, page_size=1000, order_type=ORDER_NEWEST_FIRST):
            pages += 1; total += page.raw_comment_count; last = page.raw_comment_count
            for c in page.comments:
                if c.user_id == TARGET: hits.append((name, c))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    natural = "自然结束" if 0 < last < 1000 else f"末页满 {last} 条(可疑截断)"
    print(f"  {name:<22} 扫描 {total:>7} 条 / {pages:>3} 页  末页 {last:>4} 条 → {natural}  命中 {len(hits)}  {err}", flush=True)
    return hits

with cf.ThreadPoolExecutor(max_workers=2) as pool:
    res = list(pool.map(lambda a: scan(*a), CASES))
allh = [h for r in res for h in r]
print(f"\n==> 3 首中 uid={TARGET} 命中 {len(allh)} 条")
for name, c in sorted(allh, key=lambda x: -x[1].time):
    print(f"[{c.time_str}] 《{name}》 ♥{c.liked_count} {c.nickname!r}\n    {c.content}")
