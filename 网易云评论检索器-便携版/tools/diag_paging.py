#!/usr/bin/env python3
"""诊断：单线程逐页翻，找出提前终止的原因（空页 / cursor 停滞）。"""
import sys, time, concurrent.futures as cf
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让脚本能被放在任意目录
from netease_crawler.client import WeapiClient
from netease_crawler.api import NeteaseAPI, ORDER_NEWEST_FIRST

CASES = [(2006472438, "小镇里的花", 112756), (2006471567, "走，一起去看日出吧", 115070)]

def diag(sid, name, expect):
    client = WeapiClient(min_interval=0.3); api = NeteaseAPI(client)
    total = 0; pages = 0; lens = []; cursors = []
    stop = "未知"
    try:
        for page in api.iter_song_comments(sid, page_size=1000, order_type=ORDER_NEWEST_FIRST):
            pages += 1
            total += page.raw_comment_count
            lens.append(page.raw_comment_count)
            cursors.append(str(page.next_cursor))
            if pages > 200:
                stop = "达到 200 页保护上限"; break
    except Exception as e:
        stop = f"异常 {type(e).__name__}: {e}"
    if pages and lens and lens[-1] == 0:
        stop = "末页为空页（服务端正常结束）"
    elif pages and len(set(cursors[-3:])) == 1:
        stop = "cursor 停止前进（服务端不再给新数据）"
    print(f"\n=== {name} (song_id={sid}) 服务端报 {expect} 条 ===")
    print(f"  实扫 {total} 条 / {pages} 页   终止原因: {stop}")
    print(f"  首页 {lens[0] if lens else 0} 条 | 末页 {lens[-1] if lens else 0} 条")
    print(f"  最后 3 页条数: {lens[-3:]}")

with cf.ThreadPoolExecutor(max_workers=2) as p:
    list(p.map(lambda a: diag(*a), CASES))
