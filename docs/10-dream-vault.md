# M7 Dream vault 设计说明（地基 + 整理流水线）

> 这份文档分两半：**地基**（档案长什么样、路径怎么保安全、README 怎么注入、
> 模型怎么按需进来读）与**整理流水线**（档案是怎么从会话记录里长出来的）。
> 前者在 §目录结构 ~ §路径安全，后者从 §整理流水线 开始。

## 一句话

给模型一个**可以自己走进去找**的长期档案层：后台整理的 vault 与它投影出的 README，
和"用户明确要求记住"的记忆条目是**两套独立的系统**，可信度也分两层。

## 为什么原来的 dream 是错的

原来的 M4 dream 做的是**对 `memories` 表的一次外科手术**（合并、改写、补别名、
删除、提炼洞察）。它和 Kimi.com 的 dream memory 不是同一个东西，对照如下：

| Kimi 的描述 | 原来的实现 |
|---|---|
| 输入 = 白天各会话新产生的**原始记录** | 只读 `memories` 表，**看不到任何对话** |
| 产物 = vault（prose sections + 实体文件 + 图邻接）与 README 投影 | **没有任何产物** |
| 注入 = README 每轮 + 需要时自己进 vault 找 | 只有一条注入通道 |
| 按需取用 = 模型调工具读 vault 文件 | 对话 agent 没有任何读取工具 |
| 两个系统分开，可信度 instructions > dream | **混成一个**：dream 能改写、删除用户明说的记忆 |

最后一行不是"缺功能"，是**越界**：用户说"记住我住上海"（契约），
夜里被整理成一条更泛的结论、甚至被删掉，用户会认为系统在骗他。
详见 `docs/01-decisions.md` ADR-053。

## 两个系统

| | memory instructions | dream vault |
|---|---|---|
| 谁写 | **只有**对话模型（`mem_create/edit/del`） | dream 整理流程；对话模型也可用受约束工具直接维护 |
| 存在哪 | `data/vault/memory_instruction.md`（原生 Markdown） | `data/vault/**.md` |
| 注什么 | 条目本身（**全部**，有硬上限） | **只有 README** |
| 怎么取更多 | 不需要：都在上下文里 | 模型自己调 `dream_list / dream_read / dream_search` |
| 可信度 | 最高（用户明说） | 次之（压缩推断，可能失真） |

提示词里把这条分层**写出来**了（见 `harness/context.py` 的 system 提示），工具层面也成立：
`memory_instruction.md` 始终由 MemoryStore 独占写。ADR-111 起对话模型可以写**背景档案**，
但这没有给它独立记忆或通用文件系统权限，具体禁区见下文。

### 同目录不等于同所有权（ADR-109）

逐条记忆现在也放在 vault 根下，但它是独立系统，不是 Dream 的可重建投影：

```md
# Memory instructions

## 1

用户喜欢美式咖啡。
```

* 文件只有 `## <正整数 id>` 与正文；没有分类、版本、时间戳或软删除状态。删除是硬删除。
* 旧 SQLite `memories` / `memory_ops` 不读取、不迁移；`data/memory.db` 仅用于 Dream 游标。
* Dream 档案树不显示它，`VaultStore` 的保存/删除入口一律拒绝它；设置 → 记忆才是管理窗口。
* Dream 格式化保留它的原始字节，Dream 差异不把它算成整理产物。
* 原生 zip 导入先跑同一套格式校验；`overwrite=True` 会同时替换现有记忆，必须显式选择。

## 目录结构

**原生模式（ADR-104，有 `sections.yaml`）**：

```
data/vault/
  README.md                ← 生成物：sections 的投影，每轮注入
  index.md                 ← 生成物：[[path|title]] 索引
  sections.yaml            ← 清单：inject_char_cap + 有序 section
  sections/personal_context.md
  sections/work_context.md
  sections/this_month.md
  sections/earlier_months.md   ← 生成物：由归档月份拼出
  sections/taste.md
  sections/memory_tips.md
  sections/archived_month/2026-09/this_month.md
  entities/project_photon.md
  raw/source_11/icais2026_pic1.jpg
  raw/source_11/icais2026_pic1.jpg.meta.json
  log.md / observations.md  ← 旁路导出/日志，可读
  memory_instruction.md   ← 独立逐条记忆；由「设置 → 记忆」管理，不在 Dream 树展示
```

