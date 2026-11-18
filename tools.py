"""工具注册表与内置工具。

最简用法：写一个普通函数，用 ``@tool`` 装饰一下就完事了。

    @tool
    def read_file(path: str, start_line: int = 1) -> str:
        \"\"\"读取一个文本文件。

        Args:
            path: 文件路径，相对路径相对于工作目录
            start_line: 起始行号，从 1 开始

        Returns:
            文件内容
        \"\"\"
        ...

规则：
- **工具名** = 函数名
- **工具描述** = docstring 的正文（自动去掉参数/返回值段落）
- **参数 schema** = 从函数签名自动生成（类型、默认值、必填项）
- **参数说明** = 从 docstring 的 ``Args:`` 段（或 reST ``:param x:``）自动扒出来；
  没写说明的参数会自动兜底生成一句 ``xxx（类型）参数``
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints

# 工作根目录：所有文件类工具的相对路径都相对于它
WORKDIR = Path(os.environ.get("AGENT_WORKDIR", Path(__file__).resolve().parent))

MAX_OUTPUT_CHARS = 8000


# ---------------------------------------------------------------------------
# 数据结构与注册表
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """一个可被模型调用的工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]
    # 内部用，不发给模型
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的 tools 字段格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, Tool] = {}


# ---------------------------------------------------------------------------
# 类型映射
# ---------------------------------------------------------------------------

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    tuple: "array",
    set: "array",
    dict: "object",
}

_TYPE_NAME = {
    "string": "字符串",
    "integer": "整数",
    "number": "浮点数",
    "boolean": "布尔值",
    "array": "数组",
    "object": "对象",
}


def _json_type(annotation: Any) -> str:
    """把 Python 类型注解翻译成 JSON Schema 类型。"""
    if annotation is inspect.Parameter.empty:
        return "string"
    if annotation in _PY_TO_JSON:
        return _PY_TO_JSON[annotation]
    origin = get_origin(annotation)
    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
        return "string"
    if origin in (list, set, tuple):
        return "array"
    if origin is dict:
        return "object"
    return "string"


def _py_type_name(annotation: Any) -> str:
    """给人看的类型名，用于自动兜底的参数说明。"""
    if annotation is inspect.Parameter.empty:
        return "str"
    if annotation in _PY_TO_JSON:
        return annotation.__name__
    origin = get_origin(annotation)
    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _py_type_name(args[0])
        return "str"
    return getattr(annotation, "__name__", "str")


def _coerce(value: Any, annotation: Any) -> Any:
    """尽量把模型传来的字符串转成签名声明的类型。"""
    if annotation is inspect.Parameter.empty or value is None:
        return value
    target = annotation
    if get_origin(annotation) is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        target = args[0] if args else str
    try:
        if target is bool:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "y", "是", "对")
            return bool(value)
        if target is int and not isinstance(value, bool):
            return int(value)
        if target is float and not isinstance(value, bool):
            return float(value)
        if target is str and not isinstance(value, str):
            return str(value)
    except (TypeError, ValueError):
        return value
    return value


# ---------------------------------------------------------------------------
# docstring 解析
# ---------------------------------------------------------------------------

# Google 风格参数段标题：Args / Arguments / Parameters / 参数
_ARG_SECTION_RE = re.compile(
    r"^\s*(args|arguments|parameters|参数|參數|入参)\s*[:：]\s*$", re.IGNORECASE
)
# 参数段之后可能出现的其它段落标题（用来判断参数段结束）
_OTHER_SECTION_RE = re.compile(
    r"^\s*(returns?|return|返回|返回值|renders?|raises?|异常|示例|examples?|notes?|注意|说明|see also|参考资料)\s*[:：]?\s*$",
    re.IGNORECASE,
)
# Google 风格的一行参数：  name (type): 说明
_GOOGLE_PARAM_RE = re.compile(r"^\s*(\*{0,2}[A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))?\s*[:：]\s*(.+)$")
# reST 风格： :param name: 说明  /  :param int name: 说明
_RST_PARAM_RE = re.compile(r"^\s*:param\s+(?:(\w+)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$")
# 兜底：整行就是 "name: 说明"
_PLAIN_PARAM_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(.+)$")


