"""WCDB/SQLCipher 4 页校验与解密。

格式参考 wechatauto-replica fbfb02677f8f5c8d52be166dc2a662a836f1354d；
来源及 Apache-2.0 许可证见本目录 NOTICE 与 UPSTREAM_LICENSE.txt。
输入为已验证的逐库原始 AES 密钥，而非用户口令。
"""

from __future__ import annotations

import hashlib
import hmac
import struct

from Crypto.Cipher import AES

from .errors import DatabaseReadError

PAGE_SIZE = 4096
RESERVE_SIZE = 80
SQLITE_HEADER = b"SQLite format 3\x00"


def key_parts(key: bytes, page_one: bytes) -> tuple[bytes, bytes]:
    if len(key) not in (32, 48) or len(page_one) != PAGE_SIZE:
        raise DatabaseReadError("invalid_cipher_input", "密钥或数据库页长度不正确")
    return key[:32], key[32:] if len(key) == 48 else page_one[:16]


def hmac_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", key[:32], bytes(b ^ 0x3A for b in salt), 2, 32)


def verify_page(page: bytes, page_number: int, mac_key: bytes) -> bool:
    if len(page) != PAGE_SIZE or page_number < 1:
        return False
    start = 16 if page_number == 1 else 0
    expected = hmac.new(mac_key, page[start:PAGE_SIZE - RESERVE_SIZE + 16]
                        + struct.pack("<I", page_number), hashlib.sha512).digest()
    return hmac.compare_digest(expected, page[-64:])


def verify_key(key: bytes, page_one: bytes) -> bool:
    try:
        enc_key, salt = key_parts(key, page_one)
        return verify_page(page_one, 1, hmac_key(enc_key, salt))
    except (ValueError, DatabaseReadError):
        return False


def decrypt_page(page: bytes, page_number: int, key: bytes, mac_key: bytes) -> bytes:
    if not verify_page(page, page_number, mac_key):
        raise DatabaseReadError("page_hmac_failed", "数据库页完整性校验失败")
    start = 16 if page_number == 1 else 0
    iv = page[PAGE_SIZE - RESERVE_SIZE:PAGE_SIZE - RESERVE_SIZE + 16]
    plain = AES.new(key[:32], AES.MODE_CBC, iv).decrypt(page[start:PAGE_SIZE - RESERVE_SIZE])
    if page_number == 1:
        # key+salt 也可能对应首部仍为加密盐的库；明文副本始终恢复 SQLite 魔数。
        plain = SQLITE_HEADER + plain
        # 私有副本不使用 SQLite WAL；主库 WAL 由校验后的帧显式合并。
        plain = plain[:18] + b"\x01\x01" + plain[20:]
    return plain + bytes(RESERVE_SIZE)


def wal_checksum(data: bytes, seed: tuple[int, int] = (0, 0), *, byteorder: str = "<") -> tuple[int, int]:
    """SQLite WAL 的累积双字校验；所有计算按无符号 32 位取模。"""
    if len(data) % 8:
        raise DatabaseReadError("wal_checksum_failed", "WAL 校验输入未对齐")
    s1, s2 = seed
    for a, b in struct.iter_unpack(byteorder + "II", data):
        s1 = (s1 + a + s2) & 0xFFFFFFFF
        s2 = (s2 + b + s1) & 0xFFFFFFFF
    return s1, s2