**旧模式（没有 `sections.yaml`）**：仍然是 `README.md` + `sections/` + `entities/`，
README 由模型写。整理器与导入器默认沿用"目录名小写、深度恰好为 2"的路径合同；
原生模式额外允许 `sections/archived_month/<YYYY-MM>/this_month.md` 与
`raw/source_<数字>/<图片>`（或 `.meta.json`）。ADR-111 的**会话编辑入口**另可用
`allow_nested=True` 在 sections/entities、自定义目录与根级建非保护 Markdown，读/检索/索引
都会识别它们；这个扩权不改变整理器与导入器的默认写法。`sections.yaml` 存在与否仍是模式开关，
**不会**因为一次整理或会话编辑把旧 vault 迁成原生。

frontmatter 是我们自己实现的**一个刻意做小的 YAML 子集**（标量 + 字符串列表，
原生还用到**列表套映射**与**嵌套映射**），不引入 PyYAML —— frontmatter 的内容有一半是
模型生成的，用一个规范里全是边角（锚点、标签、类型推断、`no` 被解析成 False……）的
解析器去接它，等于把风险请进核心路径。不认识的键（包括 `date` 这种嵌套映射）
**原样保留**，用户手改时加的 `pinned: true` 不会被吃掉。原生的已知键是
`name/title/kind/relation/status/created/last_updated/place` + `tags/aliases/sources`；
`created`/`last_updated` 由 harness 盖章。

## 图邻接不建数据库

实体正文里写 `[[小林]]` 这样的链接。读一份档案时，`neighborhood()` 机械地扫一遍全库，
给出三样东西：

* `links` —— 它指向谁；
* `backlinks` —— 谁指向它（判据是"**对方的 links** 与**我的名字**相交"，不是比名字）；
* `related` —— 和它共现的节点（共享链接目标）。

vault 规模有上限，所以这个扫描是有界的。好处是**永远不会和文件内容脱节**：
用户手改一个文件，邻域立刻正确。代价是 O(文件数)，可接受。

## README：原生生成 / 旧模式模型写，上限都由 harness 强制

README **每轮都进上下文**。所以"模型把它写长了"的代价不是一次性的，
而是此后每一轮都付 —— 而模型只在写它的那一次看到代价。

**原生模式（ADR-104）**：README 是 `sections.yaml` 的投影，由 `rebuild_generated()`
机械生成；模型**不写** README，也不写 `index.md` / `sections/earlier_months.md`。
生成时按清单顺序把 section 正文拼成 `## title` 块，`earlier_months` 由
`sections/archived_month/<YYYY-MM>/this_month.md` 拼出；README 的 `chars` 是**正文字节数**，
`inject_char_cap`（原生实测 6000）是**字符**上限，`readme_text()` 按段落截断并补提示。
**旧模式**仍由模型用 `readme_write` 写，harness 按
`HARNESS_VAULT_README_TOKEN_BUDGET`（默认 1200）截断，切在段落边界。

两种模式的截断提示本身都**算进预算** —— 否则会出现"截断了但总量还是超"，
测试直接断言 `len(截断结果) <= cap` / `estimate_tokens(截断结果) <= 预算`。

* 原生模式的机械校验因此从"模型有没有写对 README"变成"生成物是否与 sections 一致"：
  README/index/earlier_months 过期、清单里的 section 缺失、raw 引用缺失，`--vault`
  直接打给人看；`rebuild_generated()` 后应当清零；
* 旧模式的机械校验仍是：超预算 / 孤儿档案 / 坏 frontmatter / 不合规文件名。

**vault 是投影，原始记录才是真相来源**（同 ADR-006）：
vault 整个删掉可以重建，README 写错也不会污染历史。

## 注入位次

```
身份与规则 → 背景档案（README） → 记忆条目 → 摘要 → 历史 → 时间
```

（检索块那一层已经删掉了，见 ADR-058 —— 记忆条目现在是全量注入 + 一条硬上限。）

