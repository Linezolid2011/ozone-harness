# 13 · 设置 → 提示词（Prompts）

## 0. 一句话

在设置侧栏加一栏「提示词」：**把当前语言下真正发给模型的东西摊开给你看** ——
全部提示词（含后台整理用的那两条）与全部工具定义（名字、描述、参数 schema）。

## 1. 这个页面是什么、不是什么

**是**：一份**照实**的清单 —— 提示词直接读当前语言的 `harness/prompts/` 模块，
工具定义**用真的工具类构造一遍**再取字段。所以页面上写的**就是**模型收到的那一份。

**不是**：

* 不是"这一轮实际发出去的消息" —— 记忆条目、README 内容、历史都由 harness 每轮
  按当前状态拼进去。要看**完整的一次请求**，用 `run.cmd --show-prompt`，或在界面上
  重放那条会话。这条差别写进了页面首部的说明文案（`prompts.intro`）。
* 不是编辑器（**现在是只读的**）。每一项都带稳定 `id`，下一步要改成可编辑时按 id 定位。
* 不受「联网」「多模态」等**会话级**开关影响：这一页是全局视图，
  所以 `web_search` 的状态标成 `session`，而不是假装知道某个会话开没开。

**语言**：整页跟随界面语言（ADR-085）。`language` 字段是当前值；
切到英文时，页面上的一切（含提示词原文与工具描述）都来自英文那一套。

## 2. 接口

```
GET /api/prompts          → 200 JSON（不需要配置任何接口；不调模型、不联网、不写盘）
```

### 2.1 形状

> `summary` 里那几个数字**随当前配置变**，不是常量：上面这份是"联网开着、档案还空着"
> 那种配置（`tools_on = 11`）。档案建好之后那三个档案工具会变成 on（→ 14），
> 联网关掉则 `web_open_url` 与三条 web 提示词一起变 off。界面必须**读服务端的数字**，
> 不许写死。

```jsonc
{
  "language": "zh",
  "summary": { "prompts": 22, "prompt_groups": 5, "tools": 15, "tools_on": 11 },
  "groups": [
    {
      "id": "system",                 // 稳定分组 id：system / tail / compact / render / dream
      "title": "对话的系统提示",        // 已按当前语言取好
      "note": "一轮对话开头那条 system 消息。它是骨架：……",
      "items": [
        {
          "id": "SYSTEM_PROMPT",      // **稳定契约** = prompts 模块里的常量名
          "title": "SYSTEM_PROMPT",   // 现在与 id 相同（标识符不翻译）
          "state": "on",              // on | off | session，见 §2.2
          "condition": "",            // 为什么 off / 什么时候出现；已译好，on 且无补充时是空串
          "when": "一轮对话的第一条 system 消息，每次请求都发",
          "text": "……完整的提示词原文……",
          "chars": 4211,
          "placeholders": ["memory_text", "vault_text", "web_tools_text"]
        }
      ]
    }
  ],
  "tools": [
    {
      "id": "chat",                   // 稳定：chat / dream
      "title": "对话 agent 的工具",
      "note": "模型在对话里能调用的全部工具。标着「未注册」的现在不会发给接口 —— ……",
      "items": [
        {
          "id": "web_search",         // 稳定契约 = 工具名
          "name": "web_search",       // 模型看到的函数名
          "state": "session",
          "condition": "会话级开关：顶栏的「联网搜索」，默认关。……",
          "description": "用关键词搜索引擎查资料，返回若干条结果的标题、链接与摘要。……",
          "parameters": { "type": "object", "properties": { }, "required": ["query"] },
          "required": ["query"],
          "properties": 2
        }
      ]
    }
  ]
}
```

### 2.2 `state` 的确切含义
**它只回答一件事：按当前配置，这条现在会不会被用上。**

| 值 | 含义 | 界面上怎么说 |
|---|---|---|
| `on` | 现在会被拼进去 / 会被注册 | 「生效」 |
| `off` | 现在不会被用上，`condition` 说了为什么 | 「未注册」 |
| `session` | 由**会话级**开关决定（现在只有 `web_search`），`condition` 说明是哪个开关 | 「会话级」 |

"按情形才出现"的那些（`TAIL_OMITTED_NOTICE`、`COMPACT_*`、`RENDER_*`、`DREAM_*`）
**一律是 `on`**，什么时候出现写在 `when` / `condition` 里 ——
不要试图用 `state` 表达"取决于这一轮发生了什么"，那会让它变成一句需要解释的话。

