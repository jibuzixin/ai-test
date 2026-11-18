"""终端配色。

规则：
- 输出不是终端（比如重定向到文件、管道）时自动关闭颜色，避免出现乱码
- 设 ``NO_COLOR`` 环境变量可强制关闭；``FORCE_COLOR=1`` 可强制打开
"""

from __future__ import annotations

import os
import sys

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GREY = "\033[90m"


def _detect() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


ENABLED = _detect()


def c(text: str, *codes: str) -> str:
    """给文本套上颜色；颜色关闭时原样返回。"""
    if not ENABLED or not codes:
        return text
    return "".join(codes) + text + RESET


# 语义化快捷方式
def user(text: str) -> str:
    """用户输入相关（提示符）。"""
    return c(text, MAGENTA, BOLD)


def ai(text: str) -> str:
    """AI 的回答正文。"""
    return c(text, CYAN)


def tool_call(text: str) -> str:
    return c(text, YELLOW)


def tool_output(text: str) -> str:
    return c(text, GREY)


def info(text: str) -> str:
    return c(text, BLUE)


def notice(text: str) -> str:
    return c(text, DIM)


def error(text: str) -> str:
    return c(text, RED)
