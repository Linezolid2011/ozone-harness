# AGENTS.md —— 在这个仓库里怎么干活

> 这份文件是写给**下一个接手的人（或 agent）**的。它不解释功能，只讲规矩：
> 什么不能碰、改完怎么验、这个项目为什么这么较真。
> 功能与验收标准见 [`00-scope.md`](00-scope.md)，每个决定的来龙去脉见
> [`01-decisions.md`](01-decisions.md)。

## 这是什么

`O₃ᶻᵒⁿᵉ`：一个跑在自己电脑上的**对话服务端编排层**（agent harness）。单用户、本机、
每个人用自己的 API key。它不是框架，也不是给多人用的服务 —— 没有鉴权，默认只监听
`127.0.0.1`。

- 入口：`start.cmd`（双击开网页）/ `run.cmd`（全部命令行功能）。这些入口及类似脚本
  **都可以按需求修改**，不再要求与旧版本严格一致（ADR-107）；改动仍须同步文档与离线回归。
- 版本号：`docs/version.txt`（`harness.version()` 读它）。**不要写死版本号或产品名**
  ——产品名在 `harness/__init__.py` 的 `PRODUCT_NAME`/`PRODUCT_SLUG`。

## 三条不可协商的规矩

1. **每个决定写 ADR**。编号递增，形状固定：**背景 / 决定 / 理由 / 验证 / 教训**。
   推翻旧决定不要改旧 ADR，写一条新的并在旧的那条标题上标「已被 ADR-xxx 取代」。
2. **机械守卫优先于文档叮嘱**。能写成测试的规则就写成测试（"禁止某功能复活"
   "一个请求只能产生一个响应""英文界面里不许出现汉字"都是这种）。文档会漂移，红灯不会。
3. **测试必须离线、可重跑**。模型后端用 `FakeProvider` 替换，绝不真花钱；
   第三方依赖是可选的（没装 → 跳过，**不是变红**）。

## 不要做的事

| 别做 | 为什么 |
|---|---|
| 在代码里写绝对路径 | `tests/test_paths.py` 会扫所有"要发给别人"的文件并报错（这个仓库真的因此抓到自己两次） |
| 把产品名/版本号写死在文案里 | 改名、升号时必然漏一处，然后界面与命令行互相矛盾 |
| 在 `.cmd` 里写非 ASCII | `cmd.exe` 按 OEM 代码页读批处理，中文注释会直接让脚本崩 |
| 删/改历史 ADR、历史验收记录 | 那些是"当时为什么这么做"的唯一证据；被推翻也要留着 |
| 让网页正文 / OCR 文字变成指令或记忆来源 | 见 ADR-039/048/083：外部内容是不可信数据，只留痕不洗白 |
| 悄悄降级（"出错了但看起来成功了"） | 这个项目最讨厌的错误类型；宁可报错，也不给一个错的数字（ADR-032/052/095） |

## 代码地图

```
harness/
  loop.py           Agent 循环（对话与整理共用）：熔断、工具执行、事件落盘
  context.py        system 提示 + 记忆/README 注入 + 历史裁剪（顺序 = 缓存经济学）
  events.py         事件溯源 JSONL：唯一真相来源；seq 分配、重放、悬空 tool_calls 修补
  memory.py         记忆条目（SQLite）：软删除、可调上限、整理游标
  vault.py          背景档案：路径安全、frontmatter、README 预算、结构校验
  vault_import.py   原生 Kimi vault zip 的安全导入（ADR-104）
  dream.py          整理流水线（单向门，无预演）+ 格式化重来
  dream_records.py  会话事件 → 可读记录（渲染/分页/游标只认连续区间）
  providers/        供应商档案（providers.json）、OpenAI/Anthropic 兼容客户端
  net/              联网工具：抓取（SSRF fail-closed）、搜索后端、工具设置
  tools/            工具注册表 + 对话侧工具（记忆三件套、档案只读三件套）+ 整理侧工具
  web.py            零依赖 HTTP + SSE（所有路由都在这里，含安全闸）
  venv_setup.py     start.cmd 引导门：完整性探针、事务安装、暂停/取消与恢复
  cli.py            命令行入口
  i18n.py / prompts/  zh/en 两套文案与长提示词（**成对**，守卫会查键集一致）
  atomic_io.py      原子写（临时文件 + os.replace）
web/                index.html / style.css / app.js（无构建、无框架）
docs/               范围、ADR、各阶段设计、截图、version.txt、changelog.md、AGENTS.md
tests/              离线测试
scripts/ui_check/   前端自检（自写 DOM stub 跑真 app.js）、截图、夹具
```

