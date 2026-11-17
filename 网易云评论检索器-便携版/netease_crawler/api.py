"""业务接口封装 —— 所有网易云的端点知识与参数取值都集中在这里。

端点行为均经真机实测（2026-09），与网上流传的说法有几处重要出入，见 README。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from .client import ApiError, WeapiClient
from .models import Comment, CommentPage, Song

logger = logging.getLogger(__name__)

#: 评论资源前缀。歌曲评论固定是 ``R_SO_4_<songId>``。
SONG_RID_PREFIX = "R_SO_4_"

#: 单页评论数的**实测**上限。传 2000/5000 服务端仍只回 1000 条。
MAX_PAGE_SIZE = 1000

#: 评论排序 / 翻页方向。**只有降序能正确翻页**，详见下方说明。
ORDER_NEWEST_FIRST = 1

#: ⚠️ 实测（2026-09）orderType=0 **无法翻页**，不要用它批量抓取：
#:
#:   * 首页只返回**部分**评论（实测某歌只给 649 条，而该歌实际有 1648 条）；
#:   * 把返回的 cursor 回传 → 返回 0 条；
#:   * 改用递增的 offset      → 同样返回 0 条。
#:
#: 也就是说它拿不到完整数据，且**不会报错**——典型的静默数据丢失。
#: 本项目因此固定用降序爬取；`--order oldest` 的语义改为
#: "全量爬完后按评论时间正序导出"，见 :func:`Storage.refresh_matches`。
ORDER_OLDEST_FIRST = 0

#: 歌曲详情单次批量上限（保守取值，网易云内部上限约 1000）
SONG_DETAIL_BATCH = 400


class NeteaseAPI:
    """在 :class:`WeapiClient` 之上提供语义化方法。"""

    def __init__(self, client: WeapiClient):
        self.client = client

    # ------------------------------------------------------------ 歌单相关
    def playlist_info(self, playlist_id: int) -> dict[str, Any]:
        """返回 ``{"id", "name", "track_count", "creator", "song_ids"}``。"""
        raw = self.client.playlist_detail(playlist_id)
        pl = raw.get("playlist")
        if not pl:
            raise ApiError(
                int(raw.get("code", -1)),
                "歌单不存在、已设为私密，或缺少访问权限",
                "/weapi/v6/playlist/detail",
            )

        # 实测：trackIds 会给全量（9816/9816），tracks 明细被截断（约 10 条）
        song_ids = [int(t["id"]) for t in (pl.get("trackIds") or []) if t.get("id")]
        if not song_ids:
            song_ids = [int(t["id"]) for t in (pl.get("tracks") or []) if t.get("id")]

        track_count = int(pl.get("trackCount") or 0)
        if track_count and len(song_ids) < track_count:
            logger.warning(
                "歌单共 %d 首，但只取到 %d 个曲目 ID。"
                "该歌单可能需要登录 Cookie 才能读取完整曲目（试试 --cookie）。",
                track_count,
                len(song_ids),
            )

        creator = (pl.get("creator") or {}).get("nickname", "")
        return {
            "id": int(pl.get("id") or playlist_id),
            "name": str(pl.get("name") or ""),
            "track_count": track_count or len(song_ids),
            "creator": creator,
            "song_ids": song_ids,
        }

    def songs_detail(self, song_ids: Sequence[int]) -> list[Song]:
        """批量取歌曲详情，内部自动分批。"""
        out: list[Song] = []
        ids = list(dict.fromkeys(int(i) for i in song_ids))  # 去重且保序
        for i in range(0, len(ids), SONG_DETAIL_BATCH):
            chunk = ids[i : i + SONG_DETAIL_BATCH]
            c = ",".join(f'{{"id":{sid}}}' for sid in chunk)
            raw = self.client.weapi(
                "/weapi/v3/song/detail", {"c": f"[{c}]", "ids": f"[{c}]"}
            )
            for s in raw.get("songs") or []:
                out.append(Song.from_api(s))
        return out

    # ------------------------------------------------------------ 评论相关
    def song_comments(
        self,
        song_id: int,
        cursor: str = "-1",
        page_size: int = MAX_PAGE_SIZE,
        order_type: int = ORDER_NEWEST_FIRST,
        matcher=None,
    ) -> CommentPage:
        """抓取一页歌曲评论。

        实测要点（与常见教程不同）：
          * ``pageNo`` 参数**被完全忽略**，翻页只认 ``cursor``；
          * ``hasMore`` **恒为 False**，不能用来判断结束；
          * ``pageSize`` 超过 1000 会被静默压回 1000。

        :param matcher: 可选的 ``callable(Comment) -> (bool, str)``，
                        返回 ``(是否命中, 命中原因)``；不传则全部 ``matched=0``。
        """
        if page_size > MAX_PAGE_SIZE:
            logger.debug("page_size=%d 超过实测上限，压缩为 %d", page_size, MAX_PAGE_SIZE)
            page_size = MAX_PAGE_SIZE

        rid = f"{SONG_RID_PREFIX}{song_id}"
        raw = self.client.weapi(
            "/weapi/comment/resource/comments/get",
            {
                "csrf_token": "",
                "cursor": str(cursor),
                "offset": "0",
                "orderType": str(order_type),
                "pageNo": "1",  # 占位，服务端忽略
                "pageSize": str(page_size),
                "rid": rid,
                "threadId": rid,
            },
        )

        data = raw.get("data") or {}
        raw_comments = data.get("comments") or []

        comments: list[Comment] = []
        for item in raw_comments:
            matched, reason = (0, "")
            if matcher is not None:
                hit, why = matcher(_preview(item, song_id))
                if hit:
                    matched, reason = 1, why
            comments.append(
                Comment.from_api(item, song_id, matched=matched, match_reason=reason)
            )

        return CommentPage(
            comments=comments,
            next_cursor=str(data.get("cursor") or cursor),
            total_count=int(data.get("totalCount") or 0),
            raw_comment_count=len(raw_comments),
            has_more=bool(data.get("hasMore")),
        )

    def iter_song_comments(
        self,
        song_id: int,
        page_size: int = MAX_PAGE_SIZE,
        order_type: int = ORDER_NEWEST_FIRST,
        max_pages: int = 0,
        start_cursor: str = "-1",
        matcher=None,
        stop_on_match: bool = False,
    ):
        """按 cursor 连续翻页，逐页 yield :class:`CommentPage`。

        终止条件（``hasMore`` 不可信，故三者取其一）：
          1. 返回空页；
          2. ``next_cursor`` 与传入 cursor 完全相同（服务端不再前进，防死循环）；
          3. 达到 ``max_pages``（0 表示不限制）。

        :param start_cursor: 断点续传时的起始 cursor，默认 ``-1``（最新评论）
        :param order_type:   **请保持默认的降序**。传 :data:`ORDER_OLDEST_FIRST`
                             时只会返回首页部分数据且无法继续翻页（见常量说明），
                             这里会打印告警但不阻止（便于做接口探测）。
        """
        if order_type != ORDER_NEWEST_FIRST:
            logger.warning(
                "orderType=%s 无法翻页（只会返回首页的部分评论，且不报错）。"
                "如非接口探测用途，请改用默认的降序 orderType=%s。",
                order_type, ORDER_NEWEST_FIRST,
            )

        cursor = str(start_cursor)
        page = 0
        while True:
            if max_pages and page >= max_pages:
                logger.debug("song %s 达到页数上限 %d", song_id, max_pages)
                return
            result = self.song_comments(
                song_id,
                cursor=cursor,
                page_size=page_size,
                order_type=order_type,
                matcher=matcher,
            )
            page += 1

            if not result.comments:
                logger.debug("song %s 第 %d 页为空，抓取结束", song_id, page)
                return

            yield result

            if stop_on_match and any(c.matched for c in result.comments):
                logger.info("song %s 第 %d 页命中目标，提前结束", song_id, page)
                return

            if str(result.next_cursor) == cursor:
                logger.debug("song %s cursor 未前进，抓取结束", song_id)
                return
            cursor = str(result.next_cursor)

    # ------------------------------------------------------------ 搜索相关
    #: 明文搜索通道的 type 取值
    SEARCH_SONG = 1
    SEARCH_ALBUM = 10
    SEARCH_ARTIST = 100
    SEARCH_PLAYLIST = 1000
    SEARCH_USER = 1002

    def _plain_search(self, keyword: str, type_code: int, limit: int) -> list[dict]:
        raw = self.client.plain_get(
            "/api/search/get",
            {"s": keyword, "type": type_code, "limit": limit, "offset": 0},
        )
        result = raw.get("result") or {}
        for key in ("songs", "users", "playlists", "artists", "albums"):
            if key in result:
                return list(result[key] or [])
        return []

    def search_songs(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        """按歌名/歌手搜索歌曲。实测明文通道可用，匿名也能搜。"""
        out: list[dict[str, Any]] = []
        for s in self._plain_search(keyword, self.SEARCH_SONG, limit):
            out.append({
                "song_id": int(s.get("id") or 0),
                "name": str(s.get("name") or ""),
                "artists": " / ".join(a.get("name", "") for a in (s.get("artists") or [])),
                "album": ((s.get("album") or {}) or {}).get("name", ""),
                "duration": int(s.get("duration") or 0),
            })
        return out

    def search_users(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        """按昵称搜索用户。

        ⚠️ 实测（2026-09）：匿名状态下该接口**恒返回空列表**，
        换关键词、换 weapi/明文通道都一样。要看某人的 uid，
        请直接在网页端打开其主页，从 URL 的 ``id=`` 取值。
        """
        out: list[dict[str, Any]] = []
        for u in self._plain_search(keyword, self.SEARCH_USER, limit):
            out.append({
                "user_id": int(u.get("userId") or 0),
                "nickname": str(u.get("nickname") or ""),
                "signature": str(u.get("signature") or ""),
            })
        return out

    # ------------------------------------------------------------ 用户相关
    def user_detail(self, user_id: int) -> dict[str, Any]:
        """用户资料。注意这个接口的 uid 走**路径参数**，不是 body。"""
        raw = self.client.weapi(f"/weapi/v1/user/detail/{int(user_id)}", {})
        profile = raw.get("profile") or {}
        return {
            "user_id": int(profile.get("userId") or user_id),
            "nickname": str(profile.get("nickname") or ""),
            "level": raw.get("level"),
            "listen_songs": raw.get("listenSongs"),
            "signature": str(profile.get("signature") or ""),
        }


def _preview(item: dict, song_id: int) -> Comment:
    """只填匹配所需字段的轻量 Comment，避免为整页评论做全字段解析。"""
    user = item.get("user") or {}
    return Comment(
        comment_id=0,
        song_id=song_id,
        content=str(item.get("content") or ""),
        user_id=int(user.get("userId") or 0),
        nickname=str(user.get("nickname") or ""),
    )


def make_matcher(
    user_ids: Iterable[int] | None = None,
    nickname: str | None = None,
    keyword: str | None = None,
    match_all: bool = False,
) -> Any:
    """构造命中判定函数。

    :param user_ids:  精确匹配的用户 ID 集合（最可靠，优先用这个）
    :param nickname:  昵称精确匹配（重名多，仅作辅助）
    :param keyword:   评论正文包含关键词
    :param match_all: 不做筛选，所有评论都视为命中（对应 CLI 的 ``--all``）
    """
    ids = {int(i) for i in (user_ids or [])}
    nick = (nickname or "").strip()
    kw = (keyword or "").strip()

    def matcher(c: Comment) -> tuple[bool, str]:
        if match_all:
            return True, "all"
        reasons: list[str] = []
        if ids and c.user_id in ids:
            reasons.append(f"uid={c.user_id}")
        if nick and c.nickname == nick:
            reasons.append(f"nickname={nick}")
        if kw and kw in c.content:
            reasons.append(f"keyword={kw}")
        return (bool(reasons), ";".join(reasons))

    return matcher