排序依据是**"变得有多快"**，不是"有多重要"（ADR-056）：
记忆条目每写一次就变，README 一天最多变一次；低频道放前面，缓存前缀才长。

## 三个只读工具

| 工具 | 作用 |
|---|---|
| `dream_list(prefix?)` | 列出档案文件（自己按工具结果上限收敛，并报告还有多少没显示） |
| `dream_read(path, max_chars?)` | 读一份档案：正文 + frontmatter + **邻域** |
| `dream_search(query, limit?)` | 字面检索（复用 M2 的二元组打分，不引入第二套算法） |

细节上刻意做的三件事：

1. **每个工具都自己按结果预算收敛**（而不是让 registry 事后砍）：
   事后结构化截断会把每条命中的 snippet 一起削短，模型反而更看不出哪条相关；
   收敛时如实报告"还有多少没显示"。这是 ADR-051 那条教训的统一应用；
2. **不设分数门槛**：宁可返回"最像的几个"并带上分数，也不返回空列表 ——
   空列表会让模型以为"档案里没有这回事"，比给几个弱相关结果更糟；
3. 检索说明里写明"**换个说法可能查不到，可以换关键词再试一次**"。
   实测有效：问"我对烘焙度有什么偏好吗"，它先搜「烘焙度」得 0 条，
   自己换搜「浅烘」命中 —— 这正是提示词里那句话在起作用。

`dream_read` 返回的 `chars` 是**磁盘上的真实长度**，另给 `returned_chars`
告诉模型这次拿到了多少 —— 只报截断后的长度，模型就无法判断"这个文件到底多大、
我还差多少没看到"。

## 会话直接维护：四个受约束写工具（ADR-111）

| 工具 | 作用 |
|---|---|
| `dream_create(path, body, ...)` | 只新建；path 已存在就失败，不覆盖 |
| `dream_edit(path, body, ...)` | 只更新；path 不存在就失败。body 整体替换，未给元信息保留 |
| `dream_mkdir(path)` | 在 vault 内创建目录，可一次创建多级，也允许自定义顶层目录 |
| `dream_delete(path, reason?, recursive?)` | 删档案或目录；目录默认只能空删 |

用户明确要求维护档案时可以用，模型判断新建、修正或清理值得做时也可以主动用。写之前应先读
相关档案，优先改已有内容而不是制造重复。所有写操作与 dream 一样**不可回滚**。

允许写入的是 vault 内非保护位置的 Markdown：sections/、entities/、自定义目录与根级均可；
旧 vault 的 README 也可用 `dream_edit` 改。以下禁区由 `VaultStore` 而不是提示词自觉执行：

* `index.md`、`sections.yaml`、`log.md`、`memory_instruction.md`；
* 原生 README、`sections/earlier_months.md`、`sections/archived_month/` 与整个 `raw/`；
* README 在两种模式下都不可删除，原生固定 section 也不可删除；
* 顶层 `sections/`、`entities/` 不可作为目录整体删除。

每次成功改动后，原生模式自动重建 README/index/earlier_months 并同步 sections.yaml：
新自定义 section 会加入清单，删除后会从清单移除；旧模式只重建 index，不接管 README。
自定义根级/目录 Markdown 会进入 index 的 Other Files 与检索，但不会混进 sections.yaml 或
原生 README 的 section 投影。裸 `name.md` 仍优先解析既有 entities/sections；根级与 canonical
同名时裸名字会拒绝歧义操作，可分别用 `./name.md` 与 `entities/name.md` 明确指定。CLI 是长驻进程，因此每轮开始会重读 README；轮内保持不变，
避免多步工具循环丢失缓存前缀。

非空目录必须显式 `recursive=true`。递归删除会先检查整棵树：只要有受保护文件、非 Markdown
文件或符号链接，就在删除任何内容之前拒绝，不能通过删父目录绕过单文件禁区。

## 路径安全（本期最要紧的部分）

模型第一次能写"文件路径"，而我们在 Windows 上（ADR-054 有完整清单）：

