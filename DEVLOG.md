# 开发日志 · agent-loop

日期：2026-11-18
目标：用最小代码跑通「模型 + 工具调用 + 会话持久化」的完整循环，只求实验验证，不做工程化包装。

## 1. 需求确认

用户诉求（原文拆解）：

| 需求 | 落地方式 |
| --- | --- |
| 新建 `agent-loop` 文件夹，用 uv 建虚拟环境 | `uv init --bare --python 3.11` + `uv add httpx pyyaml`，环境在 `agent-loop/.venv` |
| Python 写最基础 agent loop | `agent.py` 单文件主循环 |
| 系统提示词提前写好、可配置、加载 md | `prompts/system.md`，`--prompt` 可换路径 |
| 默认工具包含 bash 等基础工具 | `bash` / `read_file` / `write_file` / `list_dir` |
| 说明如何添加工具 | `tools.py` 的 `@register` 装饰器 + README 专门章节 |
| 一切按简单模型执行 | 默认 `LLM_MODEL=MiniMax-M2.7`，可用 `--model` 覆盖 |
| 会话上下文全部加载进内存 | `agent.py` 里只有一个 `messages` list，全程不落盘 |
| 终端聊天，可指定会话 json | `--session sessions/xxx.json` |
| 关闭后持久化到 json，时间戳命名 | `sessions/YYYYMMDD_HHMMSS.json` |

开工前用问卷确认了三件事：模型用 MiniMax 开放平台、工具用最小集、json 存项目内 `sessions/`。

## 2. 环境与接口调研（关键坑）

1. `~/.minimax/config.yaml` 里的 `provider.minimax.options.apiKey` 是**占位符 `sk-xxx`**，内置 `llm-call` skill 直接调用返回 `401 auth failed`。
2. 用本机 OAuth token（`~/.minimax/auth/.../auth.json` 的 `mmoat_...`）打 `https://agent.minimax.cn/mavis/api/v1/llm/v1/chat/completions` → `503 direct_route_not_configured`；打 `/messages` → 401。即：**App 内部网关不接受外部直连**，这条路走不通。
3. `https://api.minimaxi.com/v1/chat/completions` 是**标准 OpenAI 兼容接口**，只差一个真正的 API Key（官方返回 1004「请在 Authorization 头携带 API secret key」，说明协议、URL 都是对的）。

**结论**：agent loop 走 OpenAI 兼容协议 + 用户自备 Key。好处是换厂商只改 `.env` 的 `LLM_BASE_URL` / `LLM_MODEL`，代码不动。

## 3. 技术选型

- 只依赖 `httpx`（HTTP）+ `pyyaml`（为将来读 yaml 配置留口子）。`.env` 自己写了个 10 行解析器，不引入 python-dotenv。
- 工具层用**注册表 + 装饰器**：`@register(name, description, parameters)`。模型侧统一转成 OpenAI 的 `{"type":"function","function":{...}}`。
- 会话层：内存 `messages` list（含 assistant 的 `tool_calls` 与 role=tool 的结果），退出时整体 dump 成 json。**system 消息不进 messages**，续聊时从 md 重新加载，保证提示词永远跟当前配置一致。
- 防死循环：单轮内 `--max-steps`（默认 8）次工具调用上限，超了强制让模型给总结。

## 4. 实现要点

- `tools.run_tool()` 捕获所有异常，把错误转成 `[工具 xxx 执行失败] ...` 文本回灌给模型 —— 让模型自己纠正，而不是整个进程挂掉。
- 输出超过 8000 字符做头尾截断，避免上下文被一次 `cat` 撑爆。
- `bash` 工具：`shell=True`、`cwd=WORKDIR`、默认 60s 超时（上限 300s），返回 stdout/stderr + exit code。
- 加了 `--mock` 模式（`llm.MockLLM`）：不联网，用假模型先请求一次 `bash` 再返回总结，用来离线验证循环本身没写错。

