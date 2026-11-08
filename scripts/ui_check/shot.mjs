'use strict';
/* 用 headless Chrome + CDP 真的渲染一遍界面并截图。
 *
 * 为什么值得做：前面十几轮我一直写着"这台机器没有可用的无头浏览器，视觉没验证" ——
 * 而它其实有。这一步把那个缺口关掉。
 *
 * 只用 Node 内置能力：全局 WebSocket（Node 21+）+ 内置 fetch。
 * 用法：node scripts/ui_check/shot.mjs [baseUrl] [outDir] [dataDir]
 *
 *   baseUrl  默认 http://127.0.0.1:8791
 *   outDir   默认 .tmp-ui-check/shots（01/02/03 三张 Dream 页的图落在这里）
 *   dataDir  可选；给了它才会**种两条带图片的夹具会话**并拍文档里那四张：
 *            `docs/screenshots/chat-attachments.png`（缩略图那张）与
 *            `docs/screenshots/chat-multimodal-ocr.png`（**默认折叠**的 OCR 气泡），
 *            以及它们的英文版 `chat-attachments-en.png` / `chat-multimodal-ocr-en.png`
 *            （ADR-085：脚本自己走 `/api/settings/language` 两种语言各拍一遍，
 *            收尾切回原来的语言。**不靠手动塞图** —— 那样就不能重拍，迟早腐烂）。
 *            没给（也没有 HARNESS_DATA_DIR）就跳过这几张，前三张照拍。
 *            同一个分支还会拍**接口页的模型表格**两张：`provider-models.png` 与
 *            `provider-models-dark.png`（ADR-092：一行一个模型 = 模型 ID / 上下文
 *            上限 / 显示名，行尾删除、底部「添加模型」）。
 *
 * 前三张是"设置 → Dream"页；第 4/5 张在**对话页**上，靠自己种进去的会话记录
 * 演示附件缩略图与 `modality: "text"` 的 OCR 文字块 —— **不调模型**，
 * 所以它们随时可以重拍（见 seedFixtures()）。
 * 第 6 张是"设置 → 提示词"页（docs/13-prompts-page.md）：浅色 `prompts-page.png`
 * ＋深色 `prompts-page-dark.png`（两张都是**分组默认全折**的样子）
 * ＋`prompts-page-group-open.png`（展开第一个分组之后），落在**项目根的
 * `.tmp-shots/`**（不写 docs/），尺寸与 `chat-light.png` 同档（1440×900）。
 * 它不依赖任何模型调用，拍之前会先断言"22 条提示词 + 20 个工具都画出来了、
 * 7 个分组默认都不带 open、原文默认折叠、页面零报错"，断言不过就不写文件。
 * 只拍这五张：设 `SHOT_PROMPTS_ONLY=1`。
 *
 * 第 4/5 张是**可编辑提示词**（docs/14-editable-prompts.md §8）：
 *   * `prompts-page-editing.png`    —— 点开「编辑」之后的样子（textarea + 三句提示）；
 *   * `prompts-page-overridden.png` —— 一条**真被改过**的样子（「已修改」+「复原」+ warn）。
 * 后一张必须先经真接口写一条覆盖（界面上那个标记与那行 warn 都是按服务端的
 * `overridden` / `warnings` 画的，凭空画一个假的就不叫验证了），**拍完立刻复原**，
 * 并核对"这门语言一条覆盖都没剩下"。所以它和 `e2e-live.mjs` 一样**会写盘**：
 * 只该对着一次性夹具目录跑（脚本开头那道 `assertFixtureTarget()` 挡着真实 data/）。
 *
 * 设 `SHOT_VARIANTS=1` 再额外拍三张**对照图**（深色主题 / 窄屏，只有 OCR 那一张），
 * 落在 outDir、**不写 docs/** —— 用来人工看一眼折叠态在别的主题与窄窗口下的样子。
 */

import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';

const BASE = process.argv[2] || 'http://127.0.0.1:8791';
const OUT = process.argv[3] || '.tmp-ui-check/shots';
const DATA_DIR = process.argv[4] || process.env.HARNESS_DATA_DIR || '';
// docs/ 里的图按脚本自身定位，谁 clone 到哪都能跑（不写死盘符、也不靠 cwd）
const DOCS_OUT = fileURLToPath(new URL('../../docs/screenshots', import.meta.url));
const REAL_DATA = fileURLToPath(new URL('../../data', import.meta.url));
/* 第 6 张（设置 → 提示词）的落点：默认是**项目根**下的 `.tmp-shots/`。
   同样**不写死盘符**：`tests/test_paths.py` 会按行扫 scripts 目录（递归）里的每个
   `.mjs`，找"盘符 + 反斜杠"那种绝对路径 —— 注释里举例一样会被拦下来
   （我这一版真被拦过），而且写死了换台机器就指到别人那儿去了。
   拿到手的是绝对路径（`URL` → `fileURLToPath`），报告里可以照抄。
   想换地方：设 `SHOT_PROMPTS_DIR`。 */
const PROMPTS_OUT = process.env.SHOT_PROMPTS_DIR
  || fileURLToPath(new URL('../../.tmp-shots', import.meta.url));
const PORT = 9333;

/* Chrome 装在哪：**不写死盘符**。
   写死一个"某个盘符 + Program Files"的绝对路径，会在别人机器上（或者系统装在别的盘时）
   直接坏掉 —— 仓库里那条可移植性守卫也会拦下来（它真的拦过我这一版，连同下面这段注释）。
   用环境变量拼，到哪都对。 */
/* 优先级：`CHROME_PATH` → `%ProgramFiles%` → `%ProgramFiles(x86)%`
   → `%LOCALAPPDATA%` → PATH 上的 `chrome` / `google-chrome` / `chromium`。 */
function findChrome() {
  const { env } = process;
  const candidates = [];
  if (env.CHROME_PATH) candidates.push(env.CHROME_PATH);
  if (env.ProgramFiles) {
    candidates.push(`${env.ProgramFiles}/Google/Chrome/Application/chrome.exe`);
  }
  if (env['ProgramFiles(x86)']) {
    candidates.push(`${env['ProgramFiles(x86)']}/Google/Chrome/Application/chrome.exe`);
  }
  if (env.LOCALAPPDATA) {
    candidates.push(`${env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`);
  }
  candidates.push('chrome', 'google-chrome', 'chromium');
  return candidates;
}

const CHROME_CANDIDATES = findChrome();

/* **先验数据目录，再动手**（函数定义在下面，声明会提升）：这个脚本会写盘
   （种夹具会话、切语言时写 `settings.json`），所以对着一个**不像夹具**的服务
   直接退出，连 Chrome 都不启动、连输出目录都不建。见 `assertFixtureTarget()`。
   失败时**只打那句话**，不吐一堆堆栈 —— 这是一句"你用法不对，该这么用"，
   堆栈只会把真正要看的那几行挤没了。 */
try {
  await assertFixtureTarget();
} catch (err) {
  console.error(`\n${(err && err.message) || err}\n`);
  process.exit(1);
}

mkdirSync(OUT, { recursive: true });

/* **只按 PID 杀自己起的那一个**：按进程名杀会把用户自己的浏览器一起杀掉
   （这件事真的发生过一次，见 ADR-073）。
   `--user-data-dir` 用**绝对路径**：相对路径是按"Chrome 进程的当前目录"解析的，
   而那不一定是我们以为的那个目录 —— 真跑到别处就会和用户自己的配置打架。 */
const profile = path.resolve(OUT, 'profile');

function chromeArgs() {
  return [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--hide-scrollbars',
    '--window-size=1500,1050',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    BASE + '/',
  ];
}

/* 起不来的**唯一判据是"调试端口没活过来"**，不是进程退没退出。
   两个原因（都真的踩过）：
     * `spawn` 找不到可执行文件时抛的是**异步 `'error'` 事件**，`try/catch` 接不住；
       没有监听器就是一个未处理的 error → 进程当场崩，连"换下一个候选"都走不到；
     * Windows 上 Chrome 的**启动器进程会先退出、浏览器进程继续跑**。
       拿 `child.exitCode !== null` 判断"这个候选起没起来"会把一个**正常启动**的
       Chrome 当成"起来了又死掉"扔掉（然后去试下一个候选）。
   所以：挂 `error` 监听 + 收 `stderr`（留下失败原因），轮询 `/json/list`。 */
async function waitForCdp(timeoutMs, spawnFailed) {
  const deadline = Date.now() + timeoutMs;
  let last = '';
  while (Date.now() < deadline) {
    if (spawnFailed()) return null;      // ENOENT 之类：不用等满超时
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((item) => item.type === 'page' && item.webSocketDebuggerUrl);
      if (page) return page.webSocketDebuggerUrl;
    } catch (error) {
      last = String((error && error.message) || error);
    }
    await sleep(200);
  }
  return null;
}

/* 只按 PID 收自己起的那个进程。启动器先退出的情况下 `kill()` 可能什么都杀不到，
   这时才补一刀 `taskkill /PID <pid> /T /F`（**仍然是按 PID**，不是按进程名）。 */
async function killByPid(child) {
  if (!child || child.pid === undefined) return;
  try { child.kill(); } catch { /* 已经没了 */ }
  await sleep(300);
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
  }
}

