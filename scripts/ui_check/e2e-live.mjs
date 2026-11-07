'use strict';
/* 端到端（无浏览器）：把**真接口**的响应（/api/vault、/api/vault/file?path=…）
   喂给真的 web/app.js 在 DOM stub 里渲染一遍。
   与 dom-stub.test.mjs 的区别：那边的夹具是照抄响应手写的，这边直接用线上数据，
   验的是"后端真给的字段，前端真的读得懂"。 */

import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const BASE = process.argv[2] || 'http://127.0.0.1:8791';

class El {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = []; this.parentNode = null; this.attrs = {}; this._text = '';
    this.onclick = null; this.firstChild = null; this._ownId = ''; this.type = '';
    this.dataset = {}; this.scrollTop = 0;
    // 图片附件的入口三件套要验"哪个元素挂了哪个监听"，所以这里不把
    // addEventListener 当空操作（与 dom-stub.test.mjs 里那个 stub 一个道理）
    this._listeners = {}; this.files = null; this.clickCount = 0;
    this.classList = { _set: new Set(), add: (...n) => n.forEach((x) => this.classList._set.add(x)),
      remove: (...n) => n.forEach((x) => this.classList._set.delete(x)),
      contains: (x) => this.classList._set.has(x),
      toggle: (x, f) => { const on = f === undefined ? !this.classList._set.has(x) : !!f; if (on) this.classList._set.add(x); else this.classList._set.delete(x); return on; } };
  }
  get id() { return this._ownId; } set id(v) { this._ownId = String(v); }
  get className() { return [...this.classList._set].join(' '); }
  set className(v) { this.classList._set = new Set(String(v || '').split(/\s+/).filter(Boolean)); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this.children = []; this.firstChild = null; this._text = String(v); }
  get innerHTML() { return ''; }
  set innerHTML(markup) {
    this.children = []; this._text = ''; this.firstChild = null;
    const stack = [this];
    const tokens = String(markup).match(/<!--[\s\S]*?-->|<\/?[a-zA-Z][^>]*>|[^<]+/g) || [];
    for (const token of tokens) {
      const parent = stack[stack.length - 1];
      if (token.startsWith('<!--')) continue;
      if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      if (token.startsWith('<')) {
        const name = /^<([a-zA-Z][a-zA-Z0-9]*)/.exec(token)[1];
        const node = new El(name);
        const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"/g;
        let attr;
        while ((attr = attrRe.exec(token)) !== null) {
          node.attrs[attr[1]] = attr[2].replace(/&quot;/g, '"').replace(/&#39;/g, "'")
            .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
          if (attr[1] === 'id') node.id = attr[2];
          if (attr[1] === 'class') node.className = attr[2];
        }
        parent.appendChild(node);
        if (!/\/>$/.test(token) && !/^(br|hr|img|input|meta|link)$/i.test(name)) stack.push(node);
        continue;
      }
      parent.appendChild(new Txt(token.replace(/&lt;/g, '<').replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')));
    }
    this.firstChild = this.children[0] || null;
  }
  appendChild(n) { n.parentNode = this; this.children.push(n); this.firstChild = this.children[0]; return n; }
  removeChild(n) { const i = this.children.indexOf(n); if (i >= 0) this.children.splice(i, 1); n.parentNode = null; this.firstChild = this.children[0] || null; return n; }
  insertBefore(n, b) { const i = b ? this.children.indexOf(b) : -1; if (i < 0) return this.appendChild(n); n.parentNode = this; this.children.splice(i, 0, n); this.firstChild = this.children[0]; return n; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  /* 提示词页的分组是原生 `<details>`：自检里"点开/收起"照浏览器做（加 / 摘
     `open`），所以要能摘掉它 —— "开一个不影响别的"那条断言的后半截靠它。 */
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { if (k === 'title' && this.title) return this.title; return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  addEventListener(t, fn) { if (!this._listeners[t]) this._listeners[t] = []; if (!this._listeners[t].includes(fn)) this._listeners[t].push(fn); }
  removeEventListener(t, fn) { const l = this._listeners[t] || []; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); }
  listenerCount(t) { return (this._listeners[t] || []).length; }
  /* 拖放的 dragleave 用 contains 判断"还在不在输入区里" */
  contains(node) { for (let c = node; c; c = c.parentNode) if (c === this) return true; return false; }
  click() { this.clickCount += 1; }
  /* 自检里没有真的浏览器焦点与选区：如实记下"谁被 focus 过、光标放到哪"，
     「插入链接之后焦点/光标回到正文」那条断言看的就是这两样。 */
  focus() { this.focusCount = (this.focusCount || 0) + 1; }
  setSelectionRange(a, b) { this.selectionSet = [a, b]; }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  /* 支持 `#id` 与 `.class` / 标签名 —— app.js 会用 `#id` 在编辑器子树里找输入框，
     不支持 id 选择器的话"找到的是另一个节点"，自检会看到假的失败。 */
  querySelectorAll(s) { const out = []; const sel = s.trim();
    const walk = (n) => { for (const c of n.children) { if (c.tagName === '#TEXT') continue;
      const hit = sel.startsWith('#') ? c._ownId === sel.slice(1)
        : sel.startsWith('.') ? c.classList.contains(sel.slice(1))
          : c.tagName === sel.toUpperCase();
      if (hit) out.push(c); walk(c); } }; walk(this); return out; }
}
class Txt extends El { constructor(t) { super('#text'); this._text = String(t); } }

/* 每次都从**这次传进来的根**重新收集：早先版本用了一个模块级累加数组，
   于是 find() 会把上一次渲染留下的、已经脱离文档的节点也找出来 ——
   自检因此"看到"了根本不存在的元素，把真正的失败盖掉。 */
const walkAll = (root) => {
  const out = [];
  const visit = (node) => {
    for (const child of node.children) {
      if (child.tagName === '#TEXT') continue;
      out.push(child);
      visit(child);
    }
  };
  visit(root);
  return out;
};
const find = (root, pred) => walkAll(root).find(pred) || null;
const byClass = (root, name) => walkAll(root).filter((n) => n.classList.contains(name));

/* 收集**不在代码里**的可见文字：行内码/代码块里的 `[[X]]` 本来就该是字面量，
   所以"页面上不再出现没渲染的双链"这条断言必须把 CODE / PRE 整棵子树排除掉。 */
const textOutsideCode = (root) => {
  let out = '';
  const visit = (node) => {
    if (node.tagName === 'CODE' || node.tagName === 'PRE') return;
    out += node._text || '';
    for (const child of node.children) {
      if (child.tagName === '#TEXT') { out += child._text; continue; }
      visit(child);
    }
  };
  visit(root);
  return out;
};

const byId = new Map();
/* 图片附件那一节要往页面壳上挂监听 / 找元素，而 index.html 不参与这个自检：
   这里先按 index.html 的样子把壳造出来（与 dom-stub.test.mjs 里 primeShell 同理）。 */
function primeShell() {
  for (const id of [
    'messages', 'input', 'toast', 'send', 'stop',
    'attach', 'attach-file', 'attach-hint', 'composer-drop', 'composer-attachments',
    'composer-hint', 'lightbox', 'lightbox-img', 'lightbox-caption',
    'toggle-web-search', 'toggle-multimodal', 'multimodal-hint', 'chat-meta',
  ]) doc.getElementById(id);
  // 两个开关都会写各自的 `.label` 子节点（index.html 里就有）
  for (const id of ['toggle-web-search', 'toggle-multimodal']) {
    const toggle = doc.getElementById(id);
    if (!find(toggle, (n) => n.classList.contains('label'))) {
      const label = doc.createElement('span');
      label.className = 'label';
      toggle.appendChild(label);
    }
  }
  const zone = doc.getElementById('composer-drop');
  for (const id of ['composer-attachments', 'input', 'attach', 'attach-file', 'attach-hint']) {
    const node = doc.getElementById(id);
    if (node.parentNode !== zone) zone.appendChild(node);
  }
  doc.getElementById('lightbox').classList.add('hidden');
  doc.getElementById('composer-attachments').classList.add('hidden');
  doc.getElementById('multimodal-hint').classList.add('hidden');
}
const doc = { documentElement: new El('html'), body: new El('body'), createElement: (t) => new El(t),
  createTextNode: (t) => new Txt(t),
  getElementById: (id) => { if (!byId.has(id)) { const n = new El('div'); n.id = id; byId.set(id, n); } return byId.get(id); },
  _listeners: {},
  // 灯箱 / 对话框的 ESC 挂在 document 上：记下来才验得到
  addEventListener(t, fn) { if (!this._listeners[t]) this._listeners[t] = []; if (!this._listeners[t].includes(fn)) this._listeners[t].push(fn); },
  removeEventListener(t, fn) { const l = this._listeners[t] || []; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); },
  listenerCount(t) { return (this._listeners[t] || []).length; },
  dispatch(t, e) { for (const fn of (this._listeners[t] || []).slice()) fn(e || { type: t }); return e; },
  querySelector: () => null, querySelectorAll: () => [] };
primeShell();

const calls = [];
const sends = [];              // 每一次 POST 的 { url, payload }
let postHandler = null;        // (url, payload) => { status, body }（没设就真打接口）
/* 原生 `confirm` 的答案与**原话**：编辑提示词时的二次确认走它。
   这里一律同意（要跑完这条路），但把文案留下来 —— "确认文案里写清了语言与条数"
   那两条断言看的就是它。 */
let lastConfirm = '';
/* FileReader 的桩（只有附件那一节用得上）：app.js 读文件走
   `readAsDataURL(file)` + `onload/onerror`，这里认识的"文件"是一个带 dataUrl
   的普通对象。**刻意不提供 URL.createObjectURL** —— 缩略图必须显示服务端
   落盘的那一版，本地 objectURL 顶替服务端图这件事连手段都不给。 */
class StubFileReader {
  readAsDataURL(file) {
    const url = file && file.dataUrl;
    if (!url) { if (this.onerror) this.onerror(new Error('这个桩文件没有 dataUrl')); return; }
    this.result = url;
    if (this.onload) this.onload();
  }
}
const ctx = { document: doc, location: { hash: '', search: '' }, localStorage: { getItem: () => null },
  console, setTimeout, clearTimeout, URLSearchParams, TextDecoder, AbortController,
  FileReader: StubFileReader,
  // window 上的监听（拖到**别处**时那个只挡默认行为的兜底）也要记下来
  _listeners: {},
  addEventListener(t, fn) { if (!this._listeners[t]) this._listeners[t] = []; if (!this._listeners[t].includes(fn)) this._listeners[t].push(fn); },
  removeEventListener(t, fn) { const l = this._listeners[t] || []; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); },
  listenerCount(t) { return (this._listeners[t] || []).length; },
  confirm: (message) => { lastConfirm = String(message); return true; },   // 二次确认：这里一律同意，但把原话记下来
  fetch: async (url, options) => {
    const opts = options || {};
    calls.push(String(url));
    if (opts.method === 'POST') {
      const payload = JSON.parse(opts.body || '{}');
      sends.push({ url: String(url), payload });
      if (postHandler) {
        const hit = postHandler(String(url), payload);
        if (hit) return { ok: hit.status < 400, status: hit.status, json: async () => hit.body };
      }
    }
    const resp = await fetch(BASE + url, opts.method ? {
      method: opts.method, headers: opts.headers, body: opts.body,
    } : undefined);
    const body = await resp.json().catch(() => ({}));
    return { ok: resp.ok, status: resp.status, json: async () => body };
  },
  __HARNESS_NO_INIT__: true };
ctx.window = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
// i18n.js 必须**先**载进同一个上下文（ADR-085）：app.js 里已经没有中文字面量了，
// 它全靠 `t(key)` 取文案。少了这一步，第一个渲染就会 `ReferenceError: t is not defined`。
// 与 `dom-stub.test.mjs` 的加载顺序一致。
vm.runInContext(
  readFileSync(new URL('../../web/i18n.js', import.meta.url), 'utf8'),
  ctx,
  { filename: 'web/i18n.js' },
);
// 相对脚本自身定位 app.js，谁克隆到哪都能跑
const appPath = new URL('../../web/app.js', import.meta.url);
vm.runInContext(readFileSync(appPath, 'utf8')
  + ';globalThis.__app={state,loadVault,loadVaultFile,openVaultFile,renderVaultPane,'
  + 'startVaultEdit,startVaultCreate,saveVaultEdit,cancelVaultEdit,askDeleteVaultFile,'
  // `formatWord` 现在是**函数**（语言可切，值不能在最外层求值冻住），直接交出去
  + 'takeFreshVaultContent,vaultEditPayload,askFormatDream,formatWord,'
  + 'wireComposerAttachments,attachFiles,uploadAttachmentData,renderAttachments,'
  + 'renderStoredMessage,openLightbox,closeLightbox,'
  // 「多模态」开关（契约 §13）：这一段只喂帧，不发网络请求
  + 'applyToolsFrame,renderMultimodalToggle,setMultimodal,boolDefaultOn,splitModelList,'
  // §13.7 第 3 句的折叠气泡：直接拿真函数建一棵出来验形状（同样不发请求）
  + 'userBubbleNode,syncAttachEntry,'
  // 设置 → 提示词（docs/13-prompts-page.md）：真 /api/prompts → 真渲染
  + 'loadPrompts,renderPrompts,promptStateLabel,promptStateChip,paramType,'
  // 提示词可编辑（docs/14-editable-prompts.md §8）：编辑器 / 保存 / 复原 / 计数
  + 'savePromptEdit,revertPrompts,startPromptEdit,cancelPromptEdit,promptItem,'
  + 'promptOverridesText,promptOverrideCount,promptWarnText,promptsLanguageOf,'
  + 'promptCardById,updatePromptCard,'
  // 切语言（这一页要"原地重渲染"，而服务端文案也得跟着换）
  + 't,chooseLanguage,setLanguageNow,currentLanguage: () => I18N.current()};',
  ctx, { filename: 'app.js' });
const app = ctx.__app;

/* 直接问接口要一份档案的原文与指纹（不经界面）——用来验证"界面里存下去的东西
   真的落了盘"，以及制造"你看着的时候文件被别人改了"这种情形。 */
async function apiFile(path) {
  const resp = await fetch(`${BASE}/api/vault/file?path=${encodeURIComponent(path)}`);
  return { status: resp.status, body: await resp.json().catch(() => ({})) };
}

async function apiOpen(path) {
  return (await apiFile(path)).body;
}

/* 让挂起的 promise（点击之后那些 await 链）跑完一轮 */
const tick = () => new Promise((resolve) => setImmediate(resolve));
/* 等某个条件成立（最多 ~2 秒）。切语言那条链里套了几层 await（POST → 重渲染 →
   子页重新拉接口），**用固定次数的 tick 去猜它跑完了没有**是不可靠的：
   真实 fetch 的那几跳在 e2e 里需要更多轮微任务，而设置页的渲染发生在
   `showSettings` 里那句没有 await 的 load 之后。等条件成立比数次数稳。 */
async function waitFor(predicate, what) {
  for (let i = 0; i < 200; i += 1) {
    if (predicate()) return true;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`等待超时：${what}`);
}

/* 在 app.js 画出来的 DOM 里按 class 找节点（每次都要重新找：重画会换节点）。 */
const viewNode = (cls) => find(byId.get('vault-viewer'), (x) => x.classList.contains(cls));
/* 目录树上的某个文件节点。每次都要**当场**在树上找：删掉一个节点之后，
   stub 的 parentNode 指针会留在旧节点上，"从树根走一遍"才是页面真实的可见集合。 */
const treeFile = (name) => find(treeNow(), (x) => x.classList.contains('vault-node-file')
  && x.textContent.includes(name));
/* 编辑器的输入框：id 落在外层字段盒上，真正的 input/textarea 在它里面。 */
const editorNode = (id) => {
  const box = find(byId.get('vault-viewer'), (x) => x._ownId === id);
  if (!box) return null;
  return find(box, (x) => x.tagName === 'INPUT' || x.tagName === 'TEXTAREA' || x.tagName === 'SELECT') || box;
};
const clickText = (root, text) => {
  const node = find(root, (x) => x.tagName === 'BUTTON' && x.textContent === text);
  if (!node) throw new Error(`找不到按钮：${text}`);
  return node.onclick();
};

let n = 0;
const failures = [];
/* 点一个"按夹具必须存在"的节点：找不到就**如实记一条失败**并返回 false。
   为什么要有这个：`null.onclick()` 会把整个脚本掀翻，连"断言总数/失败清单"都打不出来 ——
   夹具不干净（空 vault / 少一份档案）时，看的人只会以为前端坏了。
   这里不吞任何断言：该失败还是失败，只是换成一句看得懂的话。 */
const clickNode = async (node, what) => {
  if (!node) {
    n += 1;
    failures.push(`找不到要点的节点：${what}`);
    console.log(`  ✗ 找不到要点的节点：${what}`);
    return false;
  }
  return node.onclick();
};
/* 支持 async 断言：await 的检查必须等它结束再算数，否则断言失败会变成一条
   没人看的 unhandled rejection（这一轮新增了"真的去接口核对"的断言，必须 await）。 */
const check = async (label, fn) => {
  n += 1;
  try {
    const back = fn();
    if (back && typeof back.then === 'function') await back;
    console.log(`  ✓ ${label}`);
  } catch (err) {
    failures.push(`${label}: ${err.message}`);
    console.log(`  ✗ ${label} —— ${err.message}`);
  }
};

/* 兜底 handler 必须**在这里**就挂上（`finish` 的定义在文件末尾，
   函数声明会提升，所以这里调得到）：脚本中段的异常要能走到它。
   挂到末尾就太晚了 —— 夹具形状不对时那条 throw 在中段，
   结果连"断言总数 / 失败清单"都没打出来（这一轮真的踩过）。 */
let reported = false;
process.on('uncaughtException', (err) => finish(err));
process.on('unhandledRejection', (err) => finish(err));

/* ── 写操作前的第一道门：这块数据是夹具吗？────────────────────────────
   这个脚本**会写盘**（下面那句 `POST /api/settings/language` 会写
   `data/settings.json`；vault 那几节还会真的建/改/删档案）。所以它必须先问
   服务端"你的数据目录是哪个"，**不像夹具就直接不跑**。

   为什么要这一道（三条既有规矩叠在一起）：
     * ADR-050：破坏性/写操作前必须校验"这是不是我们该动的那块数据"；
     * ADR-080：能预防就绝不要靠提示 —— 文档里写十遍"别指向真实 data/"，不如
       让它**跑不起来**；
     * ADR-069：代码干净 ≠ 产物干净 —— 脚本自己写出来的 settings.json 也是产物。

   判据（`/api/config` 的 `data_dir`，项目内显示成相对路径）：
   路径里必须有一段以 `tmp-` / `.tmp-` 开头（`seed-vault.ps1` 造的
   `.tmp-ui-check\data` 正是这种）。真需要对着别处跑：`E2E_ALLOW_REAL_DATA=1`。

   ⚠ `shot.mjs` 里有一份**逐字相同**的拷贝（两个脚本各自独立、都不 import 别的
   文件，这是这个目录一贯的写法）。改这里记得两边一起改。 */
async function assertFixtureTarget() {
  let config;
  try {
    config = await (await fetch(`${BASE}/api/config`)).json();
  } catch (err) {
    throw new Error(`连不上 ${BASE}（${(err && err.message) || err}）——`
      + '这个自检需要一个**指向夹具数据目录**的服务器，先起来再跑');
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
    + '  这个脚本会写盘（settings.json 里的语言、vault 里的档案），所以只该对着\n'
    + '  一次性夹具目录跑。正确用法：\n'
    + '    powershell -File scripts\\ui_check\\seed-vault.ps1 -DataDir .tmp-ui-check\\data\n'
    + '    set HARNESS_DATA_DIR=.tmp-ui-check\\data\n'
    + '    python -m harness.cli --web --port 8791 --no-open\n'
    + '  确实要对着这个目录跑（你知道自己在做什么）：set E2E_ALLOW_REAL_DATA=1',
  );
}

await assertFixtureTarget();

/* 先把**服务端**语言钉成中文：这个自检里大量断言盯着中文文案（vault 的 404/409 原话、
   格式化确认词…），而语言是服务端的设置、还会被上一轮跑留下的 settings.json 带过来。
   界面这一侧不跑 init()，所以 app.js 的语言默认就是 zh —— 两边对齐，后面的断言
   才是在验它们本来要验的东西（而不是在验"上一次跑把语言留成了 en"）。
   ⚠ 提示词那一节会**来回切两次**并切回中文，那是唯一动语言的地方。 */
await fetch(`${BASE}/api/settings/language`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ language: 'zh' }),
});
await check('前置：服务端语言是中文（本自检的断言大多盯着中文文案）', async () => {
  const body = await (await fetch(`${BASE}/api/config`)).json();
  assert.equal(body.language, 'zh', 'POST /api/settings/language 没把语言钉成 zh');
});