### 2.3 覆盖范围（有测试钉着）

* 提示词：`harness/prompts/{zh,en}.py` 里**全部**公开常量，`LANG` 除外（22 条）；
* 工具：**全部 16 个** —— 对话侧 9 个（`mem_create`/`mem_edit`/`mem_del`、`time`、
  `dream_list`/`dream_read`/`dream_search`、`web_search`/`web_open_url`）
  ＋ 整理侧 7 个。**即使现在不会注册的也列出来**（否则你没法回答"它到底有没有这个工具"）。

## 3. 内容从哪来（供排查用，前端不需要关心）

`harness/prompt_view.py::describe_prompts(settings)`，见模块头。三条要点：

1. 分组表 `PROMPT_GROUPS` 与 prompts 模块**必须严格互补**，对不上就抛异常
   —— 新增提示词忘了归组会当场炸，而不是页面上静默少一条；
2. 工具定义用**真工具类**构造（文案在实例化时按当前语言取）；
3. **零落盘**：记忆三件套用一个 `:memory:` 的 store 当替身，档案用只读的
   `VaultStore`，联网配置走只读的 `ToolStore`。所以"点开这一页"不会在你 `data/` 里
   种出 `memory.db`。这条有测试守着。

## 4. 文案分工（契约，别搞混）

| 谁 | 住哪 | 管什么 |
|---|---|---|
| **服务端** | `harness/ui_text_{zh,en}.py` 的 `ui.prompts.*` | 分组标题与说明、每条提示词的 `when`、每条工具的状态说明 `condition` |
| **前端** | `web/i18n.js` | 这一页自己的**外壳**：侧栏项、页首说明、状态徽标、字符数、按钮 |

前端**不要**再翻译服务端给的字符串 —— 它们已经按当前语言取好了（这条与 vault 页
的 `problems` 一致）。

前端要加的 key（**下面是必需的那几个**；实现里另有分节标题、参数表表头、
统计行等同前缀的 key，以 `web/i18n.js` 的 `prompts.*` 为准 ——
两种语言的 key 集合一致由 `tests/test_i18n_ui.py` 与前端自检双重保证）：

| key | 用途 |
|---|---|
| `settings.nav.prompts` | 侧栏项（放在 `工具` 之后、`主题` 之前） |
| `prompts.intro` | 页首说明：当前语言下真正发给模型的东西；记忆/README/历史每轮按状态拼进去，要看完整请求用 `--show-prompt` 或重放。**这句里有 `<strong>` / `<code>`，所以必须走 `innerHTML`** —— 用 `textContent` 会把标签当字面量打出来（实现时真踩到过，靠截图抓到） |
| `prompts.summary` | 摘要行，占位符 `{prompts}` `{tools}` |
| `prompts.on` | 单独一句"其中 N 个现在生效"，占位符 `{on}`，取服务端的 `summary.tools_on` |
| `prompts.tally_on` / `.tally_off` / `.tally_session` | 按三态各数一遍（占位符 `{n}`）。**文案里不带分隔符** —— 分组里"为 0 的那种不显示"，而分隔符写在模板里就没法干净地省掉中间/开头/末尾那一个；`· ` 由 app.js 在 `filter(非空).join(' · ')` 里拼（页首那行的输出因此与以前逐字相同） |
| `prompts.state.on` / `.off` / `.session` | 状态徽标：生效 / 未注册 / 会话级 |
| `prompts.chars` | 占位符 `{n}` |
| `prompts.placeholders` | 占位符 `{list}` |
| `prompts.params` | "参数（JSON Schema）" |
| `prompts.required_yes` | 参数表某一格里那句"必填"（**不是**"必填：{list}"，那种整行写法实现里没用到，已删） |
| `prompts.copy` / `prompts.copied` / `prompts.copy_failed` | 复制按钮 / 已复制 / 失败时的人话 |
| `prompts.loading` / `prompts.load_error` | 加载中 / 加载失败（`{error}`） |
| `prompts.none` | 空态（理论上不会出现，但要有） |

## 5. 前端要求

1. 侧栏新增一项 + 一个 `settings-pane`（照现有 8 个子页的写法：
   `PANES` 数组、`showSettings()` 分支、`#pane-prompts` 里的 `#prompts-body`）；