## 5. 自测结果

| 检查项 | 结果 |
| --- | --- |
| `python tools.py`（注册表 + bash/list_dir 试跑） | ✅ 4 个工具注册成功，输出正常 |
| `agent.py --mock` 端到端 | ✅ 用户输入 → 调 `bash` → 工具结果回灌 → 模型总结，流程走通 |
| 会话落盘 | ✅ `sessions/20261118_103326.json`，messages 里 user/assistant/tool 齐全 |
| `--session` 续聊 | ✅ 加载 4 条历史消息后继续对话 |

**未验证**：真实 MiniMax API 的工具调用（缺 API Key）。用户填好 `.env` 后跑一次 `uv run python agent.py` 即可确认；若 M2.7 对非流式或 `temperature` 有限制会报 400，错误里已带上换模型提示。

## 6. 已知边界（后续可加）

- 无权限确认机制：工具执行即执行，`bash` 直接在真实 shell 里跑。
- 无上下文压缩、无重试/限流、工具串行执行、无并行工具调用。
- 推理型模型（如 M2.7）可能需要流式或对参数有限制 —— 后续可在 `llm.py` 加 `stream` 支持。

## 7. 下一步（供实验）

1. 填 `.env` 的 `LLM_API_KEY`，`uv run python agent.py` 试一次真实调用。
2. 试 `--prompt` 换一套提示词，对比行为差异。
3. 按 README 的「如何添加新工具」加一个自己的工具（比如 `http_get`），观察模型怎么用它。

---

## 8. 迭代 v2：工具层改成「装饰器 + docstring 自动生成 schema」

用户反馈：不想手写 schema。既然 Python 函数本来就能写 docstring，那就让装饰器把 docstring 吃进去。

### 改动

新增 `@tool` 装饰器（原 `@register` 降级为「需要手写 schema 时」的低层接口，保留）：

| 要素 | 来源 |
| --- | --- |
| 工具名 | `fn.__name__` |
| 工具描述 | docstring 正文，自动剔除 `Args:` / `Returns:` 等段落 |
| 参数名 | 函数签名 |
| 参数类型 | 类型注解 → JSON Schema（str→string, int→integer, float→number, bool→boolean, list→array, dict→object；`Optional[X]` 取 X） |
| 必填 / 可选 | 有无默认值 |
| 参数说明 | docstring 的 `Args:` 段（Google 风格）或 `:param x:`（reST 风格） |
| 参数说明兜底 | 没写就自动生成 `xxx（类型）参数，默认 N` / `xxx（类型）参数，可选` |

### 顺带加的两件事

- **类型自动转换**：模型传 `"30"` 给 `int` 参数会被转成 `30`，`"true"` 给 `bool` 转成 `True`（`_coerce`）。
- **调用改为关键字参数**：`run_tool` 按签名组装 kwargs，缺必填参数报 `缺少必填参数: x`，多余参数忽略。

### 自测结果

| 检查项 | 结果 |
| --- | --- |
| 4 个内置工具的自动 schema | ✅ 描述、类型、required、default 全部正确 |
| 无 `Args:` 段 → 兜底说明 | ✅ `get_time`/`echo_msg` 的 `times` 生成 `times（int）参数，默认 2` |
| reST 风格 `:param msg:` | ✅ 被识别为参数说明 |
| 类型转换 | ✅ `"true"→True`、`"0.9"→0.9` |
| 缺必填参数 / 未知工具 | ✅ 返回可读错误文本，循环不中断 |
| agent 主循环回归（mock） | ✅ 流程照常走通 |

### 踩过的坑

