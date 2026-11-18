#!/usr/bin/env python3
"""最基础的 agent loop：终端聊天 + 工具调用 + 会话 json 持久化。

用法::

    python agent.py                      # 新会话
    python agent.py --session sessions/20261118_102700.json   # 续聊
    python agent.py --mock               # 不联网，跑通流程自检
    python agent.py --list-sessions      # 看看有哪些历史会话
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import llm
import session as session_mod
import tools as tools_mod
import ui

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT_FILE = BASE_DIR / "prompts" / "system.md"


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def load_system_prompt(prompt_file: str | Path | None = None) -> str:
    """从 md 文件加载系统提示词（可配置路径）。"""
    path = Path(prompt_file) if prompt_file else DEFAULT_PROMPT_FILE
    if not path.exists():
        raise SystemExit(f"系统提示词文件不存在: {path}")
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# 终端交互
# ---------------------------------------------------------------------------

HELP_TEXT = """
可用命令（在输入框直接输入）：
  /help      显示本帮助
  /history   打印当前会话的消息条数
  /save      立即把当前会话写入 json
  /reset     清空当前会话上下文（重新开始，但仍用同一个会话文件）
  /exit      保存并退出（也可直接 Ctrl+D / Ctrl+C）
""".strip()


def print_assistant_text(text: str) -> None:
    """打印 AI 的回答（青色，和用户输入一眼能分开）。"""
    if text.strip():
        print(f"\n{ui.ai('🤖')} {ui.ai(text.strip())}\n")


# 工具日志的截断长度（避免刷屏）
ARGS_PREVIEW_CHARS = 200
OUTPUT_PREVIEW_CHARS = 300


def _preview(text: str, limit: int) -> tuple[str, bool]:
    """截断预览，返回 (预览文本, 是否被截断)。"""
    flat = text.strip()
    if len(flat) <= limit:
        return flat, False
    return flat[:limit] + f" …（共 {len(flat)} 字符，已截断）", True


def run_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """执行模型请求的所有工具调用，返回要回灌给模型的 tool 消息列表。"""
    results: list[dict[str, Any]] = []
    for call in tool_calls:
        fn = call.get("function", {})
        name = fn.get("name", "")
        raw_args = fn.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            args = None
            output = f"[错误] 工具参数不是合法 JSON: {raw_args}"
        else:
            output = None

        # 先打印工具名和参数（即使参数解析失败也要让人看到调了哪个工具）
        args_text = raw_args if args is None else json.dumps(args, ensure_ascii=False)
        args_text, _ = _preview(args_text, ARGS_PREVIEW_CHARS)
        print(
            f"  {ui.tool_call('⚙️')} "
            f"{ui.tool_call(f'调用工具 {name}')}"
            f"{ui.tool_output('(' + args_text + ')')}"
        )
        if output is None:
            output = tools_mod.run_tool(name, args)

        preview, truncated = _preview(output, OUTPUT_PREVIEW_CHARS)
        print(f"    {ui.tool_output('↩︎ ' + preview)}")
        results.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": output,
            }
        )
    return results


def call_model(
    client: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_text: Any,
    on_reasoning: Any,
) -> dict[str, Any]:
    """流式优先；配置里关掉流式（LLM_STREAM=0）时退回一次性返回。"""
    config = getattr(client, "config", None)
    if config is not None and not getattr(config, "stream", True):
        message = client.chat(messages, tools)
        content = message.get("content", "")
        if content:
            on_text(content)
        return message
    return client.chat_stream(
        messages, tools, on_text=on_text, on_reasoning=on_reasoning, on_tool_call=None
    )


def agent_turn(
    client: Any,
    messages: list[dict[str, Any]],
    max_steps: int,
    show_reasoning: bool = False,
) -> None:
    """处理一轮用户输入：模型 →（可能多次）工具 → 最终回复。"""
    tools_schema = tools_mod.tool_schemas()
    for step in range(1, max_steps + 1):
        printed = {"text": False, "badge": False}

        def on_text(chunk: str) -> None:
            if not printed["badge"]:
                print(f"\n{ui.ai('🤖')} ", end="", flush=True)
                printed["badge"] = True
            print(ui.ai(chunk), end="", flush=True)
            printed["text"] = True

        reasoning_state = {"shown": False}

        def on_reasoning(chunk: str) -> None:
            # 思考过程默认不刷屏；--show-reasoning 时实时暗色显示
            if not show_reasoning:
                return
            if not reasoning_state["shown"]:
                print(f"\n{ui.notice('💭 思考中…')}", end="", flush=True)
                reasoning_state["shown"] = True
            print(ui.notice(chunk), end="", flush=True)

        message = call_model(client, messages, tools_schema, on_text, on_reasoning)
        if reasoning_state["shown"]:
            print("\n")
        if printed["text"]:
            print("\n")
        messages.append(message)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            if not printed["text"]:
                # 没有正文时，可能是推理模型只给了思考过程
                reasoning = message.get("reasoning_content") or ""
                if reasoning:
                    preview, _ = _preview(reasoning, 800)
                    print(f"\n{ui.notice('🤔 ' + preview)}\n")
                else:
                    print_assistant_text(message.get("content", ""))
            return

        for tool_msg in run_tool_calls(tool_calls):
            messages.append(tool_msg)

        if step == max_steps:
            print(ui.tool_call("\n[警告] 达到单轮最大步数，停止工具调用。\n"))
            messages.append(
                {"role": "user", "content": "（系统提示：请直接给出最终总结，不再调用工具。）"}
            )
    return


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="最简 agent loop")
    p.add_argument("--session", help="要加载的会话 json 路径或文件名（留空则新建会话）")
    p.add_argument("--prompt", default=None, help="系统提示词 md 文件路径，默认 prompts/system.md")
    p.add_argument("--model", help="模型名，覆盖 .env 里的 LLM_MODEL")
    p.add_argument("--max-steps", type=int, default=8, help="单轮内最多调用工具的次数")
    p.add_argument("--mock", action="store_true", help="离线自检模式，用假模型走通流程")
    p.add_argument("--no-stream", action="store_true", help="关闭流式，一次性拿完整回复")
    p.add_argument(
        "--effort",
        default=None,
        help="思考强度: low(默认) / medium / high / xhigh / max / default(交给模型自己决定)",
    )
    p.add_argument("--show-reasoning", action="store_true", help="实时显示模型的思考过程")
    p.add_argument("--list-sessions", action="store_true", help="列出历史会话后退出")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.list_sessions:
        files = session_mod.list_sessions()
        if not files:
            print(ui.info("还没有历史会话。"))
        for f in files:
            print(f.name)
        return 0

    llm.load_dotenv()
    if args.model:
        os.environ["LLM_MODEL"] = args.model
    if args.no_stream:
        os.environ["LLM_STREAM"] = "0"
    if args.effort:
        os.environ["LLM_EFFORT"] = args.effort

    system_prompt = load_system_prompt(args.prompt)

    # 1) 准备上下文（全部在内存里）
    messages: list[dict[str, Any]] = []
    session_id = session_mod.new_session_id()
    if args.session:
        data = session_mod.load_session(args.session)
        session_id = data.get("id", session_id)
        messages.extend(data.get("messages", []))
        print(ui.info(f"↩︎  已加载会话 {session_id}（{len(messages)} 条历史消息）"))
    messages.insert(0, {"role": "system", "content": system_prompt})

    # 2) 准备模型客户端
    client = llm.MockLLM() if args.mock else llm.LLM()
    if args.mock:
        print(ui.info("🧪 mock 模式：不联网，用假模型验证 agent loop"))
    else:
        cfg = llm.LLMConfig()
        mode = "流式" if cfg.stream else "非流式"
        effort_label = cfg.effort if cfg.effort != llm.EFFORT_DEFAULT else "default(模型自定)"
        print(
            ui.info(
                f"🤖 模型: {cfg.model}（{mode}，思考强度 {effort_label}）"
                f" | 工具: {', '.join(tools_mod.tool_names())}"
            )
        )

    print(ui.info(f"📁 会话 ID: {session_id}（退出时自动保存到 sessions/{session_id}.json）"))
    print(ui.notice("输入内容开始聊天，输入 /help 查看命令，/exit 退出。\n"))

    # 3) 主循环
    try:
        while True:
            try:
                user_input = input(ui.user("你 > ")).strip()
            except EOFError:
                print()
                break
            if not user_input:
                continue

            if user_input.startswith("/"):
                cmd, _, rest = user_input.partition(" ")
                cmd = cmd.lower()
                if cmd in ("/exit", "/quit"):
                    print(ui.notice("👋 退出中…"))
                    break
                if cmd == "/help":
                    print(ui.info(HELP_TEXT) + "\n")
                    continue
                if cmd == "/history":
                    print(ui.info(f"当前共 {len(messages)} 条消息（含 system）"))
                    continue
                if cmd == "/save":
                    path = session_mod.save_session(
                        session_id,
                        messages[1:],  # 不重复存 system，续聊时会重新加载
                        model=llm.LLMConfig().model if not args.mock else "mock",
                        system_prompt=system_prompt,
                    )
                    print(ui.info(f"💾 已保存: {path}"))
                    continue
                if cmd == "/reset":
                    messages = [{"role": "system", "content": system_prompt}]
                    print(ui.notice("🧹 上下文已清空。"))
                    continue
                print(ui.error(f"未知命令 {cmd}，输入 /help 查看可用命令。"))
                continue

            messages.append({"role": "user", "content": user_input})
            try:
                agent_turn(client, messages, args.max_steps, show_reasoning=args.show_reasoning)
            except Exception as exc:
                print(ui.error(f"\n[错误] 调用模型失败: {type(exc).__name__}: {exc}\n"))
                # 撤掉这条用户消息，避免上下文被污染
                messages.pop()
    except KeyboardInterrupt:
        print(ui.notice("\n👋 被 Ctrl+C 中断，保存后退出。"))

    # 4) 持久化
    path = session_mod.save_session(
        session_id,
        messages[1:],
        model="mock" if args.mock else llm.LLMConfig().model,
        system_prompt=system_prompt,
    )
    print(ui.info(f"💾 会话已保存: {path}"))
    print(ui.notice(f"   续聊: python agent.py --session sessions/{session_id}.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
