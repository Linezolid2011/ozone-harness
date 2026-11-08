# 变更记录

> 版本号**只有一个来源**：[`version.txt`](version.txt)（`harness.version()` 读它；
> 界面侧栏与 `run.cmd --version` 都从那里取）。发布前先改它，再在这里补一节 ——
> `tests/test_release.py` 会检查这一节里有没有出现那个版本号。

## 未发布

### 破坏性变化

* **逐条记忆改为 Kimi 原生 `data/vault/memory_instruction.md`（ADR-109）**：文件头
  `# Memory instructions`，每条为 `## <数字 id>` 加正文。`mem_create(content)`、
  `mem_edit(id, content)`；删除改为硬删除。分类、revision、时间戳、软删除与操作审计不再存在。
  **旧 SQLite `memories` / `memory_ops` 不读取、不迁移、不提供兼容层**；`data/memory.db`
  仅保留 Dream 整理游标。
* **同目录不同所有权**：设置 → Dream 不再显示 `memory_instruction.md`，设置 → 记忆负责
  独立管理。Dream 差异不把它算作整理产物，格式化 Dream 在成功与失败回滚时都保留其原始字节。
  原生 zip 先校验记忆格式，只有显式覆盖才同时替换现有记忆。
* **一致性与安全**：按路径锁 + 游标库写事务覆盖锁内重读和原子替换，避免 CLI/Web 多进程互盖；
  严格拒绝坏文件、重复 id 与标题碰撞正文。新 id 使用毫秒时间戳下界，保持 JavaScript 安全整数。
  设置 → 记忆、CLI、重置统计和提示词全部改为原生 `id + 正文` 形状。

### 修复

* **所有 Web 入口默认经过 venv 引导门（ADR-110）**：修复 `run.cmd --web` / `run.ps1 --web`
  未启用检查、导致安装页一闪而过的问题；仅显式 `--no-guided-venv` 可跳过。真实缺失环境下，
  两个入口均经 headless Chrome 验证会停在同意页直到用户选择。

## 1.1.0-rc1（2026-11-07）

跨层审查与清扫（ADR-105）、双击启动引导（ADR-106），以及入口维护规则更新（ADR-107）。
全部修复与新增路径都有离线回归。

### 新增

* **当前会话统计条（ADR-108）**：输入框下方固定显示轮数、平均输出速度、累计 token、缓存命中率
  与当前模型上下文百分比（含小进度条）；新建/分叉/切换会话按各自日志重建，每轮结束更新。
  原输入区接口、附件与多模态提示整体移除，统计条不显示 agent 步数。
* **双击启动页引导 venv**：自动选择空闲端口；先显示加载页并做目标解释器、真实 import 与
  `pip check` 完整性检查。缺失时由用户选择官方 PyPI 或自定义镜像，显示真实/不确定进度，
  支持暂停、继续、退出下载；拒绝与退出继续聊天但不持久记住，下次仍检查。
* **事务式安装与恢复**：最终路径创建 venv，旧环境备份、原子 journal、失败/取消回滚与下次启动恢复；
  pip 临时内容归本次事务所有。完整验证后激活依赖，再解锁聊天 API。
* **入口维护规则**：`run.cmd`、`run.ps1`、`start.cmd` 与类似脚本此后可按需求修改，
  不再要求与旧版本严格一致（ADR-107）。这项规则变化本身不改变当前入口行为。

### 修复 / 加固

* **压缩与流式**：截断摘要不再推进历史水位线；压缩开销参与单轮熔断；SSE 缺少终止事件、
  网页字节超限与压缩中断都如实收尾，不再伪装成完整成功。
* **事件与存储**：修复无换行日志追加丢新事件、并发 seq 倒序、工具结果重复补桩、旧压缩
  水位线、历史图片拒绝标记与 prompt 日志失真；SQLite 写入、Dream 游标、附件 GC、
  vault frontmatter / zip 导入与接口配置的竞态都补了事务或回归。
* **安全边界**：回环 Web 服务校验 Host 以挡 DNS 重绑定；删除/分叉使用真实会话锁；供应商
  跨源重定向不再携带 key；运行期会话 id 与短 key 掩码收紧；官方 OpenAI 请求流式 usage。
* **前端**：修复档案名字焦点、会话/提示词延迟响应、重置预览漏项、Tavily key 静默保存、
  Dream 双击、清空会话名后的旧标题、General 页语言 busy 状态与聊天模型显示名。