```
拒绝  entities/NUL.md        设备名：写进空设备，看起来成功其实什么都没写
拒绝  entities/NUL .md       点号前的空格 Windows 同样忽略，还是设备
拒绝  entities/nul.x.md      判第一个点之前那一段，同样是设备
拒绝  entities/X.MD          大小写不敏感的系统上会和 x.md 撞成同一个文件
拒绝  entities/X.md          ← 与已存在的 x.md 撞名（会静默覆盖掉那份档案）
拒绝  entities/sub/x.md      整理器/导入器默认只允许两层；会话工具显式 allow_nested 后可写
拒绝  Entities/x.md          目录名必须小写
拒绝  raw/source_x/a.png     raw 只允许 raw/source_<数字>/<图片> 或同名 .meta.json
拒绝  穿越（点点斜杠）、POSIX 绝对路径、盘符路径、UNC 共享路径、波浪号开头
拒绝  entities/a:b.md  entities/a?.md  entities/a.md.  entities/ x.md
拒绝  超过 40 字符的名字
```

> 这一段刻意**不写出盘符路径的字面量**：`tests/test_paths.py` 会扫所有要发给别人的文件，
> 发现形如"字母 + 冒号 + 斜杠"的写法就报错（那是 ADR-042 的可移植性守卫，
> 写文档时被它抓过一次 —— 守卫不认识"这是在举反例"）。

**两条是被对抗性审查抓出来的**（详见 ADR-054，两条都有非跳过的测试钉住）：

* **只差大小写的档案会静默互相覆盖**：`Foo.md` 之后再写 `foo.md`，
  Windows 上是同一个文件，第一份的内容被无声销毁、事后还查不出证据。
  修法有两个坑：撞名检查必须**无条件跑**（大小写不敏感的系统上 `foo.md` 的
  `is_file()` 为真，新建分支根本不执行），而且比对要用**归一化后的相对路径**
  而不是 `resolve()` 之后的名字（Windows 会把它规范成磁盘上的大小写，
  拿它比永远看不出差异）。
* **扫描会跟着目录链接读穿**：`dream_read` 拒绝、`list_files` / `dream_search`
  却把 vault 外面的文件读了出来。四个扫描都走 `_iter_rels()`，
  在那儿补上与写入同一道闸即可。

再加上一层**真实前缀校验**（`resolve()` 之后必须仍在 vault 内）——
这一层专门挡**符号链接**：`entities/x.md` 本身完全合法，只有解析后比较才发现它跑到外面去了。

写入口只有一个（`VaultStore.write_file()`），**校验在 store 层而不是工具层**，
所以将来新增任何写入口都不会绕过它。原生模式还多两道：`_assert_editable()`
拒绝一切生成物 / 清单 / 归档月份；`--vault-import` 在解压前逐条校验
（白名单、大小写撞名、符号链接、图片内容嗅探、单文件/总量/条目上限），
全部通过后才从一次性暂存目录搬进**空 vault**（非空要 `--yes`）。

## 整理流水线

```
data/sessions/*.jsonl ──(dream_records 渲染)──▶ 记录文本 ──(整理者读)──▶ vault 文件
        ▲                                                                    │
        └────────── dream_cursor：每个会话整理到第几条 ◀─────────────────────┘
```

**输入**：`dream_records.py` 把会话事件渲染成可读文本。只保留用户发言、助手正文、
「（对话里记下了：…）」与「（这里联网查过一次资料）」这几类；
**工具噪声、思考过程、`prompt_built`（它含整份 prompt）一律不进记录** ——
最后这一条尤其重要：那是"网页正文能不能绕进档案"的主要入口（ADR-061）。

**整理者**（`DreamRunner`）与对话 agent 共用同一个循环，但工具集**彻底分开**：

| | 对话 agent | 整理者 |
|---|---|---|
| 记忆条目 | `mem_create / mem_edit / mem_del` | **一个都没有**（连读都没有） |
| 背景档案 | `dream_list / dream_read / dream_search`（只读） | `vault_read / vault_write / vault_delete` + **旧模式的** `readme_write` |

原生模式（有 `sections.yaml`）**不注册 `readme_write`**：README 由 harness 重建。
旧模式保持原样。两种模式都不给整理者任何记忆条目工具，也不给它直接改
`sections.yaml` / `index.md` / raw 的能力。
| 会话记录 | 看不到 | `records_list / records_read` |