async function launchChrome() {
  const reasons = [];
  for (const candidate of CHROME_CANDIDATES) {
    let spawnError = null;
    const child = spawn(candidate, chromeArgs(), { stdio: ['ignore', 'ignore', 'pipe'] });
    let stderr = '';
    if (child.stderr) {
      child.stderr.on('data', (chunk) => { stderr = (stderr + String(chunk)).slice(-1500); });
    }
    child.once('error', (error) => { spawnError = error; });

    const wsUrl = await waitForCdp(12000, () => spawnError !== null);
    if (wsUrl) return { child, wsUrl, candidate };

    const tail = stderr.trim().split('\n').filter(Boolean).pop() || '';
    reasons.push(`${candidate}：${spawnError ? spawnError.message : (tail || '12 秒内调试端口没活过来')}`);
    await killByPid(child);
  }
  throw new Error(
    '找不到可用的 Chrome。用 CHROME_PATH 指定，例如：\n' +
      '  set CHROME_PATH=<chrome.exe 的完整路径>\n' +
      '试过的候选与原因：\n  ' + reasons.join('\n  '),
  );
}

const chrome = await launchChrome();
console.log(`  Chrome：${chrome.candidate}`);

let ws = null;
const pending = new Map();
let nextId = 1;

function send(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error('页面里报错：' + JSON.stringify(result.exceptionDetails.exception));
  }
  return result.result && result.result.value;
}

/* 轮询一段表达式直到它为真。页面是异步渲染的（fetch 回来才画），
   固定 `sleep(1200)` 那种写法在慢一点的机器上就会拍到半张空页面。 */
async function waitFor(expression, timeoutMs = 15000, label = expression) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await evaluate(expression);
      if (last) return last;
    } catch (error) {
      last = String((error && error.message) || error);
    }
    await sleep(200);
  }
  throw new Error(`等不到：${label}（最后一次结果：${JSON.stringify(last)}）`);
}

async function shot(name, dir = OUT) {
  const result = await send('Page.captureScreenshot', { format: 'png' });
  const file = path.join(dir, `${name}.png`);
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, Buffer.from(result.data, 'base64'));
  console.log(`  已截图 ${file}`);
  return file;
}

/* ── ④⑤ 附件 / OCR 截图：**不调模型**，直接在数据目录里种会话 ──────────────
   种两条会话：
     * `chat-attachments`：一条带 `attachments` 的 `user_message`（缩略图那张）；
     * `chat-multimodal-ocr`：一条 `modality: "text"` 的 `user_message`，
       `attachments` 里按契约 §13.3 带 `ocr: {engine, seconds, chars, text}`
       —— 界面据此画出**默认折叠**的 OCR 气泡（契约 §13.7 第 3 句）。
   两条各配一条明写"截图夹具"的 `assistant_message`（别伪装成真回答），
   以及那个附件文件本身（内容哈希命名的 PNG，base64 嵌在下面）。
   为什么这么做：真调一次模型才能截图的话，这些图就**不可重拍**（要花钱），
   迟早会像 docs/screenshots/dream.png 那样腐烂。 */

// 96×72 的确定性小图（两条斜带 + 一个方块 + 一个圆），544 字节
const FIXTURE_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAGAAAABICAIAAACGBWc0AAAB50lEQVR42u3bMU7DMBgFYPfRiYEdMXMENgauwcYJkHqPSkicgI1rIDH2CMyIFTEwVFRiqFRVafzsJE6U3//z2FZP6Se3Sfzixff6NsTH+eNbyBu/z3dV5kA6PAfS4TmQDs+BdHgOpMNzlqcpN6uzvKh39uaq+cLmaWdRGT1SSg0TcxDS4TmQDs+BdHgOpMNzIB2eA+nwnKl/YuauKiEdngPp8BxIh+dAOjwHnVK86TSBpMOApMOApMOApMOAptGxe0cC6fTsxaTDejHpsF5MOsc5y9OUTdipF2v/k1ZrWKYX86PTpxdzpdO5F/OmE0JYbLd/0ilwJe1TJ7cXc6uTNYM866SBnOskgKTTcqth/VtdvdzHPvP58NrjeNpP8xZ1CM3x+Pj56nQ8LUDmdDJpOFPseJpAtnSuLy6HLLMcmMr0YpXpHBLK9GL16WTmwLNO8sSX1YvZvd4pMuDhnDVkEkFzp6r9YmNMn9gkqnO/WPEmCtLxsl9spBYT0qm/Fxu1AYd00rcahnT2i15jjNg6ETR3yjxhNh+dMSZRbPpY7cXChHsBTPZiZF251PQx34uVMuI6tnux4UZJnUCe7jC0TtRjgZH8Kwf1Yu56scy1Qf6TrLMXm9d+MZ9P6kE6FfZi89ov5vwZ2H+Y5gzvoykzSgAAAABJRU5ErkJggg==';
const FIXTURE_WIDTH = 96;
const FIXTURE_HEIGHT = 72;
// 事件里记的字节数是**解码后**的真实长度（用 base64 字符串的长度就错了）
const FIXTURE_PNG_BYTES = Buffer.from(FIXTURE_PNG, 'base64').length;

/* ① 缩略图那条 */
const IMAGE_SESSION = 'shot-attach-fixture-0001';
const IMAGE_ID = '5eed000000000001';     // 16 位十六进制：内容哈希的形状（契约 §7.2）
const IMAGE_NAME = 'shot-fixture.png';
const IMAGE_TEXT = '帮我看看这张图。';
const IMAGE_REPLY = '（截图夹具）这是一张测试图片。这条会话是 scripts/ui_check/shot.mjs '
  + '直接种进数据目录的，**没有调用模型** —— 所以这张文档截图随时可以重拍。';

/* ② OCR 那条（契约 §13.3 / §13.7 第 3 句）。
   识别文字照抄上一版**真跑**出来的结果（`docs/screenshots/chat-multimodal-ocr.png`
   的那一段），换行与标点都保留；`chars` 就是这段文本的真实长度 —— 折叠标题里
   那句"· N 字"用的就是它（不是拿 base64 长度、也不是随便编一个数）。 */
const OCR_SESSION = 'shot-ocr-fixture-0001';
const OCR_ID = '5eed000000000002';
const OCR_NAME = 'shot-ocr-fixture.png';
const OCR_ENGINE = 'rapidocr-onnxruntime 1.2.3';
const OCR_SECONDS = 1.57;
const OCR_TEXT = [
  '只识别图片中的文字',
  '订单号A-2026-1014，金额128.50元',
  '联系人：小林 电话138-0000-0000',
  '备注：明天下午三点前送到。',
  'Total: 128.50 CNY / 3 items',
  '这一行的作用是验证换行与标点，句号。',
].join('\n');
const OCR_CHARS = OCR_TEXT.length;
const OCR_QUESTION = '这张图里写了什么？';
const OCR_REPLY = '（截图夹具）上面那段折叠起来的就是模型当时收到的**全部**内容'
  + '（本机 OCR 文字，图片本身没有发出去）。这条会话是 scripts/ui_check/shot.mjs '
  + '直接种进数据目录的，**没有调用模型** —— 所以这张文档截图随时可以重拍。';

/* 一条附件记录（字段照抄契约 §2 / §7.2 的事件形状）。
   `file` 是**相对 data_dir 的相对路径**，绝不出现绝对路径。 */
function attachmentRecord(sessionId, id, name) {
  return {
    id,
    file: `attachments/${sessionId}/${id}.png`,
    name,
    mime: 'image/png',
    bytes: FIXTURE_PNG_BYTES,
    width: FIXTURE_WIDTH,
    height: FIXTURE_HEIGHT,
    stored_bytes: FIXTURE_PNG_BYTES,
    stored_width: FIXTURE_WIDTH,
    stored_height: FIXTURE_HEIGHT,
  };
}

/* 把一条会话写进数据目录：附件文件 + `<sid>.jsonl`。
   附件文件按**内容哈希**命名（这里就是一个确定性的假哈希）。 */
function writeSession(dataDir, sessionId, ids, records) {
  const attachDir = path.join(dataDir, 'attachments', sessionId);
  mkdirSync(attachDir, { recursive: true });
  for (const id of ids) {
    writeFileSync(path.join(attachDir, `${id}.png`), Buffer.from(FIXTURE_PNG, 'base64'));
  }
  writeFileSync(
    path.join(dataDir, 'sessions', `${sessionId}.jsonl`),
    records.map((record) => JSON.stringify(record)).join('\n') + '\n',
  );
}

