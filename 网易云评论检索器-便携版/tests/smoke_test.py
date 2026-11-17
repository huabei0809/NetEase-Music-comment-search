#!/usr/bin/env python3
"""离线自检 —— 不联网即可验证加密、分页终止、去重、续传等核心逻辑。

    python tests/smoke_test.py

装了 requests / pycryptodome 会用它们；没装会自动走内置回退实现，
本脚本对两条路径都会校验。联网的端到端验证请用：

    python run.py --playlist 3778678 --all --max-songs 2 --max-pages 1
"""

from __future__ import annotations

import base64
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netease_crawler import client as _client
from netease_crawler.api import NeteaseAPI, make_matcher
from netease_crawler.crypto import (AES_BACKEND, IV, MODULUS, PRESET_KEY,
                                    PUBLIC_KEY, aes_encrypt, encrypted_request,
                                    rsa_encrypt)
from netease_crawler.models import Comment, CommentPage, Song
from netease_crawler.storage import Storage

#: 固定明文 + 固定密钥的「黄金密文」：无论用哪个 AES 后端，结果都必须与它一致
GOLDEN_AES = "cgnYCkO/Fad/gWpq/Lh1NA=="

try:  # pycryptodome 是可选依赖，没装也能跑全部检查
    from Crypto.Cipher import AES as _RefAES
    from Crypto.Util.Padding import unpad as _ref_unpad

    HAS_PYCRYPTODOME = True
except ImportError:
    _RefAES = None
    _ref_unpad = None
    HAS_PYCRYPTODOME = False

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        FAILED.append(name)
        print(f"  \033[31mFAIL\033[0m {name} {detail}")


# --------------------------------------------------------------- 加密层
def test_crypto() -> None:
    print("\n[1] 加密层")

    plain = "hello 网易云"
    ct = aes_encrypt(plain, PRESET_KEY)
    raw = base64.b64decode(ct)
    check("AES 输出为 base64 且长度为 16 的整数倍", len(raw) % 16 == 0, f"len={len(raw)}")
    check("AES 相同输入结果确定", aes_encrypt(plain, PRESET_KEY) == ct)

    # 黄金密文：换 AES 后端也必须字节级一致，否则说明实现有偏差
    check("AES 输出匹配黄金向量（与后端无关）", ct == GOLDEN_AES,
          f"got={ct} want={GOLDEN_AES}")

    if HAS_PYCRYPTODOME:
        # 用同一套 key/IV 反向解密，验证 PKCS#7 填充正确
        dec = _RefAES.new(PRESET_KEY.encode(), _RefAES.MODE_CBC, IV.encode())
        check("AES 可逆（填充正确）", _ref_unpad(dec.decrypt(raw), 16).decode() == plain)
    else:
        check("AES 可逆（无 pycryptodome，跳过反解）", True)

    check("RSA 输出恒为 256 位十六进制", len(rsa_encrypt("0123456789abcdef")) == 256)
    # 模数写成 258 位十六进制：多出的 "00" 是正数符号前缀，实际仍为 1024 bit
    check("RSA 模数为 1024bit",
          int(MODULUS, 16).bit_length() == 1024 and MODULUS.startswith("00"),
          f"len={len(MODULUS)} bits={int(MODULUS, 16).bit_length()}")
    check("RSA 公钥指数正确", PUBLIC_KEY == "010001")

    req = encrypted_request({"csrf_token": "", "id": "1"})
    check("encrypted_request 字段齐全", sorted(req) == ["encSecKey", "params"])
    check("随机密钥生效（两次请求不同）",
          encrypted_request({"a": 1})["params"] != encrypted_request({"a": 1})["params"])


# --------------------------------------------------------------- 分页逻辑
class FakeAPI(NeteaseAPI):
    """用构造好的分页序列替换真实网络调用，验证翻页终止条件。"""

    def __init__(self, pages: list[list[dict]], cursors: list[str]):
        self._pages = pages
        self._cursors = cursors
        self.calls = 0

    def song_comments(self, song_id, cursor="-1", page_size=1000, order_type=1, matcher=None):
        idx = self.calls
        self.calls += 1
        items = self._pages[idx] if idx < len(self._pages) else []
        comments = []
        for i in items:
            matched, reason = (0, "")
            if matcher is not None:      # 真实实现里 matcher 是在这里生效的
                hit, why = matcher(Comment.from_api(i, song_id))
                if hit:
                    matched, reason = 1, why
            comments.append(Comment.from_api(i, song_id, matched=matched, match_reason=reason))
        return CommentPage(
            comments=comments,
            next_cursor=self._cursors[idx] if idx < len(self._cursors) else cursor,
            total_count=len(items),
            raw_comment_count=len(items),
        )


def _fake_comment(cid: int, uid: int = 1, content: str = "x") -> dict:
    return {"commentId": cid, "content": content, "time": 1700000000000,
            "likedCount": 0, "user": {"userId": uid, "nickname": f"u{uid}"}}