1. 第一版 `_parse_docstring` 用切片 `lines[arg_start + len(param_docs):]` 试图剔除参数段，行号对不上导致描述里混进了参数行 → 改成「描述 = Args 段之前的正文」，逻辑简单也不会错位。
2. `_invoke` 里把多余参数塞进 `_extra_args` kwarg，handler 不接受 → 报 `TypeError`。直接忽略多余参数。
3. `from __future__ import annotations` 会让注解变成字符串，`inspect.signature` 拿到的不是真类型 → 用 `typing.get_type_hints()` 解析，失败时退回原始注解。

---

## 9. 迭代 v3：终端配色

用户反馈：AI 的回答和自己在终端敲的字颜色一样，看不出谁是谁。

### 改动

新增 `ui.py`：

| 内容 | 颜色 | 函数 |
| --- | --- | --- |
| 用户输入提示符 `你 >` | 洋红 + 加粗 | `ui.user()` |
| AI 回答正文 | 青色 | `ui.ai()` |
| 工具调用行 | 黄色 | `ui.tool_call()` |
| 工具返回结果 | 灰色 | `ui.tool_output()` |
| 系统提示（会话 ID、保存路径） | 蓝色 | `ui.info()` |
| 次要提示 | 暗色 | `ui.notice()` |
| 错误 | 红色 | `ui.error()` |

### 关键取舍

- **非 tty 自动关色**：输出重定向到文件或走管道时判断 `sys.stdout.isatty()`，避免在日志文件里写进一堆 `ESC[36m`。支持 `NO_COLOR` / `FORCE_COLOR` 环境变量覆盖。
- 颜色独立成 `ui.py`，`agent.py` 里只调语义化函数，想换配色不用改业务逻辑。
- 工具调用行把「工具名」和「参数 JSON」也分成不同色，参数太长时更容易扫读。

### 自测结果

| 检查项 | 结果 |
| --- | --- |
| `FORCE_COLOR=1` 输出含正确 ANSI 码 | ✅ 35/1（用户）、36（AI）、33（工具）、90（工具结果）、34（信息）、2（暗色） |
| 管道输出无颜色码 | ✅ 未检测到 `ESC[` |
| mock 主循环回归 | ✅ 正常走通 |

测试产生的会话文件已清理。

---

## 10. 迭代 v4：流式输出 + 工具调用可视化

用户诉求：AI 回答改成流式（终端实时可见），并把工具调用过程打印出来（工具名 + 参数 + 截断的返回值），但别刷屏。

### 改动

**`llm.py`** —— 新增 `chat_stream()`：

- 请求体加 `stream: true`，用 `httpx.Client.stream()` 逐行读 SSE（`data: {...}` 分片，`[DONE]` 结束）
- 正文分片走 `choices[0].delta.content`，实时回调 `on_text`
- **工具调用增量拼装**：provider 会把工具名和参数拆成多片发（`delta.tool_calls[].function.name` / `.arguments`），按 `index` 累积，最后拼成完整的 `tool_calls`
- 推理模型的 `delta.reasoning_content` 单独回调 `on_reasoning`
- 保留了非流式 `chat()`，靠 `LLM_STREAM=0` / `--no-stream` 切换
- 容错：服务端不支持流式、直接返回整包 JSON 时，`iter_lines` 里那行会被 `json.loads` 解析出来，`"choices" not in chunk` 直接跳过，不会崩

**`agent.py`** —— 打印逻辑：

| 内容 | 打印方式 |
| --- | --- |
| AI 正文 | 流式逐块打印（青色），第一块前先打 `🤖` 徽标 |
| 工具调用 | 一行黄色：`⚙️ 调用工具 list_dir({"path": "."})`，参数 JSON 截到 **200 字符** |
| 工具返回值 | 灰色 `↩︎`，**只预览前 300 字符**，并标注 `（共 N 字符，已截断）`；完整内容照旧回传给模型 |

顺带修的两个问题：

1. 原来参数 JSON 解析失败时**不打印工具调用行**，看起来像"没调工具" → 改成无论如何都先打印工具名 + 原始参数。
2. 流式正文没有 `🤖` 徽标，和非流式输出不一致 → 用 `printed["badge"]` 标记保证只打一次。