function seedFixtures(dataDir) {
  if (!dataDir) return null;
  const resolved = path.resolve(dataDir);
  if (resolved === path.resolve(REAL_DATA)) {
    // 种假会话进真实 data/ 会污染用户自己的对话列表（还会出现在侧栏里）
    console.log('  ⚠ 数据目录指向项目自己的 data/ —— 拒绝往里种夹具，跳过附件 / OCR 两张图。'
      + '请指向一次性目录（例如 .tmp-ui-check\\data）。');
    return null;
  }
  mkdirSync(path.join(resolved, 'sessions'), { recursive: true });
  seedProviders(resolved);

  const ts = '2026-10-14T00:00:00+08:00';
  // 非零夹具让会话统计条的五种数字与上下文小进度条都能被截图验证。
  const usage = {
    prompt_tokens: 12000, completion_tokens: 600, total_tokens: 12600,
    reasoning_tokens: 0, cached_tokens: 11400, cache_miss_tokens: 600,
  };

  writeSession(resolved, IMAGE_SESSION, [IMAGE_ID], [
    {
      kind: 'user_message',
      seq: 1,
      ts,
      text: IMAGE_TEXT,
      attachments: [attachmentRecord(IMAGE_SESSION, IMAGE_ID, IMAGE_NAME)],
    },
    {
      kind: 'assistant_message',
      seq: 2,
      ts,
      message: { content: IMAGE_REPLY, reasoning: '' },
      usage,
    },
    {
      kind: 'turn_end', seq: 3, ts, steps: 1, stop_reason: 'completed',
      elapsed: 20, usage, user_seq: 1,
    },
  ]);

  writeSession(resolved, OCR_SESSION, [OCR_ID], [
    {
      kind: 'user_message',
      seq: 1,
      ts,
      text: OCR_QUESTION,
      // 契约 §13.3：这一轮**图片没发出去**，只发了本机 OCR 出来的文字
      modality: 'text',
      attachments: [{
        ...attachmentRecord(OCR_SESSION, OCR_ID, OCR_NAME),
        ocr: { engine: OCR_ENGINE, seconds: OCR_SECONDS, chars: OCR_CHARS, text: OCR_TEXT },
      }],
    },
    {
      kind: 'assistant_message',
      seq: 2,
      ts,
      message: { content: OCR_REPLY, reasoning: '' },
      usage,
    },
    {
      kind: 'turn_end', seq: 3, ts, steps: 1, stop_reason: 'completed',
      elapsed: 20, usage, user_seq: 1,
    },
    {
      // 这个会话的「多模态」是**关着**的（契约 §13.1：会话级开关，最后一条为准）。
      // 少了它，页面上会出现"多模态：开"配"多模态已关 · 只发送了下面的识别文本"
      // 这种自相矛盾的截图。
      kind: 'session_tools',
      seq: 4,
      ts,
      web_search: false,
      multimodal: false,
    },
  ]);
  console.log(`  已种夹具会话 ${IMAGE_SESSION} / ${OCR_SESSION}（+2 个附件文件）`);
  return { image: IMAGE_SESSION, ocr: OCR_SESSION };
}

/* 夹具数据目录里没有接口档案时补一份**假的**（只为截图连贯）。
   没有它就没有可选模型，统计条的上下文占用必须显示 0%；这份档案让截图能验证
   正常的非零百分比。key 是**假**的（真去发一轮只会拿到 401，不会花钱），而且
   **只在文件不存在时写**：绝不覆盖别人自己那份配置。 */
function seedProviders(dataDir) {
  const file = path.join(dataDir, 'providers.json');
  if (existsSync(file)) {
    console.log('  · 数据目录里已有 providers.json —— 不动它');
    return;
  }
  const profile = {
    id: 'deepseek',
    label: 'DeepSeek',
    base_url: 'https://api.deepseek.com/v1',
    api_key: 'sk-shot-fixture-not-a-real-key',
    models: ['deepseek-flash'],
    thinking_options: [],
    send_reasoning_content: true,
    supports_tools: true,
    notes: '（截图夹具：这里的 key 是假的，只为了让截图里不出现"还没有可用的接口"）',
    type: 'openai',
    model_limits: {},
    vision_models: ['deepseek-flash'],
  };
  writeFileSync(file, `${JSON.stringify({
    providers: [profile],
    default: { provider: 'deepseek', model: 'deepseek-flash', effort: '' },
  }, null, 2)}\n`);
  console.log('  已补一份夹具接口档案（假 key）—— 否则截图里会出现"还没有可用的接口"');
}

/* 刷新页面 → 在侧栏里按标题找到那条夹具 → **用界面自己的入口**点进去
   （不直接调内部函数：截图要证明的是"用户点一下能看到什么"）。
   标题用**精确匹配**：`includes` 会把一条更长的真实会话也算进来。 */
async function openFixtureSession(title, url = `${BASE}/`) {
  await send('Page.navigate', { url });
  await waitFor(`document.querySelectorAll('.session').length > 0`, 15000, '侧栏会话列表');
  const opened = await evaluate(`(() => {
    const items = [...document.querySelectorAll('.session')];
    const title = ${JSON.stringify(title)};
    const hit = items.find((node) => {
      const node_title = node.querySelector('.session-title');
      return node_title && node_title.textContent === title;
    });
    if (!hit) return items.map((n) => n.textContent.trim());
    hit.click();
    return true;
  })()`);
  if (opened !== true) {
    throw new Error(`侧栏里没找到夹具会话 ${JSON.stringify(title)}，实际有：${JSON.stringify(opened)}`);
  }
}

/* 等气泡里的缩略图真的加载完（`complete` 之后才能截，否则拍到空白框） */
async function waitForThumbs(label) {
  await waitFor(
    `(() => {
      const imgs = [...document.querySelectorAll('.msg.user .attach-strip img')];
      return imgs.length > 0 && imgs.every((img) => img.complete && img.naturalWidth > 0);
    })()`,
    15000,
    label,
  );
}

async function verifyStatsBar(language) {
  const actual = await evaluate(`(() => {
    const text = (id) => (document.getElementById(id) || {}).textContent || '';
    return {
      turns: text('stats-turns'), speed: text('stats-speed'), tokens: text('stats-tokens'),
      cache: text('stats-cache'), context: text('stats-context'),
      meter: (document.getElementById('context-meter-fill') || {}).style.width || '',
      oldHints: document.querySelectorAll('#composer-hint, #attach-hint, #multimodal-hint').length,
      steps: !!document.getElementById('stats-steps'),
    };
  })()`);
  const expected = language === 'en'
    ? { turns: '1 turns', cache: 'Cache hit 95%' }
    : { turns: '1 轮', cache: '缓存命中 95%' };
  if (actual.turns !== expected.turns || actual.speed !== '30 tok/s'
    || actual.tokens !== '12.6k tok' || actual.cache !== expected.cache
    || actual.context !== '6%' || actual.meter !== '6%'
    || actual.oldHints !== 0 || actual.steps) {
    throw new Error(`会话统计条形状/数值不对：${JSON.stringify(actual)}`);
  }
  console.log('  会话统计条：', JSON.stringify(actual));
}

/* 切界面语言（ADR-085）。**走真接口**，而且收尾必须切回去 ——
   截图脚本顺手改掉别人数据目录里的语言设置是不可接受的。 */
async function setLanguage(language) {
  const response = await fetch(`${BASE}/api/settings/language`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ language }),
  });
  if (!response.ok) {
    throw new Error(`切语言失败（${language}）：HTTP ${response.status} ${await response.text()}`);
  }
  return (await response.json()).language;
}

async function currentLanguage() {
  const response = await fetch(`${BASE}/api/config`);
  return (await response.json()).language;
}

async function captureFixtureShots(dataDir) {
  const seeded = seedFixtures(dataDir);
  if (!seeded) return;

  // 与 docs/screenshots/chat-light.png 一样的 1440×900
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
  });

  /* 每种语言各出一份（ADR-085）：中文用原来的文件名（README 里引用的是它们），
     英文加 `-en`。**不能只手动塞两张英文图进 docs/** —— 那种图不能重拍，
     迟早会腐烂（ADR-073）。
     语言是**服务端**的设置，所以这里走接口切；每次 `openFixtureSession()` 都会
     重新导航，页面按新语言重新渲染（前端的 `<html lang>` 也跟着变，
     下面那条 OCR 断言正是按它选期望措辞的 —— 万一没切成功，那一轮会当场报错）。 */
  const original = await currentLanguage();
  try {
    for (const [language, suffix] of [['zh', ''], ['en', '-en']]) {
      await setLanguage(language);
      console.log(`  · 语言 ${language}：文档截图后缀 "${suffix || '(无)'}"`);

      // ① 缩略图那条（原来的第 4 张）
      await openFixtureSession(IMAGE_TEXT);
      await waitForThumbs('历史气泡里的缩略图加载完成');
      await verifyStatsBar(language);
      await sleep(300);
      await shot(`chat-attachments${suffix}`, DOCS_OUT);

      // ② OCR 那条：**默认折叠**的样子（契约 §13.7 第 3 句）
      await openFixtureSession(OCR_QUESTION);
      await waitFor(
        `(() => {
          const box = document.querySelector('.msg.user .ocr-block');
          const img = document.querySelector('.msg.user .attach-strip img');
          return !!box && box.classList.contains('collapsed')
            && !!img && img.complete && img.naturalWidth > 0;
        })()`,
        15000,
        '默认折叠的 OCR 气泡（折起来 + 缩略图加载完）',
      );
      await verifyStatsBar(language);
      // 折起来不等于看不见：标题里必须有状态摘要（这里就该是那段 OCR 文字的真实字数）
      const folded = await evaluate(`(() => {
        const label = document.querySelector('.msg.user .ocr-label');
        const preview = document.querySelector('.msg.user .ocr-preview');
        const chev = document.querySelector('.msg.user .ocr-head .chev');
        const toggle = document.getElementById('toggle-multimodal');
        return {
          label: label ? label.textContent : null,
          preview: preview ? preview.textContent : null,
          chev: chev ? chev.textContent : null,
          collapsed: !!document.querySelector('.msg.user .ocr-block.collapsed'),
          bodyVisible: !!document.querySelector('.msg.user .ocr-block .ocr-body'),
          multimodalLabel: toggle ? toggle.textContent.trim() : null,
        };
      })()`);
      console.log('  折叠态 OCR 气泡：', JSON.stringify(folded));
      /* 期望的措辞**随页面自己声明的语言走**（前端会设 `<html lang>`，ADR-085）。
         写死中文会让这个脚本在英文界面下永远失败 —— 而且失败信息看起来像"字数摘要算错了"，
         与真正的原因（界面是英文）毫无关系。加一张两种语言的期望表，两边都能拍。 */
      const EXPECT = {
        zh: { chars: '字', off: '多模态：关' },
        en: { chars: 'chars', off: 'Multimodal: off' },
      };
      const pageLang = String(await evaluate(`document.documentElement.lang || 'zh'`));
      if (pageLang.toLowerCase().startsWith('en') !== (language === 'en')) {
        throw new Error(`页面语言与刚设的语言不一致（设的是 ${language}，页面是 ${pageLang}）——`
          + '说明这一轮没有被重新渲染，截出来的图会是另一套语言');
      }
      const words = EXPECT[pageLang.toLowerCase().startsWith('en') ? 'en' : 'zh'];
      if (!new RegExp(`· ${OCR_CHARS} ${words.chars}$`).test(folded.label || '')) {
        throw new Error(`折叠标题里的字数摘要不对（语言 ${pageLang}，期望 · ${OCR_CHARS} ${words.chars}）：`
          + JSON.stringify(folded.label));
      }
      if (folded.chev !== '▸') throw new Error(`折叠时箭头应当是 ▸，实际 ${folded.chev}`);
      // 截图里不许出现自相矛盾：气泡说"多模态已关"，右上角那个开关也得是关。
      if (!new RegExp(words.off).test(folded.multimodalLabel || '')) {
        throw new Error(`夹具会话的开关没恢复成"关"（会话里那条 session_tools 没写进去？）：`
          + JSON.stringify(folded.multimodalLabel));
      }
      await sleep(300);
      await shot(`chat-multimodal-ocr${suffix}`, DOCS_OUT);
    }
  } finally {
    // 把语言切回原样：截图不该留下副作用（数据目录里的 settings.json 也一并回到原值）
    const restored = await setLanguage(original);
    console.log(`  · 语言已切回 ${restored}`);
  }
  await send('Emulation.clearDeviceMetricsOverride');
}