* **入口**：`run.ps1 --test` 与 `run.cmd --test` 对齐；`run.cmd` 未改。

## 1.0.1-rc1（2026-11-07）

对齐 Kimi 原生 Dream vault（ADR-104，先做 A 读/导入 + B 输出格式；C 仍留到下一步）。

### 新增

* **原生模式**：有 `sections.yaml` 的 vault 以 sections/entities 为 canonical，
  README / `index.md` / `sections/earlier_months.md` 由 harness 按清单生成；
  旧 vault（没有清单）保持模型写 README、token 预算与两层路径，**不自动迁移**。
* **原生路径**：`sections/archived_month/<YYYY-MM>/this_month.md`、
  `raw/source_<n>/<图片>`（含 `.meta.json`）、根级 `index/log/observations/
  memory_instruction` 可读/可显示。
* **安全导入**：`--vault-import <zip>` 逐条校验路径、符号链接、大小写撞名、
  大小/总量/条目数、图片内容嗅探；目标必须是空 vault（`--yes` 覆盖），
  全过才从一次性暂存目录搬入。新增 `--vault-rebuild`。
* **原生 frontmatter**：`name/tags/created/last_updated/relation/date/place`，
  列表套映射与嵌套映射原样 round-trip；`created`/`last_updated` 由 harness 盖章。
* **界面**：`![[raw/...]]` 图片（`/api/vault/raw`，后端二次校验）、
  `[[path|别名]]`、递归树、嵌套 frontmatter 可读显示、生成物只读并说明原因。
* **结构校验**：清单 section 缺失、README/index/earlier_months 过期、
  raw 引用缺失、孤儿实体（原生按链接图）；`--vault-rebuild` 后应清零。

### 修复 / 加固

* `--vault-init` **不再把已有内容的旧 vault 转成原生**（模式开关不会被顺手加文件）。
* `has_content()` 不再把生成的 `earlier_months.md` 当作真实档案，避免空 vault
  被注入一段只有占位文本的 README。
* 原生 README 的 `sections` 元信息也纳入过期检查；字符 cap 截断提示也算进上限。
* 非 zip / 坏 zip 导入会给明确错误，不再把异常直接抛给命令行。

### 测试 / 文档

* 新增原生 vault、zip 导入、Web raw/生成物、前端原生渲染的离线测试；
  全量 Python 与前端自检、绝对路径/发布守卫全绿（条数见 README）。
* ADR-104、`docs/10-dream-vault.md`、README 与 `docs/00-scope.md` 更新；
  C 明确未做：observations 生命周期、完整 log、`[^N]`/`source_ref`、
  raw 入库、归档轮转。

## 1.0.0-dev1（2026-10-18）

1.0.0-rc1基础上的开发版。功能：`time` 工具、分叉（分支）对话、时区可配置 + 「设置 → 常规」。

### 新增

* **`time` 工具**（ADR-097）：模型可主动查询当前时间。只读、无参数，返回
  `now` / `iso` / `timezone` / `offset` / `weekday` / `unix`；无条件注册给对话 agent，
  并出现在「设置 → 提示词」页。
* **分叉（分支）对话**（ADR-098）：在任意一轮把对话分成两个**互相独立**的对话 ——
  原对话保留全部上下文，新对话保留「分叉点及以前」的上下文。实现是**事件前缀复制**：
  附件连文件一起复制进新会话目录（改写 `file`），整理游标按 seq 继承
  （已整理进背景档案的历史不会被再整理一遍）。前端每条历史用户消息上挂「分叉」按钮。
* **时区可配置 + 「设置 → 常规」**（ADR-099）：`time` 工具的时区从写死 UTC+8 改成
  运行期可改（`data/settings.json`，整数小时 -12 ~ +14，默认 8）。「设置」侧栏把
  主题 / 语言并进一个新的「常规」页，主题 / 语言 / 时区三段一起；旧深链
  `#settings/theme` / `#settings/language` 自动折到「常规」。
* **单轮最大输出可调**（ADR-101）：「设置 → 常规」新增「单轮最大输出」，默认 **5000**；
  接受 `5k` / `1M` 写法；**`-1` = 不限**（OpenAI 兼容请求不发送 `max_tokens`，
  Anthropic 兼容协议用窗口感知的输出预留兜底）。值存 `data/settings.json`，
  `HARNESS_MAX_OUTPUT_TOKENS` 退化成种子；改完下一轮对话生效。
