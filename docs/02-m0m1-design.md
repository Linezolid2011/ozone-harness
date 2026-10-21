# M0 + M1 设计说明

## 一轮对话里发生了什么

以"记住我喜欢美式咖啡"为例：

```
1. CLI 收到输入 → AgentLoop.run(text)
2. EventLog 追加 user_message
3. EventLog.conversation() 重放出完整历史（含这次输入）
4. 取一次记忆快照 memory.render_for_prompt()      ← 整轮固定（ADR-009）
5. 组装消息： [system: 规则 + 记忆 + 日期] + 裁剪后的历史
6. EventLog 追加 prompt_built（含 messages 全文）  ← 复盘的关键
7. provider.stream() 流式返回：
       ReasoningDelta → 转成 emit("reasoning") → CLI 灰字实时打印
       TextDelta      → emit("text")           → CLI 正文实时打印
       ToolCallDelta  → 拼装（不转发给 UI，太吵）
       Finished       → 拿到 finish_reason 与 usage
8. StreamAssembler 拼成一条完整 assistant 消息 → 写日志
9. 若 message.tool_calls 非空：
       逐个 registry.call(name, arguments)
       → 结果转成 role="tool" 消息追加进 history
       → 写日志、emit 给 CLI
       → 回到第 5 步（注意：记忆快照不重取）
   否则本轮结束
10. EventLog 追加 turn_end（步数 / 停止原因 / 耗时 / 用量）
```

关键点：**第 4 步只做一次**，第 9 步回到第 5 步时复用同一份记忆快照。

---

## 核心数据结构

### Message（`harness/llm_types.py`）

```python
Message(role, content, tool_calls, tool_call_id, name, reasoning)
```

- `to_api()` 负责序列化成厂商格式，是**唯一**的转换出口；
- `reasoning` 在 `role == "assistant"` 时会作为 `reasoning_content` 下发（ADR-008）；
- `to_record()` / `from_record()` 负责落盘与重放，保证"日志里的历史和送给模型的历史一致"。

### 流式事件

| 事件 | 含义 |
|---|---|
| `ReasoningDelta` | 思维链碎片 |
| `TextDelta` | 正文碎片 |
| `ToolCallDelta` | 工具调用碎片（`index` 聚合、`id`/`name` 只在首片、`arguments` 需拼接） |
| `Finished` | `finish_reason` 与 `usage`（可能分两块到达，拼装器按"非空才覆盖"处理） |

`StreamAssembler` 把碎片拼成完整消息。**测试用的 FakeProvider 与真实 Provider
走同一个拼装器**，所以流式拼装逻辑有真实覆盖。

### Usage

`prompt / completion / total / reasoning / cached / cache_miss`。
`cached` 与 `cache_miss` 来自 DeepSeek 的 `prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens` —— 用来判断我们的前缀设计是否真的省了钱。

---

## 存储

### 会话：`data/sessions/<session-id>.jsonl`

事件类型：`session_start` / `user_message` / `prompt_built` / `assistant_message` /
`tool_call` / `notice` / `error` / `turn_end`。每行一个 JSON，`seq` 单调递增。

`conversation()` 把 `user_message` / `assistant_message` / `tool_call`
重放成 `list[Message]`，这就是模型看到的历史。

### 记忆：`data/memory.db`

```sql
memories(id, content, category, status, revision,
         created_at, updated_at, deleted_at, session_id, turn)
memory_ops(id, memory_id, op, before, after, session_id, turn, at)
```

`status ∈ {active, deleted}`；只有 `active` 会进入上下文。

---

## 工具接口

```python
class Tool(Protocol):
    name: str
    description: str          # 提示词工程的一部分，直接决定记忆质量
    parameters: dict          # JSON Schema
    def run(self, args: dict) -> ToolResult
```

`ToolRegistry.call(name, arguments_json)` 的容错链：
未知工具 → JSON 不合法 → 不是对象 → 工具抛异常 → 全部转成 `ok=false` 的结果。