/* 可选（`SHOT_VARIANTS=1`）：把折叠态在**深色主题**与**窄屏**下各拍一张，
   落进 outDir（**绝不写 docs/**）。这两样正是 DOM 自检证明不了、又最容易出问题的
   地方（标题会不会挤成两行、预览会不会被撑破、`.ocr-block` 的虚线框在深色底上
   还看不看得出来）。默认不跑：日常重拍只该产出上面那五张。 */
async function captureOcrVariants() {
  if (!process.env.SHOT_VARIANTS) {
    console.log('  · 跳过深色 / 窄屏对照图（要设 SHOT_VARIANTS=1；它们只落在 outDir）');
    return;
  }
  // 深色主题走 `?theme=dark` 深链（app.js 里就是为截图留的），1440×900 先看一整屏
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
  });
  await openFixtureSession(OCR_QUESTION, `${BASE}/?theme=dark`);
  await waitFor(`!!document.querySelector('.msg.user .ocr-block.collapsed')`, 15000,
    '深色主题下的折叠 OCR 气泡');
  await sleep(300);
  await shot('variant-ocr-dark');

  // 窄一点的窗口：标题 + 状态摘要 + 预览三行都在同一个宽度里挤压
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1120, height: 820, deviceScaleFactor: 1, mobile: false,
  });
  await sleep(300);
  await shot('variant-ocr-narrow-dark');

  // 浅色窄屏（`?theme=light` 会把 localStorage 里的主题也改掉，收尾时清回来）
  await openFixtureSession(OCR_QUESTION, `${BASE}/?theme=light`);
  await waitFor(`!!document.querySelector('.msg.user .ocr-block.collapsed')`, 15000,
    '窄屏（浅色）下的折叠 OCR 气泡');
  await sleep(300);
  await shot('variant-ocr-narrow-light');
  await evaluate(`localStorage.removeItem('theme')`);
  await send('Emulation.clearDeviceMetricsOverride');
  console.log(`  （对照图在 ${OUT}：variant-ocr-dark / variant-ocr-narrow-dark / variant-ocr-narrow-light）`);
}

/* ── ⑦ 设置 → 接口：模型表格（ADR-092）────────────────────────────────────
   一行一个模型 = 模型 ID / 上下文上限 / 显示名，行尾删除、底部「添加模型」。

   这张图要证明的是**形态**，所以拍之前先断言：卡片里真的有两行、表头写着三个
   列名、「添加模型」在、而且老的「模型（逗号分隔）」与「每行 model=上限」两个框
   **已经没了** —— 否则图里可能是"两处真相并存"，而它看起来同样像拍成功了。

   夹具里那份接口只有一个模型（见 `seedProviders`），看不出这张表的样子，所以这里
   **用真接口**把它改成两个模型（带一个上限、一个显示名）。走的是页面自己也会走的
   那条 `/api/providers/save`，而不是往 JSON 里塞一份手写的世界 —— 后者会拍出
   "代码还不支持的形状"。 */
async function captureProviderPane(dataDir) {
  await assertFixtureTarget();
  seedProviders(dataDir);
  await setLanguage('zh');
  const saved = await fetch(`${BASE}/api/providers/save`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: 'deepseek',
      models: ['deepseek-flash', 'deepseek-v4-pro'],
      model_limits: { 'deepseek-v4-pro': 1000000 },
      model_labels: { 'deepseek-flash': 'DeepSeek V4.1 Flash' },
    }),
  });
  if (!saved.ok) throw new Error(`夹具接口没保存成：HTTP ${saved.status}`);

  let nav = 0;
  const shoot = async (query, name) => {
    // 每次导航的 query 都不一样（`?shot=N`）：**只差 hash 的导航是同文档跳转，
    // 页面不会重新加载**，而界面语言是加载时从服务端读的 —— 第一版就是这样拍到了
    // 一张"挂着中文环境、画着英文表头"的图。带一个变化的 query 才能保证整页重来。
    nav += 1;
    await send('Page.navigate', {
      url: `${BASE}/${query}${query ? '&' : '?'}shot=${nav}#settings/api`,
    });
    await waitFor(
      `document.querySelectorAll('.provider-card .model-row').length >= 2`,
      15000,
      '接口卡片的模型表格',
    );
    const shape = await evaluate(`(() => {
      // **必须挑带 data-id 的那张卡片**：页面上第一张 .provider-card 是
      // 「新会话默认」那一块（只有下拉框，没有模型行）—— 第一版就抓错了它。
      const card = document.querySelector('.provider-card[data-id]');
      if (!card) return { missing: true };
      const rows = [...card.querySelectorAll('.model-row')];
      return {
        rows: rows.length,
        values: rows.map((row) => [...row.querySelectorAll('input')].map((input) => input.value)),
        head: (card.querySelector('.model-head') || {}).textContent || '',
        legacy: card.querySelectorAll('[data-f="models"], [data-f="model_limits"]').length,
        add: !!card.querySelector('[data-act="model-add"]'),
      };
    })()`);
    if (shape.missing) throw new Error('页面上没有 .provider-card（接口页没画好）');
    if (shape.legacy !== 0) throw new Error('老的两个输入框还在 —— 图会拍成两处真相');
    if (shape.rows !== 2) throw new Error(`夹具应该两行，实际 ${shape.rows} 行`);
    if (!shape.add) throw new Error('「添加模型」按钮不在');
    if (!/上下文上限/.test(shape.head) || !/显示名/.test(shape.head)) {
      throw new Error(`表头没写清列名：${shape.head}`);
    }
    console.log(`  · 接口页（${name}）：${JSON.stringify(shape.values)}`);
    await shot(name, DOCS_OUT);
  };

  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
  });
  await shoot('', 'provider-models');
  await shoot('?theme=dark', 'provider-models-dark');
  await evaluate(`localStorage.removeItem('theme')`);
  await send('Emulation.clearDeviceMetricsOverride');
}

/* ── ⑥ 设置 → 提示词（docs/13-prompts-page.md）：浅色 + 深色 + 展开一组 + 两张
      可编辑（docs/14-editable-prompts.md §8）共五张 ──
   这一页不依赖任何接口调用，所以它**不**需要 `dataDir` 就能拍；但工具那一栏的
   on / off 状态是按配置算出来的，所以拍之前照旧建议指向一次性夹具目录
   （`seed-vault.ps1` 造出来的那份：联网没配 → 三种状态都能看到）。

   拍之前**先把这一页验一遍**（README 那条规矩：断言不过就不写文件）。
   验的是"图里真的会有东西"：22 条提示词 + 20 个工具、**7 个分组默认全折**、
   原文默认折叠、页面上零 JS 报错。前两张（浅色 / 深色）拍的就是这个**默认态**
   —— 用户报的问题正是"折起来之前整页太长"，所以图必须是折着的；
   第三张 `prompts-page-group-open` 才展开第一个分组（连同它里面第一条的原文），
   让人看清展开之后的样子；第四张 `prompts-page-editing` 点开「编辑」，
   第五张 `prompts-page-overridden` 是**真种了一条覆盖**之后的样子
   （「已修改」+「复原」+ 一行 warn）—— 后两张的形态断言也在写文件之前跑。

   五张图落在 `PROMPTS_OUT`（默认项目根的 `.tmp-shots/`，**不写 docs/**）。
   深色走 `?theme=dark` 深链（app.js 里为截图留的就是它）：那是**整页加载**，
   才会真的执行 initTheme()；只改 hash 的话主题不会变。收尾把 localStorage 里
   的主题与语言都恢复原样（`--user-data-dir` 是脚本自己的 profile，别人的不受影响），
   并且**把种下去的那条覆盖复原回去**（夹具不留状态）。 */

