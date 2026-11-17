"""存储层：SQLite 落库、幂等去重、断点续传、多格式导出。

为什么用 SQLite 而不是直接写 CSV
--------------------------------
1. **去重**：多线程并发时同一首歌可能被重复抓取（重试、续传），
   用 ``comment_id`` 做主键 + ``INSERT OR IGNORE`` 天然幂等；
2. **断点续传**：记 ``song_progress`` 表，中断后按歌曲粒度续爬；
3. **统计与筛选**：最后按 ``matched=1`` 导出，比在内存里过滤 40 万条评论稳得多。
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from .models import Comment, Song

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS songs (
    song_id       INTEGER PRIMARY KEY,
    name          TEXT,
    artists       TEXT,
    album         TEXT,
    duration      INTEGER,
    comment_total INTEGER DEFAULT 0,
    scanned_count INTEGER DEFAULT 0,
    cursor        TEXT DEFAULT '-1',
    done          INTEGER DEFAULT 0,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id        INTEGER PRIMARY KEY,
    song_id           INTEGER NOT NULL,
    content           TEXT,
    time              INTEGER,
    time_str          TEXT,
    time_rel          TEXT,
    liked_count       INTEGER,
    ip_location       TEXT,
    user_id           INTEGER,
    nickname          TEXT,
    avatar_url        TEXT,
    vip_type          INTEGER,
    parent_comment_id INTEGER,
    replied_user_id   INTEGER,
    replied_nickname  TEXT,
    replied_content   TEXT,
    matched           INTEGER DEFAULT 0,
    match_reason      TEXT,
    crawled_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_id);
CREATE INDEX IF NOT EXISTS idx_comments_song ON comments(song_id);
CREATE INDEX IF NOT EXISTS idx_comments_matched ON comments(matched);

CREATE TABLE IF NOT EXISTS matches (
    comment_id  INTEGER PRIMARY KEY,
    song_id     INTEGER,
    song_name   TEXT,
    song_artist TEXT,
    user_id     INTEGER,
    nickname    TEXT,
    content     TEXT,
    time        INTEGER,
    time_str    TEXT,
    liked_count INTEGER,
    ip_location TEXT,
    match_reason TEXT
);
"""

COMMENT_COLUMNS = [
    "comment_id", "song_id", "content", "time", "time_str", "time_rel",
    "liked_count", "ip_location", "user_id", "nickname", "avatar_url",
    "vip_type", "parent_comment_id", "replied_user_id", "replied_nickname",
    "replied_content", "matched", "match_reason", "crawled_at",
]

#: 后加的列 → 老库用 ALTER TABLE 补上，避免 CREATE TABLE IF NOT EXISTS 不生效
MIGRATIONS = {
    "comments": {"time_rel": "TEXT"},
    # matches 原本只有 time_str（相对时间字符串，排序错乱），补上毫秒时间戳
    "matches": {"time": "INTEGER"},
}