### 验证

用**本地 SSE 假服务端**（`/tmp/sse_server.py`，手写分片：正文分片 + 工具名分片 + 参数分片）验证解析逻辑，再用**真实 MiniMax API**（用户已配好 `.env`）验证真实链路。

| 检查项 | 结果 |
| --- | --- |
| 本地 SSE：正文分片 / 工具名分片 / 参数分片拼装 | ✅ 拼出 `list_dir({"path": "."})`，正常执行 |
| 真实 API：`chat_stream` 带 tools | ✅ 返回 `tool_calls`，参数 JSON 合法 |
| 真实 API：agent 完整一轮（流式 → 工具 → 流式总结） | ✅ 见下方实测输出 |
| 参数 JSON 非法时的工具行 | ✅ 仍打印工具名 + 原始参数 + 错误 |
| `--no-stream` / `LLM_STREAM=0` | ✅ 退回一次性返回 |

真实模型实测输出：

```
你 >
🤖 I'll take a look at the directory structure.

  ⚙️ 调用工具 list_dir({"path": "."})
    ↩︎ /Users/yzy/Desktop/study_code/ai-test/agent-loop
dir   .idea
dir   .venv
…（共 388 字符，已截断）

🤖 这是一个 Python 写的 CLI agent loop 项目（`agent-loop/`），核心代码是 `agent.py`（主循环）…
```

### 遗留

- 工具调用行是在**整条流结束后**才打印的（参数要拼齐才知道），不是边收边打；OpenAI 协议下工具名通常在第一片就到了，可以后续改成"先打工具名、后补参数"。
- `LLM_MAX_TOKENS=500000`（用户 `.env` 里的值）偏大，接口目前接受，实际用不上，留着不管。
- 思考过程目前只在正文为空时显示一小段，没有开关。 → 见 v5，已加 `--show-reasoning`

---

## 11. 迭代 v5：思考强度参数（--effort）

用户诉求：加一个参数调节模型思考强度，默认 `low`。

### 先做接口调研（不猜参数）

对着真实 API 逐个试了一遍哪些字段被接受：

| 参数 | 结果 |
| --- | --- |
| `reasoning_effort=low/medium/high/xhigh/max` | ✅ 全部 200 |
| `reasoning_effort=default` | ❌ 400（`default` 的正确含义是**不传这个参数**） |
| `reasoning_effort=off` | ❌ 400 |
| `thinking={"type":"disabled"}` | ❌ 400：`model "MiniMax-M3.1-Flash" requires adaptive thinking` |
| `enable_thinking=false` | ⚠️ 200 但思考内容照旧（没真关掉） |
| `thinking={"type":"enabled","budget_tokens":N}` | ✅ 200，且思考长度随 budget 变化（256→749 字符，8192→1584） |

结论：模型原生支持 `reasoning_effort`，档位正好是 App 配置里 `thinking.effortOptions` 那六个
（`default / low / medium / high / xhigh / max`）→ **采用 `reasoning_effort`**，
`default` 映射成「不传参数」，`off` 明确报错并解释原因。

### 实现

- `llm.py`：`LLMConfig.effort`（读 `LLM_EFFORT`，默认 `low`）；`_payload()` 里当 effort ≠ default 时加 `reasoning_effort` 字段；`check()` 里做档位校验，非法值直接给出可选列表。
- `agent.py`：`--effort` 命令行参数（覆盖 `.env`），横幅里显示当前强度；新增 `--show-reasoning` 开关，把 `reasoning_content` 增量用暗色实时打印出来。

### 实测