/* 说明：既有那套消息处理只管 `message.id`，**不记页面报错**。
   这一页比其它几张更依赖"JS 一路上没炸"（22+20 个节点都是脚本建出来的），
   所以这里自己挂一个监听，把报错收进这个数组，每次截图前断言它还是空的。
   写在函数外面：断言那一步是**另一个函数**（assertPromptsPage），
   收在 capturePromptsShots 里它看不见（第一版就这么写，当场 ReferenceError）。 */
const PROMPT_PROBLEMS = [];

/* ── 提示词覆盖：第 4/5 张要用的**真写入** ────────────────────────────────
   界面上那个「已修改」标记、那行 warn、以及页首"两门语言各改了几条"，全都是
   按服务端回的 `overridden` / `warnings` / `overrides` 画的。所以"一条被改过的
   样子"这张图必须**真的有一条覆盖**在夹具里 —— 用假 DOM 画一个标记出来不叫验证。
   流程：先确认夹具里没有覆盖 → 经真接口种一条（删掉一个占位符，好让后端真的
   给一句 warning）→ 重载页面 → 拍 → **无论拍没拍成都复原** → 再核对一次。
   `assertFixtureTarget()` 已经在脚本开头挡住了真实 data/。 */
async function apiPrompts() {
  const resp = await fetch(`${BASE}/api/prompts`);
  if (!resp.ok) throw new Error(`GET /api/prompts 失败：${resp.status}`);
  return resp.json();
}

async function postPromptApi(path, payload) {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return { status: resp.status, body: await resp.json().catch(() => ({})) };
}

/** 现在还有哪些语言留着覆盖（读真接口的 `overrides`）。 */
async function promptOverridesLeft() {
  const live = await apiPrompts();
  return Object.entries(live.overrides || {})
    .filter(([, ids]) => (ids || []).length)
    .map(([language, ids]) => `${language}:${ids.join(',')}`);
}

/** 拍之前：夹具里**不该有**覆盖。真留着（上一次跑崩了）就先说清楚再清掉 ——
 *  这台服务指着一个一次性夹具目录（开头那道门），留着它只会让默认态那几张图
 *  莫名其妙多出一个「已修改」标记。 */
async function clearLeftoverPromptOverrides() {
  const left = await promptOverridesLeft();
  if (!left.length) return;
  console.log(`  · ⚠ 夹具里留着上一次的提示词覆盖（${left.join(' ')}）：先复原掉再拍`);
  const live = await apiPrompts();
  for (const language of Object.keys(live.overrides || {})) {
    if ((live.overrides[language] || []).length) {
      await postPromptApi('/api/prompts/revert', { language });
    }
  }
  const still = await promptOverridesLeft();
  if (still.length) throw new Error(`清不掉这些提示词覆盖：${still.join(' ')}`);
}

/** 页面第一条卡片对应的是哪一条提示词（第一个分组的第一个条目）。 */
async function firstLivePromptItem() {
  const live = await apiPrompts();
  const items = (live.groups || []).flatMap((group) => group.items);
  if (!items.length) throw new Error('/api/prompts 一条提示词都没给');
  return items[0];
}

/** 种一条**真有后果**的覆盖：把内置正文里第一个占位符删掉 —— 后端会因此
 *  给一句 warning（"这些占位符不在了…"），图上那行 warn 才是真的。
 *  只删、不新增：新增一个内置里没有的占位符会被后端 400 拒掉。
 *  ⚠ 替换文本里**不能**再出现那个占位符：占位符集合是从正文里数出来的，
 *    留着它（哪怕写在括号里）就等于没删，后端一声不吭（第一版就这么写的，
 *    图上那行 warn 是空的）。 */
async function seedPromptOverride() {
  const live = await apiPrompts();
  const items = (live.groups || []).flatMap((group) => group.items);
  const target = items[0];
  if (!target) throw new Error('/api/prompts 一条提示词都没给');
  const token = (String(target.default_text).match(/\{[A-Za-z_$][\w$]*\}/) || [])[0] || '';
  const text = token
    ? String(target.default_text).replace(token, '（这里原本是一个会被填进来的占位符）')
    : `${target.default_text}\n（截图夹具改的一行）\n`;
  const saved = await postPromptApi('/api/prompts/update',
    { language: live.language, id: target.id, text });
  if (saved.status !== 200) {
    throw new Error(`种一条提示词覆盖失败：${saved.status} ${JSON.stringify(saved.body)}`);
  }
  console.log(`  · 种了一条覆盖：${live.language}/${target.id}`
    + `（删掉占位符 ${token || '（这一条没有占位符，改成追加一行）'}，好让图上那行 warn 是真的）`);
  return { language: live.language, id: target.id, text, warnings: saved.body.warnings || [] };
}

/** 复原**我们自己种的那一条**，并核对真的复原了（夹具不能留下状态）。 */
async function revertSeededOverride(seeded) {
  const back = await postPromptApi('/api/prompts/revert',
    { language: seeded.language, id: seeded.id });
  console.log(`  · 已复原 ${seeded.language}/${seeded.id}（cleared=${back.body.cleared}）`);
  const left = await promptOverridesLeft();
  if (left.length) throw new Error(`复原之后夹具里还剩着覆盖：${left.join(' ')}`);
}

async function capturePromptsShots() {
  mkdirSync(PROMPTS_OUT, { recursive: true });
  PROMPT_PROBLEMS.length = 0;
  ws.addEventListener('message', (event) => {
    let message = null;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.method === 'Runtime.exceptionThrown') {
      PROMPT_PROBLEMS.push(`页面抛异常：${message.params && message.params.exceptionDetails
        && message.params.exceptionDetails.text}`);
    }
    if (message.method === 'Runtime.consoleAPICalled'
      && message.params && message.params.type === 'error') {
      PROMPT_PROBLEMS.push(`console.error：${(message.params.args || [])
        .map((arg) => arg.value || arg.description || '').join(' ')}`);
    }
  });

  // 尺寸与 docs/screenshots/chat-light.png 那一档一致
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
  });

  const originalLang = await currentLanguage();
  await setLanguage('zh');   // 下面断言的是中文措辞；收尾切回去
  // 回读一次：语言是**服务端**的设置，如果同时有别人（比如 e2e-live）也在切它，
  // 这里当场说清楚，而不是让下面那条"页面语言不是中文"的断言去背这个锅 ——
  // 那两句话看起来一模一样，但一个要改脚本、一个要改"谁在跟我抢这台服务"。
  const pinned = await currentLanguage();
  if (pinned !== 'zh') {
    throw new Error(`把语言钉成 zh 之后回读到的却是 ${pinned} —— `
      + '这台服务上还有别人在切语言（同一台服务被两个自检共用了？）。'
      + '提示词页的图必须是中文的，换个端口重跑。');
  }
  try {
    // 拍之前先确认夹具里**没有**覆盖：浅色 / 深色那几张拍的是默认态，
    // 一条遗留的覆盖会让图上凭空多出一个「已修改」标记
    await clearLeftoverPromptOverrides();

    // 浅色第一张：**分组全是默认的折叠态**（契约 §5 第 2 条）。拍之前断言 7 组
    // 都不带 `open` —— 否则拍到的可能是"某次展开之后"的样子（用户要看的就是折起来有多短）
    await capturePromptsPage('');
    await shot('prompts-page', PROMPTS_OUT);

    // 第三张：把**第一个分组**展开（顺带展开它里面第一条提示词的原文），
    // 让人看清"展开之后长什么样"。这一张**刻意不是**默认态，所以单独一张。
    await openFirstGroupAndPrompt();
    await assertGroupOpenShape();
    await shot('prompts-page-group-open', PROMPTS_OUT);

    // 第四张：编辑器打开的样子（就在刚展开的那一条上点「编辑」）
    const firstItem = await firstLivePromptItem();
    await openFirstPromptEditor();
    await assertEditorShape(firstItem);
    await shot('prompts-page-editing', PROMPTS_OUT);
    await closePromptEditor();

    // 第五张：一条**真被改过**的样子（种一条覆盖 → 重载 → 拍 → 立刻复原）
    await captureOverriddenShot();

    // 深色：`?theme=dark` 深链（整页加载，initTheme 才会读它），再走 hash 到这一页。
    // 同样是**全折叠**的默认态（与浅色那张配成一对）。
    await send('Page.navigate', { url: `${BASE}/?theme=dark#settings/prompts` });
    await waitFor(`document.documentElement.dataset.theme === 'dark'`, 20000, '深色主题生效');
    await closeAnyDialog();
    await preparePromptsPage();
    await assertPromptsPage('dark');
    await shot('prompts-page-dark', PROMPTS_OUT);

    // 收尾：把深链留下的主题清掉（与 captureOcrVariants 同一条规矩）
    await evaluate(`localStorage.removeItem('theme')`);
  } finally {
    await setLanguage(originalLang);
    console.log(`  · 语言已切回 ${originalLang}`);
    await send('Emulation.clearDeviceMetricsOverride');
  }
}

/** 把可能还开着的模态框收掉。
 *  ⚠ 这一步是**必须**的：前三张里第 3 张会点开「格式化并初始化 dream」的确认框，
 *  而那个框**不会自己关**（`#dialog` 是一层固定定位的遮罩）。不收掉就往下拍，
 *  第 6 张会是一张"提示词页被对话框盖住一半"的图 —— 而且它看起来像是拍成功了
 *  （页面确实在下面渲染着）。我第一版就是这么截出来的，肉眼看图才发现。 */