// ① 真 /api/vault → 真渲染
/* 夹具形状**先问接口**（而不是从界面上那几个数字反推）：这一节后面的断言都以
   "seed-vault.ps1 造出来的那份夹具"为前提。夹具不干净（空目录 / 少一份档案 /
   HARNESS_DATA_DIR 指到了别的目录）时，要在这里说清"是夹具的问题、怎么修"，
   而不是让后面某条断言抛一个 `Cannot read properties of null` ——
   那种报错会把剩下 50 多条断言一起吞掉，看的人只会以为是前端坏了。 */
const vaultJson = await (await fetch(`${BASE}/api/vault`)).json();
const treeOf = (json) => (json || {}).tree || [];
const filesOf = (json) => {
  const out = [];
  const walk = (list, prefix) => {
    for (const node of list || []) {
      if (!node) continue;
      if (node.type === 'dir') walk(node.children, prefix ? `${prefix}/${node.name}` : node.name);
      else out.push(node);
    }
  };
  walk(treeOf(json), '');
  return out;
};
const SEEDED_PATHS = ['README.md', 'entities/小张.md', 'entities/小林.md', 'entities/项目A.md',
  'sections/偏好.md', 'sections/奇怪的正文.md', 'sections/长档案.md'];
await check('夹具形状（后面全部断言的前提）：/api/vault 里有 README + entities + sections，且 seed 的那 7 份都在', () => {
  const roots = treeOf(vaultJson).map((node) => node.name || node.path);
  assert.deepEqual(roots, ['README.md', 'entities', 'sections'],
    `夹具不干净：根级应是 README.md + entities + sections，实际 ${JSON.stringify(roots)}；`
    + '重跑 scripts/ui_check/seed-vault.ps1，并确认服务器的 HARNESS_DATA_DIR 指的就是那份夹具');
  const paths = filesOf(vaultJson).map((file) => file.path);
  for (const path of SEEDED_PATHS) {
    assert.ok(paths.includes(path), `夹具里少了 ${path}（重跑 seed-vault.ps1）；实际有：${paths.join(', ')}`);
  }
});
/* 夹具形状不对就**到此为止**：后面的用例会真的新建/改名/删除档案，
   拿一份"不是 seed 出来的" vault 往下跑，等于拿别人的数据做实验
   （真的把一份夹具跑空过）。抛出去 → 由下面的 uncaughtException 收尾，
   打出"断言总数 + 失败清单"再退，不会把输出吞掉。 */
if (failures.length) {
  const stop = new Error('夹具形状不对：先按上面的提示修好夹具（seed-vault.ps1 / HARNESS_DATA_DIR），再跑这套端到端自检');
  stop.e2eStop = true;
  throw stop;
}

await app.loadVault();
const treeNow = () => byId.get('vault-tree');
const tree = treeNow();
await check('真 /api/vault 的 tree 渲染出 README + entities + sections', () => {
  const rootNodes = tree.children;
  assert.equal(rootNodes.length, 3, `根级节点数 ${rootNodes.length}`);
  assert.match(rootNodes[0].textContent, /README/);
  assert.match(rootNodes[0].textContent, /索引/);
  assert.match(rootNodes[1].textContent, /entities/);
  assert.match(rootNodes[2].textContent, /sections/);
});
await check('真 files 计数直接显示（界面上那两个数字 == 接口给的那两个）', () => {
  const dirs = treeOf(vaultJson).filter((node) => node.type === 'dir');
  assert.equal(dirs.length, 2, `夹具应有 entities / sections 两个目录，实际 ${dirs.length}`);
  for (const dir of dirs) {
    const node = find(tree, (x) => x.classList.contains('vault-node-dir')
      && find(x, (y) => y.classList.contains('vn-title') && y.textContent === dir.name));
    assert.ok(node, `树上应有目录 ${dir.name}`);
    const chip = find(node, (x) => x.classList.contains('chip'));
    assert.equal(chip.textContent, String(dir.files),
      `${dir.name} 的计数应等于接口给的 ${dir.files}`);
  }
});
await check('三份奇怪的正文都出现在树上（含中文标题与摘要 title）', () => {
  const names = byClass(tree, 'vn-title').map((x) => x.textContent);
  for (const want of ['小张', '小林', '项目A', '偏好', '奇怪的正文', '长档案']) {
    assert.ok(names.includes(want), `树上应有 ${want}`);
  }
  const sum = find(tree, (x) => x.textContent === '结论先行');
  assert.ok(sum, '摘要应显示成弱化小字');
  assert.equal(sum.getAttribute('title'), '结论先行');
});
await check('没选中文件时给引导', () => {
  assert.equal(find(byId.get('vault-viewer'), (x) => x.classList.contains('vault-empty')).textContent,
    '从左边点一个档案看看。');
});
await check('待整理卡片与结构校验都还在', () => {
  const list = byId.get('dream-list');
  assert.ok(find(list, (x) => x.classList.contains('vault-card')));
  assert.ok(find(list, (x) => x.classList.contains('vault-problems')));
  // 数字与**接口给的**对齐（不写死 6）：夹具里多一份临时档案时，界面照旧只该显示接口那个数
  const want = String((vaultJson.check || {}).files || 0);
  assert.ok(list.textContent.includes(`${want} 份档案`),
    `卡里应写着接口给的"${want} 份档案"；实际：${list.textContent.slice(0, 200)}`);
});