`specs()` 按名字排序输出。**这不是洁癖**：tools 数组每轮都在请求里，
顺序不稳定就等于每轮前缀都变，缓存全废。

### 新增一个工具

```python
class MyTool:
    name = "my_tool"
    description = "什么时候该用它、什么时候不该用"
    parameters = {"type": "object", "properties": {...}, "required": [...]}
    def run(self, args): return ToolResult(ok=True, payload={...}, display="一行简报")

registry.register(MyTool(...))
```

不需要改循环、上下文或 CLI —— 它们只认 `ToolResult`。

---

## 配置（`.env`）

> ⚠ **这一节描述的是 M0/M1 当时的样子，有两条已经不成立**：
> `DEEPSEEK_API_KEY` / `HARNESS_MODEL` / `HARNESS_REASONING_EFFORT` 已不再从 `.env` 读，
> `HARNESS_PRICE_*` 已随计费功能删除。
> 现在 `.env` 只管**行为**（预算、熔断、compact 阈值、数据目录），
> 不管**身份** —— 接口与 key 全部在 Web 界面的「设置 → 接口」里，
> 见 `docs/07-m6-multi-provider.md` 与 ADR-036。

| 键 | 默认 | 说明 |
|---|---|---|
| ~~`DEEPSEEK_API_KEY`~~ | — | 已废除：key 只存在 `data/providers.json` |
| ~~`HARNESS_MODEL`~~ | — | 已废除：模型跟着接口档案走 |
| ~~`HARNESS_REASONING_EFFORT`~~ | — | 已废除：档位由界面选择（跟着会话存） |
| `HARNESS_MAX_OUTPUT_TOKENS` | 2048 | 单次回复上限 |
| `HARNESS_MAX_STEPS` | 8 | 单轮最多几步工具调用 |
| `HARNESS_MAX_TOKENS_PER_TURN` | 60000 | 单轮累计 token 上限 |
| `HARNESS_MAX_WALL_SECONDS` | 180 | 单轮墙钟上限 |
| `HARNESS_CONTEXT_TOKEN_BUDGET` | 24000 | 历史裁剪预算（也是 compact 的成本上限） |
| `HARNESS_MAX_MEMORIES` | 200 | 记忆条数上限 |
| `HARNESS_MAX_MEMORY_CHARS` | 400 | 单条记忆长度上限 |
| ~~`HARNESS_PRICE_*_PER_M`~~ | — | 已删除：多供应商下单价表无法通用 |

---

## 验收命令

```bat
:: 1. 离线测试（零成本、零网络）
python -X utf8 -m unittest discover -s tests -t . -v

:: 2. 让模型自己写记忆
run.cmd --once "记住我喜欢美式咖啡"

:: 3. 看落盘结果
run.cmd --memories

:: 4. 换新会话，验证记忆注入（关键验收点）
run.cmd --once "我喜欢喝什么？"

:: 5. 更正与遗忘
run.cmd --once "更正一下，我更喜欢拿铁"
run.cmd --once "忘掉我的咖啡偏好"

:: 6. 复盘：看当时真正发出的 prompt
run.cmd --sessions
run.cmd --replay <session-id>

:: 7. 用量与缓存命中
run.cmd --usage
```

---

## 下一步的接入点

- **M2 记忆召回**：只需替换 `MemoryStore.render_for_prompt` 的实现，
  循环与上下文组装完全不用动。
- **M4 Dream**：`memory_ops` 已经记录了每次增删改的 before/after，
  巩固任务可以直接读它；建议做成独立进程，不占对话延迟。
- **M5 Web UI**：`AgentLoop` 通过 `emit(kind, **data)` 回调驱动 UI，
  换成 SSE 推给浏览器即可，循环本身不用改。
- **M6 多模型**：实现一个新的 `LLMProvider`，改 `.env` 里的 `HARNESS_MODEL` 与 base_url。
