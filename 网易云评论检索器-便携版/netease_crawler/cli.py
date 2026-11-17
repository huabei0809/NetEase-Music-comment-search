"""命令行入口。

用法示例
--------
# 找某个 uid 在某歌单全部歌曲里的评论
python run.py --playlist 3778678 --user 123456789

# 按昵称找（重名风险大，建议先搜索确认 uid）
python run.py --playlist 3778678 --nickname "ccrek"

# 按关键词找评论
python run.py --playlist 3778678 --keyword "考研"

# 只抓指定的几首歌，全部保存（不筛选）
python run.py --song-ids 186016,347230 --all

# 小规模试跑：只抓前 5 首、每首最多 2 页
python run.py --playlist 3778678 --user 123456789 --max-songs 5 --max-pages 2
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .api import NeteaseAPI
from .client import WeapiClient
from .crawler import CrawlConfig, Crawler, validate_config

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
DATE_FORMAT = "%H:%M:%S"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="netease-comment-crawler",
        description="网易云音乐歌单评论批量爬取 + 目标用户筛选",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    src = p.add_argument_group("数据来源（二选一）")
    src.add_argument("--playlist", type=int, help="歌单 ID，例如 3778678")
    src.add_argument("--song-ids", type=str, default="",
                     help="直接指定歌曲 ID，逗号分隔，例如 186016,347230")

    tgt = p.add_argument_group("筛选目标（不填则全部保存）")
    tgt.add_argument("--user", type=str, default="",
                     help="目标用户 uid，多个用逗号分隔（最可靠）")
    tgt.add_argument("--nickname", type=str, default="", help="目标用户昵称（精确匹配）")
    tgt.add_argument("--keyword", type=str, default="", help="评论正文包含的关键词")
    tgt.add_argument("--all", action="store_true",
                     help="不筛选，保存所有抓到的评论")

    tune = p.add_argument_group("抓取调优")
    tune.add_argument("--page-size", type=int, default=1000,
                      help="每页评论数，实测上限 1000（默认 1000）")
    tune.add_argument("--order", choices=["newest", "oldest"], default="newest",
                      help="结果的排列顺序：newest=最新在前（默认），"
                           "oldest=最早在前。注意爬取方向恒为降序，"
                           "oldest 只影响导出排序，且不能与 --max-pages 连用")
    tune.add_argument("--workers", type=int, default=4, help="并发线程数（默认 4）")
    tune.add_argument("--interval", type=float, default=0.35,
                      help="全局最小请求间隔秒数，调大可降低风控风险（默认 0.35）")
    tune.add_argument("--max-pages", type=int, default=0,
                      help="单首歌最多翻多少页（0=不限）")
    tune.add_argument("--max-songs", type=int, default=0,
                      help="最多处理多少首歌（0=不限）")
    tune.add_argument("--stop-on-found", action="store_true",
                      help="单首歌内一旦命中就停止（大幅提速，但会漏掉后续命中）")
    tune.add_argument("--cookie", type=str, default="",
                      help="登录 Cookie，例如 'MUSIC_U=xxx; __csrf=yyy'（可选，可降低风控）")
    tune.add_argument("--proxy", type=str, default="", help="代理，例如 http://127.0.0.1:7890")
    tune.add_argument("--no-resume", action="store_true",
                      help="忽略已有进度，从头重爬")

    out = p.add_argument_group("输出")
    out.add_argument("--out", type=str, default="output", help="输出目录（默认 ./output）")
    out.add_argument("-v", "--verbose", action="store_true", help="打印每一页的明细")

    util = p.add_argument_group("辅助工具")
    util.add_argument("--resolve-song", type=str, default="",
                      help="按歌名/歌手搜索歌曲，打印候选 song_id 后退出")
    util.add_argument("--resolve-user", type=str, default="",
                      help="按昵称搜索用户，打印候选 uid 后退出")
    util.add_argument("--playlist-info", type=int, default=0,
                      help="只打印歌单信息后退出")
    util.add_argument("--user-info", type=int, default=0,
                      help="只打印用户信息后退出")
    return p


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_ids(raw: str) -> list[int]:
    out: list[int] = []
    for chunk in (raw or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    out_dir = Path(args.out)
    db_path = out_dir / "comments.db"

    # ---------------- 辅助工具：不发爬取任务，只查信息后退出 ----------------
    if (args.resolve_user or args.resolve_song or args.playlist_info
            or args.user_info):
        client = WeapiClient(cookie=args.cookie or None, proxy=args.proxy or None,
                             min_interval=args.interval)
        api = NeteaseAPI(client)
        try:
            if args.resolve_song:
                songs = api.search_songs(args.resolve_song, limit=10)
                if not songs:
                    print("未找到歌曲，换个关键词试试")
                for s in songs:
                    print(f"song_id={s['song_id']:<12} 《{s['name']}》 - {s['artists']}"
                          f"   [专辑: {s['album']}]")
            if args.resolve_user:
                users = api.search_users(args.resolve_user)
                if not users:
                    print("未找到用户。实测网易云用户搜索接口在匿名状态下恒返回空，"
                          "请直接在网页端打开该用户主页，从 URL 的 id= 取值")
                for u in users:
                    print(f"uid={u['user_id']:<12} 昵称={u['nickname']}")
            if args.playlist_info:
                info = api.playlist_info(args.playlist_info)
                print(f"歌单 ID   : {info['id']}")
                print(f"名称      : {info['name']}")
                print(f"创建者    : {info['creator']}")
                print(f"曲目数    : {info['track_count']}")
                print(f"可读取 ID : {len(info['song_ids'])}")
            if args.user_info:
                u = api.user_detail(args.user_info)
                print(f"uid={u['user_id']} 昵称={u['nickname']} "
                      f"等级={u['level']} 听歌={u['listen_songs']}")
        finally:
            client.close()
        return 0

    song_ids = _parse_ids(args.song_ids)
    if not args.playlist and not song_ids:
        print("错误：请用 --playlist 或 --song-ids 指定数据来源（-h 查看帮助）",
              file=sys.stderr)
        return 2

    config = CrawlConfig(
        playlist_id=args.playlist,
        song_ids=song_ids,
        target_user_ids=_parse_ids(args.user),
        target_nickname=args.nickname,
        keyword=args.keyword,
        save_all=args.all,
        page_size=args.page_size,
        oldest_first_output=(args.order == "oldest"),
        workers=max(1, args.workers),
        min_interval=args.interval,
        max_pages_per_song=args.max_pages,
        max_songs=args.max_songs,
        stop_on_match=args.stop_on_found,
        resume=not args.no_resume,
        cookie=args.cookie,
        proxy=args.proxy,
        db_path=str(db_path),
        out_dir=str(out_dir),
        verbose=args.verbose,
    )

    log = logging.getLogger("main")
    try:
        validate_config(config)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    crawler = Crawler(config)
    try:
        stats = crawler.run()
    except KeyboardInterrupt:
        log.warning("已中断。下次运行时进度会自动续传（默认开启 --resume）")
        stats = crawler.stats
    finally:
        print(crawler.report())
        try:
            files = crawler.storage.export_matches(out_dir)
            if files:
                print("已导出：")
                for f in files:
                    print(f"  - {f.resolve()}")
            else:
                print("没有命中任何评论，未生成导出文件。")
        except Exception as exc:  # 导出失败不影响已入库数据
            log.error("导出失败：%s", exc)
        crawler.close()

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