/* 这一条是给"是不是多了一次没必要的重渲染"留的回归闸：
   整页重画（真接口）之后，`$('vault-tree')` 必须还是**同一个节点** ——
   stub 的 byId 只在第一次 getElementById 时造它，没有任何路径会把它换成一个新的空 div；
   而树上照样得有 小林。真坏了的话，下面那一条"点小林"会以 null.onclick 崩掉。 */
await app.loadVault();
await check('整页重画之后树还是同一个节点、内容也还在（不是换了个空容器）', () => {
  assert.equal(treeNow(), tree, 'vault-tree 节点不该被换成新的');
  assert.ok(find(treeNow(), (x) => x.classList.contains('vault-node-file')
    && x.textContent.includes('小林')), '重画之后树上仍应有 小林');
});

// ② 真点树上的「小林」→ 真 /api/vault/file → 真渲染
const xiaolin = find(treeNow(), (x) => x.classList.contains('vault-node-file') && x.textContent.includes('小林'));
await check('树上找得到「小林」那个节点（点它才能验"点了会真的去请求"）', () => {
  assert.ok(xiaolin, '树上没有 entities/小林.md 的节点；树里现在只有：'
    + byClass(treeNow(), 'vn-title').map((x) => x.textContent).join(' / '));
});
// 找不到就不点（后面几条会如实失败），但**绝不再让 null.onclick 把整个脚本掀翻**
if (xiaolin) await xiaolin.onclick();
await check('点小林用的是编码过的 ?path=', () => {
  assert.ok(calls.includes('/api/vault/file?path=' + encodeURIComponent('entities/小林.md')), calls.join(','));
});
// 每次重刷 renderVaultPane 都会重建 #vault-viewer，所以每次都要重新查一遍
const viewerNow = () => byId.get('vault-viewer');
await check('真响应渲染出标题/路径/属性/extra', () => {
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-view-title')).textContent, '小林');
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-view-path')).textContent, 'entities/小林.md');
  const labels = byClass(viewerNow(), 'va-k').map((x) => x.textContent);
  assert.deepEqual(labels.slice(0, 7), ['name', 'kind', 'relation', 'status', 'created', 'updated', 'tags']);
  const extras = byClass(viewerNow(), 'vault-attr-extra').map((x) => x.textContent);
  assert.ok(extras.some((t) => t.includes('mood') && t.includes('偏谨慎')));
});
await check('正文渲染成 h1/ul/strong/code/blockquote，双链可点', () => {
  assert.equal(find(viewerNow(), (x) => x.tagName === 'H1').textContent, '小林');
  assert.equal(find(viewerNow(), (x) => x.tagName === 'UL').children.length, 2);
  assert.equal(find(viewerNow(), (x) => x.tagName === 'STRONG').textContent, '前端');
  assert.equal(find(viewerNow(), (x) => x.tagName === 'CODE').textContent, 'innerHTML');
  assert.ok(find(viewerNow(), (x) => x.tagName === 'BLOCKQUOTE'));
  const links = byClass(viewerNow(), 'vault-link');
  assert.equal(links.length, 2);
  assert.equal(links.find((x) => x.textContent === '小张').tagName, 'BUTTON');
  assert.equal(links.find((x) => x.textContent === '某个还不存在的名字').tagName, 'SPAN');
});
await check('真邻域：它指向/谁指向它/共同指向别处都在', () => {
  assert.deepEqual(byClass(viewerNow(), 'vnb-title').map((x) => x.textContent),
    ['它指向', '谁指向它', '共同指向别处']);
  assert.ok(byClass(viewerNow(), 'vault-chip').some((x) => x.textContent === '项目A'));
});

// ③ 真点「奇怪的正文」→ 攻击载荷必须是文本
const payloadNode = find(treeNow(), (x) => x.classList.contains('vault-node-file') && x.textContent.includes('奇怪的正文'));
await clickNode(payloadNode, '树上的「奇怪的正文」（夹具里的 XSS 载荷档案）');
await check('真接口里的 <script> / <img onerror> 只作为文本（没有 SCRIPT/IMG 元素）', () => {
  assert.equal(find(viewerNow(), (x) => x.tagName === 'SCRIPT'), null);
  assert.equal(find(viewerNow(), (x) => x.tagName === 'IMG'), null);
  const body = find(viewerNow(), (x) => x.classList.contains('vault-body'));
  assert.ok(body.textContent.includes('<script>window.__PWNED__ = 1'));
  assert.ok(body.textContent.includes('<img src=x onerror='));
  assert.equal(find(viewerNow(), (x) => x.tagName === 'H1').textContent, '里面塞了攻击载荷');
  assert.ok(find(viewerNow(), (x) => x.tagName === 'PRE').textContent.includes('const a = 1 < 2'));
});
await check('中文文件名与 title 提示都对得上', () => {
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-view-path')).textContent, 'sections/奇怪的正文.md');
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-view-title')).textContent, '奇怪的正文');
});

// ④ 真点长档案 → truncated=true 的真实响应
const longNode = find(treeNow(), (x) => x.classList.contains('vault-node-file') && x.textContent.includes('长档案'));
await clickNode(longNode, '树上的「长档案」（夹具里那份 20 万字符的）');
await check('真 truncated 响应 → 明确提示只显示了前 N 字符', () => {
  // **别写死数字**：夹具大小一改这条就假红（它真的假红过一次 —— 断言里写死的
  // 281999 是当天那份夹具的字符数，换成 313500 字符的同一份文件就报"应有截断提示"，
  // 而界面其实是对的）。正确的做法是**拿真响应的数字来对**。
  const loaded = app.state.vaultFile && app.state.vaultFile.data;
  assert.ok(loaded, '应当已经载入了长档案');
  const seen = calls.filter((url) => url.includes('长档案') || url.includes('%E9%95%BF'));
  assert.ok(seen.length, `应当向接口请求过长档案，实际请求：${calls.join(' , ')}`);
  if (!loaded.truncated) {
    // 夹具没超过查看上限时这条断言没意义 —— 明确跳过，而不是给一个假绿
    console.log('    （这份夹具只有 %d 字符，没触发截断，跳过）', loaded.total_chars);
    return;
  }
  const note = find(viewerNow(), (x) => x.classList.contains('vault-trunc'));
  assert.ok(note, '应有截断提示');
  assert.match(note.textContent, /文件太长/);
  assert.ok(
    note.textContent.includes(String(loaded.chars)),
    `提示里应出现"前 ${loaded.chars} 字符"，实际：${note.textContent}`
  );
  assert.ok(
    note.textContent.includes(String(loaded.total_chars)),
    `提示里应出现"全文 ${loaded.total_chars} 字符"，实际：${note.textContent}`
  );
  const foot = find(viewerNow(), (x) => x.classList.contains('vault-view-foot'));
  assert.ok(foot.textContent.includes(String(loaded.total_chars)), '脚注也应给出全文字数');
});

// ⑤ README 节点
await clickNode(find(treeNow(), (x) => x.classList.contains('vault-node-file')
  && x.textContent.includes('README')), '树上的 README');
await check('README 打开后标注"这是索引，模型每一轮都能看到它"', () => {
  const note = find(viewerNow(), (x) => x.classList.contains('vault-view-note'));
  assert.equal(note.textContent, '这是索引，模型每一轮都能看到它。');
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-view-title')).textContent, '背景档案');
});

// ⑥ 真 404 / 真 400
await app.openVaultFile('幽灵档案', { canonical: 'entities/幽灵档案.md' });
await check('真 404 的原话被显示出来', () => {
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-error')).textContent, '没有这个档案：幽灵档案');
});
await app.openVaultFile('other/x.md', { canonical: 'other/x.md' });
await check('真 400 的原话被显示出来（界面 == 后端那句话；措辞不写死）', async () => {
  const shown = find(viewerNow(), (x) => x.classList.contains('vault-error')).textContent;
  // 拿**同一条**请求问一次接口，拿它自己的话去比 —— 后端改文案（i18n 之后它真的
  // 从"sections 或 entities"改成了"{top_dirs}，且必须小写"）不该让这条假红。
  const back = await apiFile('other/x.md');
  assert.equal(back.status, 400, `后端应回 400，实际 ${back.status}`);
  assert.equal(shown, String(back.body.error), '界面显示的必须是后端原话');
});

/* ⑦ 新建 / 编辑 / 删除：**真的往 vault 里写，再从接口读回来核对**
   这一段只用临时名字（tmp-ui-check-…），跑完就删掉，不给下次留东西。
   与 dom-stub 那套的分工：那边验"渲染与请求体对不对"，这边验"后端真的收下了、
   界面真的读得懂自己写出来的东西"。 */
const TMP_NAME = 'tmp-ui-check-新建甲';
const TMP_PATH = `entities/${TMP_NAME}.md`;

/* 先把这个临时名字清掉：上一次自检如果在中间挂了（这条路径上真的挂过一次），
   它会把临时档案留在 vault 里，下一次"新建"就会撞名 —— 自检必须自己收拾自己
   留下的垃圾，否则会在别人的机器上假红。 */