**分页与游标**：整理者用 `records_read` 一次读一段（默认 40 条 / 6000 字符）。
游标取**"从游标的下一条起、一条不漏读过的那一段"** —— 跳读留下的空洞留到下次，
补上之后才一起推进（`ReadTracker` 合并读过的区间，ADR-060）。
**没有待整理的记录时根本不调模型**（那是一次白花的钱）。

**整理用哪个模型**（ADR-103）：Web 端是 Dream 页工具栏右侧的「整理模型」选择器，
CLI 端是 `--dream`（命令行上的 `--provider` / `--model` 是单次覆盖）。两边都走
`resolve_dream_provider()`：`providers.json` 的 `dream` 选择优先，**空 = 跟随全局
`default`**（所以升级后行为与之前一致）。它与聊天页顶栏的模型选择器**互相独立**；
选过的那家接口被删掉（或那个模型从接口的模型列表里删掉）时退回默认，**不会**
悄悄改用另一家。
运行记录里多一个 `generation` 字段，记下这次用的是哪个模型。

**产物**（全部在 `data/dreams/`，ADR-063）：

```
<run-id>.json        差异、用量、结构校验、推进了哪些游标、这次用的模型（generation）
<run-id>.jsonl       完整轨迹（每一次 vault_write 的全文都在里面）
```

**它是单向门，而且没有预演**（ADR-065 + ADR-072）：整理跑完就**不可逆** ——
没有 `--dream-undo`、不留任何快照，**也没有"先看一眼"**。所以：

* **唯一的补救是"格式化并初始化 dream 层"**：`--dream-format`（界面上是 Dream 页的
  同名按钮，**要二次确认**）清掉**档案 + 运行记录 + 整理进度**，然后建一个空骨架；
  下一次 `--dream` 会把所有会话**从头**重新整理一遍 —— 档案是会话的投影，重做是可行的，
  代价是再花一次整理的钱，而且**手写的档案不会回来**（动手前先备份 `data/vault/`）；
  它**只动 dream 那三样东西**：对话、记忆、接口配置与 key 一律不碰；
* 差异靠**内存影像**算：运行前把 vault 读进内存，进程结束就没了、不落盘；
* `--dreams` / `--dream-review <id>` 看历史与**改动清单**（逐行 diff 没有了 ——
  没有"改动前"可比）；改动后的样子可以直接 `--vault-show` 看；
* 想追回某份档案当时写了什么，只能看轨迹 `data/dreams/<run-id>.jsonl`
  （**格式化会把运行记录一起清掉，要追回就先别格式化**）。

**结构校验是机械的**：原生模式在整理结束（以及网页保存/删除）后先
`rebuild_generated()`，再 `VaultStore.check()`；旧模式在模型写完后直接 check。
超预算 / 生成物过期 / 清单 section 缺失 / 孤儿档案 / 坏 frontmatter / 坏文件名 /
raw 引用缺失都会写进运行记录并打给人看。旧模式的 `vault_write` 还会**当场**告诉模型
"README 超预算了"—— 它下一句就能改。

## 验收（已实测）

1. `--vault` 报出文件清单、README 预算占用、结构校验结果与**还有多少待整理**；
2. **地基端到端**（临时数据目录 + 手工 vault + 真实模型）：
   * 问「我喜欢喝什么咖啡」→ 它自己 `dream_read("sections/口味.md")` 拿到细节后回答；
   * 问「我对烘焙度有什么偏好吗」（README 里没有）→ 搜「烘焙度」0 条 → **自己换词**搜「浅烘」命中；
   * 问「我养过什么宠物」（档案里没有）→ 搜宠物/猫/狗各 0 条 → `dream_list` 看目录 →
     **明确说"确实不知道，不能瞎猜"**；
   * 记录下来的 `prompt_built` 事件里，README 确实在 system 里，且**没有**时间戳；
