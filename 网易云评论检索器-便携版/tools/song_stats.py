"""对抓取结果做统计分析，产出可写入文档的指标与榜单。

用法::

    python tools/song_stats.py output_love/comments.db [--json stats.json]
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import sqlite3
from pathlib import Path

CST = datetime.timezone(datetime.timedelta(hours=8))


def to_dt(ms: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000, CST)


def collect(db_path: str | Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    q = conn.execute

    song = dict(q("SELECT * FROM songs").fetchone() or {})
    total = q("SELECT COUNT(*) n FROM comments").fetchone()["n"]
    total_likes = q("SELECT COALESCE(SUM(liked_count),0) n FROM comments").fetchone()["n"]
    t_min = q("SELECT MIN(time) n FROM comments").fetchone()["n"]
    t_max = q("SELECT MAX(time) n FROM comments").fetchone()["n"]

    # 年份分布
    year_counter: collections.Counter[int] = collections.Counter()
    month_counter: collections.Counter[str] = collections.Counter()
    hour_counter: collections.Counter[int] = collections.Counter()
    for (t,) in q("SELECT time FROM comments"):
        d = to_dt(t)
        year_counter[d.year] += 1
        month_counter[f"{d.year}-{d.month:02d}"] += 1
        hour_counter[d.hour] += 1

    # 地域分布（只统计有效值）
    loc_counter = collections.Counter(
        r["ip_location"]
        for r in q("SELECT ip_location FROM comments")
        if r["ip_location"] and r["ip_location"] not in ("未知", "中国")
    )
    with_ip = sum(loc_counter.values())

    top_liked = [
        {
            "nickname": r["nickname"],
            "content": r["content"],
            "liked_count": r["liked_count"],
            "time": to_dt(r["time"]).strftime("%Y-%m-%d %H:%M"),
            "ip_location": r["ip_location"],
            "user_id": r["user_id"],
        }
        for r in q(
            "SELECT * FROM comments ORDER BY liked_count DESC, time DESC LIMIT 30"
        )
    ]

    top_users = [
        {
            "nickname": r["nickname"],
            "user_id": r["user_id"],
            "count": r["n"],
            "likes": r["l"],
        }
        for r in q(
            "SELECT user_id, nickname, COUNT(*) n, SUM(liked_count) l "
            "FROM comments GROUP BY user_id ORDER BY n DESC, l DESC LIMIT 30"
        )
    ]

    longest = [
        {
            "nickname": r["nickname"],
            "content": r["content"],
            "length": len(r["content"]),
            "liked_count": r["liked_count"],
            "time": to_dt(r["time"]).strftime("%Y-%m-%d %H:%M"),
        }
        for r in q(
            "SELECT * FROM comments WHERE TRIM(content) <> '' "
            "ORDER BY LENGTH(content) DESC LIMIT 15"
        )
    ]

    return {
        "song": {
            "song_id": song.get("song_id"),
            "name": song.get("name", ""),
            "artists": song.get("artists", ""),
            "album": song.get("album", ""),
            "duration_ms": song.get("duration", 0),
            "comment_total_reported": song.get("comment_total", 0),
        },
        "totals": {
            "comments": total,
            "unique_users": q("SELECT COUNT(DISTINCT user_id) n FROM comments").fetchone()["n"],
            "total_likes": total_likes,
            "max_likes": q("SELECT MAX(liked_count) n FROM comments").fetchone()["n"],
            "with_ip": with_ip,
            "replies": q("SELECT COUNT(*) n FROM comments WHERE parent_comment_id <> 0").fetchone()["n"],
            "empty_content": q("SELECT COUNT(*) n FROM comments WHERE TRIM(content) = ''").fetchone()["n"],
            "avg_len": round(q("SELECT AVG(LENGTH(content)) n FROM comments").fetchone()["n"] or 0, 1),
            "max_len": q("SELECT MAX(LENGTH(content)) n FROM comments").fetchone()["n"],
        },
        "range": {
            "start": to_dt(t_min).strftime("%Y-%m-%d %H:%M:%S"),
            "end": to_dt(t_max).strftime("%Y-%m-%d %H:%M:%S"),
        },
        "by_year": dict(sorted(year_counter.items())),
        "by_month_top": dict(month_counter.most_common(12)),
        "by_hour": dict(sorted(hour_counter.items())),
        "by_location": dict(loc_counter.most_common(30)),
        "top_liked": top_liked,
        "top_users": top_users,
        "longest": longest,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="评论数据分析")
    ap.add_argument("db", help="comments.db 路径")
    ap.add_argument("--json", dest="json_out", default="", help="把统计结果写成 JSON")
    args = ap.parse_args()

    st = collect(args.db)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写入 {args.json_out}")

    s, t = st["song"], st["totals"]
    print(f"《{s['name']}》 - {s['artists']}  专辑《{s['album']}》")
    print(f"评论 {t['comments']} 条 / 用户 {t['unique_users']} 人 / 获赞 {t['total_likes']:,}")
    print(f"时间跨度 {st['range']['start']} ~ {st['range']['end']}")
    print("\n按年份：")
    peak = max(st["by_year"].values())
    for y, n in st["by_year"].items():
        print(f"  {y}  {n:>6}  {'█' * round(n / peak * 40)}")
    print("\n点赞 TOP 5：")
    for c in st["top_liked"][:5]:
        print(f"  ♥{c['liked_count']:>5} [{c['time'][:10]}] {c['nickname'][:16]} {c['content'][:40]!r}")
    print("\n地域 TOP 8：")
    for k, v in list(st["by_location"].items())[:8]:
        print(f"  {k:>8} {v:>5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