const stale = await apiFile(TMP_NAME);
if (stale.status === 200) {
  await fetch(`${BASE}/api/vault/file/delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: TMP_PATH, expect_hash: stale.body.hash }),
  });
}

// 确认这个名字真的不存在（否则后面"新建成功"可能是撞上了一件旧东西）
const beforeCreate = await apiFile(TMP_NAME);
await check('临时档案一开始不存在（这条是后面几条断言的前提）', () => {
  assert.equal(beforeCreate.status, 404);
});

await app.startVaultCreate();
await check('「新建档案」进的是新建态，默认 entities、路径由名字算出来', () => {
  assert.equal(app.state.vaultEdit.mode, 'create');
  assert.equal(app.state.vaultEdit.draft.dir, 'entities');
  const name = editorNode('vault-name');
  assert.ok(name, '应有名字输入框');
  name.value = TMP_NAME;
  name.oninput();
  assert.equal(editorNode('vault-edit-path').textContent, TMP_PATH);
});
const createdBody = [
  '# 临时档案',
  '',
  '这段字是这次自检写进 vault 的。',
  '',
  '相关：[[小林]]',
  '',
  '加粗的链接：**[[小林]]**；行内码里的不算数：`[[小林]]`。',
].join('\n');
editorNode('vault-body').value = createdBody;
editorNode('vault-body').oninput();
editorNode('vault-title').value = TMP_NAME;
editorNode('vault-title').oninput();
editorNode('vault-kind').value = 'person';
editorNode('vault-kind').oninput();
await app.saveVaultEdit();
await tick();

await check('界面新建之后，接口真的能读到这份档案（落盘了，正文一字不差）', async () => {
  const view = await apiOpen(TMP_PATH);
  assert.equal(view.title, TMP_NAME);
  assert.equal(view.kind, 'person');
  assert.equal(view.body.trim(), createdBody.trim());
  assert.ok(view.hash, '真响应应带 hash');
});
await check('新建之后界面停在"刚建的那一份"上', () => {
  assert.equal(app.state.vaultEdit, null);
  assert.equal(app.state.vaultFile.data.path, TMP_PATH);
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-view-path')).textContent, TMP_PATH);
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-view-title')).textContent, TMP_NAME);
});
await check('新建之后目录树上多出这一份（真 /api/vault 重刷过了）', () => {
  assert.ok(treeFile(TMP_NAME), '树上应出现新档案');
});
await check('新建之后那句结果提示说了"已新建"', () => {
  assert.match(find(viewerNow(), (x) => x.classList.contains('vault-note')).textContent, /已新建/);
});

/* ① 本轮修的 bug：`**[[X]]**` 以前会原样显示方括号（bold 按纯文本输出）。
   这一份正文是**真的写进 vault、再从真接口读回来**的，所以它同时验了
   "前端渲染"与"这个正文确实从后端的原样字段里来"。 */
await check('加粗的双链（**[[X]]**）真的渲染成 <strong> 里的可点链接', () => {
  const strongs = walkAll(viewerNow()).filter((x) => x.tagName === 'STRONG');
  const box = strongs.find((s) => find(s, (x) => x.classList.contains('vault-link')));
  assert.ok(box, `没有任何 <strong> 里带链接；strong 文本：${strongs.map((s) => s.textContent).join(' / ')}`);
  const link = find(box, (x) => x.classList.contains('vault-link'));
  assert.equal(link.tagName, 'BUTTON', '命中的双链要能点');
  assert.equal(link.textContent, '小林');
  assert.equal(box.textContent, '小林', '方括号不该留在文字里');
});
await check('正文里（代码之外）不再出现 [[ 与 ]] —— 最能抓住回归的一条', () => {
  const text = textOutsideCode(viewerNow());
  assert.ok(!text.includes('[['), `还有没渲染的双链：${text}`);
  assert.ok(!text.includes(']]'), `还有没渲染的双链：${text}`);
});
await check('行内码里的 [[X]] 仍然是字面量（代码的语义没被改掉）', () => {
  const code = find(viewerNow(), (x) => x.tagName === 'CODE' && x.textContent === '[[小林]]');
  assert.ok(code, '行内代码里的双链应原样保留');
  assert.ok(!code.classList.contains('vault-link'), '代码里的双链不该变成链接');
});

// 编辑：正文改成别的，保存后重新从接口读
const loaded = app.state.vaultFile.data;
await check('编辑态的正文与载入的那份一致（预填）', () => {
  clickText(viewerNow(), '编辑');
  assert.equal(editorNode('vault-body').value, loaded.body, '正文应逐字预填');
  assert.equal(editorNode('vault-title').value, TMP_NAME);
  assert.equal(editorNode('vault-tags').value, loaded.tags.join(', '));
  assert.equal(editorNode('vault-tags').value, '', '这份档案本来没有 tags');
});
const editedBody = `${createdBody}\n\n后来又加了一行。`;

/* ② 字段标签改成中文了，但**发出去的契约不能变**：真的存一次，看请求体的键名。 */
await check('编辑器字段标签是中文，英文键名退到输入框的 title 提示里', () => {
  const labels = byClass(viewerNow(), 'vault-field-label').map((x) => x.textContent);
  for (const want of ['标题', '类型', '关系', '状态', '标签（逗号分隔）', '别名（逗号分隔）']) {
    assert.ok(labels.includes(want), `缺少中文标签「${want}」，实际：${labels.join(' / ')}`);
  }
  assert.ok(!labels.includes('title') && !labels.includes('kind'), '标签里不该还有英文键名');
  assert.equal(editorNode('vault-title').getAttribute('title'), '写进 frontmatter 的键名：title');
  assert.equal(editorNode('vault-aliases').getAttribute('title'), '写进 frontmatter 的键名：aliases');
});

/* ③ 「插入链接」：列表来自**真** /api/vault 的树，插进去的是 title。 */
await check('「插入链接」列出真 vault 里的档案，插入 [[title]] 并把光标交回正文', () => {
  clickText(viewerNow(), '插入链接');
  const panel = find(viewerNow(), (x) => x.classList.contains('vault-link-insert'));
  assert.ok(panel, '点开应列出档案');
  const items = byClass(panel, 'vli-item');
  assert.ok(items.length >= 5, `真 vault 里应有目录里那几份档案，实际 ${items.length} 项`);
  assert.ok(items.some((x) => x.textContent.includes('小张')), 'entities 下的档案要列出来');
  assert.ok(items.some((x) => x.textContent.includes('sections/偏好.md')), 'sections 下的档案也要列出来');
  assert.ok(!items.some((x) => x.textContent.includes('README.md')), 'README 是索引，不是能链接的档案');

  const item = items.find((x) => x.textContent.includes('小张'));
  assert.ok(item, '应有一项是小张');
  const area = editorNode('vault-body');
  area.selectionStart = 0;             // 光标放在开头
  area.selectionEnd = 0;
  area.onclick();                      // 等价于人在正文里点了一下（记下光标）
  item.onclick();
  const now = editorNode('vault-body');
  const token = '[[小张]]';
  assert.ok(now.value.startsWith(token), `应插在光标处，实际开头：${now.value.slice(0, 20)}`);
  assert.deepEqual(now.selectionSet, [token.length, token.length], '光标应回到刚插入的链接之后');
  assert.ok(now.focusCount >= 1, '焦点应回到正文框');
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-link-insert')), null, '插完面板应收起');
});

editorNode('vault-body').value = editedBody;
editorNode('vault-body').oninput();
const firstHash = app.state.vaultEdit.hash;
await check('编辑态手里拿的就是载入时那个真 hash', () => assert.equal(firstHash, loaded.hash));
await app.saveVaultEdit();
await tick();
await check('真存下去时请求体里的键名仍然是英文（11 个键，一个不多一个不少）', () => {
  const post = sends.filter((item) => item.url === '/api/vault/file').pop();
  assert.ok(post, '应有一次保存');
  assert.deepEqual(Object.keys(post.payload).sort(),
    ['aliases', 'body', 'expect_hash', 'kind', 'mode', 'path', 'place', 'relation', 'status', 'tags', 'title']);
  assert.equal(post.payload.title, TMP_NAME, '值也要落在对的键上');
});
await check('保存之后接口里是新正文，hash 也变了', async () => {
  const view = await apiOpen(TMP_PATH);
  assert.ok(view.body.includes('后来又加了一行。'));
  assert.notEqual(view.hash, firstHash);
});
await check('保存之后界面回到只读，显示新正文渲染出来的段落', () => {
  assert.equal(app.state.vaultEdit, null);
  assert.equal(viewNode('vault-body').textContent.includes('后来又加了一行。'), true);
});

// 409「内容被改过」：绕过界面直接改文件，再拿旧 hash 保存
await app.openVaultFile(TMP_PATH, { canonical: TMP_PATH });
clickText(viewerNow(), '编辑');
const MINE = `${editedBody}\n\n这一行是我敲进去的，保存时必须还在。`;
editorNode('vault-body').value = MINE;
editorNode('vault-body').oninput();
const staleHash = app.state.vaultEdit.hash;
await fetch(`${BASE}/api/vault/file`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ mode: 'update', path: TMP_PATH, body: '# 被人改过了', title: TMP_NAME,
    expect_hash: staleHash }),
});
await app.saveVaultEdit();
await tick();
await check('真 409：后端说"被改过了"，界面把它原样显示出来', () => {
  const notice = find(viewerNow(), (x) => x.classList.contains('vault-notice'));
  assert.ok(notice, '应有冲突提示');
  assert.match(notice.textContent, /被改过了/);
});
await check('真 409：我敲进去的正文一个字都没丢', () => {
  assert.equal(editorNode('vault-body').value, MINE);
  assert.equal(app.state.vaultEdit.draft.body, MINE);
});
await check('真 409：给了「看最新内容」与「仍然覆盖保存」两条路', () => {
  const buttons = walkAll(viewerNow()).filter((x) => x.tagName === 'BUTTON'
    && (x.textContent.includes('看最新内容') || x.textContent.includes('仍然覆盖保存')));
  assert.equal(buttons.length, 2, `实际按钮：${walkAll(viewerNow()).filter((x) => x.tagName === 'BUTTON').map((x) => x.textContent).join(' / ')}`);
});
await clickText(viewerNow(), '仍然覆盖保存（用最新 hash 再存一次）');
await tick();
await tick();
await check('「仍然覆盖保存」之后，接口里就是我的那一版', async () => {
  assert.equal(app.state.vaultEdit, null, '覆盖成功应离开编辑态');
  const view = await apiOpen(TMP_PATH);
  assert.ok(view.body.includes('这一行是我敲进去的'), `实际正文：${view.body.slice(-80)}`);
  assert.ok(!view.body.includes('被人改过了'), '覆盖之后不该还有"被人改过"的那一版');
});

// 删除：先让小林指向临时档案，删除确认里就该把"谁指向它"列出来
const lin = await apiOpen('entities/小林.md');
await fetch(`${BASE}/api/vault/file`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ mode: 'update', path: 'entities/小林.md', body: `${lin.body}\n\n相关：[[${TMP_NAME}]]`,
    title: lin.title, kind: lin.kind, expect_hash: lin.hash }),
});
await app.loadVault();
await app.openVaultFile(TMP_PATH, { canonical: TMP_PATH });
app.askDeleteVaultFile(app.state.vaultFile.data);
await check('删除确认（真反链）里列出了指向它的那一份', () => {
  const text = byId.get('dialog-body').textContent;
  assert.match(text, /有 1 份档案指向它/);
  assert.match(text, /小林/, `对话框文本：${text.slice(0, 200)}`);
  assert.match(text, /还没写/);
});
await byId.get('dialog-confirm').onclick();
await tick();
await check('真删除：接口里没有了（404），界面回到引导态', async () => {
  const gone = await apiFile(TMP_PATH);
  assert.equal(gone.status, 404);
  assert.equal(app.state.vaultFile, null);
  assert.equal(find(viewerNow(), (x) => x.classList.contains('vault-empty')).textContent,
    '从左边点一个档案看看。');
});
await check('删除结果把 dangling 说了出来（小林的双链变成了"还没写"）', () => {
  const note = find(viewerNow(), (x) => x.classList.contains('vault-note'));
  assert.ok(note, '应有删除结果提示');
  assert.match(note.textContent, new RegExp(`已删除 entities/${TMP_NAME}`));
  assert.match(note.textContent, /有 1 份档案的双链变成了"还没写"/);
  assert.match(note.textContent, /小林/);
});
await check('删除之后目录树上不再有它', () => {
  assert.equal(treeFile(TMP_NAME), null);
});

/* ⑧ 「这份档案还没写」→ 一键建它。删掉临时档案之后，小林里那条双链正好成了
   真的"还没写"（后端不再给它 path），拿它走一遍闭环。
   收尾那一句「取消」很重要：这里只是想验"预填对不对"，不该真把档案又建回来。 */
await app.openVaultFile('entities/小林.md', { canonical: 'entities/小林.md' });
await check('小林里指向已删档案的那条双链变成"还没写"，那段虚线文字本身可以点', () => {
  const miss = byClass(viewerNow(), 'vault-link')
    .find((x) => x.classList.contains('missing') && x.textContent === TMP_NAME);
  assert.ok(miss, `应有 [[${TMP_NAME}]] 的"还没写"`);
  assert.equal(miss.tagName, 'SPAN', '"还没写"本身不是链接（没假装能读）');
  assert.equal(miss.getAttribute('title'), '这个档案还没写');
  const create = walkAll(viewerNow())
    .find((x) => x.classList.contains('vault-link-create') && x.title.includes(TMP_NAME));
  assert.ok(create, '应有一个指向它的"点一下就建一份"入口');
  assert.equal(create.tagName, 'BUTTON');
  assert.equal(miss.parentNode, create, '点那段虚线文字要能命中这个按钮');
});
await check('点那段虚线文字 → 真的进新建态，名字与标题都预填成那个链接名', () => {
  const create = walkAll(viewerNow())
    .find((x) => x.classList.contains('vault-link-create') && x.title.includes(TMP_NAME));
  assert.ok(create.onclick, '「还没写」要真的能点');
  create.onclick();                    // 自检环境的 confirm 一律当"同意"
  assert.ok(app.state.vaultEdit, '应进新建态');
  assert.equal(app.state.vaultEdit.mode, 'create');
  assert.equal(app.state.vaultEdit.draft.name, TMP_NAME);
  assert.equal(app.state.vaultEdit.draft.title, TMP_NAME);
  assert.equal(editorNode('vault-name').value, TMP_NAME);
  assert.equal(editorNode('vault-title').value, TMP_NAME);
  assert.equal(editorNode('vault-edit-path').textContent, TMP_PATH);
});
await check('收尾：取消掉这次新建（别把刚删掉的临时档案又建回来）', () => {
  clickText(viewerNow(), '取消');
  assert.equal(app.state.vaultEdit, null);
});

/* ⑨ 「链接名用 title 而不是文件名」这条在**真接口**上也要站得住：
   建一份标题与文件名**不一样**的档案，再用它的**标题**从别处链过去。
   服务端（vault._name_to_path）真能解析出来，这个功能才算有用；
   只验"插进去的字符串对不对"是不够的。 */
const TMP2_NAME = 'tmp-ui-check-新建乙';
const TMP2_PATH = `entities/${TMP2_NAME}.md`;
const TMP2_TITLE = '临时乙号';
const post = (path, payload) => fetch(`${BASE}${path}`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
});
const stale2 = await apiFile(TMP2_NAME);
if (stale2.status === 200) {
  await post('/api/vault/file/delete', { path: TMP2_PATH, expect_hash: stale2.body.hash });
}
const made2 = await post('/api/vault/file', {
  mode: 'create', path: TMP2_PATH, title: TMP2_TITLE, kind: 'person',
  body: '# 临时乙号\n\n用来验"链接名用标题"。',
});
await check('前提：造出一份标题与文件名不同的档案', async () => {
  assert.equal(made2.status, 200, `创建失败：${made2.status}`);
  const view = await apiOpen(TMP2_PATH);
  assert.equal(view.title, TMP2_TITLE);
  assert.notEqual(view.title, TMP2_NAME, '标题与文件名必须是两个不同的字符串');
});
const linNow = await apiOpen('entities/小林.md');
await post('/api/vault/file', {
  mode: 'update', path: 'entities/小林.md', title: linNow.title, kind: linNow.kind,
  body: `${linNow.body}\n\n按标题链过去：[[${TMP2_TITLE}]]`, expect_hash: linNow.hash,
});
await app.loadVault();
await app.openVaultFile('entities/小林.md', { canonical: 'entities/小林.md' });
await check('用**标题**写的双链在真接口上解析成了那一份（文件名对不上也没关系）', () => {
  const link = byClass(viewerNow(), 'vault-link').find((x) => x.textContent === TMP2_TITLE);
  assert.ok(link, `正文里应有 [[${TMP2_TITLE}]]；实际：`
    + byClass(viewerNow(), 'vault-link').map((x) => x.textContent).join(' / '));
  assert.equal(link.tagName, 'BUTTON', '应解析成可点链接');
  assert.equal(link.getAttribute('title'), `打开 ${TMP2_PATH}`);
});
await check('点它读到的就是那一份（标题这条路真的通到文件）', async () => {
  const link = byClass(viewerNow(), 'vault-link').find((x) => x.textContent === TMP2_TITLE);
  calls.length = 0;
  link.onclick();
  await tick();
  assert.ok(calls.includes('/api/vault/file?path=' + encodeURIComponent(TMP2_PATH)), calls.join(' , '));
});
await check('收尾：第二份临时档案也删掉（自检收拾自己的垃圾）', async () => {
  const view = await apiOpen(TMP2_PATH);
  const gone = await post('/api/vault/file/delete', { path: TMP2_PATH, expect_hash: view.hash });
  assert.ok(gone.ok, `删除失败：${gone.status}`);
  assert.equal((await apiFile(TMP2_NAME)).status, 404);
});

// 收拾：把被自检改过的小林恢复原样（它就是别人断言用的夹具）
await fetch(`${BASE}/api/vault/file`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ mode: 'update', path: 'entities/小林.md', body: lin.body,
    title: lin.title, kind: lin.kind, relation: lin.relation, status: lin.status,
    tags: lin.tags, aliases: lin.aliases, expect_hash: (await apiOpen('entities/小林.md')).hash }),
});
await check('收尾：小林被恢复成自检开始前那一份', async () => {
  const now = await apiOpen('entities/小林.md');
  // 标点符号级别的相等，只容忍两处**写入约定**带来的差别：
  //   ① 行尾：夹具是 seed-vault.ps1（PowerShell）写的 CRLF，档案写入统一用 LF；
  //   ② 末尾：写档案时统一补一个换行。
  // 这两条都不是"内容变了"，所以按 trim 之后比 —— 比 hardcode 一个指纹稳，
  // 换个行尾的编辑器重写夹具也不会假红。
  assert.equal(now.body.trim(), lin.body.trim(), '正文应一字不差（只容忍行尾与末尾换行）');
  assert.ok(!now.body.includes('tmp-ui-check'), '不该留下自检加的链接');
});
await check('收尾：临时档案确实已经不在 vault 里了', async () => {
  const view = await app.loadVault().then(() => app.state.vault);
  assert.equal((view.files || []).some((f) => f.path === TMP_PATH), false, '清单里不该有它');
});

/* ⑩ 「格式化并初始化 dream」（ADR-072）。默认这一段**只跑不会改动任何东西的那一半**：
   真 400（确认词不对 → 后端什么都没做）、以及对话框现场从真接口读到的数字。
   真 200 会把整个 vault 清掉，而且 API 写不回 `mood` 这类自定义 frontmatter 键
   （只有 title/kind/relation/status/tags/aliases 六个键），夹具没法完整恢复 ——
   那会让下一次自检假红。所以它默认不跑，只做**显式**开关：
       $env:E2E_FORMAT_LIVE='1'; node scripts\ui_check\e2e-live.mjs http://127.0.0.1:8791
   （跑之前先备份/换一份一次性数据目录；跑完要重新 seed-vault.ps1。） */
const vaultBefore = await (await fetch(`${BASE}/api/vault`)).json();
const runsBefore = ((await (await fetch(`${BASE}/api/dreams`)).json()).runs || []).length;

const wrongResp = await post('/api/dream/format', { confirm: '这个不是那个词' });
const wrongBody = await wrongResp.json().catch(() => ({}));
await check('真 400：确认词不对时后端拒绝，并把 confirm_word 回给我们', () => {
  assert.equal(wrongResp.status, 400, `实际 ${wrongResp.status}`);
  assert.ok(wrongBody.confirm_word, `响应里应带 confirm_word：${JSON.stringify(wrongBody)}`);
  assert.match(String(wrongBody.error), /confirm/, `后端原话：${wrongBody.error}`);
});
await check('真 400：确认词不对时**什么都没做**（档案一个都没少）', async () => {
  const now = await (await fetch(`${BASE}/api/vault`)).json();
  assert.equal(now.check.files, vaultBefore.check.files);
  assert.equal((now.files || []).length, (vaultBefore.files || []).length);
});

/* 让界面吃**真的**那份 400 响应（不是手抄的文案）：显示的是不是后端原话、
   界面用的确认词与后端要的是不是同一个，这两条只有这样才验得实。 */
postHandler = (url) => (url === '/api/dream/format' ? { status: 400, body: wrongBody } : null);
await app.askFormatDream();
const fmtBody = byId.get('dialog-body');
const fmtInput = find(fmtBody, (x) => x.classList.contains('dream-format-input'));
const fmtConfirm = byId.get('dialog-confirm');
await check('对话框现场列出要失去什么（数字来自真 /api/vault）', () => {
  assert.match(fmtBody.textContent, new RegExp(`背景档案：${vaultBefore.check.files} 份`));
  assert.match(fmtBody.textContent, new RegExp(`整理运行记录：${runsBefore} 条`));
  assert.match(fmtBody.textContent, /不会被碰/);
  assert.match(fmtBody.textContent, /手写的内容不会回来/);
});
await check('界面用的确认词与后端要的是同一个（"不硬编码"要防的就是这两边走样）', () => {
  assert.equal(wrongBody.confirm_word, app.formatWord(),
    `后端要「${wrongBody.confirm_word}」，界面却在提示「${app.formatWord()}」`);
});
await check('确认词没打对之前，执行按钮是禁用的', () => {
  assert.equal(fmtConfirm.disabled, true);
  fmtInput.value = `${app.formatWord()}不行`;
  fmtInput.oninput();
  assert.equal(fmtConfirm.disabled, true, '打错不该解锁');
  fmtInput.value = app.formatWord();
  fmtInput.oninput();
  assert.equal(fmtConfirm.disabled, false, '打对了才该解锁');
});
await clickNode(fmtConfirm, '格式化对话框的确认按钮');
await check('真 400 的原话原样显示在对话框里，而且对话框不关', () => {
  assert.equal(byId.get('dialog').classList.contains('hidden'), false, '失败不该把对话框关掉');
  const line = find(fmtBody, (x) => x.classList.contains('dream-format-error'));
  assert.ok(line, '应有错误行');
  assert.equal(line.textContent, wrongBody.error, '必须是后端原话');
});
await check('发出去的请求体就是 {confirm: "<那个词>"}，一个字段都不多', () => {
  const sent = sends.filter((item) => item.url === '/api/dream/format').pop();
  assert.ok(sent, '应发出过格式化请求');
  assert.deepEqual(Object.keys(sent.payload), ['confirm']);
  assert.equal(sent.payload.confirm, app.formatWord());
});
byId.get('dialog-cancel').onclick();      // 关掉，别挡住后面

/* 真 409（整理正在运行）造不出来 —— 要真的跑一次整理才会占住那把锁，而那是要花钱的。
   所以这一条用**合成的** 409 响应，验的是前端分支：原话照显示、对话框不关。 */
postHandler = (url) => (url === '/api/dream/format'
  ? { status: 409, body: { error: '整理正在运行 —— 等它跑完再格式化' } }
  : null);
await app.askFormatDream();
const s409Body = byId.get('dialog-body');
const s409Input = find(s409Body, (x) => x.classList.contains('dream-format-input'));
s409Input.value = app.formatWord();
s409Input.oninput();
await byId.get('dialog-confirm').onclick();
await check('409（整理正在运行）的原话显示在对话框里，可以等它跑完再试', () => {
  assert.equal(byId.get('dialog').classList.contains('hidden'), false);
  const line = find(s409Body, (x) => x.classList.contains('dream-format-error'));
  assert.equal(line.textContent, '整理正在运行 —— 等它跑完再格式化');
  assert.equal(byId.get('dialog-confirm').disabled, false);
});
byId.get('dialog-cancel').onclick();

if (process.env.E2E_FORMAT_LIVE) {
  /* **显式开关**下的真 200：真的把这一份数据目录的 vault / 运行记录 / 游标清掉。
     只对一次性数据目录用（跑完重新 seed）。 */
  const restoreList = [];
  for (const path of ['README.md', ...vaultBefore.files.map((f) => f.path)]) {
    const view = await apiOpen(path);
    if (view && view.path) restoreList.push(view);
  }
  await check('真 200 的前置：格式化之前先把每一份都读下来（待会儿照它恢复）', () => {
    assert.equal(restoreList.length, vaultBefore.files.length + 1, 'README + 每一份档案');
  });

  postHandler = null;
  await app.askFormatDream();
  const okInput = find(byId.get('dialog-body'), (x) => x.classList.contains('dream-format-input'));
  okInput.value = app.formatWord();
  okInput.oninput();
  await byId.get('dialog-confirm').onclick();
  await tick();
  await check('真 200：before 就是刚才数出来的那些，after 全是 0、leftovers 是空的', () => {
    const result = app.state.dreamFormatResult;
    assert.ok(result, '应有执行结果');
    assert.equal(result.before.vault_files, vaultBefore.check.files);
    assert.equal(result.before.dream_runs, runsBefore);
    assert.deepEqual(result.after, { vault_files: 0, dream_runs: 0, cursor_sessions: 0 });
    assert.deepEqual(result.leftovers, []);
    assert.equal(result.initialized, true);
    // `moved` 只列**真的挪走了**的那几个目录（这份夹具里没有运行记录，dreams/ 可能压根不存在），
    // 所以不能写死两个都在 —— 只要求它是这两个名字的子集、且 vault 一定在里面。
    assert.ok((result.moved || []).includes('vault'), `moved 里应有 vault：${JSON.stringify(result.moved)}`);
    assert.deepEqual(result.moved.filter((name) => !['vault', 'dreams'].includes(name)), []);
  });
  await check('真 200：界面把 before → after 留在页面上', () => {
    const card = find(byId.get('dream-list'), (x) => x.classList.contains('dream-format-result'));
    assert.ok(card, '结果条应在页面上');
    assert.match(card.textContent, new RegExp(`背景档案：${vaultBefore.check.files} → 0 份`));
    assert.match(card.textContent, /运行记录：\d+ → 0 条/);
    assert.match(card.textContent, /整理进度：\d+ → 0 个会话/);
  });
  await check('真 200：接口那一侧真的空了（不是界面自说自话）', async () => {
    const now = await (await fetch(`${BASE}/api/vault`)).json();
    assert.equal(now.check.files, 0);
    assert.equal(((await (await fetch(`${BASE}/api/dreams`)).json()).runs || []).length, 0);
  });

  /* 恢复夹具：正文与那六个能从接口写回去的元信息键都写回原位。
     **写入有单文件上限**（`HARNESS_VAULT_WRITE_MAX_CHARS`，默认 12000 字符），
     所以那份"故意撑到二十万字符"的长档案**没法通过接口恢复** —— 它会被拒（400），
     如实记下来、不假装成功。这也是这个开关默认关着的原因。 */
  let restored = 0;
  const unrestorable = [];
  for (const view of restoreList) {
    const current = await apiFile(view.path);
    const payload = {
      mode: current.status === 200 ? 'update' : 'create',
      path: view.path, body: view.body,
      title: view.title, kind: view.kind, relation: view.relation, status: view.status,
      tags: view.tags || [], aliases: view.aliases || [],
    };
    if (current.status === 200) payload.expect_hash = current.body.hash;
    const resp = await post('/api/vault/file', payload);
    if (resp.ok) restored += 1;
    else {
      unrestorable.push(view.path);
      console.log(`    （${view.path} 没能写回去：HTTP ${resp.status}`
        + ' —— 超过单文件写入上限的内容没法通过接口恢复）');
    }
  }
  await check('收尾：能通过接口写回去的档案都恢复原位（写不回去的那份如实排除）', async () => {
    assert.equal(restored, restoreList.length - unrestorable.length, '成功数应与"没被排除的"对上');
    const now = await app.loadVault().then(() => app.state.vault);
    assert.equal(now.check.files, vaultBefore.check.files - unrestorable.length,
      `应恢复 ${vaultBefore.check.files - unrestorable.length} 份（写不回去的：${unrestorable.join('、') || '无'}）`);
    const onTree = byClass(treeNow(), 'vn-title').map((x) => x.textContent);
    for (const view of restoreList) {
      if (unrestorable.includes(view.path)) continue;
      const want = view.path === 'README.md' ? 'README' : view.title;
      assert.ok(onTree.includes(want), `树上应有 ${want}，实际：${onTree.join(' / ')}`);
    }
  });
  console.log('  （E2E_FORMAT_LIVE=1：真格式化跑过了。自定义 frontmatter 键（mood 之类）与'
    + '超过写入上限的长档案写不回去，这份夹具请重新 seed-vault.ps1）');
} else {
  console.log('  · 跳过"真格式化并初始化"（要显式开 E2E_FORMAT_LIVE=1；它会清空夹具）');
}

/* ⑪ 图片附件（docs/11-attachments.md）。

   默认只验**不碰网络**的那一半：入口三件套接在哪个元素上。真上传要
   `POST /api/attachments`（另一条线正在实现），所以它由显式开关控制：

       $env:E2E_ATTACH_LIVE='1'; node scripts\ui_check\e2e-live.mjs http://127.0.0.1:8791

   开关打开时做的事：真的把一张 24×16 的 PNG 传上去（走 ui 的入口函数，
   不是直接打接口）→ 真拿服务端给的 URL 去 GET 那串字节 → 断言缩略图用的
   就是那个 URL。**不**发 /api/chat：那要花模型的钱，而且 provider 得配好；
   "模型真的看到了图"这件事只能人工跑一次，报告里如实写。 */
await check('附件入口：拖放挂在输入区（不是 document/window）、粘贴挂在输入框、📎 点的是隐藏的 file input', () => {
  app.wireComposerAttachments();
  const zone = byId.get('composer-drop');
  for (const type of ['dragover', 'dragleave', 'drop']) {
    assert.equal(zone.listenerCount(type), 1, `输入区应有 1 个 ${type} 监听`);
  }
  assert.equal(byId.get('input').listenerCount('paste'), 1);
  assert.equal(doc.listenerCount('drop'), 0, 'document 上不该有 drop');
  const picker = byId.get('attach-file');
  picker.clickCount = 0;
  byId.get('attach').onclick();
  assert.equal(picker.clickCount, 1, '点 📎 应触发隐藏 file input 的 click');
  assert.ok(byId.get('lightbox').classList.contains('hidden'), '灯箱一开始是关着的');
});

if (process.env.E2E_ATTACH_LIVE) {
  // 92 字节的合法 PNG（24×16 纯色）：由 Pillow 生成后把 base64 抄在这里，
  // 不依赖仓库里再多一个二进制夹具文件。
  const PNG_BASE64 = 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAQCAIAAACDRijCAAAAI0lEQVR4nGM8ISfH'
    + 'QA3ARBVTGEYNIgaMBjZhMBpGhMHgCyMA7q4BJFSoQw4AAAAASUVORK5CYII=';

  app.state.attachments = [];
  app.state.sessionId = null;          // 新对话：服务端会给一个 id
  app.renderAttachments();

  const item = await app.uploadAttachmentData({
    name: 'e2e-attach-probe.png', mime: 'image/png', data_base64: PNG_BASE64,
  });
  await check('真上传：服务端给了会话 id 与附件 id，扩展名是 png', () => {
    assert.ok(item.id, `附件 id 应有：${JSON.stringify(item)}`);
    assert.equal(item.ext, 'png');
    assert.ok(app.state.sessionId, '服务端应回一个新会话 id');
    assert.match(item.url, /^\/api\/attachments\?session=/);
  });

  const imgResp = await fetch(BASE + item.url);
  await check('缩略图那个 URL 真的取得到图片字节（不是 404、也不是本地兜底）', async () => {
    assert.equal(imgResp.status, 200, `GET ${item.url} 实际 ${imgResp.status}`);
    assert.match(String(imgResp.headers.get('content-type') || ''), /^image\//);
    const bytes = new Uint8Array(await imgResp.arrayBuffer());
    assert.ok(bytes.length > 0, '字节数应大于 0');
    assert.equal(bytes[0], 0x89, 'PNG 魔数第一字节');
  });

  app.state.attachments.push(item);
  app.renderAttachments();
  await check('待发条上的缩略图 src 就是服务端那个 URL（刷新后历史也走同一条路）', () => {
    const host = byId.get('composer-attachments');
    assert.equal(find(host, (x) => x.classList.contains('attach-thumb')).getAttribute('src'), item.url);
  });

  // 真会话视图里 user 消息的附件长什么样，只有在真的发过一轮之后才有 ——
  // 这里先把**服务端给的那个 attachment** 按契约 §7.4 的形状喂给渲染函数，
  // 验"字段读得懂"；这一条用的是合成消息，不是真会话记录（报告里写明）。
  byId.get('messages').innerHTML = '';
  app.renderStoredMessage({
    role: 'user', text: '',
    attachments: [{
      id: item.id, name: item.name, mime: item.mime,
      width: item.width, height: item.height, bytes: item.bytes, url: item.url,
    }],
  });
  await check('历史气泡能渲染服务端那份 attachment 的 url（合成消息，不是真记录）', () => {
    const img = find(byId.get('messages'), (x) => x.classList.contains('attach-thumb'));
    assert.equal(img.getAttribute('src'), item.url);
  });

  console.log(`  （E2E_ATTACH_LIVE=1：真上传跑过了，落在会话 ${app.state.sessionId} 的附件目录里；`
    + '没有清理接口，这份夹具会多一个文件）');
} else {
  console.log('  · 跳过"真上传图片"（要显式开 E2E_ATTACH_LIVE=1；它需要后端已实现 /api/attachments）');
}

/* ⑫ 「多模态」开关（契约 §13）：默认开、tools 帧两个键一起回填。
   这一段**一个网络请求都不发**：只把 SSE 的 tools 帧喂给真的处理函数，
   看状态与按钮跟不跟得上。默认开关下（不带 E2E_* 变量）它也会跑。 */
app.state.attachments = [];
app.renderAttachments();
await check('多模态：默认**开**（缺键 = 开），tools 帧两个键一起回填', () => {
  const before = calls.length;
  const button = byId.get('toggle-multimodal');
  const label = () => find(button, (x) => x.classList.contains('label')).textContent;

  app.renderMultimodalToggle();
  assert.equal(app.state.multimodal, true, '默认必须是开（契约 §13.1）');
  assert.ok(button.classList.contains('on'), '默认态按钮应是 on');
  assert.equal(label(), '多模态：开');

  // tools 帧：契约里它固定回两个键
  app.applyToolsFrame({ web_search: true, multimodal: false });
  assert.equal(app.state.multimodal, false, '帧里说 false 就要跟着关');
  assert.equal(app.state.webSearch, true, '同一个帧里的 web_search 也要读到');
  assert.equal(label(), '多模态：关');
  assert.ok(button.classList.contains('off'));

  app.applyToolsFrame({ web_search: false, multimodal: true });
  assert.equal(app.state.multimodal, true);
  assert.equal(label(), '多模态：开');

  // 老会话 / 老后端：帧里**没有**这个键时必须是"开"，
  // 否则一个缺字段就会把图片悄悄降级成 OCR 文字
  app.state.multimodal = false;
  app.applyToolsFrame({ web_search: false });
  assert.equal(app.state.multimodal, true, '缺 multimodal 键 = 开');

  assert.equal(calls.length, before, '这一段不该发任何网络请求');
});
await check('splitModelList：空串 → 空列表（接口表单那个字段的规矩）', () => {
  assert.deepEqual([...app.splitModelList('')], []);
  assert.deepEqual([...app.splitModelList(' deepseek-flash , deepseek-vl ')],
    ['deepseek-flash', 'deepseek-vl']);
});

/* ⑬ §13.7 第 3 句：OCR 文字块**默认折叠**，标题里带状态摘要。
   这一段也用真函数（userBubbleNode）建节点，但**不发任何网络请求**。 */
await check('§13.7 第 3 句：OCR 文字块默认折叠，标题带状态摘要，点一下展开', () => {
  const attachment = {
    id: 'aaaa00000000000e', name: '字图.png', mime: 'image/png',
    url: '/api/attachments?session=s&id=aaaa00000000000e&ext=png',
    // 契约 §13.3 的 ocr：引擎 / 秒数 / 字数 / 原文
    ocr: { engine: 'rapidocr-onnxruntime 1.2.3', seconds: 1.2, chars: 12, text: '第一行\n第二行' },
  };
  const node = app.userBubbleNode('这张图写了什么？', [attachment], 'text');
  const box = byClass(node, 'ocr-block')[0];
  assert.ok(box, '应有 .ocr-block');
  assert.ok(box.classList.contains('collapsed'), '默认必须是折叠态（与刷新后的历史回放同一个形状）');
  assert.equal(find(box, (n) => n.classList.contains('ocr-label')).textContent,
    '多模态已关 · 只发送了下面的识别文本 · 12 字');
  assert.equal(find(box, (n) => n.classList.contains('chev')).textContent, '▸');
  // 折叠不等于丢掉信息：正文仍在，预览是一行
  assert.match(box.textContent, /【图片 1：字图.png】/);
  assert.equal(find(box, (n) => n.classList.contains('ocr-preview')).textContent, '第一行');
  find(box, (n) => n.classList.contains('ocr-head')).onclick();
  assert.equal(box.classList.contains('collapsed'), false, '点标题就展开');
  assert.equal(find(box, (n) => n.classList.contains('chev')).textContent, '▾');
});

await check('那句"图片会随请求发给模型"只在开关打开时出现（关着时与有没有待发图片无关）', () => {
  const hint = byId.get('attach-hint');
  const wasBusy = app.state.busy;
  app.state.busy = false;
  app.state.multimodal = false;
  app.state.attachments = [];
  app.renderAttachments();
  assert.ok(hint.classList.contains('hidden'), '关着 + **没有**待发图片时它也不许出现');
  app.state.attachments = [{ id: 'aaaa00000000000f', ext: 'png', name: 'a.png', url: '/x.png' }];
  app.renderAttachments();
  assert.ok(hint.classList.contains('hidden'), '关着 + 有待发图片时同样不出现');
  app.state.multimodal = true;
  app.state.attachments = [];
  app.renderAttachments();
  assert.equal(hint.classList.contains('hidden'), false, '开关打开时回来');
  app.state.busy = wasBusy;
  app.state.attachments = [];
  app.renderAttachments();
});

/* ⑭ 设置 → 提示词（docs/13-prompts-page.md）：**真 /api/prompts → 真 app.js**。
   这一节恰好是 e2e 最该管的那种事：那一页上的每一个字都是服务端给的
   （提示词原文、工具描述、when / condition），前端只负责画 —— 所以"字段读懂了没有"
   只能拿真响应来验。**不调模型、不写盘**（接口本身是只读的）。 */
const promptsResp = await fetch(`${BASE}/api/prompts`);
const promptsBody = await promptsResp.json().catch(() => ({}));
const livePrompts = Array.isArray(promptsBody.groups)
  ? promptsBody.groups.flatMap((group) => group.items) : [];
const liveTools = Array.isArray(promptsBody.tools)
  ? promptsBody.tools.flatMap((group) => group.items) : [];
// 这一节要用**真路由**：切语言那条路径只在"当前视图就是设置"时才重画子页
// （rerenderForLanguage 会调 showSettings(state.pane)）。前面几节把视图留在了对话页，
// 所以要先把位置摆正 —— 否则切语言之后这一页会停在旧语言上，而那正是要验的事。
app.state.pane = 'prompts';
app.state.view = 'settings';

await check('真 /api/prompts 形状（后面全部断言的前提）：22 条提示词 + 16 个工具，5+2 个分组', () => {
  if (promptsResp.status !== 200 || !livePrompts.length) {
    throw new Error(`/api/prompts 没给出这一页要的形状（status=${promptsResp.status}）`);
  }
  assert.equal(livePrompts.length, 22, '契约 §2.3：全部公开常量，LANG 除外');
  assert.equal(liveTools.length, 16, '契约 §2.3：对话侧 9（含 time）+ 整理侧 7');
  assert.deepEqual([...promptsBody.groups.map((g) => g.id)].sort(),
    ['compact', 'dream', 'render', 'system', 'tail']);
  assert.deepEqual([...promptsBody.tools.map((g) => g.id)].sort(), ['chat', 'dream']);
});

await check('真响应喂给真 app.js：22 + 16 全部画出来，两条钩子都在', async () => {
  await app.loadPrompts();
  const body = byId.get('prompts-body');
  const promptCards = byClass(body, 'prompt-card').filter((n) => n.dataset.promptId);
  assert.equal(promptCards.length, 22);
  assert.equal(byClass(body, 'tool-card').length, 16);
  assert.deepEqual([...promptCards.map((n) => n.dataset.promptId)].sort(),
    [...livePrompts.map((item) => item.id)].sort());
  assert.deepEqual([...byClass(body, 'tool-card').map((n) => n.dataset.toolId)].sort(),
    [...liveTools.map((item) => item.name)].sort());
});

/* 分组**默认折叠**（契约 §5 第 2 条）：外壳是原生 `<details>`，真响应来的 5 个
   提示词组 + 2 个工具组一个都不带 `open`。后面所有"数 22 / 16"的断言都建立在
   "折的只是可见性、元素还在 DOM 里"这件事上。 */
await check('7 个分组都是原生 <details class="prompts-group">，默认一个都不带 open（真响应）', () => {
  const groups = byClass(byId.get('prompts-body'), 'prompts-group');
  assert.equal(groups.length, 7, '五个提示词组 + 两个工具组');
  for (const group of groups) {
    const head = find(group, (n) => n.tagName === 'SUMMARY');
    assert.ok(head, '每个分组都要有 <summary>');
    assert.equal(group.tagName, 'DETAILS', '分组必须是原生 <details>（键盘可达、零 JS 状态）');
    assert.equal(group.getAttribute('open'), null,
      `默认应当是折起来的：${head.textContent.slice(0, 40)}`);
    assert.equal(group.children[0], head, 'summary 必须是 details 的第一个子节点');
    assert.equal(head.onclick, null, 'summary 上不该挂 onclick（否则就是手风琴的写法了）');
    assert.equal(head.listenerCount('click'), 0, 'summary 上不该有 click 监听');
  }
});

await check('全折叠时 22 / 16 个钩子元素**仍然在 DOM 里**（折的是可见性，不是元素）', () => {
  const body = byId.get('prompts-body');
  const groups = byClass(body, 'prompts-group');
  assert.ok(groups.every((group) => group.getAttribute('open') === null), '前置：七组全折着');
  const prompts = walkAll(body).filter((n) => n.dataset && n.dataset.promptId);
  const tools = walkAll(body).filter((n) => n.dataset && n.dataset.toolId);
  assert.equal(prompts.length, 22);
  assert.equal(tools.length, 16);
  const insideGroup = (node) => {
    for (let cur = node.parentNode; cur; cur = cur.parentNode) {
      if (cur.classList && cur.classList.contains('prompts-group')) return true;
    }
    return false;
  };
  assert.ok(prompts.every(insideGroup) && tools.every(insideGroup),
    '折着的那些条目应当在某个分组的折叠体里，而不是被挪走/删掉');
});

await check('分组 summary 里的条目数与状态统计**照真响应算**（为 0 的那种不显示）', () => {
  const groups = [...promptsBody.groups, ...promptsBody.tools];
  const nodes = byClass(byId.get('prompts-body'), 'prompts-group');
  const expectedTally = (items) => {
    let on = 0; let off = 0; let session = 0;
    for (const item of items) {
      if (item.state === 'off') off += 1;
      else if (item.state === 'session') session += 1;
      else on += 1;
    }
    const parts = [];
    if (on) parts.push(app.t('prompts.tally_on', { n: on }));
    if (off) parts.push(app.t('prompts.tally_off', { n: off }));
    if (session) parts.push(app.t('prompts.tally_session', { n: session }));
    return parts.join(' · ');
  };
  for (const [index, group] of groups.entries()) {
    const head = find(nodes[index], (n) => n.tagName === 'SUMMARY');
    assert.ok(head.textContent.includes(group.title), `summary 里缺标题：${head.textContent}`);
    assert.ok(head.textContent.includes(app.t('prompts.group_count', { n: group.items.length })),
      `summary 里缺条目数：${head.textContent}`);
    const tally = find(head, (n) => n.classList.contains('pg-tally'));
    assert.equal(tally ? tally.textContent : '', expectedTally(group.items),
      `「${group.title}」的统计对不上`);
    if (group.note) assert.ok(!head.textContent.includes(group.note), '说明句不该在 summary 里');
  }
});

await check('开一个分组**不影响别的**（原生 details 没有手风琴行为）', () => {
  const groups = byClass(byId.get('prompts-body'), 'prompts-group');
  // 原生 `<details>` 的开合就是加 / 摘 `open` 属性：这里照浏览器做一遍
  groups[0].setAttribute('open', '');
  assert.equal(groups[0].getAttribute('open'), '', '前置：这一组开了');
  for (const [index, other] of groups.entries()) {
    if (index === 0) continue;
    assert.equal(other.getAttribute('open'), null, `第 ${index + 1} 组被带着一起开了`);
  }
  // 展开的那一组里，条目照样在 DOM 里（展开不动元素，只动可见性）
  assert.equal(walkAll(groups[0]).filter((n) => n.dataset && n.dataset.promptId).length,
    promptsBody.groups[0].items.length);
  groups[0].removeAttribute('open');
  assert.ok(groups.every((group) => group.getAttribute('open') === null), '收起来之后仍是全折');
});

await check('摘要行与**真响应**里的数字一致（不写死夹具的数字）', () => {
  const text = byId.get('prompts-body').textContent;
  assert.ok(text.includes(app.t('prompts.summary', {
    prompts: promptsBody.summary.prompts, tools: promptsBody.summary.tools,
  })), `摘要行对不上真响应：${text.slice(0, 120)}`);
  assert.ok(text.includes(app.t('prompts.on', { on: promptsBody.summary.tools_on })));
});

await check('状态徽标三种文案都按契约 §2.2 的界面词翻（真 state 值喂进去）', () => {
  assert.equal(app.promptStateLabel('on'), '生效');
  assert.equal(app.promptStateLabel('off'), '未注册');
  assert.equal(app.promptStateLabel('session'), '会话级');
  // 真响应当前这份配置下的三种值都能在页面上找到对应徽标
  for (const state of new Set(liveTools.map((item) => item.state))) {
    assert.ok(byClass(byId.get('prompts-body'), 'prompts-state')
      .some((n) => n.textContent === app.promptStateLabel(state)), `页面上缺 ${state} 徽标`);
  }
});

await check('每条提示词的原文都在折叠块里（默认折叠），且与真响应逐字相同', () => {
  const card = find(byId.get('prompts-body'), (n) => n.dataset
    && n.dataset.promptId === 'SYSTEM_PROMPT');
  const box = find(card, (n) => n.classList.contains('prompt-details'));
  assert.ok(box.classList.contains('collapsed'), '原文默认折叠');
  const pre = find(box, (n) => n.tagName === 'PRE');
  const live = livePrompts.find((item) => item.id === 'SYSTEM_PROMPT');
  assert.equal(pre.textContent, live.text, '页面上那份必须就是服务端那一份');
});

await check('参数表按真 schema 的 properties 逐行画（名字 / 类型 / 必填 / 说明）', () => {
  const live = liveTools.find((item) => item.name === 'web_search')
    || liveTools.find((item) => Object.keys(item.parameters.properties || {}).length);
  const card = find(byId.get('prompts-body'), (n) => n.dataset
    && n.dataset.toolId === live.name);
  const table = find(card, (n) => n.classList.contains('prompt-params'));
  const keys = Object.keys(live.parameters.properties || {});
  assert.equal(table.children.length - 1, keys.length);
  assert.deepEqual([...table.children[0].children.map((c) => c.textContent)],
    ['参数', '类型', '必填', '说明']);
  assert.deepEqual([...table.children.slice(1).map((row) => row.children[0].textContent)],
    keys);
  const raw = JSON.parse(find(find(card, (n) => n.classList.contains('prompt-details')),
    (n) => n.tagName === 'PRE').textContent);
  assert.deepEqual(raw, live.parameters, '原始 JSON 必须就是服务端那份 schema');
});

await check('切到 en：这一页整页变英文（含提示词原文与工具描述），而且**不刷新页面**', async () => {
  // 前置：确保真的从中文出发（语言是**服务端**的设置，上一次跑可能把它留在 en）
  if (app.currentLanguage() !== 'zh') {
    await app.chooseLanguage('zh');
    await waitFor(() => app.currentLanguage() === 'zh', '切回中文');
  }
  assert.equal(app.currentLanguage(), 'zh', '前置条件：从中文出发');
  await app.chooseLanguage('en');
  // 等这一页真的按新语言重画出来（子页那次重新拉接口没有 await，见 waitFor 的注释）
  await waitFor(() => /tool definitions/.test(byId.get('prompts-body').textContent),
    '提示词页重画成英文');
  assert.equal(app.currentLanguage(), 'en', '前置条件：语言真的切过去了');
  const text = byId.get('prompts-body').textContent;
  assert.ok(!/[\u4e00-\u9fff]/.test(text), `英文页面上不该有汉字：${text.slice(0, 300)}`);
  assert.ok(text.includes(app.t('prompts.summary', {
    prompts: promptsBody.summary.prompts, tools: promptsBody.summary.tools,
  })));
  assert.equal(app.promptStateLabel('on'), 'In effect');
  assert.equal(byClass(byId.get('prompts-body'), 'prompt-card')
    .filter((n) => n.dataset.promptId).length, 22, '换语言之后钩子一个都没丢');
  // 收尾切回中文：语言是**服务端**的设置，留在 en 会让后面（以及下一次跑）的断言看到英文
  await app.chooseLanguage('zh');
  await waitFor(() => /条提示词/.test(byId.get('prompts-body').textContent), '提示词页切回中文');
});
await check('切回中文：页面又变回中文（同一次运行里来回切两次都不刷新）', () => {
  assert.equal(app.currentLanguage(), 'zh');
  assert.ok(/[\u4e00-\u9fff]/.test(byId.get('prompts-body').textContent));
});
await check('接口是只读的：这一节**一个 POST 都没发**（看一眼不会写盘）', () => {
  const posts = sends.filter((item) => item.url === '/api/prompts');
  assert.deepEqual(posts, []);
});

/* ⑮ 提示词**可编辑**（契约 docs/14-editable-prompts.md §8）：真接口 → 真 app.js。
   ⚠ 这一节**真的会写盘**：update / revert 都写夹具数据目录里的 `prompts.json`。
   所以它只在这两个前提下跑：
     * 脚本开头那道 `assertFixtureTarget()` 已经把"指向真实 data/"挡掉了；
     * 收尾必须把改过的东西**复原回去** —— 最后那两条断言就是"夹具没留下状态"。
   语言隔离在这里用**真接口的 `overrides`** 验一遍：两门语言各一份清单，
   动了中文之后英文那一门必须一个字都没变。 */

/** 真接口的 GET /api/prompts（写完之后重新看一眼服务端到底怎么记的）。 */
async function apiPrompts() {
  const resp = await fetch(`${BASE}/api/prompts`);
  return { status: resp.status, body: await resp.json().catch(() => ({})) };
}
/** 真接口的 POST（**不经界面**：用来做前置清场与最终核对）。 */
async function apiPost(path, payload) {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return { status: resp.status, body: await resp.json().catch(() => ({})) };
}
const overridesIn = (body) => ({ zh: (body.overrides || {}).zh || [], en: (body.overrides || {}).en || [] });
const promptsOf = (body) => (body.groups || []).flatMap((group) => group.items);
const liveItem = (body, id) => promptsOf(body).find((item) => item.id === id) || null;

/* 页面上那条卡片 / 卡片里的某个零件：**每次都当场找**（保存之后整页重画过） */
const promptCardBy = (id) => find(byId.get('prompts-body'),
  (node) => node.dataset && node.dataset.promptId === id);
const partIn = (card, cls) => (card ? find(card, (node) => node.classList.contains(cls)) : null);
/** 点某个零件；**等它的处理函数跑完**（保存那条链里套着"重新拉一次接口"）。 */
async function clickPart(id, cls) {
  const card = promptCardBy(id);
  if (!card) throw new Error(`页面上没有 ${id} 这条提示词（前置没摆好）`);
  const node = partIn(card, cls);
  if (!node) throw new Error(`${id} 上没有 .${cls}`);
  const back = node.onclick ? node.onclick() : undefined;
  if (back && typeof back.then === 'function') await back;
  await tick();
  return node;
}
/** 在这一条上开编辑器、填文本（textarea 的 value 与 oninput 都照真浏览器来） */
function typeInEditor(id, text) {
  const card = promptCardBy(id);
  const area = card && find(card, (node) => node.tagName === 'TEXTAREA');
  if (!area) throw new Error(`${id} 上没有 textarea（编辑器没开）`);
  area.value = text;
  if (area.oninput) area.oninput();
  return area;
}
const overridesLineText = () => {
  const node = find(byId.get('prompts-body'), (n) => n.classList.contains('prompts-overrides'));
  return node ? node.textContent : '';
};
const revertAllBtn = () => find(byId.get('prompts-body'),
  (n) => n.classList.contains('prompts-revert-all'));
const noteNode = () => find(byId.get('prompts-body'), (n) => n.classList.contains('prompt-note'));

const liveLangBefore = app.currentLanguage();
const zhBefore = livePrompts.find((item) => item.id === 'SYSTEM_PROMPT');
const zhBuiltin = zhBefore ? zhBefore.default_text : '';
// 前置清场：把两门语言都复原一次（上一次跑剩下状态时，下面每条断言都会被带偏）。
// `revert` 没东西可清时**不写文件**，所以这一步对干净的夹具是无副作用的。
await apiPost('/api/prompts/revert', { language: 'zh' });
await apiPost('/api/prompts/revert', { language: 'en' });
await app.loadPrompts();

await check('前置：两门语言都没有覆盖（先对着夹具清一次），这一页是 22 条中文提示词', async () => {
  const live = await apiPrompts();
  assert.equal(live.status, 200);
  assert.deepEqual(overridesIn(live.body), { zh: [], en: [] },
    '前置清场没干净：夹具里还留着上一次跑的覆盖');
  assert.equal(app.currentLanguage(), 'zh', '前置：界面语言是中文');
  assert.equal(byClass(byId.get('prompts-body'), 'prompt-card')
    .filter((n) => n.dataset.promptId).length, 22);
  await app.loadPrompts();
});

await check('保存一条（真 POST /api/prompts/update）：页面上出现「已修改」+「复原」', async () => {
  const keep = `${zhBuiltin}\n# 自检追加的一行（保留全部占位符）\n`;
  await clickPart('SYSTEM_PROMPT', 'pc-edit');
  assert.ok(find(promptCardBy('SYSTEM_PROMPT'), (n) => n.tagName === 'TEXTAREA'),
    '点「编辑」之后应当出现编辑器');
  typeInEditor('SYSTEM_PROMPT', keep);
  const before = sends.length;
  await clickPart('SYSTEM_PROMPT', 'pc-save');
  const posted = sends.slice(before);
  assert.deepEqual(posted.map((item) => item.url), ['/api/prompts/update']);
  assert.deepEqual(posted[0].payload, { language: 'zh', id: 'SYSTEM_PROMPT', text: keep });

  const card = promptCardBy('SYSTEM_PROMPT');
  assert.equal(partIn(card, 'pc-edited').textContent, '已修改');
  assert.equal(partIn(card, 'pc-revert').textContent, '复原');
  assert.equal(find(card, (n) => n.tagName === 'TEXTAREA'), null, '保存成功之后编辑器要收起来');
  // 「当前生效」与「内置原文」并排（真响应里的 default_text）
  assert.deepEqual(byClass(card, 'pd-label').map((n) => n.textContent), ['当前生效', '内置原文']);
  assert.equal(byClass(card, 'prompt-text')[0].textContent, keep);
  assert.equal(byClass(card, 'prompt-text')[1].textContent, zhBuiltin);
});

