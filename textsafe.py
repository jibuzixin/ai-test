"""处理流式文本里可能出现的不完整代理对（surrogate pair）。

背景：某些模型服务在 SSE 里把一个 emoji 的代理对（`\\ud83d\\ude00`）拆到**两个分片**中下发，
或者干脆返回落单代理对（lone surrogate）。Python 的 str 允许存在这种字符，但它**无法编码成 UTF-8**，
于是 httpx 序列化请求体时会抛：

    UnicodeEncodeError: 'utf-8' codec can't encode characters ... surrogates not allowed

本模块提供两层保护：

1. ``SafeTextBuffer``：跨分片缓冲，把被拆开的代理对重新拼成真正的字符
2. ``clean()``：把剩下的落单代理对替换成 U+FFFD（），保证任何时候都能安全编码
"""

from __future__ import annotations

import re

# 代理对：高位 + 低位
_PAIR_RE = re.compile("[\ud800-\udbff][\udc00-\udfff]")
# 落单代理对
_LONE_RE = re.compile("[\ud800-\udfff]")


def _combine_pair(match: re.Match[str]) -> str:
    """把一对代理合并成真正的 Unicode 字符。"""
    hi = ord(match.group(0)[0])
    lo = ord(match.group(0)[1])
    return chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))


def combine_pairs(text: str) -> str:
    """把成对的代理合并成真正的字符（emoji 恢复正常）。"""
    if not text:
        return text
    return _PAIR_RE.sub(_combine_pair, text)


def drop_lone(text: str) -> str:
    """把落单代理对替换成 U+FFFD。"""
    if not text:
        return text
    return _LONE_RE.sub("\ufffd", text)


def clean(text: str) -> str:
    """保证 text 可以安全地编码成 UTF-8（不抛异常）。"""
    if not text:
        return text
    try:
        text.encode("utf-8")  # 快路径：本来就干净
        return text
    except UnicodeEncodeError:
        pass
    return drop_lone(combine_pairs(text))


def is_clean(text: str) -> bool:
    """text 是否可以直接编码成 UTF-8。"""
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


class SafeTextBuffer:
    """跨 chunk 累积文本的缓冲区，能救回被分片拆开的代理对。

    用法::

        buf = SafeTextBuffer()
        for chunk in stream:
            safe = buf.feed(chunk)      # 可以安全打印/存储
            ...
        tail = buf.flush()             # 收尾
    """

    def __init__(self) -> None:
        self._pending = ""  # 暂存末尾的高位代理，等下一个 chunk

    def feed(self, chunk: str) -> str:
        """喂入一个分片，返回可以安全使用的文本（末尾可能暂存了一个高位代理）。"""
        if not chunk:
            return ""
        text = self._pending + chunk
        self._pending = ""
        # 如果结尾是个高位代理，先扣住，等下一个 chunk 补低位
        if text and 0xD800 <= ord(text[-1]) <= 0xDBFF:
            text, self._pending = text[:-1], text[-1:]
        return clean(text)

    def flush(self) -> str:
        """收尾：把暂存的高位代理也处理掉。"""
        rest, self._pending = self._pending, ""
        return clean(rest) if rest else ""
