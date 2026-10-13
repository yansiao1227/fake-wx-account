"""仅识别微信文件另存为产生的 packed_info 路径变化。

这里不读取文件正文，也不放宽原生消息签名。未识别的 protobuf 结构一律
拒绝；除唯一文件路径外，字段顺序、编码和全部内容必须保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re


@dataclass(eq=False, repr=False)
class ReferenceFileSnapshot:
    """一次 SaveAs 的内存凭据；不得进入事件、日志或持久化存储。"""

    owner: object = field(repr=False)
    event: object = field(repr=False)
    event_proof: tuple = field(repr=False)
    reference_proof: tuple = field(repr=False)
    event_stream: tuple = field(repr=False)
    origin_stream: tuple = field(repr=False)
    event_row: dict = field(repr=False)
    origin_row: dict = field(repr=False)
    packed_column: str = field(repr=False)
    filename: str = field(repr=False)
    consumed: bool = field(default=False, repr=False)

    def __repr__(self):
        return "<ReferenceFileSnapshot>"

    def __reduce_ex__(self, protocol):
        raise TypeError("引用文件快照仅可在当前进程内使用")


def _encode_varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _varint(data, offset):
    start, value = offset, 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("invalid_packed_info")
        current = data[offset]
        offset += 1
        if index == 9 and current > 1:
            raise ValueError("invalid_packed_info")
        value |= (current & 127) << (7 * index)
        if current < 128:
            if data[start:offset] != _encode_varint(value):
                raise ValueError("invalid_packed_info")
            return value, offset
    raise ValueError("invalid_packed_info")


def _fields(data):
    if not isinstance(data, (bytes, bytearray, memoryview)) or not 0 < len(data) <= 65536:
        raise ValueError("invalid_packed_info")
    data, offset, result = bytes(data), 0, []
    while offset < len(data):
        start = offset
        key, offset = _varint(data, offset)
        number, wire = key >> 3, key & 7
        if not 0 < number < (1 << 29) or wire not in {0, 2}:
            raise ValueError("invalid_packed_info")
        if wire == 0:
            value, offset = _varint(data, offset)
        else:
            size, offset = _varint(data, offset)
            if size > len(data) - offset:
                raise ValueError("invalid_packed_info")
            value, offset = data[offset:offset + size], offset + size
        result.append((number, wire, value, data[start:offset]))
        if len(result) > 32:
            raise ValueError("invalid_packed_info")
    return result


def _length_field(number, value):
    return _encode_varint((number << 3) | 2) + _encode_varint(len(value)) + value


def _packed_file(data):
    top = _fields(data)
    expected = {1: (0, 2), 2: (0, 1), 7: (2, None), 11: (0, 0)}
    if len(top) != len(expected) or {item[0] for item in top} != set(expected):
        raise ValueError("unsupported_packed_info")
    normalized, file_path = [], None
    for number, wire, value, raw in top:
        expected_wire, expected_value = expected[number]
        if wire != expected_wire or expected_value is not None and value != expected_value:
            raise ValueError("unsupported_packed_info")
        if number != 7:
            normalized.append(raw)
            continue
        entries = _fields(value)
        if (len(entries) != 3 or sum(item[0] == 1 for item in entries) != 2 or
                sum(item[0] == 4 and item[1:3] == (0, 2) for item in entries) != 1):
            raise ValueError("unsupported_packed_info")
        nested, types = [], set()
        for entry_number, entry_wire, entry_value, entry_raw in entries:
            if entry_number == 4:
                nested.append(entry_raw)
                continue
            if entry_number != 1 or entry_wire != 2:
                raise ValueError("unsupported_packed_info")
            parts = _fields(entry_value)
            if (len(parts) != 2 or {part[0] for part in parts} != {1, 2} or
                    next(part[1] for part in parts if part[0] == 1) != 0 or
                    next(part[1] for part in parts if part[0] == 2) != 2):
                raise ValueError("unsupported_packed_info")
            entry_type = next(part[2] for part in parts if part[0] == 1)
            if entry_type not in {0, 1} or entry_type in types:
                raise ValueError("unsupported_packed_info")
            types.add(entry_type)
            if entry_type == 0:
                nested.append(entry_raw)
                continue
            encoded_path = next(part[2] for part in parts if part[0] == 2)
            file_path = encoded_path.decode("utf-8", errors="strict")
            if not file_path or "\x00" in file_path or not Path(file_path).is_absolute():
                raise ValueError("invalid_file_path")
            normalized_entry = b"".join(
                _length_field(2, b"\x00verified-save-as-path\x00") if part[0] == 2 else part[3]
                for part in parts)
            nested.append(_length_field(1, normalized_entry))
        if types != {0, 1} or file_path is None:
            raise ValueError("unsupported_packed_info")
        normalized.append(_length_field(7, b"".join(nested)))
    return file_path, b"".join(normalized)


def file_path_transition_matches(before, after, saved_path, filename):
    """只有原卡片 SaveAs 返回的真实路径与唯一原生路径一致才接受。"""
    try:
        before_path, before_normalized = _packed_file(before)
        after_path, after_normalized = _packed_file(after)
        if before_normalized != after_normalized:
            return False
        # 文件标题必须是单一文件名；不解释目录、URL 或 Windows 流名称。
        if (not isinstance(filename, str) or not filename or
                re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or filename != filename.strip(" .")):
            return False
        supplied = Path(saved_path)
        current = Path(after_path)
        if (not supplied.is_absolute() or not current.is_absolute() or
                str(supplied).startswith(("\\\\", "//")) or str(current).startswith(("\\\\", "//"))):
            return False
        resolved = supplied.resolve(strict=True)
        if not resolved.is_file() or current.resolve(strict=True) != resolved:
            return False
        if (resolved.name != filename and
                re.fullmatch(r"[a-f0-9]{12}_" + re.escape(filename), resolved.name) is None):
            return False
        # 同路径重存没有改变任何原生字节，仍由最终完整签名复核。
        return before_path != after_path or bytes(before) == bytes(after)
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError):
        return False