await check('服务端真的记下了这一条，而且**只记在 zh 那一门**（en 一个字都没动）', async () => {
  const live = await apiPrompts();
  const item = liveItem(live.body, 'SYSTEM_PROMPT');
  assert.equal(item.overridden, true);
  assert.ok(item.text.includes('自检追加的一行'), '生效的必须是刚存的那一版');
  assert.equal(item.default_text, zhBuiltin, '内置原文恒定不变');
  assert.equal(live.body.summary.overridden, 1);
  assert.deepEqual(overridesIn(live.body).zh, ['SYSTEM_PROMPT']);
  assert.deepEqual(overridesIn(live.body).en, [], '存中文不该在英文那一门留下任何东西');
  assert.equal(overridesLineText(), app.t('prompts.overrides_summary', {
    parts: '中文已改 1 条 · 英文已改 0 条',
  }));
  assert.match(noteNode().textContent, /已保存/);
});

await check('保存一段少了 {memory_text} 的文本：卡片上出现一行 warn（后端那句原话）', async () => {
  const withoutMemory = `${zhBuiltin.replace('{memory_text}', '（记忆）')}\n`;
  await clickPart('SYSTEM_PROMPT', 'pc-edit');
  typeInEditor('SYSTEM_PROMPT', withoutMemory);
  await clickPart('SYSTEM_PROMPT', 'pc-save');
  const card = promptCardBy('SYSTEM_PROMPT');
  const warn = partIn(card, 'pc-warning');
  assert.ok(warn, '少了占位符就该有一行 warn');
  // 这句话是**后端写的**（harness/prompts/__init__.py::inspect），前端一个字都不翻
  assert.match(warn.textContent, /不会再被填进来/);
  assert.match(warn.textContent, /\{memory_text\}/);
  assert.ok(!partIn(card, 'pc-editor-error'), '这是警告不是错误：不该出现错误那一行');
  assert.equal(partIn(card, 'pc-edited').textContent, '已修改', '有警告不改变"已改过"这件事');
  // 真接口那边同样报出来（warnings 与 placeholders_missing 两个字段）
  const live = await apiPrompts();
  const item = liveItem(live.body, 'SYSTEM_PROMPT');
  assert.deepEqual(item.placeholders_missing, ['memory_text']);
  assert.ok(item.warnings.length >= 1);
});

