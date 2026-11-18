"""会话持久化：内存里保存完整上下文，退出时写成时间戳命名的 json。

json 结构::

    {
      "id": "20261118_102700",
      "created_at": "2026-11-18T10:27:00+08:00",
      "updated_at": "...",
      "model": "MiniMax-M2.7",
      "system_prompt": "……（存档一份，方便复现）",
      "messages": [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "", "tool_calls": [...]},
        {"role": "tool", "tool_call_id": "...", "content": "..."},
        {"role": "assistant", "content": "你好！"}
      ]
    }
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"


def new_session_id() -> str:
    """时间戳文件名，如 20261118_102700。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def session_path(session_id: str, sessions_dir: Path | None = None) -> Path:
    base = sessions_dir or SESSIONS_DIR
    return base / f"{session_id}.json"


def save_session(
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    model: str = "",
    system_prompt: str = "",
    sessions_dir: Path | None = None,
) -> Path:
    """把会话写到磁盘，返回文件路径。"""
    path = session_path(session_id, sessions_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {
        "id": session_id,
        "created_at": now,
        "updated_at": now,
        "model": model,
        "system_prompt": system_prompt,
        "messages": messages,
    }
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except (UnicodeEncodeError, ValueError):
        # 极端情况：数据里有无法编码的字符，退回 ascii 转义保存，保证不丢会话
        text = json.dumps(payload, ensure_ascii=True, indent=2)
    path.write_text(text, encoding="utf-8")
    return path


def load_session(path: str | Path) -> dict[str, Any]:
    """读取会话文件。path 可以是完整路径，也可以是 sessions/ 下的文件名。"""
    p = Path(path)
    if not p.exists():
        candidate = SESSIONS_DIR / p.name
        if candidate.exists():
            p = candidate
        else:
            raise FileNotFoundError(f"会话文件不存在: {path}")
    return json.loads(p.read_text(encoding="utf-8"))


def list_sessions(sessions_dir: Path | None = None) -> list[Path]:
    base = sessions_dir or SESSIONS_DIR
    if not base.exists():
        return []
    return sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
