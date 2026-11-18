# agent-loop

一个用 Python 写的**最简 agent loop**，用来实验验证「模型 + 工具 + 循环」这件事到底怎么跑起来的。

- 系统提示词写在 `prompts/system.md`（可改路径）
- 默认工具：`bash`、`read_file`、`write_file`、`list_dir`
- 会话上下文 **全部放在内存**，退出时落盘成一个 **时间戳命名** 的 json
- 启动时可以用 `--session` 指定 json 续聊

## 快速开始

```bash
cd agent-loop

# 1. 装依赖（uv 会自动用 .venv）
uv sync

# 2. 配 Key
cp .env.example .env
$EDITOR .env          # 填入 LLM_API_KEY

# 3. 跑
uv run python agent.py

# 离线自检（不联网，用假模型验证 loop 本身）
uv run python agent.py --mock
```

终端里直接聊天，输入 `/exit` 或 Ctrl+C 退出。

```
你 > 帮我看看当前目录有哪些文件
  ⚙️  调用工具 list_dir({"path": "."})
     ↩︎ agent-loop/  file  agent.py  ...
🤖 当前目录里有 agent.py、tools.py、llm.py、session.py …

你 > /exit
💾 会话已保存: sessions/20261118_102700.json
```

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--session PATH` | 加载会话 json 续聊。可以给完整路径，也可以给 `sessions/` 下的文件名 |
| `--prompt PATH` | 换一个系统提示词 md 文件，默认 `prompts/system.md` |
| `--model NAME` | 临时覆盖 `.env` 里的 `LLM_MODEL` |
| `--max-steps N` | 单轮内最多调用几次工具，默认 8，防死循环 |
| `--mock` | 离线自检模式，不联网 |
| `--no-stream` | 关闭流式，一次性拿完整回复（默认是流式） |
| `--effort LEVEL` | 思考强度：`low`(默认) / `medium` / `high` / `xhigh` / `max` / `default` |
| `--show-reasoning` | 实时显示模型的思考过程（暗色） |
| `--list-sessions` | 列出 `sessions/` 下的历史会话 |

## 交互命令（在输入框里输入）

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示帮助 |
| `/history` | 看当前消息条数 |
| `/save` | 立即存盘 |
| `/reset` | 清空上下文但保留会话文件 |
| `/exit` | 保存并退出 |

## 会话文件

存在 `sessions/YYYYMMDD_HHMMSS.json`（本地时间时间戳）：

```jsonc
{
  "id": "20261118_102700",
  "created_at": "2026-11-18T10:27:00+08:00",
  "updated_at": "2026-11-18T10:27:00+08:00",
  "model": "MiniMax-M2.7",
  "system_prompt": "……存档一份，方便复现",
  "messages": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\": \"ls\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "agent.py\ntools.py"},
    {"role": "assistant", "content": "目录里有 4 个 py 文件。"}
  ]
}
```

续聊：

```bash
uv run python agent.py --session sessions/20261118_102700.json
```

`system` 那条消息不写进 `messages`，续聊时从 md 文件重新加载，保证提示词永远跟当前配置一致。

## 如何添加新工具

**写一个函数，加个 `@tool` 装饰器，就完事了。** 其余全自动：

- **工具名** = 函数名
- **工具描述** = docstring 正文（`Args:` / `Returns:` 这些段落会自动剔除）
- **参数 schema** = 从函数签名自动生成（类型注解 → JSON Schema 类型，带默认值的参数自动变成可选）
- **参数说明** = 从 docstring 的 `Args:` 段自动扒出来；**没写说明的会自动兜底**生成 `xxx（类型）参数`

```python
from tools import tool

@tool
def http_get(url: str, timeout: int = 30) -> str:
    """发起一次 HTTP GET 请求，返回响应正文。

    Args:
        url: 要请求的 URL，例如 https://example.com
        timeout: 超时秒数，默认 30
    """
    import httpx
    return httpx.get(url, timeout=timeout).text[:4000]
