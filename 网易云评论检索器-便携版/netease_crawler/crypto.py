"""
网易云音乐 WeAPI 加密层（纯本地实现，不依赖第三方 cloudmusic 包）。

背景
----
网易云的音乐接口分两套：
  1. `eapi`  —— 移动端，请求体整体加密（AES-ECB + 追加 md5 摘要），复杂；
  2. `weapi` —— 网页端，只加密 `params` 与 `encSecKey` 两个表单字段，简单、稳定，
     正是本项目使用的通道。

weapi 的加密流程（web 端 JS 里 `window.asrsea` 干的事）：

    原文 JSON ──AES-CBC(key1)──> base64 ──AES-CBC(key2)──> params
    随机 key2 ──RSA(公钥)──────> encSecKey

其中：
    key1  = "0CoJUm6Qyw8W8jud"   （固定）
    key2  = 随机 16 位可见字符    （每次请求都不同）
    IV    = "0102030405060708"   （固定）
    RSA   = 公钥指数 010001，模数为下面那串十六进制大整数（1024 bit，
            写成 258 位是因为带了一个 "00" 正数符号前缀）

对称加密用 AES-128-CBC + PKCS7 填充，非对称用大学课本里的
`c = m^e mod n`——因为模数只有 1024 bit 且指数固定，纯 Python 的
`pow()` 就够了，不需要额外的 RSA 库。

AES 后端是可插拔的
------------------
优先用已安装的 ``pycryptodome``（快、经过广泛验证）；如果目标机器上没装，
自动回退到本包内置的纯 Python 实现 ``_aes.py``。
两条路径的密文逐字节一致（由 ``tests/smoke_test.py`` 用 FIPS-197 官方向量
交叉验证），所以**换台电脑不需要装任何东西**。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any, Mapping

from . import _aes

try:  # 可选加速：装了 pycryptodome 就用它
    from Crypto.Cipher import AES as _PyCryptoAES

    AES_BACKEND = "pycryptodome"
except ImportError:  # pragma: no cover - 取决于运行环境
    _PyCryptoAES = None
    AES_BACKEND = "内置纯Python实现"

__all__ = [
    "NONCE",
    "PRESET_KEY",
    "IV",
    "PUBLIC_KEY",
    "MODULUS",
    "AES_BACKEND",
    "aes_encrypt",
    "rsa_encrypt",
    "encrypted_request",
]

#: 第一层 AES 的固定密钥（网易云 web 端硬编码值）
NONCE = "0CoJUm6Qyw8W8jud"

#: 等价于 NONCE，保留一个语义更清晰的别名
PRESET_KEY = NONCE

#: AES-CBC 固定初始向量
IV = "0102030405060708"

#: RSA 公钥指数
PUBLIC_KEY = "010001"

#: RSA 模数（1024 bit）
MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa7"
    "6d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a4"
    "6bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)

#: key2 的随机字节表
_BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _pkcs7_pad(raw: bytes, block_size: int = 16) -> bytes:
    """PKCS#7 填充。"""
    return _aes.pkcs7_pad(raw, block_size)


def aes_encrypt(text: str | bytes, key: str, iv: str = IV) -> str:
    """AES-128-CBC 加密后做 base64，返回 str。

    :param text: 待加密明文
    :param key:  16 字节密钥
    :param iv:   16 字节初始向量
    """
    if isinstance(text, str):
        text = text.encode("utf-8")

    if _PyCryptoAES is not None:
        cipher = _PyCryptoAES.new(key.encode("utf-8"), _PyCryptoAES.MODE_CBC, iv.encode("utf-8"))
        raw = cipher.encrypt(_pkcs7_pad(text))
    else:
        raw = _aes.aes_cbc_encrypt(text, key, iv)

    return base64.b64encode(raw).decode("utf-8")


def rsa_encrypt(text: str, pubkey: str = PUBLIC_KEY, modulus: str = MODULUS) -> str:
    """网易云版"RSA"：把明文倒序后当作大整数做模幂，再左补零到 256 位十六进制。

    不用标准 RSA 库的原因：这里没有标准填充（PKCS#1），只是裸的
    `m^e mod n`，用标准库反而对不上。
    """
    # 1) 明文反转（web 端 JS 里确实做了 reverse）
    reversed_text = text[::-1]
    # 2) 转成大整数
    message = int(binascii.hexlify(reversed_text.encode("utf-8")), 16)
    # 3) 模幂
    encrypted = pow(message, int(pubkey, 16), int(modulus, 16))
    # 4) 十六进制、左补零到 256 位（1024 bit）
    return format(encrypted, "x").zfill(256)


def _random_key(size: int = 16) -> str:
    """生成 size 位的随机可打印密钥（与原版 JS 的随机策略等价）。"""
    return "".join(_BASE62[b % 62] for b in os.urandom(size))


def encrypted_request(payload: Mapping[str, Any]) -> dict[str, str]:
    """把业务参数 dict 变成 weapi 需要的 ``{"params": ..., "encSecKey": ...}``。

    >>> req = encrypted_request({"csrf_token": "", "id": "123"})
    >>> sorted(req)
    ['encSecKey', 'params']
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    secret_key = _random_key(16)
    # 双重 AES：先用固定密钥，再用随机密钥
    params = aes_encrypt(aes_encrypt(text, PRESET_KEY), secret_key)
    enc_sec_key = rsa_encrypt(secret_key)
    return {"params": params, "encSecKey": enc_sec_key}


# --------------------------------------------------------------------------
# 自检：`python -m netease_crawler.crypto` 可直接验证加密链路
# --------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    print(f"AES 后端：{AES_BACKEND}")
    demo = encrypted_request({"csrf_token": "", "id": "186016"})
    for k, v in demo.items():
        print(f"{k}: {v[:60]}{'...' if len(v) > 60 else ''} (len={len(v)})")
    assert len(demo["encSecKey"]) == 256, "encSecKey 必须为 256 位十六进制"
    print("\n[OK] 加密链路自检通过")