## 不变量（改了会出事，都有测试钉着）

* **`tool_calls` 必须永远有配对结果**（ADR-045）：补桩 + 重放层修补，两道。
* **一个请求只能产生一个响应**（ADR-089）：SSE 已经开始的异常只能变成 `notice` 帧。
* **事件日志的 `seq` 唯一且递增**：整理游标靠它定位；重复 = 有记录被无声吞掉。
* **`data/` 下的 JSON 一律原子写**：半截文件会被读成"没有配置"（= key 全丢）。
* **记忆条目与背景档案是两个存储、两种写权限**（ADR-053）：对话模型只读档案，
  整理者碰不到记忆条目。
* **会话 id 必须校验**（ADR-049）：它直接拼文件名；非法一律 400 且不碰磁盘。
* **完整 API key 永不出现在任何响应里**（只给掩码）。
* **"只看一眼"的接口不落盘**（预览、`GET /api/reset`、`prompt_view`）。
* **README 注入有硬上限**（ADR-056）：由 harness 截断，不靠提示词自觉。
* **原生 vault 的生成物只读**（ADR-104）：有 `sections.yaml` 时，README / index /
  earlier_months / 清单 / archived_month 由 harness 维护；模型只写 sections/entities。
  模式开关是**已有文件** `sections.yaml`，不是新设置项；旧 vault 不自动迁移。
* **原生 zip 导入只写空 vault**（ADR-104）：逐条校验路径/符号链接/大小/图片内容，
  全过才从暂存搬入；非空要 `--yes`。
* **SSRF 判据要自己归一化、fail-closed**（ADR-052）：不认的输入一律拒。

## 怎么验证（每次改完都跑）

```bat
:: 全量（必须用 .venv，否则联网工具那约 30 条会被跳过而汇总仍写 OK）
.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t .

:: 前端自检（无浏览器，跑真 app.js）
node scripts\ui_check\dom-stub.test.mjs

:: 可移植性（绝对路径守卫）
.venv\Scripts\python.exe -X utf8 -m unittest tests.test_paths
```

期望：全量 `OK (skipped=2)`（那 2 条是本机环境跳过：建符号链接要特权、
"大小写敏感才能构造撞名"），前端 `失败：0`。**当前条数只记在 README 的「测试」一节**，
别在各处复制数字。

改了界面之后，还要**重拍文档截图**（`scripts/ui_check/seed_doc_fixtures.py` +
`scripts/ui_check/README.md` 里那条 Chrome 命令）—— 图会腐烂，但一条命令能重拍。

## 写作约定

* 注释与文档**用中文**，并解释**为什么**（不是"这行做了什么"）。用户可见的界面文案
  走 `t()`，中英各一份；CLI 输出保持中文。
* 错误信息要说清"下一步怎么做"，而不是只报"失败了"。
* 数字要**量出来**再写（这个仓库的 ADR 里到处都是实测值）；没量过的就写"没验过"。
* 新增功能先问"这是 Kimi 的事实形态吗"，不是就先写 ADR 论证（ADR-058 是反面教材：
  我们加了一层用不上的检索，最后连根删掉）。

## 常见坑（踩过才写下来的）

* **Windows 代码页**：`cmd` 读批处理用 OEM 代码页 —— `.cmd` 只用 ASCII；
  Python 输出中文没问题（`PYTHONUTF8=1`）。
* **别用 PowerShell 做 UTF-8 源文件的文本往返**：`Get-Content -Raw` → `Set-Content`
  会把中文变成乱码并吞掉引号（真的毁过一次 `web/app.js`，见 ADR-附录）。
* **临时目录可能在项目里**：测试与脚本一律用一次性目录，而且**绝不许指向真实 `data/`**
  （那是"恢复出厂设置"会整目录删掉的地方）。
* **端口不许静默双绑**（ADR-087）：Windows 的 `SO_REUSEADDR` 语义会骗你。
* **测试会替你挡掉一整类 bug**：`_post()` 每条请求新开连接，于是"一个请求两个响应"
  这种协议级错误 936 条测试全绿都抓不到（ADR-089/095）。**协议层的事要造出真形状**。
* **截图会腐烂**：界面一变，docs 里的图就过时，而且它不会自己报错。改了界面就重拍。