```

模型最终看到的 schema 长这样：

```jsonc
{
  "name": "http_get",
  "description": "发起一次 HTTP GET 请求，返回响应正文。",
  "parameters": {
    "type": "object",
    "properties": {
      "url":   {"type": "string",  "description": "要请求的 URL，例如 https://example.com"},
      "timeout": {"type": "integer", "description": "超时秒数，默认 30", "default": 30}
    },
    "required": ["url"]
  }
}
```

### 参数说明的三种写法（都能识别）

| 写法 | 例子 |
| --- | --- |
| Google 风格 | `Args:` 段里 `参数名: 说明` |
| reST 风格 | `:param 参数名: 说明` |
| 啥也不写 | 自动兜底成 `参数名（类型）参数，默认 xxx` |

### 其它约定

- 函数**只接收关键字参数**、返回一个字符串；返回的字符串就是喂回给模型的工具结果。
- 类型会自动转换：模型传 `"30"` 给 `int` 参数会被转成 `30`，传 `"true"` 给 `bool` 会转成 `True`。
- 缺必填参数、参数类型不对、执行报错，都会转成 `[工具 xxx …]` 文本回灌给模型，**不会打断循环**。
- `*args` / `**kwargs` 不会进 schema。

### 注册表会自动生效

`REGISTRY` 里的工具会被 `tools.tool_schemas()` 转成 OpenAI 格式发给模型，`agent.py` 每次调用前都会重新取一次，所以**改完文件直接重启程序即可，不用改任何其它地方**。

### 自检

```bash
uv run python tools.py     # 打印已注册工具 + 自动生成的 schema + 试跑一次
```

### 想把工具拆到别的文件？

在别的文件里 import `tool` 装饰器即可（前提是那个模块被 import 过，装饰器在 import 时就完成注册）：

```python
# my_tools.py
from tools import tool

@tool
def get_time() -> str:
    """返回当前时间。"""
    import datetime
    return datetime.datetime.now().isoformat()
```

然后在 `tools.py` 末尾加一行 `import my_tools`（或在 `agent.py` 里 import）。

### 需要手写 schema 的极端情况

如果参数结构比较特殊（比如要传任意 key 的字典），可以用 `register` 手动指定 schema，此时 handler 接收一个 dict：

```python
from tools import register

@register(
    name="query_db",
    description="按 SQL 查询数据库",
    parameters={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "SQL 语句"}},
        "required": ["sql"],
    },
)
def query_db(args: dict) -> str:
    return run_sql(args["sql"])
```

### 加个「需要用户确认」的工具？

现在这个最小版**没有权限机制**，工具执行即执行。如果要加，改 `tools.run_tool()`：在真正调用 `tool.handler` 之前插入你的确认逻辑（比如 `input("确认执行? [y/N] ")`）。

## 代码结构

| 文件 | 作用 |
| --- | --- |
| `agent.py` | 主循环、命令行参数、终端交互、会话存取 |
| `tools.py` | 工具注册表 + 4 个内置工具 |
| `llm.py` | OpenAI 兼容模型客户端（流式 + 工具调用 + 思考强度，含 mock 模式） |
| `session.py` | 会话 json 的读 / 写 / 列举 |
| `ui.py` | 终端配色（AI 回答 / 工具日志 / 提示信息分色） |
| `textsafe.py` | 代理对（emoji）清洗，防止 UTF-8 编码崩溃 |
| `prompts/system.md` | 系统提示词 |

## 思考强度（effort）

模型是推理模型（默认 `MiniMax-M3.1-Flash`），思考量可以通过 `reasoning_effort` 调节：

| 档位 | 用途 |
| --- | --- |
| `low` | **默认**，够用且快，适合日常问答、简单改代码 |
| `medium` | 中等推理 |
| `high` | 复杂任务、需要多步推理时 |
| `xhigh` / `max` | 很难的题（算法、方案权衡），慢但更稳 |
| `default` | **不传该参数**，完全交给模型自己决定强度 |

```bash
uv run python agent.py --effort high
LLM_EFFORT=max uv run python agent.py     # 或用环境变量
```

也可以写进 `.env`：`LLM_EFFORT=low`。

两个注意点：

- **`off` 不可用**：MiniMax-M3.1-Flash 强制 adaptive thinking，关掉思考会被接口直接拒绝（400），所以最省的一档就是 `low`。
- 强度只影响**思考量**，不改变工具调用能力；简单任务用 `high` 纯属浪费时间和 token。

### 看思考过程

默认思考内容会被存进会话但不打印。加 `--show-reasoning` 可以实时看到（暗色，不刷正文）：

```
你 >
💭 思考中…9.9 is bigger than 9.11. Decimal comparison: 9.9 = 9.90 > 9.11.
🤖 9.9 大。
```

## 流式输出与工具调用可视化

默认走**流式**（SSE），模型一边生成一边打印，回答是"逐字冒出来"的：

```
你 >
🤖 I'll take a look at the directory structure.

  ⚙️ 调用工具 list_dir({"path": "."})
    ↩︎ /Users/yzy/Desktop/study_code/ai-test/agent-loop