async function closeAnyDialog() {
  const wasOpen = await evaluate(`(() => {
    const dialog = document.getElementById('dialog');
    if (!dialog || dialog.classList.contains('hidden')) return false;
    const cancel = document.getElementById('dialog-cancel');
    if (cancel) cancel.click();
    return true;
  })()`);
  if (wasOpen) console.log('  · 先把上一步留下的对话框关掉，再拍提示词页');
  await waitFor(`(() => {
    const dialog = document.getElementById('dialog');
    return !dialog || dialog.classList.contains('hidden');
  })()`, 10000, '对话框已关闭');
}

/** 打开 `#settings/prompts`（走 hash 路由，不点侧栏 —— 这一页的入口在设置里换过位置，
 *  hash 是它真正的地址），等它画好，然后**断言它是默认的折叠态**（7 个分组都不带
 *  `open`）。浅色 / 深色那两张拍的就是这个默认态 —— 用户要看的正是"折起来有多短"。 */
async function capturePromptsPage(query) {
  await closeAnyDialog();
  await send('Page.navigate', { url: `${BASE}/${query}#settings/prompts` });
  await preparePromptsPage();
  await assertPromptsPage('light');
}

/** 展开**第一个分组**（原生 `<details>`：点它的 `<summary>`）。
 *  这条路可能还挂着一次没有 await 的整页重画（会把开合重置回默认），
 *  所以点完等一下再看一眼：被折回去就再点一次，最多三轮 —— 否则拍出来的是
 *  "分组其实没开"的图，而它看起来像是拍成功了。 */
async function openFirstGroup() {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const outcome = await evaluate(`(() => {
      const group = document.querySelector('.prompts-group');
      if (!group) return 'nope';
      const head = group.querySelector('summary');
      if (!head) return 'nope';
      if (!group.hasAttribute('open')) head.click();
      return group.hasAttribute('open') ? 'open' : 'closed';
    })()`);
    if (outcome === 'nope') throw new Error('页面上没有分组（提示词页没画好）');
    await sleep(400);
    const openCount = await evaluate(
      `document.querySelectorAll('.prompts-group[open]').length`);
    if (openCount === 1) return;
    console.log(`  · 分组开了又被重画折回去（第 ${attempt} 次），再点一次`);
  }
  throw new Error('点开第一个分组之后总被整页重画折回去 —— 不写这一张');
}

/** 展开第一个分组 + 它里面第一条提示词的原文；重画会把两步一起重置，所以整轮重试。 */
async function openFirstGroupAndPrompt() {
  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      await openFirstGroup();
      await expandFirstPrompt();      // 里面自带"<pre> 真的可见"的等待
      await assertGroupOpenShape();
      return;
    } catch (err) {
      lastError = err;
      console.log(`  · 展开这一步第 ${attempt} 次没过：${(err && err.message) || err}`);
      await preparePromptsPage();
      await sleep(400);
    }
  }
  throw lastError;
}

/** 展开**第一条**提示词的原文：图里要看得见真实的正文，而不是一排折叠条。 */
async function expandFirstPrompt() {
  const expanded = await evaluate(`(() => {
    const box = document.querySelector('[data-prompt-id] .prompt-details');
    if (!box || !box.classList.contains('collapsed')) return false;
    box.querySelector('.pd-head').click();
    return true;
  })()`);
  if (expanded !== true) throw new Error('第一条提示词的折叠块不是折叠态，展开这一步没做成');
  await waitFor(`(() => {
    const pre = document.querySelector('[data-prompt-id] .prompt-details:not(.collapsed) pre');
    return !!pre && pre.getClientRects().length > 0;
  })()`, 10000, '第一条提示词的原文已展开（<pre> 真的可见）');
  await sleep(300);
  await assertFirstPromptExpanded();
}

/** ── 写操作前的第一道门：这块数据是夹具吗？──────────────────────────────
 *  这个脚本**会写盘**：
 *    * `captureFixtureShots()` 往数据目录里种两条夹具会话（`sessions/*.jsonl`
 *      ＋ `attachments/…`）、可能补一份假的 `providers.json`；
 *    * 切语言那一步会写 `settings.json`（语言是**服务端**的运行期设置）。
 *  所以它必须先问服务端"你的数据目录是哪个"，**不像夹具就直接不跑**。
 *
 *  为什么要这一道（三条既有规矩叠在一起）：
 *   * ADR-050：破坏性/写操作前必须校验"这是不是我们该动的那块数据"；
 *   * ADR-080：能预防就绝不要靠提示 —— 文档里写十遍"别指向真实 data/"，不如让它
 *     **跑不起来**（下面 `seedFixtures()` 里那句"拒绝往 data/ 种"就是同一件事，
 *     但那只挡住了种子；切语言那条路当时是**没有门**的）；
 *   * ADR-069：代码干净 ≠ 产物干净 —— 脚本自己写出来的 settings.json 也是产物。
 *
 *  判据（`/api/config` 的 `data_dir`，项目内显示成相对路径）：路径里必须有一段
 *  以 `tmp-` / `.tmp-` 开头（`seed-vault.ps1` 造的 `.tmp-ui-check\data` 正是这种）。
 *  真需要对着别处跑：`E2E_ALLOW_REAL_DATA=1`。
 *
 *  ⚠ `e2e-live.mjs` 里有一份**逐字相同**的拷贝（两个脚本各自独立、都不 import 别的
 *  文件，这是这个目录一贯的写法）。改这里记得两边一起改。 */
async function assertFixtureTarget() {
  let config;
  try {
    config = await (await fetch(`${BASE}/api/config`)).json();
  } catch (err) {
    throw new Error(`连不上 ${BASE}（${(err && err.message) || err}）——`
      + '截图需要一个**指向夹具数据目录**的服务器，先起来再跑');
  }
  const shown = String(config.data_dir || '(没给)');
  const normalized = shown.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  const looksLikeFixture = /(^|\/)[.]?tmp[-._]/.test(normalized);
  if (looksLikeFixture || process.env.E2E_ALLOW_REAL_DATA === '1') {
    console.log(`  · 目标数据目录：${shown}`
      + (looksLikeFixture ? '' : '（E2E_ALLOW_REAL_DATA=1：**明知不是夹具仍然继续**）'));
    return shown;
  }
  throw new Error(
    `拒绝运行：这台服务器的数据目录是 ${shown}，它**不像夹具**。\n`
    + '  这个脚本会写盘（种夹具会话、切语言时写 settings.json），所以只该对着\n'
    + '  一次性夹具目录跑。正确用法：\n'
    + '    powershell -File scripts\\ui_check\\seed-vault.ps1 -DataDir .tmp-ui-check\\data\n'
    + '    set HARNESS_DATA_DIR=.tmp-ui-check\\data\n'
    + '    python -m harness.cli --web --port 8791 --no-open\n'
    + '  确实要对着这个目录跑（你知道自己在做什么）：set E2E_ALLOW_REAL_DATA=1',
  );
}

/** 截图**前一刻**再确认一次"第一条的原文还开着"。
 *  这一步是补出来的：展开之后这一页还会再画一遍（路由那条路径没有 await 的
 *  `loadPrompts` 回来时会重画整页，折叠态被重置成默认的"折起来"），
 *  于是图里**看不到正文**，而先前的断言是在重画之前跑过的 —— 图"看起来拍成了"。
 *  所以判据要放在这里，而不是只放在展开之后。
 *  （现在它只服务 `prompts-page-group-open` 那一张：浅色 / 深色两张拍的正是
 *  **默认的全折叠态**，不展开任何东西。） */
async function assertFirstPromptExpanded() {
  const shape = await evaluate(`(() => {
    const box = document.querySelector('[data-prompt-id] .prompt-details');
    const pre = box && box.querySelector('pre');
    return {
      collapsed: !!(box && box.classList.contains('collapsed')),
      preVisible: !!(pre && pre.getClientRects().length > 0),
      firstCardTop: box ? Math.round(box.getBoundingClientRect().top) : null,
      scrollTop: Math.round((document.querySelector('.settings-body .scroll-area')
        || document.getElementById('prompts-body') || {}).scrollTop || 0),
    };
  })()`);
  if (shape.collapsed || !shape.preVisible) {
    throw new Error('拍这一张之前，第一条提示词的原文又折回去了（页面重画过一次）——'
      + `不写这张图：${JSON.stringify(shape)}`);
  }
}

/** 等这一页的骨架齐了（22 条提示词 + 20 个工具都是脚本建出来的，慢机器上要多等）。 */
async function preparePromptsPage() {
  await waitFor(`document.querySelectorAll('[data-prompt-id]').length === 22
    && document.querySelectorAll('[data-tool-id]').length === 20`, 25000,
  '提示词页画完（22 条提示词 + 20 个工具）');
}