3. **流水线端到端**（临时数据目录 + 两段真实对话 + 真实模型）：
   * 整理者读完两段记录，建了 3 个 section + 3 个实体 + README（**7 份档案**，
     全部用了 `[[双链]]`），README 301/1200 token，结构校验通过，游标推进 2 个会话，
     一轮花 27k token（缓存命中 21k）；
   * 它在思考里**主动执行了"宁缺勿滥"**：「妈妈」只出现一次，够不上一个实体，写进 section；
   * 再跑一次 → **没有待整理的记录，不调模型**（0 token）；
   * **闭环**：让对话模型在这个真·产物上回答「我喝咖啡有什么讲究？小张是谁？」——
     它读 README 后自己 `dream_read` 两个文件，答对，还**把两处信息串起来**
     （"下午三点后不喝"与"11 点前睡"是同一件事的两面），并如实说"这些只在背景档案里、
     记忆条目还是空的"；
   * `--dream-review` 打出改动清单、结构校验、改动后的样子与轨迹路径；
     **整理不可撤销、也没有预演**（ADR-065/072）；
   * **格式化重来**（ADR-072，实测）：造好档案 + 运行记录 + 整理进度（1/1/1）→
     `--dream-format` / `POST /api/dream/format`（确认词一字不差）→ 三者都变 0、
     建好空骨架、暂存目录无残留；**对话与记忆逐字节未变**（有断言钉着）；
     确认词不对时 **400 且什么都不做**；整理运行中 **409**；
     把 `dry_run` 发给整理接口会**明确 400**（而不是被忽略成一次真整理）；
4. 地基测试 + 流水线测试全绿；全量测试全绿（条数见 README），无依赖环境只 skip、不变红；
   其中安全类断言**不是跳过的那种**（目录 junction 不需要管理员权限，
   所以"扫描读穿"那条是真跑过的）；
5. **空 vault 不注册那三个工具、也不在 data/ 里创建任何目录**；
   判据是"**有没有真档案**"（`sections/` 或 `entities/` 里至少一个文件），
   不是"README 在不在" —— `--vault-init` 建的骨架只有一份占位 README，
   那时注册工具、注入一段"这里什么都没有"的 README 都是纯浪费；
6. 恢复出厂设置的预览会列出"背景档案 N 个文件"，占用空间直接量整个数据目录；
7. **原生导入实测（ADR-104）**：用户那份 Kimi 导出 zip（59 条目 / 47 份档案 /
   12 个 raw 条目）导入空 vault 成功，自动进入原生模式；`check()` 如实报出
   README/index/earlier_months 三类 stale（真档案里那几处导出瑕疵），
   `rebuild_generated()` 后清零；README 注入 4351 字符 / cap 6000，未截断。

## 已知局限

* **README 与正文会漂移**：机械校验只能发现"忘了索引"，发现不了"索引写得不准确"。
* **检索是字面的**：换个说法（同义词）就查不到，只能靠模型换关键词再试。
* 邻域扫描是 O(文件数)，且 `related` 只算"共享链接目标"这一种共现关系。
* 孤儿检查是**子串启发式**：正文里恰好提到那个词就不算孤儿，可能漏报。
* 符号链接检测依赖 `resolve()`，理论上存在 TOCTOU 窗口（本机单用户，接受）。
* **`dream_read` 没有续读参数**（没有 offset）：长文件只能读到前一段，
  尾部目前拿不到（现在至少会如实报出"共 N 字符、本次给了 M 个"）。
* **写入期间目标被占用会失败**：`os.replace` 带了几次重试，但"全程占着"的窗口救不了，
  只能报一句人话并保持旧内容（旧内容不会被写坏）。
* **整理一次最多处理 3 个会话**（`dream_max_sessions`）：一天的对话多起来要跑几轮。
  这是成本闸，不是能力上限。
* **档案是投影，会失真**：它抽取的是"长期是什么样"，一次性的、细节性的内容会被丢掉 ——
  这正是设计意图，但意味着**档案里查不到的不等于没发生过**（原始记录还在会话里）。
* **整理不可撤销，而且没有预演**（ADR-065 / ADR-072，用户决定）：一次糟糕的整理
  只能用轨迹里的全文**手工**改回去（又慢又容易漏），或者**整层格式化重来**
  （清空 + 重新整理一遍，档案是投影所以能重做，代价是再花一次钱、且手写的档案会没）。
  **"事前看一眼"这条安全网被明确去掉了** —— 换来的是一条更简单的规则：
  改坏了就重来。所以动手之前自己备份 `data/vault/` 是个好习惯。