dir   .idea
dir   .venv
…（共 388 字符，已截断）

🤖 这是一个 Python 写的 CLI agent loop 项目（`agent-loop/`），核心代码是 `agent.py`…
```

- **AI 正文**：青色，逐块流式打印
- **工具调用**：黄色一行，含**工具名 + 截断后的参数 JSON**（参数截到 200 字符）
- **工具返回值**：灰色，**只预览前 300 字符**并标注总长度（完整结果仍然会完整回传给模型）
- 推理模型的思考过程（`reasoning_content`）会被保存但不刷屏；只有当正文为空时才暗色显示一小段

关闭流式：`.env` 里设 `LLM_STREAM=0`，或命令行加 `--no-stream`。

## 乱码/崩溃兜底（surrogate pair）

偶尔会遇到这种报错：

```
[错误] 调用模型失败: UnicodeEncodeError: 'utf-8' codec can't encode characters ... surrogates not allowed
```

**原因**：模型服务在 SSE 里把一个 emoji 的代理对（`\ud83d\ude00`）拆到**两个分片**下发，
Python 的 str 里就留下了落单代理对 —— 它**无法编码成 UTF-8**。于是下一次请求 httpx 序列化
请求体时就炸了（所以报错出现在"你输入文本之后"，其实是上一轮回复里埋的雷）。

**已处理**（`textsafe.py`）：

| 情况 | 处理 |
| --- | --- |
| 代理对被拆到两个分片 | 跨分片缓冲，重新拼成真正的 emoji |
| 真的落单（凑不齐对） | 替换成 ``(U+FFFD)，不崩 |
| 老会话文件里已有脏数据 | 每次请求前统一清洗一遍 |
| 存盘时仍编码不了 | 自动退回 ascii 转义保存，会话不丢 |

所以正常情况下你**不会再看到这个报错**，emoji 也能正确显示。

## 终端配色

终端里不同来源的内容用颜色区分，方便一眼看清：

| 内容 | 颜色 |
| --- | --- |
| 你的输入提示符 `你 >` | 洋红加粗 |
| AI 的回答正文 | 青色 |
| 工具调用行 `⚙️ 调用工具 xxx` | 黄色 |
| 工具返回结果 `↩︎` | 灰色 |
| 系统提示（会话 ID、保存路径等） | 蓝色 / 暗色 |
| 错误信息 | 红色 |

行为规则：

- 输出被重定向到文件或走管道时**自动关闭颜色**（不会在文件里留下乱码）。
- `NO_COLOR=1` 强制关闭；`FORCE_COLOR=1` 强制开启。
- 想改配色，直接改 `ui.py` 里的常量（如 `CYAN`、`YELLOW`）。

## 已知边界

- 没有并行工具调用、没有上下文压缩、没有重试与限流处理。
- 工具之间是串行执行的。
- `bash` 就是在你的真实 shell 里跑命令，没有沙箱（本地实验环境，注意别让模型删掉重要文件）。
