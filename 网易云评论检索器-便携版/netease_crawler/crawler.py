"""并发编排层：把"歌单 → 歌曲 → 评论"串起来跑。

并发模型
--------
* **歌单内多首歌并行**（线程池）：不同歌曲之间互不依赖，是天然的并行维度；
* **同一首歌内部串行**：cursor 分页必须顺序推进，强行并发会重复抓取；
* **全局限速**：由 :class:`WeapiClient` 的 ``RateLimiter`` 统一控制总速率，
  避免"线程数 × 单线程速率"把总 QPS 放大到触发风控。

这样设计的收益：单首歌 2 秒/千条，4 线程并行时等效吞吐约 2000 条/秒。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .api import MAX_PAGE_SIZE, ORDER_NEWEST_FIRST, NeteaseAPI, make_matcher
from .client import ApiError, WeapiClient
from .models import Comment
from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class CrawlConfig:
    playlist_id: int | None = None
    song_ids: Sequence[int] = field(default_factory=list)

    target_user_ids: Sequence[int] = field(default_factory=list)
    target_nickname: str = ""
    keyword: str = ""
    save_all: bool = False           # True 时不筛选，全部计入命中

    page_size: int = MAX_PAGE_SIZE
    # 爬取方向恒为降序（orderType=1）——这是唯一能正确翻页的模式。
    # `oldest_first_output` 只影响导出时的排序，不影响爬取过程。
    oldest_first_output: bool = False
    workers: int = 4
    min_interval: float = 0.35
    max_pages_per_song: int = 0      # 0 = 不限制
    max_songs: int = 0               # 0 = 不限制
    stop_on_match: bool = False      # 命中后提前结束该首歌
    resume: bool = True
    cookie: str = ""
    proxy: str = ""
    db_path: str = "output/comments.db"
    out_dir: str = "output"
    verbose: bool = False


@dataclass
class CrawlStats:
    songs_total: int = 0
    songs_done: int = 0
    songs_failed: int = 0
    songs_skipped: int = 0
    comments_fetched: int = 0
    comments_new: int = 0
    matched: int = 0
    #: 疑似被服务端限流截断（末页恰好满页）的曲目数
    truncated_songs: int = 0
    started_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0


def validate_config(cfg: CrawlConfig) -> None:
    """在发起任何网络请求前拦掉逻辑上不可能成立的组合。

    ``--order oldest`` 的实现是"全量降序爬完 → 按时间正序导出"，
    因为网易云 orderType=0 只返回首页部分数据且翻不动页。
    配上 ``--max-pages`` 会让用户以为抽到最早的一批，实际抽到的是最新的一批
    —— 这种静默错误必须直接拦下。
    """
    if cfg.oldest_first_output and cfg.max_pages_per_song:
        raise ValueError(
            "无法做『最早若干条』的抽样：网易云 orderType=0 只返回首页部分数据，"
            "且 cursor 与 offset 都无法继续翻页（实测均返回空）。\n"
            "  · 想看最早的评论 → 去掉 --max-pages 全量爬取，再用 --order oldest 导出；\n"
            "  · 只想抽样看最新评论 → 用 --order newest 配合 --max-pages。"
        )
    if not cfg.playlist_id and not cfg.song_ids:
        raise ValueError("必须提供 playlist_id 或 song_ids 之一")


class Crawler:
    """按歌单批量抓取歌曲评论并筛选目标用户。"""

    def __init__(self, config: CrawlConfig):
        self.cfg = config
        self.client = WeapiClient(
            cookie=config.cookie or None,
            min_interval=config.min_interval,
            proxy=config.proxy or None,
        )
        self.api = NeteaseAPI(self.client)
        self.storage = Storage(config.db_path)
        self.stats = CrawlStats()
        self.matcher = make_matcher(
            user_ids=config.target_user_ids,
            nickname=config.target_nickname,
            keyword=config.keyword,
            match_all=config.save_all,
        )
        self._stop = threading.Event()

    # ------------------------------------------------------------ 前置准备
    def resolve_songs(self) -> list[int]:
        """确定要抓哪些歌：优先显式 song_ids，否则拉歌单曲目。"""
        if self.cfg.song_ids:
            ids = [int(i) for i in self.cfg.song_ids]
            logger.info("使用显式指定的 %d 首歌", len(ids))
        else:
            if not self.cfg.playlist_id:
                raise ValueError("必须提供 playlist_id 或 song_ids 之一")
            info = self.api.playlist_info(self.cfg.playlist_id)
            logger.info(
                "歌单《%s》（by %s）共 %d 首",
                info["name"], info["creator"], info["track_count"],
            )
            self.storage.set_meta("playlist_id", str(info["id"]))
            self.storage.set_meta("playlist_name", info["name"])
            ids = info["song_ids"]

        # 去重保序
        ids = list(dict.fromkeys(ids))
        if self.cfg.max_songs:
            ids = ids[: self.cfg.max_songs]
            logger.info("按 --max-songs 截取前 %d 首", len(ids))

        # 回填歌曲元信息（歌单详情里的 tracks 明细会被截断，必须另取）
        try:
            songs = self.api.songs_detail(ids)
            self.storage.upsert_songs(songs)
            logger.info("已缓存 %d 首歌的元信息", len(songs))
        except ApiError as exc:
            logger.warning("歌曲详情获取失败，继续抓评论（导出时歌名会为空）：%s", exc)

        return ids

    def _should_skip(self, song_id: int) -> bool:
        if not self.cfg.resume:
            return False
        prog = self.storage.get_progress(song_id)
        if prog and prog["done"]:
            self.stats.songs_skipped += 1
            return True
        return False

    # ------------------------------------------------------------ 单曲抓取
    def _crawl_song(self, song_id: int) -> None:
        """抓取单首歌的全部（或指定页数的）评论。"""
        if self._stop.is_set():
            return

        start_cursor = "-1"
        scanned = 0
        if self.cfg.resume:
            prog = self.storage.get_progress(song_id)
            if prog:
                start_cursor = prog["cursor"]
                scanned = prog["scanned_count"]

        page_no = 0
        last_page_raw = 0
        try:
            for chunk in self.api.iter_song_comments(
                song_id,
                page_size=self.cfg.page_size,
                # 固定降序：orderType=0 只会返回首页部分数据且无法翻页
                order_type=ORDER_NEWEST_FIRST,
                max_pages=self.cfg.max_pages_per_song,
                start_cursor=start_cursor,
                matcher=self.matcher,
                stop_on_match=self.cfg.stop_on_match,
            ):
                page_no += 1
                last_page_raw = chunk.raw_comment_count
                new = self.storage.save_comments(chunk.comments)
                matched_here = sum(1 for c in chunk.comments if c.matched)

                with self.stats._lock:
                    self.stats.comments_fetched += chunk.raw_comment_count
                    self.stats.comments_new += new
                    self.stats.matched += matched_here

                scanned += chunk.raw_comment_count
                if chunk.total_count:
                    self.storage.update_song_total(song_id, chunk.total_count)

                if self.cfg.verbose or matched_here:
                    logger.info(
                        "song %-10s 第%2d页 本页%d条 新增%d 命中%d 累计%d/%d",
                        song_id, page_no, chunk.raw_comment_count, new,
                        matched_here, scanned, chunk.total_count or 0,
                    )

                # 每页都落一次进度 → 进程被杀也能续传
                self.storage.save_progress(
                    song_id, chunk.next_cursor, scanned, done=False
                )

            # 服务端限流时会在「满页」处静默截断——不报错、不返回空页，只是停住。
            # 实测 6 并发下 3 首歌各少 3~4 万条。自然结束的末页必定不满 page_size，
            # 所以末页恰好满页就是最可靠的截断信号。
            if (
                not self.cfg.max_pages_per_song
                and last_page_raw
                and last_page_raw == self.cfg.page_size
            ):
                with self.stats._lock:
                    self.stats.truncated_songs += 1
                logger.warning(
                    "song %s 疑似被限流截断：末页恰好满 %d 条（共扫描 %d 条）。"
                    "服务端限流时会静默停在满页处，不报错也不返回空页。"
                    "建议降低 --workers / 调大 --interval 后重跑该曲。",
                    song_id, self.cfg.page_size, scanned,
                )

            self.storage.save_progress(song_id, "-1", scanned, done=True)
            with self.stats._lock:
                self.stats.songs_done += 1

        except ApiError as exc:
            with self.stats._lock:
                self.stats.songs_failed += 1
            logger.error("song %s 抓取失败：%s", song_id, exc)

    # ------------------------------------------------------------ 主流程
    def run(self) -> CrawlStats:
        self.stats.started_at = time.monotonic()
        validate_config(self.cfg)

        logger.info("=" * 68)
        logger.info("目标：user_ids=%s nickname=%r keyword=%r",
                    list(self.cfg.target_user_ids) or "-",
                    self.cfg.target_nickname, self.cfg.keyword)
        logger.info("并发 %d 线程 | pageSize=%d | 全局最小间隔 %.2fs",
                    self.cfg.workers, self.cfg.page_size, self.cfg.min_interval)
        logger.info("=" * 68)

        song_ids = self.resolve_songs()
        pending = [sid for sid in song_ids if not self._should_skip(sid)]
        self.stats.songs_total = len(song_ids)

        logger.info("待抓取 %d 首（跳过已完成 %d 首）",
                    len(pending), self.stats.songs_skipped)

        try:
            if self.cfg.workers <= 1:
                for sid in pending:
                    self._crawl_song(sid)
            else:
                with ThreadPoolExecutor(max_workers=self.cfg.workers) as pool:
                    futures = {pool.submit(self._crawl_song, sid): sid for sid in pending}
                    for fut in as_completed(futures):
                        sid = futures[fut]
                        try:
                            fut.result()
                        except Exception as exc:  # 单曲失败不该拖垮整个歌单
                            with self.stats._lock:
                                self.stats.songs_failed += 1
                            logger.error("song %s 线程异常：%s", sid, exc)
        except KeyboardInterrupt:
            logger.warning("收到中断信号，正在保存进度…")
            self._stop.set()
            raise

        n = self.storage.refresh_matches(order_asc=self.cfg.oldest_first_output)
        logger.info("已把 %d 条命中记录写入 matches 表（排序：%s）",
                    n, "时间正序" if self.cfg.oldest_first_output else "时间倒序")
        return self.stats

    # ------------------------------------------------------------ 收尾
    def report(self) -> str:
        s = self.stats
        st = self.storage.stats()
        elapsed = s.elapsed
        rate = st["comments"] / elapsed if elapsed > 0 else 0
        lines = [
            "",
            "=" * 68,
            "抓取结果",
            "=" * 68,
            f"歌单曲目        : {st['songs_total']} 首（已完成 {st['songs_done']} / 跳过 {s.songs_skipped} / 失败 {s.songs_failed}）",
            f"评论累计（去重）: {st['comments']} 条",
            f"涉及用户数      : {st['unique_users']} 人",
            f"命中目标        : {st['matched']} 条",
            f"总耗时          : {elapsed:.1f}s（平均 {rate:.0f} 条/秒）",
            f"请求次数        : {self.client.request_count}",
            f"数据库          : {Path(self.cfg.db_path).resolve()}",
        ]
        if s.truncated_songs:
            lines.insert(
                -1,
                f"⚠ 疑似被限流截断: {s.truncated_songs} 首（末页恰好满页）"
                f" → 请降低 --workers 后重跑这些曲目",
            )
        lines.append("=" * 68)
        return "\n".join(lines)

    def close(self) -> None:
        self.storage.close()
        self.client.close()

    def __enter__(self) -> "Crawler":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