* **没有逐行 diff 了**：只能看到"哪几份变了、字符数从多少到多少、改完长什么样"。
  要行级差异就得留"改动前"，而那就是快照 —— 与"不可逆"直接冲突。
* **格式化会连运行记录一起清掉**：轨迹（追回单份文件全文的唯一凭据）也没了。
  要先追回就先 `--dream-review`，别急着格式化。
* **`data/dreams/` 仍然没有保留策略**：不过现在一次运行只多两个文件
  （`.json` + `.jsonl`），不再是一整份 vault 副本。想清理就删旧记录，
  代价只是失去那几次的审查材料与轨迹。
* **游标是"序号"，不是"内容指纹"**：会话文件被**改短**能发现（按从头整理并报警），
  但"同名会话被重建、内容变了、却比游标更长"发现不了。要修得把游标换成内容指纹。
* **一条坏日志不再拖垮整轮**（ADR-064）：读不出来的会话会被跳过并报出来，
  `seq` 不是数字会退回行序号；但**内容层面**的损坏（比如一整行被改成别的 JSON）
  看不出来。
* ~~README 可能被标成占位而永久不注入~~ —— 已修：写入端拒绝那个标记，
  `check()` 也把"被判为占位"与"真的没有 README"分开说（ADR-064）。
* **C 阶段还没做**（ADR-104）：observations 生命周期、完整 `log.md` 语义、
  `[^N]`/`source_ref` 引用、raw 图片入库、归档轮转。导入的原生
  `observations.md` / `log.md` / `raw/` **可以读和看**，但不会自动维护；
  原生 README 的 `user_id`/`last_avatar` 等 Kimi 专有字段只保留/给本地稳定值，
  不假装懂它们的语义。
* **原生导入不迁移旧 vault**：`sections.yaml` 存在才是原生模式；旧 vault 要迁移
  得手工建清单（见 ADR-104）。

## 下一步

1. Web 端的档案视图已经接上（Dream 页：待整理 + **可逐层点开的目录树 + 点开看正文**
   （含 frontmatter 属性、`[[双链]]` 跳转、反链）+ **直接新建 / 编辑 / 删除** +
   README 预览 + 结构校验 + 运行历史与改动清单 + "不可撤销、无预演"的说明 +
   **「格式化并初始化 dream」**（双重确认））；
2. **恢复出厂设置**会把 dream 层格式化并初始化（ADR-065）：
   档案、运行记录、整理进度全没，下一次整理从零开始；
   **只想清 dream 那一层就用 `--dream-format` / 界面上的同名按钮**（ADR-072）——
   它不碰对话、记忆与 key；
3. **vault 的目录层级**：旧模式是两级（`sections/`、`entities/`）；原生模式额外允许
   `sections/archived_month/<YYYY-MM>/this_month.md` 与 `raw/source_<n>/<图片>`。
   前端那棵树是**递归**的，所以放宽层级界面不用改；要再放宽得先改
   `normalize_rel_path` / `normalize_raw_path` 与相关的结构校验；
4. **界面上可以改档案**（ADR-070，推翻了 ADR-067 的"只读"）：写入走
   `VaultStore.save_note()` / `delete_file()` —— 与整理工具**同一份实现**；
   保存带内容指纹（被改过就 409）、整理运行时拒绝写入、删除会报出会变成死链的反链。
   代价仍在：手写与模型写混在一起时，README 与正文更容易漂移 ——
   这一版的办法是**让结构校验把它说出来**，而不是禁止手写；
5. 如果哪天觉得"完全不能退"太硬，可选的折中是**只保留审查用的一小份**
   （比如把改动前的内容只存进运行记录、有大小上限），但那本质上还是备份 ——
   要加必须先回答"留多久、多大、谁能还原"（预演已经因为这个问题被否掉过一次，ADR-072）；
6. 如果哪天需要"按需调回原文"，可以在整理者手里加一个"读会话原文"的工具 ——
   现在它只能读到渲染后的记录。