await check('存一个内置里没有的占位符 → 真 400，后端原话**原样**显示在编辑器里', async () => {
  await clickPart('SYSTEM_PROMPT', 'pc-edit');
  typeInEditor('SYSTEM_PROMPT', '你是一个助手。{foo}\n');
  const before = sends.length;
  await clickPart('SYSTEM_PROMPT', 'pc-save');
  assert.deepEqual(sends.slice(before).map((item) => item.url), ['/api/prompts/update'],
    '被拒的那一次也是真的发出去了（拒绝来自后端，不是前端自己拦的）');
  const card = promptCardBy('SYSTEM_PROMPT');
  const line = partIn(card, 'pc-editor-error');
  assert.ok(line, '被拒之后那行原话要留在编辑器里');
  assert.match(line.textContent, /提示词保存失败|保存失败/);
  assert.match(line.textContent, /\{foo\}/, '后端那句话里点名的占位符必须原样出现');
  assert.ok(find(card, (n) => n.tagName === 'TEXTAREA'), '被拒之后编辑器**不许**关掉');
  assert.equal(find(card, (n) => n.tagName === 'TEXTAREA').value, '你是一个助手。{foo}\n',
    '草稿一个字都不能丢（改完还能再试）');
  // 服务端那份**没有**被这次失败的保存改掉
  const live = await apiPrompts();
  assert.ok(liveItem(live.body, 'SYSTEM_PROMPT').text.includes('（记忆）'),
    '被拒的那一版绝不能落盘');
  await clickPart('SYSTEM_PROMPT', 'pc-cancel');
  assert.equal(find(promptCardBy('SYSTEM_PROMPT'), (n) => n.tagName === 'TEXTAREA'), null);
});