* **整理模型可调**（ADR-103）：Dream 页工具栏右侧新增「整理模型」选择器，
  默认「跟随默认」（= 全局默认接口 / 模型，与之前的行为完全一致），也可以显式指定。
  设置存在 `data/providers.json` 的 `dream` 里，**CLI 与 Web 共用**同一份；
  命令行上的 `--provider` / `--model` 是单次覆盖、不改设置。
  运行记录多一个 `generation` 字段，事后能回答"这次档案是哪个模型整理的"。
* **对齐 Kimi 原生 Dream vault（ADR-104，只做 A 读/导入 + B 输出格式）**：
  * `sections.yaml` 存在即原生模式：README / `index.md` / `sections/earlier_months.md`
    由 harness 从 sections 生成，模型只写 `sections/` 与 `entities/`；旧 vault 保持原样；
  * 路径放宽到 `sections/archived_month/<YYYY-MM>/this_month.md` 与
    `raw/source_<n>/<图片>`（+ `.meta.json`），并新增安全导入 `--vault-import`；
  * frontmatter 支持原生 `name/tags/created/last_updated/relation/date/place`，
    嵌套映射与未知键原样 round-trip；`created`/`last_updated` 由 harness 盖章；
  * README 的 `chars` 用 UTF-8 字节、注入上限用 `inject_char_cap` 字符；
  * 界面：`![[raw/...]]` 图片、`[[path|别名]]`、嵌套树、生成物只读；
  * **C 未做**：observations 生命周期、完整 log、`[^N]`/`source_ref`、raw 入库、归档轮转。

### 修复

* **上下文预算改成"整条请求"**（ADR-100）：以前只数历史，工具定义 / system /
  记忆 / README / 摘要 / 思维链 / 输出预留都在窗外；32k 窗口下"看起来没超"的请求
  会被 API 直接 400。现在 `history_budget` 先扣掉真实固定开销，发请求前再核一次
  "输入 + 输出"：装不下先压低本次输出上限，连最小输出都留不出就**本地中止**
  （`stop_reason=context_limit`，不花钱、也不让 API 报 `context window exceed`）。
  摘要调用同样按窗口检查；新增 `HARNESS_CONTEXT_SAFETY_TOKENS`（默认 1024）。
* **`context_limit` 那条提示的数字算错了**：`-1`（不限输出）时旧写法把 `-1` 当成
  数字加进 `needed`，得出 `estimated - 1` —— 看起来正常，实际少报了一整块输出预留。
  现在统一用 `output_reserve()`（`-1` 时是窗口感知的那块预留）。
  同时把"最小输出 64 token"从 `loop.py` 的常量挪进 `config.py`：
  `Settings.min_output_tokens`（`HARNESS_MIN_OUTPUT_TOKENS`，默认 64，写坏夹到 >=1）。
* **Anthropic 用量少算缓存**（ADR-102）：`input_tokens` 只是未命中的输入，
  `cache_read/cache_creation` 是另外两列。以前 `prompt_tokens` / `total_tokens`
  都把缓存漏掉（实测 12k 窗口下每轮少 896~3712 token），用量页与单轮 token 熔断
  都偏松。现在与 OpenAI 同口径：`prompt_tokens` 含缓存，`cached_tokens` 是其中的
  命中部分。
* **只调工具、没有正文/思考的那一步，工具卡片横跨整个页面**：会话视图会把这种
  assistant 消息省略掉（`build_session_view` 只看 content / reasoning），卡片因此没有
  assistant 锚点、被直接挂在 `.messages` 上（`.msg` 的 820px 居中约束不生效）。
  现在 `addToolChip` 找不到锚点时自己建一个 `.msg.assistant` 容器，宽度与其它卡片一致。

### 文档

* ADR 补到 **104**（ADR-097 `time` 工具、ADR-098 分叉对话、ADR-099 时区可配置 + 常规分组、
  ADR-100 整条请求的上下文预算、ADR-101 单轮最大输出可调、ADR-102 Anthropic 缓存用量、
  ADR-103 整理模型可调、ADR-104 对齐 Kimi 原生 Dream vault）。

## 1.0.0-rc1（2026-10-17）

第一个"可以交给同学用"的版本。单用户、跑在自己电脑上、用每个人自己的 API key。

### 新增

* **产品定名 O₃ᶻᵒⁿᵉ**（ADR-094）：徽标只放 **O₃**，名称是 **O₃ᶻᵒⁿᵉ**；名字只在
  `harness/__init__.py` 定义一处，ASCII 场合（HTTP 头、`.cmd`）退化成 `O3zone`。