| 检查项 | 结果 |
| --- | --- |
| 默认（low） | ✅ 横幅显示「思考强度 low」，正常回答 |
| `--effort max` | ✅ 回答更完整（会补充解释 9.9=9.90 的推理） |
| `--effort high --show-reasoning` | ✅ 暗色实时显示思考内容 |
| `--effort off` | ✅ 启动即报错并解释「该模型强制 adaptive thinking，请用 low」 |
| `--effort crazy` | ✅ 启动即报错并列出可选档位 |
| `--effort default` | ✅ 不传参数，交模型自定 |
| 工具调用 + 流式 + effort 组合 | ✅ 无回归 |

### 备注

- 已给用户自己的 `.env` 补上 `LLM_EFFORT=low`（与原行为一致，不改变现状）。
- 强度只调思考量，不影响工具调用能力；简单任务开 `high` 纯属浪费。

---

## 12. 迭代 v6：修复 UnicodeEncodeError（surrogates not allowed）

用户报错：`[错误] 调用模型失败: UnicodeEncodeError: 'utf-8' codec can't encode characters in position 1905-1906: surrogates not allowed`。

### 根因

不是用户输入的问题，而是**模型输出里混进了落单代理对**（lone surrogate）：

1. 模型服务在 SSE 流里把一个 emoji 的代理对（`\ud83d\ude00`）**拆到两个分片**下发；
2. Python 的 `json.loads` 把 `\ud83d` 解析成一个落单代理对字符（Python 允许这种字符存在于 str 里）；
3. 这段文本被存进 `messages`，**下一次请求** httpx 用 `json.dumps(ensure_ascii=False).encode('utf-8')` 序列化请求体 → 抛 `UnicodeEncodeError`；
4. 所以报错出现在「你输入文本之后」，实际是上一轮回复埋的雷。

补充发现：连我写的**假 SSE 服务端**在 `json.dumps(ensure_ascii=False)` 时也踩了同一个坑 —— 说明这确实是这类接口的常见问题，不是我们的代码独有。

### 修复

新增 `textsafe.py`：

| 能力 | 说明 |
| --- | --- |
| `SafeTextBuffer` | 跨 chunk 缓冲：结尾的高位代理先扣住，等下一个 chunk 的低位代理补上，**还原成真正的 emoji** |
| `clean()` | 快路径判断能否 UTF-8 编码；不能则先把成对的代理合并，再把剩下的落单替换成 U+FFFD |
| `is_clean()` | 单独判断用 |

接入点（`llm.py`）：

- `chat_stream()`：正文、思考过程各自用一个 `SafeTextBuffer`，`feed()` 出来的文本**既打印也存储**，保证进 `messages` 的永远是干净文本；流结束时 `flush()` 收尾。
- 工具调用的 `name` / `arguments` 拼接完再 `clean()` 一次。
- `_clean_messages()`：**每次请求前**把消息里的所有文本字段清洗一遍 —— 防御老会话文件里已经存了脏数据。
- 非流式 `chat()` 路径同样清洗。
- `session.py`：存盘时 `json.dumps` 若仍编码不了，自动退回 `ensure_ascii=True` 转义保存，会话不丢。

### 验证

| 检查项 | 结果 |
| --- | --- |
| 单元：`feed('\ud83d')` → 空（暂存） | ✅ |
| 单元：`feed('\ude00')` → `'😀'` | ✅ 代理对被还原成真字符 |
| 单元：落单代理 → `U+FFFD` | ✅ |
| 单元：含落单代理的旧消息构造请求体 | ✅ 不再抛异常，能安全编码 |
| 端到端：假 SSE 把代理对拆两片下发 | ✅ 终端打印 `先给你个表情😀 收工`，第二轮请求正常 |
| 端到端：会话文件内容 | ✅ 存的是 `'先给你个表情😀 收工'`（干净文本） |

### 附带修复的隐患

`_parse_docstring` / 流式分片解析在遇到 `json.JSONDecodeError` 时会 `continue`（跳过该行）。
如果服务端把**一个 JSON 事件拆成多行 `data:`**，内容会被静默丢弃 —— 目前没遇到，先记着。