await check('再改一条（TAIL_NOW）：页首计数变成「中文已改 2 条 · 英文已改 0 条」', async () => {
  await clickPart('TAIL_NOW', 'pc-edit');
  typeInEditor('TAIL_NOW', '（自检改过的时钟行）');
  await clickPart('TAIL_NOW', 'pc-save');
  assert.equal(partIn(promptCardBy('TAIL_NOW'), 'pc-edited').textContent, '已修改');
  const live = await apiPrompts();
  assert.equal(live.body.summary.overridden, 2);
  assert.deepEqual([...overridesIn(live.body).zh].sort(), ['SYSTEM_PROMPT', 'TAIL_NOW']);
  assert.deepEqual(overridesIn(live.body).en, []);
  assert.equal(overridesLineText(), app.t('prompts.overrides_summary', {
    parts: '中文已改 2 条 · 英文已改 0 条',
  }));
  assert.ok(revertAllBtn(), '「把这门语言的提示词全部复原」应当在这一块的顶部');
  assert.equal(revertAllBtn().disabled, false, '有 2 条改动，可点');
});

await check('英文那一门另有覆盖时，页首把两门语言分别写出来（中文 2 · 英文 1）', async () => {
  // 切到英文，用它**自己**那一份内置原文去改一条：这一段要的是英文正文，
  // 不能拿上面那份中文负载里的 default_text（那样存进 en 的会是中文，
  // 后面"英文页面上没有汉字"那条断言就会因为**我自己造的**数据而红）
  await app.chooseLanguage('en');
  await waitFor(() => /tool definitions/.test(byId.get('prompts-body').textContent),
    '提示词页重画成英文');
  await clickPart('RENDER_USER', 'pc-edit');
  const area = find(promptCardBy('RENDER_USER'), (n) => n.tagName === 'TEXTAREA');
  const enText = `${area.value} (edited by ui_check)`;
  typeInEditor('RENDER_USER', enText);
  const beforeSave = sends.length;
  await clickPart('RENDER_USER', 'pc-save');
  assert.deepEqual(sends[beforeSave].payload,
    { language: 'en', id: 'RENDER_USER', text: enText });
  const liveEn = await apiPrompts();
  assert.deepEqual(overridesIn(liveEn.body).en, ['RENDER_USER']);
  assert.deepEqual([...overridesIn(liveEn.body).zh].sort(), ['SYSTEM_PROMPT', 'TAIL_NOW'],
    '改英文不该动中文那一门');
  assert.equal(liveItem(liveEn.body, 'RENDER_USER').text, enText);
  // 切回中文：页首那行要把**两门语言**的数字分别写出来
  await app.chooseLanguage('zh');
  await waitFor(() => /条提示词/.test(byId.get('prompts-body').textContent), '提示词页切回中文');
  assert.equal(overridesLineText(), app.t('prompts.overrides_summary', {
    parts: '中文已改 2 条 · 英文已改 1 条',
  }));
  assert.equal(partIn(promptCardBy('SYSTEM_PROMPT'), 'pc-edited').textContent, '已修改');
});