/** 截图前的断言：**不过就不写文件**。返回一份"图里会有什么"的形状，顺手打到日志里。 */
async function assertPromptsPage(theme) {
  const state = await evaluate(`(() => {
    const body = document.getElementById('prompts-body');
    const cards = [...document.querySelectorAll('[data-prompt-id]')];
    const first = cards[0];
    const box = first && first.querySelector('.prompt-details');
    const pre = box && box.querySelector('pre');
    const table = document.querySelector('[data-tool-id] table.prompt-params');
    const chips = [...document.querySelectorAll('.prompts-state')].map((n) => n.textContent.trim());
    const groups = [...document.querySelectorAll('.prompts-group')];
    return {
      lang: document.documentElement.lang || '',
      datasetTheme: document.documentElement.dataset.theme || '(跟随系统)',
      promptCards: cards.length,
      toolCards: document.querySelectorAll('[data-tool-id]').length,
      groups: groups.length,
      groupsCollapsed: groups.filter((group) => !group.hasAttribute('open')).length,
      groupSummary: groups.slice(0, 2).map((group) => (group.querySelector('summary') || {}).textContent || ''),
      hidden: !!(body && body.closest('.settings-pane').classList.contains('hidden')),
      dialogOpen: (() => {
        const dialog = document.getElementById('dialog');
        return !!dialog && !dialog.classList.contains('hidden');
      })(),
      firstBoxCollapsed: !!(box && box.classList.contains('collapsed')),
      firstPreVisible: !!(pre && pre.getClientRects().length > 0),
      preWhiteSpace: pre ? getComputedStyle(pre).whiteSpace : '',
      tableRows: table ? table.rows.length : 0,
      stateChips: [...new Set(chips)],
      summary: (body && body.querySelector('.pane-note') || {}).textContent || '',
      overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      scrollHeight: body ? body.scrollHeight : 0,
    };
  })()`);
  console.log(`  提示词页（${theme}）：`, JSON.stringify(state, null, 2));
  const bad = [];
  if (!String(state.lang).toLowerCase().startsWith('zh')) bad.push(`页面语言不是中文（${state.lang}）`);
  if (state.hidden) bad.push('提示词那一页还藏着（路由没生效）');
  if (state.dialogOpen) bad.push('有一个对话框还开着，会盖住这一页');
  if (state.promptCards !== 22) bad.push(`data-prompt-id 应有 22 个，实际 ${state.promptCards}`);
  if (state.toolCards !== 20) bad.push(`data-tool-id 应有 20 个，实际 ${state.toolCards}`);
  if (state.groups !== 7) bad.push(`分组应有 7 个，实际 ${state.groups}`);
  // 这两张拍的是**默认的折叠态**（契约 §5 第 2 条）：有任何一个分组带着 open 就说明
  // 拍到的不是默认样子 —— 而这正是用户要看的那件事（折起来之后整页有多短）。
  if (state.groupsCollapsed !== 7) {
    bad.push(`7 个分组默认都该折着，实际折着 ${state.groupsCollapsed} 个`);
  }
  if (!state.firstBoxCollapsed) bad.push('第一条提示词的原文没有处于折叠态');
  if (state.firstPreVisible) bad.push('折叠着的时候 <pre> 却可见 —— 折叠没生效，图上会是一片长文');
  if (PROMPT_PROBLEMS.length) bad.push(`页面报错：${PROMPT_PROBLEMS.slice(0, 3).join(' | ')}`);
  if (bad.length) {
    throw new Error(`提示词页没画好，**不写这张图**：${bad.join('；')}`);
  }
}

/* ── 第 4/5 张（可编辑提示词）的形态断言：**不过就不写文件** ────────────── */

/** 点开第一条提示词的「编辑」按钮，等编辑器（textarea）真的出现。 */
async function openFirstPromptEditor() {
  const clicked = await evaluate(`(() => {
    const button = document.querySelector('[data-prompt-id] .pc-edit');
    if (!button) return false;
    button.click();
    return true;
  })()`);
  if (!clicked) throw new Error('第一条提示词上没有「编辑」按钮（提示词页没画好？）');
  await waitFor(`!!document.querySelector('[data-prompt-id] textarea.pc-editor-area')`, 10000,
    '编辑器出现（textarea）');
  await sleep(250);
}

/** 收掉编辑器（点「取消」），后面的图才不会被它挡着。 */
async function closePromptEditor() {
  const closed = await evaluate(`(() => {
    const button = document.querySelector('[data-prompt-id] .pc-cancel');
    if (!button) return false;
    button.click();
    return true;
  })()`);
  if (!closed) throw new Error('编辑器里没有「取消」按钮');
  await waitFor(`!document.querySelector('textarea.pc-editor-area')`, 10000, '编辑器已收起');
  await sleep(200);
}

/** `prompts-page-editing` 拍之前的形态：**恰好一个**编辑器、里面是这一条当前生效的
 *  正文、三句提示都在、保存 / 取消都在、而且它在视口里看得见（否则图上是空的）。 */
async function assertEditorShape(item) {
  const shape = await evaluate(`(() => {
    const card = document.querySelector('[data-prompt-id]');
    const area = card && card.querySelector('textarea.pc-editor-area');
    const rect = area ? area.getBoundingClientRect() : null;
    const save = card && card.querySelector('.pc-save');
    const cancel = card && card.querySelector('.pc-cancel');
    return {
      cardId: card ? card.dataset.promptId : '',
      editors: document.querySelectorAll('textarea.pc-editor-area').length,
      value: area ? area.value : '',
      hints: card ? [...card.querySelectorAll('.pc-hint')].map((n) => n.textContent.trim()) : [],
      save: save ? save.textContent.trim() : '',
      cancel: cancel ? cancel.textContent.trim() : '',
      visible: !!(rect && rect.height > 80 && rect.width > 200 && rect.top < 900),
      editButtons: document.querySelectorAll('.pc-edit').length,
      detailsBlocks: card ? card.querySelectorAll('.prompt-details').length : -1,
    };
  })()`);
  console.log('  编辑器打开的样子：', JSON.stringify({
    ...shape, value: `${String(shape.value).slice(0, 30)}…（共 ${String(shape.value).length} 字符）`,
  }));
  const bad = [];
  if (shape.cardId !== item.id) bad.push(`第一条卡片是 ${shape.cardId}，接口说第一条是 ${item.id}`);
  if (shape.editors !== 1) bad.push(`页面上应当**恰好**一个编辑器，实际 ${shape.editors} 个`);
  if (shape.value !== item.text) bad.push('textarea 里装的不是这一条当前生效的正文');
  if (shape.hints.length !== 3) bad.push(`三句提示应当都在，实际 ${shape.hints.length} 句`);
  if (shape.hints.some((line) => !line)) bad.push('有一句提示是空的');
  if (shape.save !== '保存' || shape.cancel !== '取消') {
    bad.push(`「保存 / 取消」按钮不对：${shape.save} / ${shape.cancel}`);
  }
  if (!shape.visible) bad.push('编辑器在视口里看不见（这张图上会是空的）');
  if (shape.editButtons !== 21) bad.push(`编辑态里其余 21 条还该有「编辑」，实际 ${shape.editButtons} 个`);
  if (shape.detailsBlocks !== 0) bad.push('编辑态里不该还并排摆着正文折叠块');
  if (PROMPT_PROBLEMS.length) bad.push(`页面报错：${PROMPT_PROBLEMS.slice(0, 3).join(' | ')}`);
  if (bad.length) throw new Error(`编辑器这一张不对劲，**不写这张图**：${bad.join('；')}`);
}

/** `prompts-page-overridden` 拍之前的形态：这一条带「已修改」+「复原」+ 一行 warn、
 *  两个折叠块（当前生效 / 内置原文）都在，页首把两门语言的计数分别写出来。 */