def test_pagination() -> None:
    print("\n[2] 分页终止条件（hasMore 不可信）")

    # a) 空页终止
    api = FakeAPI([[_fake_comment(1)]], ["100"])
    pages = list(api.iter_song_comments(1))
    check("空页时终止（只产出 1 页）", len(pages) == 1 and api.calls == 2,
          f"pages={len(pages)} calls={api.calls}")

    # b) cursor 不前进时终止，避免死循环
    api = FakeAPI([[_fake_comment(1)], [_fake_comment(2)]], ["100", "100"])
    pages = list(api.iter_song_comments(1))
    check("cursor 未前进时终止（不死循环）", len(pages) == 2 and api.calls == 2,
          f"pages={len(pages)} calls={api.calls}")

    # c) max_pages 限制
    api = FakeAPI([[_fake_comment(i)] for i in range(1, 6)],
                  ["2", "3", "4", "5", "6"])
    pages = list(api.iter_song_comments(1, max_pages=3))
    check("max_pages 生效", len(pages) == 3 and api.calls == 3, f"pages={len(pages)}")

    # d) stop_on_match 生效
    api = FakeAPI([[_fake_comment(1, uid=7)], [_fake_comment(2, uid=8)]], ["2", "3"])
    pages = list(api.iter_song_comments(1, matcher=make_matcher(user_ids=[7]),
                                        stop_on_match=True))
    check("stop_on_match 命中即停", len(pages) == 1 and api.calls == 1,
          f"pages={len(pages)} calls={api.calls}")

    # e) 正常多页推进
    api = FakeAPI([[_fake_comment(i)] for i in range(1, 4)], ["2", "3", "4"])
    pages = list(api.iter_song_comments(1))
    check("多页顺序推进至耗尽", len(pages) == 3 and api.calls == 4,
          f"pages={len(pages)} calls={api.calls}")


# --------------------------------------------------------------- 匹配逻辑
def test_matcher() -> None:
    print("\n[3] 命中判定")

    m = make_matcher(user_ids=[42], nickname="阿七", keyword="考研")
    c1 = Comment(comment_id=1, song_id=1, user_id=42, nickname="路人", content="无关")
    c2 = Comment(comment_id=2, song_id=1, user_id=9, nickname="阿七", content="无关")
    c3 = Comment(comment_id=3, song_id=1, user_id=9, nickname="路人", content="考研加油")
    c4 = Comment(comment_id=4, song_id=1, user_id=9, nickname="路人", content="无关")

    check("按 uid 命中", m(c1) == (True, "uid=42"))
    check("按昵称命中", m(c2) == (True, "nickname=阿七"))
    check("按关键词命中", m(c3) == (True, "keyword=考研"))
    check("不命中返回 False", m(c4) == (False, ""))

    ma = make_matcher(match_all=True)
    check("--all 模式全部命中", ma(c4) == (True, "all"))

    mn = make_matcher()
    check("无筛选条件时不命中", mn(c1) == (False, ""))


# --------------------------------------------------------------- 存储层
def test_storage() -> None:
    print("\n[4] 存储 / 去重 / 续传")

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    st = Storage(tmp)

    st.upsert_songs([Song(song_id=1, name="歌A", artists="歌手A")])
    row = sqlite3.connect(tmp).execute("SELECT name, artists FROM songs WHERE song_id=1").fetchone()
    check("歌曲元信息落库", row == ("歌A", "歌手A"), str(row))

    cs = [Comment(comment_id=i, song_id=1, content=f"c{i}", user_id=i % 3, matched=1)
          for i in range(1, 6)]
    check("首轮写入 5 条", st.save_comments(cs) == 5)
    check("重复写入被去重（幂等）", st.save_comments(cs) == 0)

    st.save_comments([Comment(comment_id=6, song_id=1, content="c6", user_id=0, matched=1)])
    check("统计正确", st.stats()["comments"] == 6, str(st.stats()))

    n = st.refresh_matches()
    check("matches 物化 6 条", n == 6, f"n={n}")

    st.save_progress(1, "12345", 1000, done=False)
    check("进度可写可读", st.get_progress(1) ==
          {"cursor": "12345", "scanned_count": 1000, "done": 0})

    st.save_progress(1, "-1", 2000, done=True)
    check("done 标记生效", st.get_progress(1)["done"] == 1)

    files = st.export_matches(tmp.parent, formats=("csv", "json", "jsonl"))
    check("导出三种格式", len(files) == 3 and all(f.exists() for f in files))
    st.close()