def _parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """返回 (描述, {参数名: 说明})。

    支持三种写法：
    - Google 风格 ``Args:`` 段（描述取 Args 之前的正文）
    - reST 风格 ``:param x: 说明``（描述取其余正文）
    - 两种都没有时，描述就是整段 docstring
    """
    if not doc:
        return "", {}

    lines = inspect.cleandoc(doc).splitlines()

    # --- 找 Google 风格的参数段 ---
    arg_start = -1
    for i, line in enumerate(lines):
        if _ARG_SECTION_RE.match(line):
            arg_start = i
            break

    param_docs: dict[str, str] = {}

    if arg_start >= 0:
        # 参数段：从 header 下一行开始，直到遇到下一个段落标题
        for line in lines[arg_start + 1 :]:
            if _OTHER_SECTION_RE.match(line):
                break
            m = _GOOGLE_PARAM_RE.match(line)
            if m:
                param_docs[m.group(1).lstrip("*")] = m.group(3).strip()
        # 描述 = Args 之前的正文（最干净）
        description_lines = lines[:arg_start]
    else:
        description_lines = list(lines)

    # --- reST 风格参数 ---
    for line in description_lines:
        m = _RST_PARAM_RE.match(line)
        if m:
            param_docs.setdefault(m.group(2), m.group(3).strip())

    # --- 描述正文：去掉段落标题和 :param 行 ---
    cleaned: list[str] = []
    for line in description_lines:
        if _RST_PARAM_RE.match(line):
            continue
        if _ARG_SECTION_RE.match(line) or _OTHER_SECTION_RE.match(line):
            continue
        cleaned.append(line)

    description = "\n".join(cleaned).strip()
    return description, param_docs


# ---------------------------------------------------------------------------
# 装饰器
# ---------------------------------------------------------------------------


def tool(fn: Callable[..., str]) -> Callable[..., str]:
    """把一个函数注册成模型可调用的工具。

    工具名 = 函数名；描述 = docstring 正文；参数 schema = 从签名自动生成。
    """
    name = fn.__name__
    if name in REGISTRY:
        raise ValueError(f"工具名重复: {name}")

    description, param_docs = _parse_docstring(fn.__doc__)

    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue  # *args / **kwargs 不进 schema
        annotation = hints.get(pname, param.annotation)
        jtype = _json_type(annotation)
        prop: dict[str, Any] = {"type": jtype}

        desc = param_docs.get(pname)
        if desc:
            prop["description"] = desc
        else:
            # 兜底：没写说明就自动生成一句
            if param.default is inspect.Parameter.empty:
                prop["description"] = f"{pname}（{_py_type_name(annotation)}）参数"
            elif param.default is None:
                prop["description"] = f"{pname}（{_py_type_name(annotation)}）参数，可选"
            else:
                prop["description"] = f"{pname}（{_py_type_name(annotation)}）参数，默认 {param.default!r}"

        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(pname)

        properties[pname] = prop

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    REGISTRY[name] = Tool(
        name=name,
        description=description or name,
        parameters=schema,
        handler=fn,
        metadata={"signature": sig, "hints": hints},
    )
    return fn


def register(
    name: str,
    description: str,
    parameters: dict[str, Any],
):
    """手写 schema 的注册方式（需要精细控制参数时用这个）。

    参数和函数名可以不一致；handler 依然接收一个 dict。
    """

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        if name in REGISTRY:
            raise ValueError(f"工具名重复: {name}")
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=lambda args: fn(args),
            metadata={"style": "manual"},
        )
        return fn

    return decorator


def tool_schemas() -> list[dict[str, Any]]:
    """返回发给模型的工具列表。"""
    return [t.to_openai_schema() for t in REGISTRY.values()]