* **双击启动**：`start.cmd` —— 不用打参数，起来就开浏览器；再双击一次不会起第二个实例
  （发现端口在听就只开浏览器）。`run.cmd` 一个字节没动，参数照样全在。
* **版本号从 `docs/version.txt` 读**：侧栏第二行显示它（原来显示的是模型名/「未配置接口」），
  CLI 有 `--version`。三个读端同一个来源。
* **接口页的模型表格**（ADR-092）：一行一个模型 = 模型 ID / 上下文上限 / 显示名，
  行尾删除、底部添加；**显示名**只影响下拉框怎么显示，不参与决策。
* **上下文可以写 `128k` / `1M`**（ADR-093）：k = 1000、M = 1000k（不是 1024）；
  存储只有一种形态（整数）。
* **提示词可编辑**（ADR-088）：`设置 → 提示词` 里 22 条提示词按语言分开改、逐条可复原；
  工具定义仍不可编辑（那是代码里的契约）。

### 修复（发布前的一轮清扫，19 个，ADR-095）

* **流开始之后出错，用户什么也看不到**：`_fail` 在已经发出 `200 text/event-stream`
  之后又写一个 500，前端只认 `event:` 帧 → 回答被截断且没有错误。现在改发 `notice` 帧。
* **接口地址少写 `https://`** → 以前是 500 + 一句 Python 原文（对话路径上整轮中断）；
  现在保存时就 400 并说清怎么写。
* **四个 JSON store 改成原子写**（临时文件 + `os.replace`）：半截写入不再等于
  "所有接口和 key 都不见了"。
* **「格式化 dream」失败时不再删档案**：把搬走的东西搬回去再报错，游标不动。
* 工具调用被重复补桩（同一个 `tool_call_id` 两条结果）、孤立代理字符崩掉整轮、
  SSE 里出现非对象 JSON、`/api/tools/test` 的 `count` 非数字、vault 保存与整理的 TOCTOU、
  两个 `EventLog` 抢同一个 `seq` —— 逐条见 ADR-095。
* 前端：会话切换竞态（旧响应画进新会话）、恢复出厂设置后不清屏、上传响应覆盖
  `sessionId`、SSE 逐帧隔离与缺省值、用量页容错、三处漏翻、`#settings-meta` 串页、
  顶栏标题被当前消息顶掉、两处 `innerHTML` 加固。

### 文档

* 新增 `docs/AGENTS.md`（给下一个 agent 的说明书）、这份 changelog、
  `docs/version.txt`；ADR 补到 **096**。
* **截图全部重拍**：19 张，都是当前界面与当前名字；新增
  `scripts/ui_check/seed_doc_fixtures.py` 与一条 Chrome 命令，让这批图能一条命令重来
  （截图会腐烂，但它至少不再是"只能手工拍"）。

## 0.x 开发期（2026-10-04 ~ 10-16）

按里程碑推进，每一步都有验收标准（见 [`00-scope.md`](00-scope.md)）与决策记录
（见 [`01-decisions.md`](01-decisions.md)）：

| 日期 | 里程碑 | 一句话 |
|---|---|---|
| 10-04 ~ 10-06 | M0 + M1 | 骨架 + Agent 循环 + 记忆三件套（零依赖，全标准库） |
| 10-09 | M4 → 后被推翻 | dream 起初是"在记忆表上做手术"，越界了 |
| 10-11 | M5 | 对标 Kimi 的 Web 界面（零依赖 `http.server` + SSE） |
| 10-12 ~ 10-13 | M6 | 多供应商 + 设置面板 + 自动 compact + 恢复出厂设置 |
| 10-15 | M7 | **dream 重装**：会话记录 → 背景档案（vault），两套存储、两种可信度 |
| 10-12 ~ 10-15 | M8 / M9 | 图片附件（内容寻址）+ 多模态开关与本地 OCR 降级 |
| 10-15 | M10 | 联网工具（实测选后端、SSRF 防护、提示注入两层防御） |
| 10-16 | M11 | 界面中英双语（提示词跟着语言走，换语言不刷新页面） |
| 10-16 | — | 「设置 → 提示词」页：把发给模型的东西摊开给人看，并且可改 |

期间删掉的东西也记在决策记录里，最典型的是 **M2 的记忆检索与别名机制**
（ADR-058：那是我们自己造出来的需求，后来连根删掉，改为全量注入 + 一条硬上限）。