class Storage:
    """线程安全的 SQLite 封装（单连接 + 写锁）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """给已存在的老库补新列（``CREATE TABLE IF NOT EXISTS`` 不会加列）。"""
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------ 元信息
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------------ 歌曲
    def upsert_songs(self, songs: Iterable[Song]) -> None:
        rows = [
            (s.song_id, s.name, s.artists, s.album, s.duration,
             s.comment_total, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            for s in songs
        ]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                """INSERT INTO songs(song_id, name, artists, album, duration,
                                     comment_total, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(song_id) DO UPDATE SET
                       name=excluded.name, artists=excluded.artists,
                       album=excluded.album, duration=excluded.duration,
                       comment_total=excluded.comment_total,
                       updated_at=excluded.updated_at""",
                rows,
            )
            self._conn.commit()

    def update_song_total(self, song_id: int, total: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE songs SET comment_total=? WHERE song_id=?", (total, song_id)
            )
            self._conn.commit()

    # -------------------------------------------------------- 断点续传
    def get_progress(self, song_id: int) -> dict | None:
        """返回 ``{"cursor", "scanned_count", "done"}``；无记录返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor, scanned_count, done FROM songs WHERE song_id=?",
                (song_id,),
            ).fetchone()
        if not row:
            return None
        return {"cursor": row["cursor"] or "-1",
                "scanned_count": row["scanned_count"] or 0,
                "done": int(row["done"] or 0)}

    def save_progress(self, song_id: int, cursor: str, scanned_count: int,
                      done: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO songs(song_id, cursor, scanned_count, done, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(song_id) DO UPDATE SET
                       cursor=excluded.cursor,
                       scanned_count=excluded.scanned_count,
                       done=excluded.done,
                       updated_at=excluded.updated_at""",
                (song_id, str(cursor), int(scanned_count), int(done),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._conn.commit()

    # ------------------------------------------------------------ 评论
    def save_comments(self, comments: Sequence[Comment]) -> int:
        """批量写入，返回**真正新增**的条数（重复的会被忽略）。"""
        if not comments:
            return 0
        rows = [tuple(c.to_dict()[col] for col in COMMENT_COLUMNS) for c in comments]
        placeholders = ",".join("?" * len(COMMENT_COLUMNS))
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                f"INSERT OR IGNORE INTO comments({','.join(COMMENT_COLUMNS)}) "
                f"VALUES({placeholders})",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def refresh_matches(self, order_asc: bool = False) -> int:
        """把 ``matched=1`` 的评论物化到 matches 表（带歌曲名，便于阅读）。

        :param order_asc: True 时按评论时间**正序**（最早在前），默认倒序。
        """
        order = "ASC" if order_asc else "DESC"
        with self._lock:
            self._conn.execute("DELETE FROM matches")
            self._conn.execute(
                f"""INSERT INTO matches
                   (comment_id, song_id, song_name, song_artist, user_id, nickname,
                    content, time, time_str, liked_count, ip_location, match_reason)
                   SELECT c.comment_id, c.song_id, s.name, s.artists, c.user_id,
                          c.nickname, c.content, c.time, c.time_str, c.liked_count,
                          c.ip_location, c.match_reason
                   FROM comments c LEFT JOIN songs s ON s.song_id = c.song_id
                   WHERE c.matched = 1
                   ORDER BY c.time {order}"""
            )
            self._conn.commit()
            return self._conn.execute("SELECT COUNT(*) n FROM matches").fetchone()["n"]

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict:
        with self._lock:
            q = self._conn.execute
            return {
                "songs_total": q("SELECT COUNT(*) n FROM songs").fetchone()["n"],
                "songs_done": q("SELECT COUNT(*) n FROM songs WHERE done=1").fetchone()["n"],
                "comments": q("SELECT COUNT(*) n FROM comments").fetchone()["n"],
                "matched": q("SELECT COUNT(*) n FROM comments WHERE matched=1").fetchone()["n"],
                "unique_users": q("SELECT COUNT(DISTINCT user_id) n FROM comments").fetchone()["n"],
            }

    # ------------------------------------------------------------ 导出
    def export_matches(self, out_dir: str | Path, formats: Sequence[str] = ("csv", "json", "jsonl")) -> list[Path]:
        """导出命中记录，默认同时产出 csv / json / jsonl 三种。"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                # 必须按毫秒时间戳排；time_str 是相对时间字符串，按它排会错乱
                "SELECT * FROM matches ORDER BY time DESC"
            ).fetchall()]

        written: list[Path] = []
        if "csv" in formats:
            p = out_dir / "matched_comments.csv"
            with p.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                                        ["comment_id", "song_id", "song_name", "user_id",
                                         "nickname", "content", "time", "time_str"])
                writer.writeheader()
                writer.writerows(rows)
            written.append(p)

        if "json" in formats:
            p = out_dir / "matched_comments.json"
            p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(p)

        if "jsonl" in formats:
            p = out_dir / "matched_comments.jsonl"
            with p.open("w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            written.append(p)

        return written

    def export_all_comments(self, path: str | Path) -> Path:
        """导出全部抓到的评论（调试 / 复用分析时用）。"""
        path = Path(path)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM comments ORDER BY song_id, time DESC"
            ).fetchall()]
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return path

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
