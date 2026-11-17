"""数据模型：把网易云返回的嵌套 JSON 摊平成好落库、好导出的结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

__all__ = ["Song", "Comment", "CommentPage", "ts_to_str"]

#: 东八区（国内用户为主，统一按北京时间展示）
CST = timezone(timedelta(hours=8))


def _parse_ip_location(value: Any) -> str:
    """``ipLocation`` 实测是 dict（``{'location': '江苏', ...}``），
    但也见过直接给字符串的版本，两种都兼容。"""
    if isinstance(value, Mapping):
        return str(value.get("location") or "")
    return str(value or "")


def ts_to_str(ms: int | None) -> str:
    """毫秒时间戳 → ``YYYY-MM-DD HH:MM:SS``（东八区）。"""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(
            CST
        ).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


@dataclass
class Song:
    song_id: int
    name: str = ""
    artists: str = ""
    album: str = ""
    duration: int = 0          # 毫秒
    comment_total: int = 0     # 该曲评论总数（拿到第一页评论后回填）

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Song":
        artists = " / ".join(
            a.get("name", "") for a in (raw.get("ar") or raw.get("artists") or [])
        )
        album = ((raw.get("al") or raw.get("album") or {}) or {}).get("name", "")
        return cls(
            song_id=int(raw.get("id", 0)),
            name=str(raw.get("name", "")),
            artists=artists,
            album=str(album),
            duration=int(raw.get("dt") or raw.get("duration") or 0),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Comment:
    comment_id: int
    song_id: int
    content: str = ""
    time: int = 0
    #: 由 ``time`` 重算的绝对时间 ``YYYY-MM-DD HH:MM:SS``。
    #: **不要**直接沿用服务端的 ``timeStr``——实测它是相对时间
    #: （近处给 "昨天12:25"/"3天前"，远处只给 "2017-04-14"），
    #: 既不统一，拿去做字符串排序也完全错乱。
    time_str: str = ""
    #: 服务端原始的 ``timeStr``（相对时间），仅作留档，勿用于排序/展示
    time_rel: str = ""
    liked_count: int = 0
    ip_location: str = ""
    user_id: int = 0
    nickname: str = ""
    avatar_url: str = ""
    vip_type: int = 0
    parent_comment_id: int = 0
    replied_user_id: int = 0
    replied_nickname: str = ""
    replied_content: str = ""
    matched: int = 0
    match_reason: str = ""
    crawled_at: str = ""

    @classmethod
    def from_api(
        cls,
        raw: Mapping[str, Any],
        song_id: int,
        matched: int = 0,
        match_reason: str = "",
    ) -> "Comment":
        user = raw.get("user") or {}
        # beReplied 是被回复的那条楼中楼评论（可能为空）
        replied = (raw.get("beReplied") or [{}])[0] or {}
        replied_user = replied.get("user") or {}
        ts = int(raw.get("time") or 0)
        return cls(
            comment_id=int(raw.get("commentId") or 0),
            song_id=song_id,
            content=str(raw.get("content") or ""),
            time=ts,
            time_str=ts_to_str(ts) or str(raw.get("timeStr") or ""),
            time_rel=str(raw.get("timeStr") or ""),
            liked_count=int(raw.get("likedCount") or 0),
            ip_location=_parse_ip_location(raw.get("ipLocation")),
            user_id=int(user.get("userId") or 0),
            nickname=str(user.get("nickname") or ""),
            avatar_url=str(user.get("avatarUrl") or ""),
            vip_type=int(user.get("vipType") or 0),
            parent_comment_id=int(raw.get("parentCommentId") or 0),
            replied_user_id=int(replied_user.get("userId") or 0),
            replied_nickname=str(replied_user.get("nickname") or ""),
            replied_content=str(replied.get("content") or ""),
            matched=matched,
            match_reason=match_reason,
            crawled_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CommentPage:
    """一页评论的结果。

    :param next_cursor: 下一页要回传的 cursor
    :param has_more:    **不可信**。实测该接口 ``hasMore`` 恒为 False，
                        判断是否还有下一页请用 ``comments`` 是否为空，
                        或 ``next_cursor`` 是否与传入值相同。这里仅做原样透传。
    """

    comments: list[Comment] = field(default_factory=list)
    next_cursor: str = "-1"
    total_count: int = 0
    raw_comment_count: int = 0
    has_more: bool = False

    def __bool__(self) -> bool:  # 空页为 False
        return bool(self.comments)
