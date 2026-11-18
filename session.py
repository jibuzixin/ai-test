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

import textsafe

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"


def new_session_id() -> str:
    """时间戳文件名，如 20261118_102700。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def session_path(session_id: str, sessions_dir: Path | None = None) -> Path:
    base = sessions_dir or SESSIONS_DIR
    return base / f"{session_id}.json"


def _sanitize(value: Any) -> Any:
    """递归清洗字符串里的落单代理对，保证能安全编码成 UTF-8。"""
    if isinstance(value, str):
        return textsafe.clean(value)
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


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
    payload = _sanitize(
        {
            "id": session_id,
            "created_at": now,
            "updated_at": now,
            "model": model,
            "system_prompt": system_prompt,
            "messages": messages,
        }
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        path.write_text(text, encoding="utf-8")
    except UnicodeEncodeError:
        # 最后一道兜底：退回 ascii 转义保存，保证会话不丢
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
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
    # 老会话文件里可能已经存了落单代理对，读进来先清洗
    return _sanitize(json.loads(p.read_text(encoding="utf-8")))


def list_sessions(sessions_dir: Path | None = None) -> list[Path]:
    base = sessions_dir or SESSIONS_DIR
    if not base.exists():
        return []
    return sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