async function assertOverriddenShape(seeded) {
  const shape = await evaluate(`(() => {
    const card = document.querySelector('[data-prompt-id]');
    const badge = document.querySelector('.pc-edited');
    const revert = document.querySelector('.pc-revert');
    const warn = document.querySelector('.pc-warning');
    const line = document.querySelector('.prompts-overrides');
    const revertRect = revert ? revert.getBoundingClientRect() : null;
    const warnRect = warn ? warn.getBoundingClientRect() : null;
    const all = document.querySelector('.prompts-revert-all');
    return {
      cardId: card ? card.dataset.promptId : '',
      editors: document.querySelectorAll('textarea.pc-editor-area').length,
      badge: badge ? badge.textContent.trim() : '',
      revert: revert ? revert.textContent.trim() : '',
      revertVisible: !!(revertRect && revertRect.top < 900),
      warn: warn ? warn.textContent.trim() : '',
      warnVisible: !!(warnRect && warnRect.top < 900),
      overrides: line ? line.textContent.trim() : '',
      details: card ? card.querySelectorAll('.prompt-details').length : -1,
      labels: card ? [...card.querySelectorAll('.pd-label')].map((n) => n.textContent.trim()) : [],
      builtinText: card ? [...card.querySelectorAll('.prompt-text')]
        .some((n) => n.classList.contains('prompt-builtin')) : false,
      editButton: !!(card && card.querySelector('.pc-edit')),
      revertAll: all ? { text: all.textContent.trim(), disabled: all.disabled } : null,
      groupsOpen: document.querySelectorAll('.prompts-group[open]').length,
    };
  })()`);
  console.log('  一条被改过的样子：', JSON.stringify(shape));
  const bad = [];
  if (shape.cardId !== seeded.id) bad.push(`第一条卡片是 ${shape.cardId}，种下去的是 ${seeded.id}`);
  if (shape.editors !== 0) bad.push('这一张不是编辑态：不该有 textarea');
  if (shape.badge !== '已修改') bad.push(`「已修改」标记不对：${shape.badge}`);
  if (shape.revert !== '复原') bad.push(`「复原」按钮不对：${shape.revert}`);
  if (!shape.revertVisible) bad.push('「复原」按钮不在视口里（图上拍不到）');
  if (!shape.warn) bad.push('少了占位符却没有那行 warn —— 后端那句警告没画出来');
  if (shape.warn && !/\{/.test(shape.warn)) bad.push(`那行 warn 里没点名占位符：${shape.warn}`);
  if (!shape.warnVisible) bad.push('那行 warn 不在视口里（图上拍不到）');
  if (!/中文已改 1 条/.test(shape.overrides) || !/英文已改 0 条/.test(shape.overrides)) {
    bad.push(`页首那行没把两门语言分别写出来：${shape.overrides}`);
  }
  if (shape.details !== 2) bad.push(`被改过时应当有两个折叠块，实际 ${shape.details} 个`);
  if (shape.labels.join('/') !== '当前生效/内置原文') {
    bad.push(`两个折叠块的标题不对：${shape.labels.join('/')}`);
  }
  if (!shape.builtinText) bad.push('「内置原文」那一块不在');
  if (!shape.editButton) bad.push('这一条少了「编辑」按钮');
  if (!shape.revertAll || shape.revertAll.disabled) bad.push('顶部那个「全部复原」应当可点');
  if (shape.groupsOpen !== 1) bad.push(`这一张应当**恰好**展开一个分组，实际 ${shape.groupsOpen} 个`);
  if (PROMPT_PROBLEMS.length) bad.push(`页面报错：${PROMPT_PROBLEMS.slice(0, 3).join(' | ')}`);
  if (bad.length) throw new Error(`"被改过"这一张不对劲，**不写这张图**：${bad.join('；')}`);
}

/** 第五张那条路：种覆盖 → 重载 → 开分组 → 断言 → 拍 → **无论成没成都复原**。 */
async function captureOverriddenShot() {
  const seeded = await seedPromptOverride();
  try {
    // 整页重载（不是再点一次 hash）：覆盖是**服务端**的状态，只有重新拉一次
    // `/api/prompts` 才会带上 `overridden` 与 `warnings`
    await send('Page.reload', { ignoreCache: true });
    await preparePromptsPage();
    await closeAnyDialog();
    await openFirstGroup();
    await assertOverriddenShape(seeded);
    await shot('prompts-page-overridden', PROMPTS_OUT);
  } finally {
    await revertSeededOverride(seeded);
  }
}

/** 展开第一个分组之后那张图（`prompts-page-group-open`）拍之前的形态断言：
 *  **恰好一个**分组开着、其余六组仍然折着（不是手风琴）、第一条原文真的可见。 */
async function assertGroupOpenShape() {
  const shape = await evaluate(`(() => {
    const groups = [...document.querySelectorAll('.prompts-group')];
    const open = groups.filter((group) => group.hasAttribute('open'));
    const pre = document.querySelector('[data-prompt-id] .prompt-details:not(.collapsed) pre');
    return {
      groups: groups.length,
      openGroups: open.length,
      collapsedGroups: groups.length - open.length,
      firstGroupItems: open[0] ? open[0].querySelectorAll('[data-prompt-id]').length : 0,
      firstGroupTally: open[0] ? ((open[0].querySelector('.pg-tally') || {}).textContent || '') : '',
      preVisible: !!(pre && pre.getClientRects().length > 0),
      scrollHeight: (document.getElementById('prompts-body') || {}).scrollHeight || 0,
    };
  })()`);
  console.log('  展开第一个分组之后：', JSON.stringify(shape));
  const bad = [];
  if (shape.groups !== 7) bad.push(`分组应有 7 个，实际 ${shape.groups}`);
  if (shape.openGroups !== 1) bad.push(`这一张应当**恰好**展开一个分组，实际开了 ${shape.openGroups} 个`);
  if (shape.collapsedGroups !== 6) bad.push(`其余 6 组应当仍然折着，实际折着 ${shape.collapsedGroups} 组`);
  if (!shape.firstGroupItems) bad.push('展开的那一组里一个条目都没有（钩子丢了？）');
  if (!shape.preVisible) bad.push('第一条提示词的原文没展开（图里看不到正文）');
  if (PROMPT_PROBLEMS.length) bad.push(`页面报错：${PROMPT_PROBLEMS.slice(0, 3).join(' | ')}`);
  if (bad.length) {
    throw new Error(`展开分组之后这一页不对劲，**不写这张图**：${bad.join('；')}`);
  }
}

try {
  ws = new WebSocket(chrome.wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  ws.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else resolve(message.result);
    }
  });

  await send('Page.enable');
  await send('Runtime.enable');
  // 等**应用外壳**出现，而不是等 `document.readyState === 'complete'`：
  // /json/list 里那个 page 目标一开始是 `about:blank`（命令行给的地址还没加载），
  // 而 about:blank 的 readyState 立刻就是 complete —— 那样会对着空白页点按钮，
  // 报一句 "Cannot read properties of null (reading 'click')"（真的踩过）。
  await waitFor(`!!document.querySelector('[data-pane="dream"]')`, 20000, '应用外壳加载完成');
  // 再等**导航接线**：init() 里 `item.onclick = ...` 是最后几步才做的，
  // 在那之前点侧栏的「Dream」什么都不会发生（截出来是一张对话页 —— 真的截到过）。
  await waitFor(
    `(() => { const item = document.querySelector('[data-pane="dream"]'); return !!(item && item.onclick); })()`,
    20000,
    '应用初始化完成（导航已接线）',
  );

  // 进入 设置 → Dream（用界面自己的入口，不直接调内部函数）
  await evaluate(`document.querySelector('[data-pane="dream"]').click()`);
  // 等这一页真的画出来（vault + 运行记录两次请求，慢了就不止 1 秒）
  //
  // ⚠ **按类名找，不按按钮文案找**（ADR-085）：这里原来写的是
  // `/格式化并初始化/.test(b.textContent)` —— 界面语言一切到英文，
  // 这个条件永远为假，于是"等不到 Dream 页渲染完成"，**脚本再也拍不了图**。
  // `.dream-format-open` 是前端给那个按钮留的稳定钩子（与语言、与措辞都无关）。
  await waitFor(`(() => {
    const pane = document.getElementById('pane-dream');
    if (!pane || pane.classList.contains('hidden')) return false;
    return !!document.querySelector('.dream-format-open');
  })()`, 25000, 'Dream 页渲染完成（含危险区）');
  const state = await evaluate(`(() => {
    const zone = document.getElementById('dream-format-zone') || document.querySelector('.dream-danger');
    return {
      title: document.title,
      dangerButtons: [...document.querySelectorAll('.dream-format-open, .dream-danger button')]
        .map((b) => b.textContent.trim()),
      vaultFiles: (document.getElementById('vault-meta') || {}).textContent || '',
      runsTitle: (document.getElementById('dream-runs-title') || {}).textContent || '',
      zoneExists: !!zone,
    };
  })()`);
  console.log('  页面状态：', JSON.stringify(state, null, 2));
  await shot('01-dream-page');

  // 危险区在页面最下面，默认拍不到 —— 滚到它那里再拍一张
  await evaluate(`(() => {
    const btn = document.querySelector('.dream-format-open');
    if (btn) btn.scrollIntoView({ block: 'center' });
    return true;
  })()`);
  await sleep(400);
  await shot('02-danger-zone');

  // 打开格式化对话框（危险区那个按钮）
  const opened = await evaluate(`(() => {
    const btn = document.querySelector('.dream-format-open');
    if (!btn) return false;
    btn.click();
    return true;
  })()`);
  await sleep(600);
  if (opened) {
    const dialog = await evaluate(`(() => {
      const dlg = document.getElementById('dialog') || document.querySelector('.dialog-backdrop, #dialog-body');
      const confirmBtn = document.getElementById('dialog-confirm');
      return {
        confirmDisabled: confirmBtn ? confirmBtn.disabled : null,
        confirmLabel: confirmBtn ? confirmBtn.textContent.trim() : '',
        title: (document.getElementById('dialog-title') || {}).textContent || '',
        text: (dlg ? dlg.textContent : '').replace(/\\s+/g, ' ').slice(0, 500),
      };
    })()`);
    console.log('  对话框：', JSON.stringify(dialog, null, 2));
    await shot('03-format-dialog');
  } else {
    console.log('  ⚠ 没找到「格式化并初始化」按钮');
  }

  // ④⑤⑥ 对话页的图片附件 + 折叠态的 OCR 气泡 + 提示词页（都不调模型）
  if (DATA_DIR && !process.env.SHOT_PROMPTS_ONLY) {
    await captureFixtureShots(DATA_DIR);
    await captureOcrVariants();
    await captureProviderPane(DATA_DIR);
  } else if (!DATA_DIR) {
    console.log('  · 跳过附件 / OCR 截图（要给第 3 个参数 dataDir，或设 HARNESS_DATA_DIR）；'
      + '它只该指向一次性夹具目录');
  } else {
    console.log('  · SHOT_PROMPTS_ONLY=1：只拍提示词那两张，跳过附件 / OCR');
  }

  // ⑥ 设置 → 提示词（docs/13-prompts-page.md）：浅色 + 深色
  await capturePromptsShots();
} finally {
  try {
    if (ws && ws.readyState === 1) {
      // 先按 CDP 关掉**自己这一个**实例：启动器进程可能早就退了，
      // 只 kill 那个 PID 会留下一个孤儿浏览器进程。
      // 加一个超时：浏览器关掉时可能在回包之前就把 socket 断了，
      // 那样这个 promise 永远不 resolve，脚本会挂在这里。
      await Promise.race([send('Browser.close'), sleep(2000)]);
    }
  } catch {
    /* 已经关了 / 连接断了 */
  }
  try {
    if (ws) ws.close();
  } catch {
    /* ignore */
  }
  await killByPid(chrome.child); // ← 只杀这一条进程（兜底）
}