# --------------------------------------------------------------- 模型
def test_models() -> None:
    print("\n[5] 数据模型")

    raw = {
        "commentId": 999, "content": "测试", "time": 1700000000000,
        "likedCount": 12, "ipLocation": {"location": "江苏", "userId": 1},
        "user": {"userId": 5, "nickname": "小明", "vipType": 11},
        "beReplied": [{"content": "被回复内容", "user": {"userId": 6, "nickname": "小红"}}],
    }
    c = Comment.from_api(raw, song_id=1)
    check("ipLocation dict 被正确解析", c.ip_location == "江苏", repr(c.ip_location))
    check("楼中楼被展开", c.replied_user_id == 6 and c.replied_content == "被回复内容")
    check("时间戳转北京时间", c.time_str.startswith("2023-11-1"), c.time_str)

    # 服务端 timeStr 是相对时间（"昨天12:25"/"3天前"），绝不能让它覆盖绝对时间，
    # 否则导出时按字符串排序会彻底错乱。
    rel = Comment.from_api({**raw, "timeStr": "前天08:05"}, song_id=1)
    check("相对 timeStr 不覆盖绝对时间", rel.time_str.startswith("2023-11-1"), rel.time_str)
    check("原始相对时间留档在 time_rel", rel.time_rel == "前天08:05", rel.time_rel)
    check("time 缺失时才回退到服务端字符串",
          Comment.from_api({"commentId": 1, "content": "x", "timeStr": "3天前"}, 1).time_str == "3天前")

    s = Song.from_api({"id": 1, "name": "歌", "ar": [{"name": "A"}, {"name": "B"}],
                       "al": {"name": "专辑"}, "dt": 200000})
    check("歌手多值拼接", s.artists == "A / B", s.artists)
    check("专辑与时长", s.album == "专辑" and s.duration == 200000)

    # ipLocation 为字符串的兼容分支
    c2 = Comment.from_api({"commentId": 1, "ipLocation": "北京", "user": {}}, song_id=1)
    check("ipLocation 字符串兼容", c2.ip_location == "北京")


def test_portability() -> None:
    """验证「换台电脑零安装也能跑」所依赖的两条回退通道。"""
    print("\n[6] 便携性 / 依赖回退")

    print(f"      当前 AES 后端  ：{AES_BACKEND}")
    print(f"      当前 HTTP 后端 ：{_client.HTTP_BACKEND}")

    check("AES 有可用后端", AES_BACKEND in ("pycryptodome", "内置纯Python实现"), AES_BACKEND)
    check("HTTP 有可用后端", _client.HTTP_BACKEND in ("requests", "urllib(标准库)"),
          _client.HTTP_BACKEND)

    # 纯 Python AES 必须自洽：官方 FIPS-197 / NIST SP 800-38A 向量
    from netease_crawler._aes import AES128CBC

    fips = AES128CBC(bytes(range(16))).encrypt_block(
        bytes.fromhex("00112233445566778899aabbccddeeff")).hex()
    check("纯Python AES 通过 FIPS-197 向量",
          fips == "69c4e0d86a7b0430d8cdb78070b4c55a", fips)

    nist = AES128CBC(bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")).encrypt_cbc(
        bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"),
        bytes.fromhex("000102030405060708090a0b0c0d0e0f")).hex()
    check("纯Python AES-CBC 通过 NIST SP 800-38A 向量",
          nist == "7649abac8119b246cee98e9b12e9197d", nist)

    # 两个后端必须给出完全相同的密文（这是回退安全的前提）
    from netease_crawler._aes import aes_cbc_encrypt as _pure
    from netease_crawler._aes import pkcs7_pad as _pure_pad

    if HAS_PYCRYPTODOME:
        agree = True
        samples = (b"", b"a", b"x" * 15, b"y" * 16, b"z" * 17,
                   "中文内容测试".encode(), b'{"csrf_token":"","id":"1"}')
        for text in samples:
            ref = _RefAES.new(PRESET_KEY.encode(), _RefAES.MODE_CBC, IV.encode()).encrypt(
                _pure_pad(text))
            if _pure(text, PRESET_KEY, IV) != ref:
                agree = False
                print(f"      不一致：len={len(text)}")
        check("纯Python 与 pycryptodome 密文逐字节一致", agree)
    else:
        check("纯Python 与 pycryptodome 密文逐字节一致（未装后者，跳过）", True)

    # 无 requests 时 client 仍应能建出可用的会话对象（不触网）
    sess = _client.requests.Session()
    check("可创建 HTTP 会话对象", hasattr(sess, "get") and hasattr(sess, "post"))
    sess.headers["X-Selftest"] = "1"
    check("会话头可读写", sess.headers.get("X-Selftest") == "1")
    check("client 暴露可捕获的 RequestException",
          isinstance(getattr(_client.requests, "RequestException", None), type))


def main() -> int:
    print("=" * 60)
    print("网易云评论检索器 —— 离线自检")
    print("=" * 60)
    test_crypto()
    test_pagination()
    test_matcher()
    test_storage()
    test_models()
    test_portability()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"结果：{PASSED} 项通过，\033[31m{len(FAILED)} 项失败\033[0m")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print(f"结果：\033[32m全部 {PASSED} 项通过\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