await check('点「把这门语言的提示词全部复原」：确认文案里有语言名与条数，只动中文那一门', async () => {
  lastConfirm = '';
  const before = sends.length;
  await revertAllBtn().onclick();
  await tick();
  assert.match(lastConfirm, /中文/, `确认文案里要有语言名：${lastConfirm}`);
  assert.match(lastConfirm, /2 条/, `确认文案里要有条数：${lastConfirm}`);
  const posted = sends.slice(before);
  assert.ok(posted.every((item) => item.url === '/api/prompts/revert'),
    `这一段只该发 revert：${posted.map((i) => i.url).join(',')}`);
  assert.deepEqual(Object.keys(posted[posted.length - 1].payload), ['language'],
    '复原整门**不能**带 id');
  assert.equal(posted[posted.length - 1].payload.language, 'zh');
});

await check('全部复原之后：中文回到内置（标记消失），**英文那一门还在**', async () => {
  const live = await apiPrompts();
  assert.deepEqual(overridesIn(live.body).zh, [], '中文这一门应当清空了');
  assert.deepEqual(overridesIn(live.body).en, ['RENDER_USER'], '英文那一门一个字都不该被带走');
  const item = liveItem(live.body, 'SYSTEM_PROMPT');
  assert.equal(item.overridden, false);
  assert.equal(item.text, item.default_text);
  assert.equal(live.body.summary.overridden, 0);
  assert.equal(partIn(promptCardBy('SYSTEM_PROMPT'), 'pc-edited'), null);
  assert.equal(partIn(promptCardBy('SYSTEM_PROMPT'), 'pc-revert'), null);
  assert.equal(overridesLineText(), app.t('prompts.overrides_summary', {
    parts: '中文已改 0 条 · 英文已改 1 条',
  }));
  assert.match(noteNode().textContent, /已复原 中文 的 2 条改动/);
  assert.equal(revertAllBtn().disabled, true, '中文没有改动了：按钮该禁用');
});

await check('切到英文：那条英文覆盖显示 Edited / Revert，整页没有汉字', async () => {
  await app.chooseLanguage('en');
  await waitFor(() => /tool definitions/.test(byId.get('prompts-body').textContent),
    '提示词页重画成英文');
  const card = promptCardBy('RENDER_USER');
  assert.ok(card, '英文页面上应当有 RENDER_USER 这一条');
  assert.equal(partIn(card, 'pc-edited').textContent, 'Edited');
  assert.equal(partIn(card, 'pc-revert').textContent, 'Revert');
  assert.equal(overridesLineText(), app.t('prompts.overrides_summary', {
    parts: 'Chinese: 0 changed · English: 1 changed',
  }));
  assert.ok(!/[\u4e00-\u9fff]/.test(byId.get('prompts-body').textContent),
    '英文页面上不该有汉字');
});

await check('在英文界面上复原那一条（二次确认说的也是英文与这门语言）', async () => {
  lastConfirm = '';
  await clickPart('RENDER_USER', 'pc-revert');
  assert.match(lastConfirm, /English/);
  assert.match(lastConfirm, /RENDER_USER/);
  const live = await apiPrompts();
  assert.deepEqual(overridesIn(live.body), { zh: [], en: [] });
  const item = liveItem(live.body, 'RENDER_USER');
  assert.equal(item.overridden, false);
  assert.equal(item.text, item.default_text);
  assert.equal(partIn(promptCardBy('RENDER_USER'), 'pc-edited'), null);
});

await check('收工核对：两门语言的覆盖都空了，夹具**没留下状态**（这一节写盘的东西都还回去了）', async () => {
  const live = await apiPrompts();
  assert.deepEqual(overridesIn(live.body), { zh: [], en: [] });
  assert.equal(live.body.summary.overridden, 0);
  assert.equal(live.body.store_error, '', '顺带确认覆盖文件本身是好的（没被写坏）');
  // 语言是**服务端**的设置：切回中文，别把它留在 en（下一个自检会以为页面该是英文）
  await app.chooseLanguage(liveLangBefore);
  await waitFor(() => app.currentLanguage() === liveLangBefore, '语言切回原来的那一门');
  console.log(`  · 提示词覆盖已全部复原（中英各 0 条），语言切回 ${liveLangBefore}`);
});

/* 收尾：**无论怎么结束都要打出"断言总数 + 失败清单"**。
   夹具不干净时，脚本里总有几处按夹具写死的点击找不到节点；要是让那句
   `null.onclick` 直接把进程掀翻，看的人只会看到一行 TypeError，
   连"到底几条断言、哪几条红了"都看不到 —— 那正是这一轮踩过的坑。
   正常走完、被 check 记下的失败、以及没被保护的语句崩掉，三种都从这里出去。
   ⚠ 这里**不调 process.exit()**：它会截断还没刷出去的 stdout
   （管道里跑的时候，总数与失败清单会整段消失）。改成设 exitCode + 自然退出。
   handler 在文件**前面**就挂上了（见那里的注释）。 */
function finish(crash) {
  if (reported) return;
  reported = true;
  // `e2eStop` = "我自己主动停的"（夹具形状不对那条），不再重复记一遍
  if (crash && !crash.e2eStop) {
    failures.push(`脚本在一条未受保护的语句上崩了：${(crash && crash.message) || crash}`);
  }
  console.log(`\n端到端（真接口 → 真 app.js）断言：${n}，失败：${failures.length}`);
  for (const item of failures) console.log(`  - ${item}`);
  if (failures.length) process.exitCode = 1;
  else console.log('端到端全部通过。');
}
finish();
