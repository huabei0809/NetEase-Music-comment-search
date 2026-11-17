#!/usr/bin/env python3
"""网易云评论检索器 —— 本地 Web 后端。

设计立场
--------
本服务**不接入任何 AI**：整条链路都是确定性算法。
    输入条件 -> 关键词搜索接口解析出 song_id
             -> weapi 加密请求 + cursor 翻页拿全量评论
             -> 用 uid / 昵称做精确字符串比较筛出目标
这些步骤没有一步需要模型推理，所以它可以完全离线自洽地跑起来
（只需要能访问 music.163.com）。

依赖：仅 Python 标准库 + 项目已有的 requests，无需 Flask 之类框架。
业务逻辑全部复用 ``netease_crawler`` 包，不重复实现。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netease_crawler.api import (  # noqa: E402
    MAX_PAGE_SIZE,
    ORDER_NEWEST_FIRST,
    NeteaseAPI,
)
from netease_crawler.client import ApiError, WeapiClient  # noqa: E402
from netease_crawler.models import Song  # noqa: E402

STATIC_DIR = HERE / "static"

#: 歌单/专辑一次最多展开多少首（防止误点一个 9000 首歌单把界面拖死）
MAX_EXPAND = 300


# ===================================================================== 任务模型
@dataclass
class SongTask:
    song_id: int
    name: str = ""
    artists: str = ""
    album: str = ""
    total_count: int = 0      # 服务端报告的评论总数（实测常低估）
    scanned: int = 0          # 实际扫描条数
    pages: int = 0
    hits: int = 0
    status: str = "pending"   # pending / running / done / error / stopped
    truncated: bool = False   # 末页恰好满页 => 疑似被服务端限流静默截断
    rescanned: bool = False
    error: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["label"] = self.name or f"song_id={self.song_id}"
        return d


@dataclass
class Job:
    id: str
    songs: list[SongTask]
    uid: list[int]
    nickname: str
    fuzzy: bool
    opts: dict
    status: str = "running"   # running / done / stopped / error
    message: str = ""
    hits: list[dict] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    stop_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> dict:
        with self.lock:
            elapsed = (self.finished_at or time.monotonic()) - self.started_at
            scanned = sum(s.scanned for s in self.songs)
            return {
                "id": self.id,
                "status": self.status,
                "message": self.message,
                "elapsed": round(elapsed, 1),
                "rate": round(scanned / elapsed) if elapsed > 0.5 else 0,
                "songs_total": len(self.songs),
                "songs_done": sum(1 for s in self.songs if s.status in ("done", "error", "stopped")),
                "songs_failed": sum(1 for s in self.songs if s.status == "error"),
                "songs_truncated": sum(1 for s in self.songs if s.truncated),
                "scanned": scanned,
                "hits": len(self.hits),
                "target": {
                    "uid": self.uid,
                    "nickname": self.nickname,
                    "fuzzy": self.fuzzy,
                },
                "songs": [s.to_dict() for s in self.songs],
                "results": sorted(self.hits, key=lambda h: -h["time"]),
            }

    def drop_song_hits(self, song_id: int) -> None:
        with self.lock:
            self.hits = [h for h in self.hits if h["song_id"] != song_id]


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


# ===================================================================== 匹配逻辑
def _match(comment, uids: set[int], nickname: str, fuzzy: bool) -> list[str]:
    """返回命中原因列表；空列表表示未命中。

    规则与 CLI 的 ``make_matcher`` 保持一致：
      * uid 精确相等（最可靠，首选）
      * 昵称精确相等；勾选"模糊"后改为包含匹配（重名风险大，仅作辅助）
    两者是"或"的关系，任一命中即算。
    """
    reasons: list[str] = []
    if uids and comment.user_id in uids:
        reasons.append(f"uid={comment.user_id}")
    if nickname:
        if fuzzy and nickname in comment.nickname:
            reasons.append(f"昵称包含「{nickname}」")
        elif not fuzzy and comment.nickname == nickname:
            reasons.append(f"昵称={nickname}")
    return reasons


def _hit_dict(c, task: SongTask, reasons: list[str]) -> dict:
    return {
        "comment_id": c.comment_id,
        "song_id": c.song_id,
        "song_name": task.name or f"song_id={c.song_id}",
        "user_id": c.user_id,
        "nickname": c.nickname,
        "content": c.content,
        "time": c.time,
        "time_str": c.time_str,
        "liked_count": c.liked_count,
        "ip_location": c.ip_location,
        "match_reason": " + ".join(reasons),
        "reply_to": c.replied_nickname,
        "reply_content": c.replied_content,
    }


# ===================================================================== 扫描执行
def _scan_one(
    job: Job,
    task: SongTask,
    *,
    interval: float,
    page_size: int = MAX_PAGE_SIZE,
    max_pages: int = 0,
) -> None:
    """扫完一首歌的全部评论，命中即收进 job.hits（不落库，纯内存）。"""
    t0 = time.monotonic()
    task.status = "running"
    client = WeapiClient(
        cookie=job.opts.get("cookie") or None,
        min_interval=interval,
    )
    api = NeteaseAPI(client)
    exhausted = False
    last_raw = 0
    try:
        for page in api.iter_song_comments(
            task.song_id,
            page_size=page_size,
            order_type=ORDER_NEWEST_FIRST,  # 恒降序：orderType=0 无法翻页
            max_pages=max_pages,
        ):
            if job.stop_event.is_set():
                task.status = "stopped"
                return

            task.pages += 1
            last_raw = page.raw_comment_count
            task.scanned += page.raw_comment_count
            if page.total_count:
                task.total_count = page.total_count

            page_hits = []
            for c in page.comments:
                reasons = _match(c, set(job.uid), job.nickname, job.fuzzy)
                if reasons:
                    page_hits.append(_hit_dict(c, task, reasons))

            if page_hits:
                with job.lock:
                    job.hits.extend(page_hits)
                    task.hits += len(page_hits)

            if page_hits and job.opts.get("stop_on_match"):
                return

        exhausted = True
        # 服务端限流时会「停在满页处」——不报错、不返回空页。
        # 自然结束的末页必定不满 page_size，所以满页就是最可靠的截断信号。
        if exhausted and not max_pages and last_raw and last_raw == page_size:
            task.truncated = True
        task.status = "done"
    except Exception as exc:  # noqa: BLE001 —— 单曲失败不该拖垮整个任务
        task.status = "error"
        task.error = f"{type(exc).__name__}: {exc}"
        _log(f"  [{task.label}] 抓取失败：{task.error}")
    finally:
        task.elapsed = round(time.monotonic() - t0, 1)
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _rescan_truncated(job: Job, interval: float, page_size: int, max_pages: int) -> None:
    """对疑似被截断的曲目串行重扫一遍（降并发 + 放宽间隔）。"""
    targets = [s for s in job.songs if s.truncated]
    if not targets:
        return
    _log(f"检测到 {len(targets)} 首疑似被限流截断，开始串行重扫……")
    for task in targets:
        if job.stop_event.is_set():
            return
        job.drop_song_hits(task.song_id)
        with job.lock:
            task.hits = 0
            task.scanned = 0
            task.pages = 0
            task.truncated = False
            task.rescanned = True
            task.status = "pending"
        _scan_one(job, task, interval=interval * 2.5, page_size=page_size, max_pages=max_pages)


def _run_job(job: Job) -> None:
    opts = job.opts
    workers = max(1, min(4, int(opts.get("workers") or 3)))
    interval = float(opts.get("interval") or 0.35)
    page_size = min(MAX_PAGE_SIZE, int(opts.get("page_size") or MAX_PAGE_SIZE))
    max_pages = int(opts.get("max_pages") or 0)

    _log(
        f"任务 {job.id} 启动：{len(job.songs)} 首歌 | 并发 {workers} | "
        f"间隔 {interval}s | 目标 uid={job.uid or '-'} 昵称={job.nickname or '-'}"
    )
    try:
        if workers == 1:
            for task in job.songs:
                if job.stop_event.is_set():
                    break
                _scan_one(job, task, interval=interval, page_size=page_size, max_pages=max_pages)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        _scan_one, job, task,
                        interval=interval, page_size=page_size, max_pages=max_pages,
                    )
                    for task in job.songs
                ]
                for fut in futures:
                    fut.result()

        if not job.stop_event.is_set() and opts.get("auto_rescan", True):
            _rescan_truncated(job, interval, page_size, max_pages)

        with job.lock:
            job.status = "stopped" if job.stop_event.is_set() else "done"
    except Exception as exc:  # noqa: BLE001
        with job.lock:
            job.status = "error"
            job.message = f"{type(exc).__name__}: {exc}"
        _log(f"任务 {job.id} 异常：{traceback.format_exc()}")
    finally:
        job.finished_at = time.monotonic()
        snap = job.snapshot()
        _log(
            f"任务 {job.id} 结束：{snap['status']}，扫描 {snap['scanned']} 条评论，"
            f"命中 {snap['hits']} 条，耗时 {snap['elapsed']}s"
        )


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ===================================================================== 歌曲解析
def _parse_ref(text: str) -> tuple[str, Any]:
    """把一行输入识别成 (类型, 值)。

    纯数字 ID 在网易云里是**有歧义**的：歌曲、歌单、专辑的 ID 都长一个样，
    没有任何位数规律可以区分（实测专辑 156507145 与一个歌单 ID 正好撞车）。
    所以额外支持显式前缀 ``song:`` / ``playlist:`` / ``album:`` 来消歧；
    没写前缀时按 歌曲 -> 歌单 -> 专辑 的顺序试探，并把结果明写在提示里。
    """
    t = text.strip()
    m = re.fullmatch(r"(song|playlist|album)\s*[:：]\s*(\d{1,19})", t, re.IGNORECASE)
    if m:
        return m.group(1).lower(), int(m.group(2))
    if "music.163.com" in t:
        m = re.search(r"(song|playlist|album)(?:/[^?\s]*)?\?[^\s]*?id=(\d+)", t)
        if not m:
            m = re.search(r"(song|playlist|album)/(\d+)", t)
        if m:
            return m.group(1), int(m.group(2))
        return "unknown", t
    if re.fullmatch(r"\d{1,19}", t):
        return "numeric", int(t)
    return "keyword", t


def _song_item(song: Song, source: str) -> dict:
    return {
        "song_id": song.song_id,
        "name": song.name,
        "artists": song.artists,
        "album": song.album,
        "duration": song.duration,
        "comment_total": song.comment_total,
        "source": source,
    }


def _expand_album(api: NeteaseAPI, album_id: int) -> tuple[list[dict], str]:
    raw = api.client.weapi(f"/weapi/v1/album/{album_id}", {})
    album = raw.get("album") or {}
    raw_songs = raw.get("songs") or []
    ids = [int(s["id"]) for s in raw_songs if s.get("id")]
    if not ids:
        return [], f"专辑 {album_id}：没有读到曲目"
    ids = ids[:MAX_EXPAND]
    details = {s.song_id: s for s in api.songs_detail(ids)}
    items = []
    for sid in ids:
        s = details.get(sid)
        if s is None:
            s = Song(song_id=sid)
            s.album = str(album.get("name") or "")
        items.append(_song_item(s, "album"))
    return items, f"专辑《{album.get('name') or album_id}》共 {len(ids)} 首"


def _resolve_numeric(api: NeteaseAPI, ident: int) -> tuple[list[dict], str]:
    """纯数字输入：依次按 歌曲 -> 歌单 -> 专辑 试探。"""
    try:
        details = {s.song_id: s for s in api.songs_detail([ident])}
        if ident in details:
            return [_song_item(details[ident], "id")], f"ID {ident} 已按「歌曲」解析"
    except ApiError:
        pass
    try:
        info = api.playlist_info(ident)
        ids = info["song_ids"][:MAX_EXPAND]
        if ids:
            details = {s.song_id: s for s in api.songs_detail(ids)}
            items = [_song_item(details.get(i) or Song(song_id=i), "playlist") for i in ids]
            return items, (
                f"ID {ident} 已按「歌单」解析为《{info['name']}》(by {info['creator']})，"
                f"共 {info['track_count']} 首，已展开 {len(ids)} 首。"
                f"若你要的其实是专辑，请写成 album:{ident}"
            )
    except ApiError:
        pass
    try:
        items, note = _expand_album(api, ident)
        if items:
            return items, note
    except ApiError:
        pass
    return [], f"{ident}：既不是有效歌曲，也不是可读的歌单 / 专辑"


def resolve_query(query: str) -> dict:
    lines = [l.strip() for l in (query or "").splitlines() if l.strip()]
    if not lines:
        return {"items": [], "notes": ["请先输入歌曲名、歌曲 ID 或链接"]}

    items: list[dict] = []
    notes: list[str] = []
    seen: set[int] = set()

    def push(new_items: list[dict], note: str = "") -> None:
        if note:
            notes.append(note)
        for it in new_items:
            if it["song_id"] and it["song_id"] not in seen:
                seen.add(it["song_id"])
                items.append(it)

    # resolve 是「点一下等结果」的交互式请求：前端只等这一次，没有后台任务兜底。
    # 默认的 timeout=20 / retries=4 是给批量抓取用的，放在这里最坏会让一次解析
    # 挂到近 90 秒（数字输入还要串行试 歌曲→歌单→专辑 三步）。这里收紧为
    # 3 次尝试 × 10 秒 + 退避 ≈ 35 秒封顶，与前端的长超时配合。
    client = WeapiClient(min_interval=0.3, timeout=10.0, retries=2)
    api = NeteaseAPI(client)
    try:
        for line in lines:
            kind, value = _parse_ref(line)
            try:
                if kind == "song":
                    details = {s.song_id: s for s in api.songs_detail([int(value)])}
                    if int(value) in details:
                        push([_song_item(details[int(value)], "id")])
                    else:
                        notes.append(f"歌曲 ID {value} 无效")
                elif kind == "playlist":
                    info = api.playlist_info(int(value))
                    ids = info["song_ids"][:MAX_EXPAND]
                    details = {s.song_id: s for s in api.songs_detail(ids)}
                    push(
                        [_song_item(details.get(i) or Song(song_id=i), "playlist") for i in ids],
                        f"歌单《{info['name']}》(by {info['creator']}) 共 {info['track_count']} 首，"
                        f"已展开 {len(ids)} 首",
                    )
                elif kind == "album":
                    new_items, note = _expand_album(api, int(value))
                    push(new_items, note)
                elif kind == "numeric":
                    new_items, note = _resolve_numeric(api, int(value))
                    push(new_items, note or "")
                elif kind == "keyword":
                    found = api.search_songs(line, limit=8)
                    if not found:
                        notes.append(f"「{line}」没有搜到歌曲，换个关键词试试")
                    push(
                        [
                            {
                                "song_id": s["song_id"],
                                "name": s["name"],
                                "artists": s["artists"],
                                "album": s["album"],
                                "duration": s["duration"],
                                "comment_total": 0,
                                "source": "search",
                            }
                            for s in found
                        ],
                        f"「{line}」搜到 {len(found)} 个候选",
                    )
                else:
                    notes.append(f"无法识别：{line}")
            except ApiError as exc:
                notes.append(f"{line}：接口报错 {exc.message or exc.code}")
    finally:
        client.close()

    return {"items": items, "notes": notes}


# ===================================================================== HTTP 服务
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NeteaseCommentWeb/1.0"

    # ---------------------------------------------------------- 基础设施
    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 允许跨源调用：页面可能被别的本地预览服务器（或 file://）托管，
        # 那种情况下浏览器要发跨域请求，这里统一放行，省得用户还得对地址。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802 —— 跨域预检
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, _json_bytes(obj), "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path in ("/", "/index.html"):
            return self._file(STATIC_DIR / "index.html")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR.resolve() in target.parents and target.is_file():
                return self._file(target)
            return self._json({"error": "not found"}, 404)
        if path == "/api/job":
            job_id = _qs(query, "id")
            job = JOBS.get(job_id)
            if not job:
                return self._json({"error": "任务不存在或已过期"}, 404)
            return self._json(job.snapshot())
        if path == "/api/health":
            return self._json({"ok": True, "jobs": len(JOBS)})
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        payload = self._body()

        if path == "/api/resolve":
            try:
                return self._json(resolve_query(str(payload.get("query") or "")))
            except Exception as exc:  # noqa: BLE001
                _log(f"解析失败：{traceback.format_exc()}")
                return self._json({"items": [], "notes": [f"解析失败：{exc}"]}, 200)

        if path == "/api/scan":
            return self._start_scan(payload)

        if path == "/api/stop":
            job = JOBS.get(str(payload.get("id") or ""))
            if not job:
                return self._json({"error": "任务不存在"}, 404)
            job.stop_event.set()
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------ 处理函数
    def _file(self, target: Path) -> None:
        try:
            data = target.read_bytes()
        except OSError:
            return self._json({"error": f"缺少文件 {target.name}"}, 404)
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, data, ctype)

    def _start_scan(self, payload: dict) -> None:
        songs = payload.get("songs") or []
        tasks: list[SongTask] = []
        for s in songs:
            try:
                sid = int(s.get("song_id") or 0)
            except (TypeError, ValueError):
                continue
            if sid <= 0 or any(t.song_id == sid for t in tasks):
                continue
            tasks.append(
                SongTask(
                    song_id=sid,
                    name=str(s.get("name") or ""),
                    artists=str(s.get("artists") or ""),
                    album=str(s.get("album") or ""),
                )
            )
        if not tasks:
            return self._json({"error": "没有可检索的曲目，请先解析并勾选歌曲"}, 400)

        uids: list[int] = []
        raw_uid = payload.get("uid") or ""
        if isinstance(raw_uid, list):
            raw_uid = ",".join(str(x) for x in raw_uid)
        for chunk in re.split(r"[,，\s]+", str(raw_uid)):
            if chunk.strip().isdigit():
                uids.append(int(chunk.strip()))
        nickname = str(payload.get("nickname") or "").strip()
        if not uids and not nickname:
            return self._json({"error": "请至少填写用户 uid 或昵称（否则无法判定命中）"}, 400)

        job = Job(
            id=uuid.uuid4().hex[:12],
            songs=tasks,
            uid=uids,
            nickname=nickname,
            fuzzy=bool(payload.get("fuzzy")),
            opts={
                "workers": payload.get("workers") or 3,
                "interval": payload.get("interval") or 0.35,
                "page_size": payload.get("page_size") or MAX_PAGE_SIZE,
                "max_pages": payload.get("max_pages") or 0,
                "stop_on_match": bool(payload.get("stop_on_match")),
                "auto_rescan": payload.get("auto_rescan", True),
                "cookie": str(payload.get("cookie") or "").strip(),
            },
        )
        job.started_at = time.monotonic()
        with JOBS_LOCK:
            JOBS[job.id] = job
            # 只保留最近 20 个任务，避免内存里堆积
            for old in sorted(JOBS.values(), key=lambda j: j.started_at)[:-20]:
                JOBS.pop(old.id, None)

        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        return self._json({"job_id": job.id, "songs": len(tasks)})


def _qs(query: str, key: str) -> str:
    from urllib.parse import parse_qs

    return (parse_qs(query).get(key) or [""])[0]


# ===================================================================== 启动
def _bind(host: str, port: int, tries: int = 20) -> ThreadingHTTPServer:
    last: Exception | None = None
    for offset in range(tries):
        try:
            return ThreadingHTTPServer((host, port + offset), Handler)
        except OSError as exc:
            last = exc
    raise SystemExit(f"端口 {port}~{port + tries} 都被占用，请用 --port 指定其它端口。（{last}）")


def _selfcheck() -> int:
    """环境自检：确认「零安装也能跑」的前提都成立，并顺带探一次网络。"""
    from netease_crawler.client import HTTP_BACKEND, WeapiClient
    from netease_crawler.crypto import AES_BACKEND

    print("=" * 62)
    print("网易云评论检索器 —— 环境自检")
    print("=" * 62)
    print(f"  Python 版本    : {sys.version.split()[0]}")
    print(f"  解释器路径     : {sys.executable}")
    print(f"  AES 加密后端   : {AES_BACKEND}")
    print(f"  HTTP 通道后端  : {HTTP_BACKEND}")
    print(f"  前端文件       : {'存在 ✓' if (STATIC_DIR / 'index.html').is_file() else '缺失 ✗'}")
    print(f"  第三方依赖     : {'已安装 requests' if HTTP_BACKEND == 'requests' else '未装 requests（已用标准库顶替）'}"
          f" / {'已安装 pycryptodome' if AES_BACKEND == 'pycryptodome' else '未装 pycryptodome（已用内置实现顶替）'}")

    # python.org 版 macOS Python 默认不带根证书，会让所有 HTTPS 请求失败，
    # 并报出很容易被误解的「self-signed certificate」。这里提前亮出来。
    from netease_crawler import http_compat

    print(f"  HTTPS 根证书   : {http_compat.ca_bundle_status()}")

    print("\n  正在探测 music.163.com …")
    try:
        resp = WeapiClient(min_interval=0, timeout=10)._session().get(
            "https://music.163.com/", timeout=10)
        print(f"  连通性         : 可达 ✓（HTTP {resp.status_code}）")
        reachable = True
    except Exception as exc:  # noqa: BLE001
        print(f"  连通性         : 不可达 ✗（{type(exc).__name__}: {exc}）")
        print("                  请检查网络，或确认所在网络能否访问 music.163.com")
        reachable = False

    ok = reachable and (STATIC_DIR / "index.html").is_file()
    print("\n" + "=" * 62)
    print("结论：" + ("环境就绪，可以正常使用 ✓" if ok else "存在问题，见上方提示 ✗"))
    print("=" * 62)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="网易云评论检索器 · 本地 Web 界面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765，占用则自动顺延）")
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--check", action="store_true",
                        help="只做环境自检（依赖回退 / 网络连通性）然后退出")
    args = parser.parse_args(argv)

    if args.check:
        return _selfcheck()

    if not (STATIC_DIR / "index.html").is_file():
        raise SystemExit(f"缺少前端文件：{STATIC_DIR / 'index.html'}")

    httpd = _bind(args.host, args.port)
    host, port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"

    from netease_crawler.client import HTTP_BACKEND
    from netease_crawler.crypto import AES_BACKEND

    _log("=" * 62)
    _log("网易云评论检索器 —— 纯本地运行，不调用任何 AI 服务")
    _log(f"界面地址：{url}")
    _log(f"运行环境：Python {sys.version.split()[0]} · AES={AES_BACKEND} · HTTP={HTTP_BACKEND}")
    _log("关闭这个窗口（或按 Ctrl+C）即可停止服务")
    _log("=" * 62)

    if not args.no_browser:
        threading.Timer(0.8, lambda: _open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("已停止。")
    finally:
        httpd.server_close()
    return 0


def _open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