def run_tool(name: str, args: dict[str, Any] | str) -> str:
    """执行一次工具调用，返回给模型的文本结果（永不抛异常，错误也变成文本反馈）。"""
    t = REGISTRY.get(name)
    if t is None:
        return f"[错误] 未知工具: {name}。可用工具: {', '.join(REGISTRY)}"

    # 解析参数
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            return f"[错误] 工具参数不是合法 JSON: {args}"
    if not isinstance(args, dict):
        return f"[错误] 工具参数必须是对象，收到: {type(args).__name__}"

    try:
        result = _invoke(t, args)
    except TypeError as exc:
        return f"[工具 {name} 调用失败] 参数不匹配: {exc}"
    except Exception as exc:  # 把异常转成模型能读懂的反馈，而不是打断整个循环
        return f"[工具 {name} 执行失败] {type(exc).__name__}: {exc}"

    if not isinstance(result, str):
        result = str(result)
    if len(result) > MAX_OUTPUT_CHARS:
        head = result[: MAX_OUTPUT_CHARS // 2]
        tail = result[-MAX_OUTPUT_CHARS // 2 :]
        result = f"{head}\n...[输出过长，已截断 {len(result) - MAX_OUTPUT_CHARS} 字符]...\n{tail}"
    return result


def _invoke(t: Tool, args: dict[str, Any]) -> str:
    """按函数签名组装参数并调用 handler。"""
    if t.metadata.get("style") == "manual":
        return t.handler(args)

    sig: inspect.Signature = t.metadata["signature"]
    hints: dict[str, Any] = t.metadata.get("hints", {})

    call_kwargs: dict[str, Any] = {}
    missing: list[str] = []
    for pname, param in sig.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if pname in args:
            call_kwargs[pname] = _coerce(args[pname], hints.get(pname, param.annotation))
        elif param.default is not inspect.Parameter.empty:
            continue  # 用默认值
        else:
            missing.append(pname)

    if missing:
        raise TypeError(f"缺少必填参数: {', '.join(missing)}")

    # 多余参数直接忽略（模型偶尔会带些没意义的字段）
    return t.handler(**call_kwargs)


def tool_names() -> list[str]:
    return list(REGISTRY.keys())


def _resolve_path(raw: str) -> Path:
    """把模型给的路径解析成绝对路径（相对 WORKDIR）。"""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (WORKDIR / p)


# ---------------------------------------------------------------------------
# 内置工具
# ---------------------------------------------------------------------------


@tool
def bash(command: str, timeout: int = 60) -> str:
    """在 bash 中执行一条 shell 命令并返回 stdout、stderr 和退出码。

    适合查看目录、跑 git、检查环境、运行测试等。

    Args:
        command: 要执行的 shell 命令，例如 "ls -la"
        timeout: 超时秒数，默认 60，最大 300
    """
    timeout = max(1, min(int(timeout), 300))
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(WORKDIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[超时] 命令在 {timeout}s 内未结束: {command}"
    parts = []
    if proc.stdout:
        parts.append(proc.stdout)
    if proc.stderr:
        parts.append("[stderr]\n" + proc.stderr)
    parts.append(f"[exit_code={proc.returncode}]")
    return "\n".join(parts)


@tool
def read_file(path: str, start_line: int = 1, max_lines: int = 0) -> str:
    """读取一个文本文件的内容，相对路径相对于项目工作目录。

    Args:
        path: 文件路径
        start_line: 起始行号，从 1 开始，默认 1
        max_lines: 最多读取多少行，0 表示读到文件末尾
    """
    p = _resolve_path(path)
    if not p.exists():
        return f"[错误] 文件不存在: {p}"
    if p.is_dir():
        return f"[错误] 这是一个目录: {p}"
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(start_line))
    end = len(lines) + 1 if not max_lines else start + int(max_lines)
    chunk = lines[start - 1 : end - 1]
    body = "\n".join(chunk)
    return f"{p} (行 {start}-{start + len(chunk) - 1} / 共 {len(lines)} 行)\n{body}"


@tool
def write_file(path: str, content: str) -> str:
    """写入文本文件，会覆盖已有内容，必要时自动创建父目录。

    Args:
        path: 文件路径
        content: 要写入的完整文本内容
    """
    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p}（{len(content)} 字符）"


@tool
def list_dir(path: str = ".") -> str:
    """列出某个目录下的文件和子目录。

    Args:
        path: 目录路径，默认是工作根目录
    """
    p = _resolve_path(path or ".")
    if not p.exists():
        return f"[错误] 目录不存在: {p}"
    if not p.is_dir():
        return f"[错误] 这不是目录: {p}"
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    if not entries:
        return f"{p} 是空目录"
    lines = []
    for entry in entries:
        kind = "dir " if entry.is_dir() else "file"
        size = "" if entry.is_dir() else f"  {entry.stat().st_size}B"
        lines.append(f"{kind}  {entry.name}{size}")
    return f"{p}\n" + "\n".join(lines)


if __name__ == "__main__":
    # 快速自检：python tools.py
    import json

    print("已注册工具:", tool_names())
    print(json.dumps(tool_schemas(), ensure_ascii=False, indent=2))
    print("--- bash 测试 ---")
    print(run_tool("bash", {"command": "echo hello from bash"}))
    print("--- list_dir 测试 ---")
    print(run_tool("list_dir", {"path": "."})[:300])
    print("--- 参数缺失测试 ---")
    print(run_tool("read_file", {}))