2. **分组默认折叠**（用户明确要求：**就算每条提示词的原文都折着，光标题与说明加起来
   也把页面拉到八千多像素**）。每个分组 —— 提示词的 5 组和工具的 2 组都一样 ——
   是一个 `<details class="prompts-group">`，**默认不带 `open`**：

   * `summary` 里放：分组标题 + 条目数（`prompts.group_count`）+
     **该分组的状态统计**（生效 x · 未注册 y · 会话级 z，为 0 的那几种不显示，
     复用 `prompts.tally_on/off/session`）。页首那行总统计仍然保留 ——
     全折叠时它是唯一一眼能看全的东西；
   * 分组的说明句（`note`）与全部条目都放在折叠体里（说明句是"读的时候想看"的东西，
     不是"扫一眼"的东西）；
   * 分组之间**互不影响**（不要手风琴行为）；不做"全部展开"按钮
     （那等于把长度问题又请回来）；
   * 用原生 `<details>/<summary>`（键盘可达、零 JS 状态）；
3. **每条提示词**渲染成：标题（= id）＋ 状态徽标 ＋ `when` ＋ `condition`（非空时）
   ＋ 占位符提示（非空时）＋ 字符数 ＋ **可折叠的原文 `<pre>`**
   （原文可能几千字，默认折叠；`white-space: pre-wrap`）；
4. **每条工具**渲染成：名字（等宽字体）＋ 状态徽标 ＋ `condition` ＋ 描述
   ＋ 参数表（属性名 / 类型 / 是否必填 / 说明）＋ 可折叠的原始 `parameters` JSON；
5. **DOM 上留好钩子**（下一步要做可编辑，靠它定位）：
   每条提示词的外层元素带 `data-prompt-id="<id>"`，每条工具带 `data-tool-id="<name>"`；
   **折叠不影响它们的数量**（22 / 16 个元素始终在 DOM 里 —— 断言数的是元素，不是可见性）；
6. 复制按钮：`navigator.clipboard` 在非安全上下文/受限环境可能不可用 ——
   失败必须**说一句人话**，不能静默；
7. 切语言时这一页要能**原地重渲染**（ADR-085 的四号岔路口：不刷新页面）——
   注意这一页的数据**跟着语言走**（提示词原文与服务端文案都是按当前语言取的），
   所以切语言必须**重新拉一次** `/api/prompts`，不能拿旧数据重画。
   实现走的是 `rerenderForLanguage() → showSettings(当前子页) → loadPrompts()`，
   与其它子页同一条路（没有单独的 `renderLanguagePane` 钩子）。

## 6. 不变量（测试在钉）

* Python：`tests/test_prompt_view.py` —— 22 条提示词与 16 个工具**一个不少**、
  分组表与 prompts 模块严格互补、`state` 三值合法、**全新数据目录上零落盘**、
  `en` 模式下所有服务端文案里没有汉字、接口在**没有配置任何接口**时也能 200
  （`tests/test_web.py`）；
* i18n：两个 catalog 的 key 集合一致（`tests/test_i18n.py`，自动覆盖新加的 key）；
* 前端：`scripts/ui_check/dom-stub.test.mjs`（**437 条断言**）与
  `scripts/ui_check/e2e-live.mjs`（**78 条断言**，真 `/api/prompts` → 真 `app.js`）
  —— 覆盖：侧栏项与顺序、22 条提示词与 16 个工具全画出来、`data-prompt-id` /
  `data-tool-id` 与真 id 集合逐一相同、三种徽标文案、原文默认折叠且与真响应**逐字相同**、
  **7 个分组默认全折**、参数表按真 schema 逐行、切 en 整页变英文且不刷新、
  接口 500 / 缺字段 / 空数组都不崩。

## 7. 下一步：已经做了

**提示词那一半已经可编辑了** —— 见 `docs/14-editable-prompts.md`（ADR-088）：
每条可改、每条有各自的复原按钮、**按语言分家**（改中文不影响英文）、
恢复出厂设置把两门一起复原。这一页里留好的稳定 `id` 与 `data-*` 钩子
正是那一步没有重写前端的原因（契约 §7 当年的预判是对的）。

**工具定义仍然不可编辑**：它是代码里的契约（还牵着 i18n 的成对守卫），
要改得另开一轮、另写一份契约。
