"""模型客户端：OpenAI 兼容的 /chat/completions 接口（MiniMax 开放平台即为此协议）。

只依赖 httpx；配置全部从 .env / 环境变量读取，不在代码里写死任何 Key。
默认走**流式**（SSE），方便终端实时看到模型输出和工具调用过程。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

import textsafe

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """极简 .env 读取器：解析 KEY=VALUE，已存在的环境变量优先。"""
    env_path = Path(path) if path else BASE_DIR / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        values[key] = val
        os.environ.setdefault(key, val)
    return values


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


# 思考强度档位（对应 MiniMax 模型的 reasoning_effort 取值）
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
# default = 不传该参数，由模型自行决定；off = 不支持（M3.1-Flash 强制 adaptive thinking）
EFFORT_DEFAULT = "default"


class LLMConfig:
    """从环境变量构造的模型配置。"""

    def __init__(self) -> None:
        load_dotenv()
        self.base_url = (env("LLM_BASE_URL", "https://api.minimaxi.com/v1")).rstrip("/")
        self.api_key = env("LLM_API_KEY", "")
        self.model = env("LLM_MODEL", "MiniMax-M2.7")
        self.temperature = float(env("LLM_TEMPERATURE", "0.7"))
        self.max_tokens = int(env("LLM_MAX_TOKENS", "4096"))
        self.timeout = float(env("LLM_TIMEOUT", "180"))
        self.stream = env("LLM_STREAM", "1") not in ("0", "false", "False", "")
        self.effort = (env("LLM_EFFORT", "low") or "low").strip().lower()

    def check(self) -> None:
        if not self.api_key:
            raise SystemExit(
                "缺少 LLM_API_KEY。请复制 .env.example 为 .env 并填入你的 API Key。\n"
                f"参考文件: {BASE_DIR / '.env.example'}"
            )
        if self.effort == "off":
            raise SystemExit(
                "思考强度 off 不可用：MiniMax-M3.1-Flash 强制 adaptive thinking，"
                "关闭思考会被接口拒绝（400）。请用 low 作为最省的一档。"
            )
        if self.effort not in EFFORT_LEVELS + [EFFORT_DEFAULT]:
            raise SystemExit(
                f"未知的思考强度 {self.effort!r}。可选: {' / '.join(EFFORT_LEVELS)}"
                f" / {EFFORT_DEFAULT}（由模型自行决定）。"
            )


# ---------------------------------------------------------------------------
# 流式事件
# ---------------------------------------------------------------------------


class StreamEvent:
    """流式过程中可以回调的事件类型。"""

    TEXT = "text"          # 正文增量
    REASONING = "reasoning"  # 思考过程增量（推理模型）
    TOOL_CALL = "tool_call"  # 一个完整的工具调用（参数已拼齐）


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class LLM:
    """把消息和工具列表发给模型，拿回一条 assistant 消息（默认流式）。"""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig()
        self.config.check()
        self._client = httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
        )

    # -- 请求体 ---------------------------------------------------------

    @staticmethod
    def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把消息里的所有文本清洗成可安全 UTF-8 编码的形式。

        防御场景：旧会话文件里可能已经存了落单代理对，直接回传会让 httpx 序列化时抛
        UnicodeEncodeError。
        """
        cleaned: list[dict[str, Any]] = []
        for raw in messages:
            m = dict(raw)
            if isinstance(m.get("content"), str):
                m["content"] = textsafe.clean(m["content"])
            if isinstance(m.get("reasoning_content"), str):
                m["reasoning_content"] = textsafe.clean(m["reasoning_content"])
            if m.get("tool_calls"):
                new_calls = []
                for tc in m["tool_calls"]:
                    tc = dict(tc)
                    fn = dict(tc.get("function") or {})
                    if isinstance(fn.get("arguments"), str):
                        fn["arguments"] = textsafe.clean(fn["arguments"])
                    tc["function"] = fn
                    new_calls.append(tc)
                m["tool_calls"] = new_calls
            cleaned.append(m)
        return cleaned

    def _payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._clean_messages(messages),
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # 思考强度：default 表示不传，由模型自行决定
        if self.config.effort and self.config.effort != EFFORT_DEFAULT:
            payload["reasoning_effort"] = self.config.effort
        return payload

    # -- 非流式（保留，调试用） ------------------------------------------

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """一次性拿到完整回复（不走流式）。"""
        payload = self._payload(messages, tools, stream=False)
        payload.pop("stream_options", None)
        resp = self._client.post("/chat/completions", json=payload)
        self._raise_for_status(resp)
        data = resp.json()
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"模型返回结构异常: {str(data)[:800]}") from exc
        return self._normalize(message)

    # -- 流式 ------------------------------------------------------------

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        on_tool_call: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """流式调用模型。

        - ``on_text``       每收到一段正文就回调一次（终端可以实时打印）
        - ``on_reasoning``  推理模型的思考过程增量
        - ``on_tool_call``  每拼齐一个工具调用就回调一次

        返回拼装好的 assistant 消息（OpenAI 格式）。
        """
        payload = self._payload(messages, tools, stream=True)
        content_buf = textsafe.SafeTextBuffer()
        reasoning_buf = textsafe.SafeTextBuffer()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}

        with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            self._raise_for_status(resp)
            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        break
                    continue

                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # 服务端不支持流式时，可能直接返回整包 JSON
                if "choices" not in chunk:
                    continue
                try:
                    delta = chunk["choices"][0].get("delta") or {}
                except (KeyError, IndexError):
                    continue

                text = delta.get("content")
                if text:
                    # 先把可能被拆开的代理对拼好，再打印/存储，避免后续请求编码失败
                    safe = content_buf.feed(text)
                    if safe:
                        content_parts.append(safe)
                        if on_text:
                            on_text(safe)

                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    safe = reasoning_buf.feed(reasoning)
                    if safe:
                        reasoning_parts.append(safe)
                        if on_reasoning:
                            on_reasoning(safe)

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

        # 收尾：把暂存的高位代理也处理掉
        tail = content_buf.flush()
        if tail:
            content_parts.append(tail)
            if on_text:
                on_text(tail)
        reasoning_buf.flush()

        # 拼装工具调用
        tool_calls: list[dict[str, Any]] = []
        for i, slot in sorted(tool_acc.items()):
            if not slot["name"]:
                continue
            call = {
                "id": slot["id"] or f"call_{i}",
                "type": "function",
                "function": {
                    "name": textsafe.clean(slot["name"]),
                    "arguments": textsafe.clean(slot["arguments"]) or "{}",
                },
            }
            tool_calls.append(call)
            if on_tool_call:
                on_tool_call(call)

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    # -- 工具 ------------------------------------------------------------

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code != 200:
            try:
                body = resp.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            hint = ""
            if resp.status_code == 400:
                hint = (
                    "\n提示：部分模型对参数有限制。若报 reasoning_effort 相关错误，"
                    "可以改 LLM_EFFORT（low/medium/high/xhigh/max/default）；"
                    "若报 thinking 相关错误，换个模型试试，例如 LLM_MODEL=MiniMax-M2.5。"
                )
            raise RuntimeError(f"模型接口返回 {resp.status_code}: {body[:800]}{hint}")

    @staticmethod
    def _normalize(message: dict[str, Any]) -> dict[str, Any]:
        """归一化：去掉 null 字段，保证回灌给模型的消息干净。"""
        message = {k: v for k, v in message.items() if v is not None}
        message.setdefault("role", "assistant")
        message.setdefault("content", "")
        if isinstance(message.get("content"), str):
            message["content"] = textsafe.clean(message["content"])
        if message.get("tool_calls"):
            message["tool_calls"] = [
                {
                    "id": tc.get("id", f"call_{i}"),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments", "{}"),
                    },
                }
                for i, tc in enumerate(message["tool_calls"])
            ]
        return message

    def close(self) -> None:
        self._client.close()


class MockLLM:
    """不联网的假模型，用来离线验证 agent loop 本身。

    规则：第一次收到用户消息就请求调用 bash 工具，第二次返回最终文本（分块吐出来，模拟流式）。
    """

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.calls = 0

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.chat_stream(messages, tools)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        on_tool_call: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            call = {
                "id": "call_mock_1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "echo mock-tool-ran"}'},
            }
            if on_tool_call:
                on_tool_call(call)
            return {"role": "assistant", "content": "", "tool_calls": [call]}

        text = "收到工具结果，mock 流程走通 ✅（这是不联网的自检模式）"
        if on_text:
            for i in range(0, len(text), 8):
                on_text(text[i : i + 8])
        return {"role": "assistant", "content": text}

    def close(self) -> None:
        pass
