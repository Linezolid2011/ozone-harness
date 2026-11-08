'use strict';

/* O₃ᶻᵒⁿᵉ · Web 前端
   零依赖、零构建：原生 DOM + fetch(SSE)。
   后端把 AgentLoop 的 emit 事件翻译成 SSE 帧，这里只负责把它们画出来。

   布局：左侧栏（会话列表 + 左下角设置入口）
         主区 = 对话视图 / 设置视图（设置里再分子页：记忆 Dream 用量 接口 主题） */

const PANES = ['memory', 'dream', 'usage', 'api', 'tools', 'prompts', 'general', 'reset'];
const SESSION_USAGE_FIELDS = [
  'prompt_tokens', 'completion_tokens', 'total_tokens',
  'reasoning_tokens', 'cached_tokens', 'cache_miss_tokens',
];

const state = {
  view: 'chat',          // chat | settings
  pane: 'memory',        // 设置里的子页
  // 界面语言（'zh' | 'en'）。`init()` 从 /api/config 的 language 取初值，取不到就 zh。
  // 真正的"当前语言"住在 i18n.js 里（I18N.lang）；这一份是**界面自己的镜子**，
  // 设置 → 语言 靠它画当前值。两处永远一起改（见 setLanguageNow()）。
  language: 'zh',
  // `time` 工具的时区偏移（小时），从 /api/config 读；「设置 → 常规」里改。
  // 默认 8 = UTC+8。真正的值在服务端（data/settings.json），这份只是界面镜子。
  timezone: 8,
  // 单轮最大输出（token；-1 = 不限），从 /api/config 读；「设置 → 常规」里改（ADR-101）。
  // 真正的值在服务端（data/settings.json），这份只是界面镜子。
  maxOutputTokens: 5000,
  sessionId: null,
  sessions: [],
  // 输入区下方的统计只属于当前会话。切会话时先清零，再用该会话自己的
  // 事件日志快照替换，避免慢响应期间把上一个会话的数字展示成新会话的。
  sessionStats: emptySessionStats(),
  config: null,
  providers: null,
  tools: null,           // 联网工具配置（设置 → 工具）
  memories: null,
  selection: { provider: '', model: '', effort: '' },
  webSearch: false,      // 会话级「联网搜索」开关，默认关
  // 会话级「多模态」开关（契约 §13）：**默认开**。开 = 图片本身随请求发给模型；
  // 关 = 本机 OCR，只把识别出的文字发出去，"图片本身绝不发出去"。
  // 缺键一律当**开**（老会话沿用旧行为）—— 见 boolDefaultOn()。
  multimodal: true,
  controller: null,
  busy: false,
  stick: true,
  // 待发图片附件（已经上传成功、还没随消息发出去的那些）。
  // 每一项的形状见 uploadAttachmentData()：{id, ext, name, url, width, height...}。
  // 存的是**服务端落盘那一版**（缩略图 src 就是服务端那个 URL），
  // 不是本地 objectURL —— 用户看到的必须就是模型将看到的那一张。
  attachments: [],
  memoriesLoaded: false,
  dreamsLoaded: false,
  dreams: 0,             // 运行记录条数（列表接口给的，只用来填右上角那句统计）
  dreamRuns: null,       // 运行记录本身（最近一次拉到的）。见 renderVaultPane 末尾那句注释。
  // 背景档案（GET /api/vault）：待整理状态 + 目录树 + 结构校验 + README 正文
  vault: null,
  vaultLoaded: false,
  // 目录树的展开状态（相对路径 → 是否展开）。**只覆盖用户真的点过的目录**：
  // 没记过的目录一律默认展开 —— 这样「重新加载 vault」之后既不丢用户的折叠，
  // 也不会因为新目录没在表里而把它画成收起来的。
  vaultTreeOpen: {},
  // 右边正在看的档案：state 里存着它，所以 loadDreamPane() 重刷（整理完、
  // 点「刷新」）之后不会掉回空页面，也不会白白重发一次请求。
  // requested 是"用哪个名字读的"，canonical 是后端回的真实路径（高亮用）。
  vaultFile: null,
  vaultFileLoaded: false,
  // `[[名字]] → 相对路径`（后端 neighborhood().links 给的），正文渲染双链时查这张表
  vaultFileLinks: {},
  // 界面上的新建 / 编辑（null = 不在编辑态）。见下面的 renderVaultEditor。
  //   draft     表单里的当前值（**用户在输入框里敲的**，409 冲突时靠它不丢字）
  //   hash      载入时后端给的内容指纹，保存/删除时要带回去（ADR-070）
  //   file      编辑开始时正在看的那份（取消 = 原样退回去）
  //   note      编辑器里那一行就地提示（错误原话、冲突说明…）
  //   conflict  {view, hash}：内容在你看的时候被改过，等用户选"放弃"还是"覆盖"
  vaultEdit: null,
  // 上一次保存/删除的结果，用来在查看器头部说一句"已保存 / 已删除 + 连带结果"。
  // 重刷（renderVaultPane）不重建查看器，所以这句不会被自己刷掉。
  vaultNote: null,
  // 上一次「格式化并初始化 dream」的结果（before → after + 没删掉的东西）。
  // 存在 state 里而不是只弹一个 toast：这个动作不可逆，结果必须留在页面上看得见，
  // 而且重刷（renderVaultPane）不能把它擦掉。点「刷新」时清掉。
  dreamFormatResult: null,
  // 正在保存的「整理模型」选择（ADR-103）。整理开始时 `runDream()` 会等它落盘 ——
  // 否则用户改完模型立刻点「整理一次」，后端存下的还是上一个模型，
  // 界面显示的却是新选的那个（WYSIWYG 被一次网络往返打破）。
  dreamModelSaving: null,
  // 提示词页（GET /api/prompts）：最近一次拉到的整份负载。
  // 它是**当前语言那一份**，所以切语言时必须重新拉，不能拿它重画（见 loadPrompts）。
  // 没有 loaded 标记：这一页没有"局部刷新"的读法，只有整拉 / 整画。
  prompts: null,
  // 提示词编辑态（契约 `docs/14-editable-prompts.md` §8）：null = 不在编辑态。
  // 一次只开一个（点另一条的「编辑」，这一条就先收起来）。
  //   id     正在编辑的那一条（= data-prompt-id）
  //   draft  文本框里当前的值（**用户敲的**；保存被拒时靠它不丢字）
  //   error  保存被后端拒绝时就地显示的那句**原话**（null = 没有）
  promptEdit: null,
  // 上一次保存 / 复原的结果，留在页首看得见（同 state.vaultNote 那条规矩：
  // 这种"改了到底成没成"的结论不能被一次重画或一个一闪而过的 toast 吞掉）。
  //   kind  ok | warn  → 决定那行字用什么颜色（warn = 成功了但有后果）
  promptNote: null,
  // 双击启动门与普通聊天 busy/controller 完全分开：pip 的暂停与取消绝不能误停一轮对话。
  setup: null,
  setupGateOpen: false,
  setupActionBusy: false,
  setupActionError: '',
  setupNetworkError: '',
};

const $ = (id) => document.getElementById(id);

/* ── 小工具 ───────────────────────────────────────────── */

/* 契约 §13.1 的那条默认：**缺这个键 = 开**。
   它管的是「多模态」—— 缺键必须是"沿用旧行为（发图）"，不能是"悄悄改成 OCR"。
   写成一个小函数是因为这条默认出现在三个地方（会话加载 / tools 帧 / 新对话），
   抄三遍迟早有一遍写反。 */
function boolDefaultOn(value) {
  return value === undefined || value === null ? true : !!value;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/* 极简富文本：转义 → 保留代码块 → 行内代码 / 粗体 / 换行。
   刻意不做完整 Markdown —— 任何解析器都要处理 XSS，这里只保证"不注入"。 */
function renderRich(text) {
  const blocks = [];
  const src = String(text).replace(/```([\s\S]*?)```/g, (_m, code) => {
    blocks.push(code.replace(/^\n/, ''));
    return `\u0000${blocks.length - 1}\u0000`;
  });
  let html = escapeHtml(src)
    .replace(/`([^`\n]+)`/g, '<code class="ic">$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>');
  html = html.replace(/\u0000(\d+)\u0000/g, (_m, index) =>
    `<pre class="codeblock">${escapeHtml(blocks[Number(index)])}</pre>`);
  return html;
}

let toastTimer = null;
function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2800);
}

/* 复制一段文字（设置 → 提示词的「复制」按钮）。
   `navigator.clipboard` **不是到处都有**：非安全上下文（http 且不是 localhost）、
   权限被拒、或者干脆没实现时它都不在。所以：能用就用它；用不了退回
   `document.execCommand('copy')`（老路，但这台机器上真的能用）；
   两条路都不行就**返回 false** —— 调用方必须说一句人话，不许静默。

   为什么不用 `navigator.clipboard` 一条路走到底：失败时那句"复制失败"会
   看起来像界面坏了，而用户其实只是需要手动选中那几行字。 */
async function copyText(text) {
  const value = String(text === undefined || text === null ? '' : text);
  const clipboard = navigator && navigator.clipboard;
  if (clipboard && typeof clipboard.writeText === 'function') {
    try {
      await clipboard.writeText(value);
      return true;
    } catch {
      // 掉下去试第二条路（权限被拒是最常见的形态）
    }
  }
  try {
    const box = document.createElement('textarea');
    box.value = value;
    box.setAttribute('readonly', 'readonly');
    box.style.position = 'fixed';
    box.style.top = '-1000px';
    const host = document.body || document.documentElement;
    host.appendChild(box);
    if (typeof box.select === 'function') box.select();
    // 老接口返回**布尔**：只有明说 false 才算失败。有些实现返回 undefined，
    // 那不能当成失败 —— 否则一次成功的复制也会被报成"没复制上"。
    const ok = document.execCommand ? document.execCommand('copy') : false;
    if (host.removeChild) host.removeChild(box);
    return ok !== false;
  } catch {
    return false;
  }
}

function relTime(seconds) {
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return t("time.just_now");
  if (delta < 3600) return t("time.minutes_ago", { n: Math.floor(delta / 60) });
  if (delta < 86400) return t("time.hours_ago", { n: Math.floor(delta / 3600) });
  return t("time.days_ago", { n: Math.floor(delta / 86400) });
}

async function getJSON(url) {
  const resp = await fetch(url);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body;
}

/* 失败时把**状态码与响应体**挂在 Error 上。
   档案的新建/编辑/删除必须能分开处理 409 的三种情形（内容被改过 / 撞名 /
   整理正在跑），而 409 的响应体里带着"最新内容"这种要拿来救场的东西。
   只留一句 message 的话，界面就只能说一句"失败了"，那正是这一层最不该做的事。 */
async function requestJSON(url, payload) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(body.error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

async function postJSON(url, payload) {
  return requestJSON(url, payload);
}

/* ── SSE over fetch ───────────────────────────────────── */

async function streamPost(url, payload, handlers) {
  const controller = new AbortController();
  state.controller = controller;
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: controller.signal,
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    // 状态码与响应体挂在 Error 上（与 requestJSON 一致）：
    // 契约 §13.5 的 `need: "vision"` 就在 body 里，而界面正是靠它决定
    // "该去动哪个开关"。丢掉 body 的话那句提示只能变成一句泛泛的失败。
    const err = new Error(body.error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let reportedHandlerError = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = 'message';
      let data = '';
      for (const line of frame.split('\n')) {
        if (line.startsWith('event: ')) event = line.slice(7);
        else if (line.startsWith('data: ')) data += line.slice(6);
      }
      if (!data) continue;
      let parsed;
      try { parsed = JSON.parse(data); } catch { continue; }
      const handler = handlers[event];
      if (!handler) continue;
      try {
        handler(parsed);
      } catch (err) {
        // 一帧画不出来不能把整条流弄死：服务端还在生成、还占着会话锁，
        // 停在这里用户既看不到后文、也点不了别的会话。只报一次，继续读。
        if (!reportedHandlerError) {
          reportedHandlerError = true;
          if (typeof handlers.onHandlerError === 'function') {
            try { handlers.onHandlerError(err, event); } catch { /* 报错本身也不能再炸一次 */ }
          } else {
            console.error('[stream] handler error:', event, err);
          }
        }
      }
    }
  }
  return controller;
}

/* ── 主题 ─────────────────────────────────────────────── */

const THEMES = ['', 'light', 'dark'];

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  try {
    if (theme) localStorage.setItem('theme', theme);
    else localStorage.removeItem('theme');
  } catch { /* 隐私模式下 localStorage 可能不可用，忽略即可 */ }
}

function currentTheme() {
  const theme = document.documentElement.dataset.theme || '';
  return THEMES.includes(theme) ? theme : '';
}

function initTheme() {
  // 支持 ?theme=dark 深链：既方便截图自检，也方便把某个主题做成书签
  let theme = new URLSearchParams(location.search).get('theme') || '';
  if (!THEMES.includes(theme)) theme = '';
  if (!theme) {
    try { theme = localStorage.getItem('theme') || ''; } catch { theme = ''; }
  }
  applyTheme(THEMES.includes(theme) ? theme : '');
}

/* ── 路由与视图 ───────────────────────────────────────── */

function showChat() {
  state.view = 'chat';
  $('view-chat').classList.remove('hidden');
  $('view-settings').classList.add('hidden');
  $('settings-entry').classList.remove('active');
  $('sessions-label').textContent = t("session.list_recent");
}

/* 设置子页的数据加载：失败必须在那块地方**说一句**。
   这些调用都来自路由（没人 await），抛出去只会变成一条控制台里的
   unhandled rejection —— 用户看到的是空白或上一次的旧内容。 */
function loadPaneSafely(hostId, loader) {
  const fail = (err) => {
    const host = $(hostId);
    if (!host) return;
    host.innerHTML = t("session.load_failed", {
      msg: escapeHtml(String((err && err.message) || err)),
    });
  };
  try {
    const pending = loader();
    if (pending && typeof pending.catch === 'function') pending.catch(fail);
  } catch (err) {
    fail(err);
  }
}

function showSettings(pane) {
  state.view = 'settings';
  // 旧深链（#settings/theme / #settings/language）迁到「常规」：三个设置并进去了。
  if (pane === 'theme' || pane === 'language') pane = 'general';
  state.pane = PANES.includes(pane) ? pane : 'memory';
  $('view-chat').classList.add('hidden');
  $('view-settings').classList.remove('hidden');
  $('settings-entry').classList.add('active');
  $('sessions-label').textContent = t("session.list_settings");
  // 这一行是**跨子页共用**的：接口页写"N 个接口"，别的页不该继续显示它。
  // 每次切页先清掉，由当前页自己的加载器决定要不要重新写（见 renderApiPane）。
  $('settings-meta').textContent = '';

  for (const item of document.querySelectorAll('.settings-nav-item')) {
    item.classList.toggle('active', item.dataset.pane === state.pane);
  }
  for (const el of document.querySelectorAll('.settings-pane')) {
    el.classList.toggle('hidden', el.id !== `pane-${state.pane}`);
  }

  if (state.pane === 'memory') loadPaneSafely('memory-list', loadMemories);
  // 这一页是两份数据拼出来的（vault 现状 + 运行历史），刷新按钮也走同一个入口。
  // 失败了要在这里就地报出来：路由这条路径没人 await，抛出去只会变成一条
  // 控制台里的 unhandled rejection，用户看到的是一片空白。
  if (state.pane === 'dream') {
    loadDreamPane().catch((err) => {
      const host = $('dream-list');
      host.innerHTML =
        t("session.load_failed", { msg: escapeHtml(String(err.message || err)) });
      // 数据读不到也**不能把危险区藏起来**：vault 坏掉的时候，
      // 「格式化并初始化 dream」恰恰是那条补救路（它是唯一的重来方式）。
      renderDreamDangerZone(host);
    });
  }
  if (state.pane === 'usage') loadPaneSafely('usage-body', loadUsage);
  if (state.pane === 'api') loadPaneSafely('api-body', loadProviders);
  if (state.pane === 'tools') loadPaneSafely('tools-body', loadToolsPane);
  if (state.pane === 'prompts') loadPrompts();
  if (state.pane === 'general') renderGeneralPane();
  if (state.pane === 'reset') loadPaneSafely('reset-body', loadResetPane);
}

/* hash 路由：#chat / #chat/<session-id> / #settings/<pane>
   好处有两个：刷新页面不丢当前视图，而且可以直接把某个会话做成书签。 */
function navigate(view, arg) {
  const target = arg ? `#${view}/${arg}` : `#${view}`;
  if (location.hash === target) { applyRoute(); return; }
  location.hash = target;   // 变化会触发 hashchange → applyRoute
}

async function applyRoute() {
  const [rawView, rawArg] = (location.hash || '#chat').slice(1).split('/');
  if (rawView === 'settings') {
    showSettings(rawArg);
    return;
  }
  showChat();
  if (rawArg && rawArg !== state.sessionId) {
    await openSession(rawArg);
  }
}

/* ── 会话 ─────────────────────────────────────────────── */

async function loadSessions() {
  const data = await getJSON('/api/sessions');
  state.sessions = data.sessions || [];
  renderSessions();
}

function renderSessions() {
  const list = $('session-list');
  list.innerHTML = '';
  if (!state.sessions.length) {
    list.innerHTML = t("session.none");
    return;
  }
  for (const session of state.sessions) {
    const item = document.createElement('div');
    item.className = 'session' + (session.id === state.sessionId ? ' active' : '');
    item.innerHTML = t("session.item", { title: escapeHtml(session.title || t("session.untitled")), turns: session.turns, time: relTime(session.updated_at) });
    item.onclick = () => navigate('chat', session.id);
    // 点小按钮时别把点击冒泡给整行（否则会顺手把会话打开）
    item.querySelector('[data-act="rename"]').onclick = (event) => {
      event.stopPropagation();
      renameSession(session);
    };
    item.querySelector('[data-act="delete"]').onclick = (event) => {
      event.stopPropagation();
      deleteSession(session);
    };
    list.appendChild(item);
  }
}

function renameSession(session) {
  openDialog({
    title: t("dialog.rename.title"),
    html: t("dialog.rename.body", { title: escapeHtml(session.title || '') }),
    confirmLabel: t("action.save"),
    onConfirm: async () => {
      const value = ($('rename-input').value || '').trim();
      try {
        const saved = await postJSON(`/api/sessions/${encodeURIComponent(session.id)}/title`,
          { title: value });
        await loadSessions();
        if (session.id === state.sessionId) {
          // 清空自定义标题时，顶栏必须显示服务端重新算出的自动标题；
          // 闭包里的 session.title 仍是保存前那份旧值，不能拿来兜底。
          const refreshed = state.sessions.find((item) => item.id === session.id) || {};
          $('chat-title').textContent = (saved && saved.title) || refreshed.title || value || session.id;
        }
        toast(value ? t("toast.renamed") : t("toast.rename_restored"));
      } catch (err) { toast(t("toast.rename_failed", { msg: err.message || err })); }
    },
  });
  const input = $('rename-input');
  if (input) {
    input.focus();
    input.select();
    // 输入框里按 Enter 直接保存（不然还得去点按钮）
    input.onkeydown = (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        $('dialog-confirm').click();
      }
    };
  }
}

function deleteSession(session) {
  openDialog({
    title: t("dialog.delete_chat.title"),
    html: t("dialog.delete_chat.body", { title: escapeHtml(session.title || session.id), turns: session.turns }),
    confirmLabel: t("action.delete"),
    onConfirm: async () => {
      try {
        await postJSON(`/api/sessions/${encodeURIComponent(session.id)}/delete`, {});
        toast(t("toast.deleted"));
        // 删掉的正好是当前打开的那个 → 回到新对话
        if (session.id === state.sessionId) {
          state.sessionId = null;
          newChat();
        }
        await loadSessions();
      } catch (err) { toast(t("toast.delete_failed", { msg: err.message || err })); }
    },
  });
}

/* 分叉：以某条用户消息为界，新建一个"只保留它之前上下文"的独立对话。
   原对话一个字不动（保留全部上下文）；新对话从分叉点之前接着走。
   分叉成功后把侧栏刷新一下，并直接切到新出来的那个对话。 */
async function forkSession(seq) {
  const id = state.sessionId;
  if (!id) return;
  if (state.busy) { toast(t("toast.busy_running")); return; }
  try {
    const result = await postJSON(`/api/sessions/${encodeURIComponent(id)}/fork`, { seq });
    // 分叉已经创建，源会话的统计不再属于屏幕上的新会话；打开新日志前显示零态。
    setSessionStats(null);
    toast(t("toast.forked"));
    await loadSessions();
    navigate('chat', result.session_id);
  } catch (err) {
    toast(t("toast.fork_failed", { msg: err.message || err }));
  }
}

async function openSession(id) {
  if (state.busy) { toast(t("toast.busy_running")); return; }
  // 换了会话：待发的那几张属于**上一个**会话目录（服务端是按会话找 id 的），
  // 带过去只会让服务端拒一次。这里明确丢掉，别让用户白等一轮。
  // （点的是当前这个会话就不丢 —— 那只是重看一遍，没道理把图扔了。）
  if (id !== state.sessionId) clearAttachments();
  state.sessionId = id;
  // 立即撤掉上一个会话的统计；真正的快照回来后由 setSessionStats 替换。
  setSessionStats(null);
  showChat();
  renderSessions();
  $('messages').innerHTML = '';
  let data;
  try {
    data = await getJSON(`/api/sessions/${encodeURIComponent(id)}`);
  } catch (err) {
    // 一次加载失败不能把整页弄死：applyRoute() 是 init() 里 await 的，
    // 异常从这里抛出去会让后面那批接线（发送 / 新对话 / 设置 / 附件）
    // 全部跳过，页面看起来就是"活着但点不动"。
    // 请求期间已经切走了就静默丢掉（错的是那个旧请求，不是当前这个会话）。
    if (state.sessionId !== id) return;
    addNotice('error', t("session.open_failed", { msg: String((err && err.message) || err) }));
    return;
  }
  // 请求回来时用户可能已经切走了（A → B、A → 新对话）：慢回来的旧响应
  // 一律丢掉，否则它会把 A 的消息追加进 B 清空后的列表，还顺手改写标题。
  if (state.sessionId !== id) return;
  if (data.error) { toast(data.error); return; }
  $('chat-title').textContent = (state.sessions.find((s) => s.id === id) || {}).title || id;
  if (data.generation && data.generation.model) applySelection(data.generation);
  state.webSearch = !!((data.tools || {}).web_search);   // 每个会话各自记着
  renderWebToggle();
  // 多模态：**缺键 = 开**（老会话沿用旧行为，契约 §13.1）
  state.multimodal = boolDefaultOn((data.tools || {}).multimodal);
  renderMultimodalToggle();
  for (const message of data.messages || []) renderStoredMessage(message);
  setSessionStats(data.stats);
  updateUsageHint(data.stats ? state.sessionStats.usage : data.usage);
  state.stick = true;
  scrollToBottom();
}

function renderStoredMessage(message) {
  // 历史里的用户消息带 attachments（契约 §7.4）：刷新页面之后图片还在，
  // 这就是"事件日志是唯一真相来源"在界面上的样子。
  if (message.role === 'user') { addUser(message.text, message.attachments, message.modality); return; }
  if (message.role === 'summary') {
    addSummaryBlock(message.text, message.covered, false);
    return;
  }
  if (message.role === 'assistant') {
    const ui = beginAssistant();
    if (message.reasoning) ui.reasoning(message.reasoning, true);
    if (message.text) ui.text(message.text, true);
    ui.finish();
    return;
  }
  if (message.role === 'tool') {
    const chip = addToolChip(message.name);
    fillToolChip(chip, { ok: message.ok, display: message.display, result: message.result },
      message.arguments);
    return;
  }
  if (message.role === 'notice') { addNotice(message.level, message.text); return; }
  if (message.role === 'meta') {
    // `turn_seq` 是这一轮 user_message 的 seq：右上角「分叉」按钮用它定位分叉点
    addMeta(t("meta.steps_line", { steps: message.steps, reason: message.stop_reason, elapsed: message.elapsed })
      + usageText(message.usage), message.turn_seq);
  }
}

/* 空对话那一屏。抽成函数是因为它出现在三处（新对话 / 没有会话时重渲染 / 语言切换后的
   消息流重跑），而且它自己就是一段要翻的静态 HTML（key: chat.empty_html）。 */
function emptyChatHtml() {
  return t('chat.empty_html');
}

function newChat() {
  if (state.busy) { toast(t("toast.busy_running")); return; }
  state.sessionId = null;
  setSessionStats(null);
  // 待发附件属于刚才那个（服务端已经给了 id 的）会话目录，新对话用不上它们
  clearAttachments();
  navigate('chat');
  renderSessions();
  $('messages').innerHTML = emptyChatHtml();
  $('chat-title').textContent = t("chat.new_title");
  $('chat-meta').textContent = '';
  const defaults = (state.providers && state.providers.default) || {};
  if (defaults.provider && defaults.model) applySelection(defaults);
  state.webSearch = false;   // 新会话默认关（与后端默认一致）
  renderWebToggle();
  state.multimodal = true;   // 新会话默认**开**（契约 §13.1：它等于既有行为）
  renderMultimodalToggle();
  $('input').focus();
}

/* ── 模型 / 档位选择（会话内直接可调）────────────────────── */

function providerById(id) {
  return ((state.providers && state.providers.providers) || [])
    .find((p) => p.id === id) || null;
}

function applySelection(selection) {
  const next = {
    provider: selection.provider || '',
    model: selection.model || '',
    effort: selection.effort || '',
  };
  if (next.provider && next.model) {
    state.selection = next;
  } else if (next.provider) {
    const profile = providerById(next.provider);
    state.selection = {
      provider: next.provider,
      model: (profile && profile.models[0]) || '',
      effort: next.effort || '',
    };
  }
  renderPickers();
}

function renderPickers() {
  const modelSelect = $('pick-model');
  const models = (state.providers && state.providers.models) || [];
  modelSelect.innerHTML = '';
  if (!models.length) {
    modelSelect.innerHTML = t("picker.no_models");
    // **选中项也必须一起清掉**：这一支以前直接 return，于是"一个模型都没有"时
    // `state.selection` 还留着上一次的值 —— 真实场景就是恢复出厂设置
    // （providers.json 被删了），而下一次请求会拿一个**已不存在的 provider** 发出去。
    state.selection = { provider: '', model: '', effort: '' };
    renderEffortPicker();
    renderSessionStats();
    return;
  }
  const groups = new Map();
  for (const entry of models) {
    const key = entry.provider_label || entry.provider;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  for (const [label, list] of groups) {
    const group = document.createElement('optgroup');
    group.label = label;
    for (const entry of list) {
      const option = document.createElement('option');
      option.value = `${entry.provider}|${entry.model}`;
      option.textContent = entry.model_label || entry.model;
      group.appendChild(option);
    }
    modelSelect.appendChild(group);
  }
  let wanted = `${state.selection.provider}|${state.selection.model}`;
  const values = Array.from(modelSelect.options).map((o) => o.value);
  if (!values.includes(wanted)) {
    wanted = values[0];
    const [provider, model] = wanted.split('|');
    state.selection.provider = provider;
    state.selection.model = model;
  }
  modelSelect.value = wanted;
  renderEffortPicker();
  renderSessionStats();
}

function renderEffortPicker() {
  const select = $('pick-effort');
  const profile = providerById(state.selection.provider);
  const options = (profile && profile.thinking_options) || [];
  select.innerHTML = '';
  if (!options.length) {
    select.innerHTML = t("picker.effort_none");
    select.disabled = true;
    state.selection.effort = '';
    return;
  }
  select.disabled = false;
  for (const value of options) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = t("picker.effort_option", { value });
    select.appendChild(option);
  }
  if (!options.includes(state.selection.effort)) state.selection.effort = options[0];
  select.value = state.selection.effort;
}

async function persistSelection() {
  if (!state.sessionId) return;   // 还没有会话，等第一条消息一起写进日志
  try {
    await postJSON(`/api/sessions/${encodeURIComponent(state.sessionId)}/settings`,
      state.selection);
  } catch (err) {
    toast(t("toast.save_model_failed", { msg: err.message || err }));
  }
}

/* ── 会话级「联网搜索」开关 ──────────────────────────────
   默认关。它**只管 web_search**：web_open_url（打开用户给的链接）
   不受影响 —— "读用户递过来的东西"和"模型自己决定去外面找"是两种权限。 */

function renderWebToggle() {
  const button = $('toggle-web-search');
  const config = state.config || {};
  const usable = config.web_enabled && config.web_search_available;
  const on = usable && state.webSearch;

  button.classList.toggle('on', !!on);
  button.classList.toggle('off', !on);
  button.disabled = !usable;
  button.querySelector('.label').textContent = on ? t("toggle.web.on") : t("toggle.web.off");
  button.title = !config.web_enabled
    ? t("toggle.web.disabled")
    : (!config.web_search_available
      ? t("toggle.web.unavailable")
      : (on
        ? t("toggle.web.on_title")
        : t("toggle.web.off_title")));
}

async function setWebSearch(on) {
  state.webSearch = !!on;
  renderWebToggle();
  if (!state.sessionId) return;   // 新会话还没落盘，等第一条消息一起写进日志
  try {
    await postJSON(`/api/sessions/${encodeURIComponent(state.sessionId)}/tools`,
      { web_search: !!on });
  } catch (err) {
    toast(t("toast.save_web_failed", { msg: err.message || err }));
  }
}

/* ── 会话级「多模态」开关（契约 §13）──────────────────────
   形状与「联网搜索」一模一样：会话级、随消息传、tools 帧回填。
   两处刻意的差别：
     * 默认**开**（缺键 = 开）—— 它等于上一轮的行为；
     * 它不吃 config：后端没有"多模态不可用"这种状态（OCR 引擎不在是发送那一刻
       400 的事，不是开关的事），所以这个按钮**永远可点**。
       busy 时也能切 —— 切了影响的是**下一条**消息（与联网开关一致）。 */

/* 缩略图徽标与 OCR 标注写成**函数**而不是常量：语言是运行期可变的，
   常量会在加载时就被冻成当时的语言，切完语言这里会继续吐旧语言。 */
function multimodalBadgeText() { return t('multimodal.badge'); }
/* §13.7 第 3 句的那句标注。它现在是折叠标题的**前半段**（后半段是状态摘要），
   所以只写一处 —— 抄两遍迟早有一遍走样。 */
function multimodalOcrLabel() { return t('multimodal.ocr_label'); }

/* 「只发文字」这条路是不是当前的选择。写成函数而不是到处写 `=== false`：
   缺键当开这条默认只该有一个落点（boolDefaultOn），而 state 里存的已经是布尔。 */
function multimodalTextOnly() {
  return state.multimodal === false;
}

function renderMultimodalToggle() {
  const button = $('toggle-multimodal');
  if (!button) return;
  const on = !multimodalTextOnly();
  button.classList.toggle('on', on);
  button.classList.toggle('off', !on);
  const label = button.querySelector('.label');
  if (label) label.textContent = on ? t("toggle.multimodal.on") : t("toggle.multimodal.off");
  button.title = on
    ? t("toggle.multimodal.on_title")
    : t("toggle.multimodal.off_title");
  // 开关一变，每张待发缩略图上的模态标注也要跟着变。
  renderAttachments();
}

async function setMultimodal(on) {
  state.multimodal = boolDefaultOn(on);
  renderMultimodalToggle();
  if (!state.sessionId) return;   // 新会话还没落盘，等第一条消息一起写进日志
  try {
    await postJSON(`/api/sessions/${encodeURIComponent(state.sessionId)}/tools`,
      { multimodal: !multimodalTextOnly() });
  } catch (err) {
    toast(t("toast.save_multimodal_failed", { msg: err.message || err }));
  }
}

/* 按钮点一下要干的事。单独写成函数（而不是 init 里一个匿名箭头）是为了让自检
   能点的就是**按钮真的挂的那一个**，而不是照着重写一遍语义。 */
function toggleMultimodal() {
  return setMultimodal(multimodalTextOnly());
}

let multimodalAttentionTimer = null;

/* 「模型不支持图片」那一次拒绝的界面落点（契约 §13.5 / §13.8）：
   把「多模态」按钮短暂地高亮一下 —— 用户看到的是一句后端原话 + 一个
   明确该动的按钮，而不是"失败了，自己猜"。 */
function highlightMultimodalToggle() {
  const button = $('toggle-multimodal');
  if (!button) return;
  button.classList.add('attention');
  clearTimeout(multimodalAttentionTimer);
  multimodalAttentionTimer = setTimeout(() => {
    const now = $('toggle-multimodal');
    if (now) now.classList.remove('attention');
  }, 2600);
}

/* SSE 的 `tools` 帧：契约里它固定回两个键（§13.1）。
   单独拿出来是为了能直接喂一帧给自检 —— 不用为了验"回填"去造一整条流。 */
function applyToolsFrame(payload) {
  const raw = payload || {};
  state.webSearch = !!raw.web_search;
  state.multimodal = boolDefaultOn(raw.multimodal);
  renderWebToggle();
  renderMultimodalToggle();
}

/* ── 消息渲染 ─────────────────────────────────────────── */

function scrollToBottom() {
  const el = $('messages');
  if (state.stick) el.scrollTop = el.scrollHeight;
}

function messagesEl() { return $('messages'); }

function stripEmpty() { $('chat-empty')?.classList.add('hidden'); }

/* 用户气泡的节点（把建 DOM 与"插到哪里"分开）：发送那一刻要用它，
   这一轮结束后"按会话视图重画那一条"也要用它 —— 两边必须是同一个形状。
   分叉按钮**不挂在这里**：它挂在每一轮结尾那行统计（`.meta`）上，见 addMeta()。 */
function userBubbleNode(text, attachments, modality) {
  const node = document.createElement('div');
  node.className = 'msg user';
  const bubble = el('div', 'bubble');
  const images = (attachments || []).filter(Boolean);
  if (images.length) {
    bubble.appendChild(attachmentStrip(images));
    if (modality === 'text') bubble.appendChild(ocrBlock(images));
  }
  if (text) bubble.appendChild(el('div', 'bubble-text', text));
  node.appendChild(bubble);
  return node;
}

/* 用户气泡。图片排在文字前面（契约 §4 里发给模型也是文本在前 —— 但那是
   请求体的形状；界面上"先看你发的图、再看你说的字"更好读）。
   文字走 textContent：它可能是用户从别处粘来的任意字符。

   `modality === 'text'` = 这一轮**图片没发出去**，只发了本机 OCR 出来的文字
   （契约 §13.3）。这时气泡里必须把模型实际收到的那段文字原样摆出来 ——
   数据来自**会话视图**（`message.modality` / `attachment.ocr`），不是发送那一刻
   临时拼的：刷新页面之后看到的东西要一模一样。 */
function addUser(text, attachments, modality) {
  stripEmpty();
  messagesEl().appendChild(userBubbleNode(text, attachments, modality));
  state.stick = true;
  scrollToBottom();
}

/* OCR 文字块（契约 §13.7 第 3 句）。**每一个节点都走 textContent**：
   OCR 文字是**图片里的内容**，和文件名、后端错误一样属于外部可控输入，
   拼进 innerHTML 就等于让图片里的字执行脚本。
   `ocr.error` 也照原话显示 —— 识别失败时"当时错在哪"是唯一线索。

   默认**折起来**（`refreshLastUserMessage` 与刷新后的历史回放走的是同一个函数，
   所以两边都是同一个形状）。折叠的视觉语言照抄压缩摘要块
   （`.summary.collapsed` + 点 `.summary-head` 切换，箭头在 `.chev`），
   不另发明一种。折起来**不等于**看不见发生了什么：标题里带状态摘要
   （多少字 / 没有识别到文字 / 识别失败 / 几张图），下面还给一行截断的预览。
   多张图共用**一个**折叠容器（不是每张各一个）。 */

/* 预览长度：一行以内，超了截断加省略号 */
const OCR_PREVIEW_CHARS = 40;

/* 每张图的识别状态。三种如实的状态（§13.7 第 3 句）之外多认一种"没记录"：
   `ocr` 整个缺失时**不能**当成"没有识别到文字" —— 那是替一份半截数据下结论，
   正文里那句"这一张没有留下识别记录。"就是为它写的。 */
function ocrItemState(item) {
  const ocr = item && item.ocr;
  if (ocr && ocr.error) return { kind: 'failed' };
  if (ocr && typeof ocr.text === 'string' && ocr.text !== '') {
    // 字数优先用服务端记下的 `ocr.chars`（契约 §13.3），没有才拿文本长度兜底
    const chars = Number.isFinite(ocr.chars) ? ocr.chars : String(ocr.text).length;
    return { kind: 'text', chars, text: ocr.text };
  }
  if (ocr) return { kind: 'empty' };      // text 是空串 = 真的没识别到文字
  return { kind: 'missing' };
}

/* 这一张在**正文**里那一行该写什么。预览复用同一处，免得两边的话对不上。 */
function ocrItemText(item) {
  const state = ocrItemState(item);
  if (state.kind === 'failed') return t("ocr.failed", { msg: (item.ocr || {}).error });
  if (state.kind === 'text') return state.text;
  if (state.kind === 'empty') return t("ocr.no_text");
  return t("ocr.no_record");
}

function ocrStats(items) {
  const stats = { count: items.length, text: 0, chars: 0, failed: 0, empty: 0, missing: 0 };
  for (const item of items) {
    const state = ocrItemState(item);
    if (state.kind === 'text') {
      stats.text += 1;
      stats.chars += state.chars;
      continue;
    }
    const bucket = state.kind === 'failed' ? 'failed' : state.kind === 'empty' ? 'empty' : 'missing';
    stats[bucket] += 1;
  }
  return stats;
}

/* 标题里那句状态摘要 —— "折起来"不许等于"看不见发生了什么"。
   单张图：`127 字` / `没有识别到文字` / `识别失败`；
   多张图：先写张数，再写**合计**字数（`2 张图 · 340 字`）；
   两种以上的状态混在一起时，异常的那几种也一并写出来（不吞信息）。 */
function ocrSummary(items) {
  const stats = ocrStats(items);
  const parts = [];
  if (stats.count > 1) parts.push(t("ocr.count_images", { n: stats.count }));
  if (stats.text) parts.push(t("ocr.count_chars", { n: stats.chars }));
  if (stats.failed) parts.push(stats.text ? t("ocr.count_failed", { n: stats.failed }) : t("ocr.failed_short"));
  if (stats.empty) parts.push(stats.text ? t("ocr.count_empty", { n: stats.empty }) : t("ocr.empty_short"));
  if (stats.missing) parts.push(stats.text ? t("ocr.count_norec", { n: stats.missing }) : t("ocr.norec_short"));
  return parts.join(' · ');
}

/* 折叠时那一行预览：OCR 文本的第一行前 40 个字符（一行以内，CSS 再兜一层省略号）。
   没有文字 / 失败时用那一张正文里的话。**预览也是 textContent** —— 它同样是
   图片里的内容。 */
function ocrPreviewText(items) {
  const states = items.map((item) => ({ item, state: ocrItemState(item) }));
  const withText = states.find((entry) => entry.state.kind === 'text');
  if (withText) {
    const line = String(withText.state.text).split(/\r?\n/)
      .map((part) => part.trim()).find((part) => part !== '') || '';
    if (line) {
      return line.length > OCR_PREVIEW_CHARS ? `${line.slice(0, OCR_PREVIEW_CHARS)}…` : line;
    }
  }
  return states.length ? ocrItemText(states[0].item) : '';
}

function ocrBlock(items) {
  const list = (items || []).filter(Boolean);
  const box = el('div', 'ocr-block collapsed');
  const head = el('div', 'ocr-head');
  const chev = el('span', 'chev', '▸');
  head.appendChild(chev);
  head.appendChild(el('span', 'ocr-label', `${multimodalOcrLabel()} · ${ocrSummary(list)}`));
  box.appendChild(head);
  // 预览只在折叠时露出来（展开后就是原样全文，那时候预览是废话）
  box.appendChild(el('div', 'ocr-preview', ocrPreviewText(list)));

  const body = el('div', 'ocr-body');
  list.forEach((item, index) => {
    const entry = el('div', 'ocr-item');
    const name = (item && item.name) || t("attachment.image");
    entry.appendChild(el('div', 'ocr-item-name', t("attachment.image_label", { n: index + 1, name: name })));
    const state = ocrItemState(item);
    const className = state.kind === 'failed' ? 'ocr-text ocr-error'
      : state.kind === 'text' ? 'ocr-text' : 'ocr-text empty';
    entry.appendChild(el('div', className, ocrItemText(item)));
    body.appendChild(entry);
  });
  box.appendChild(body);

  // 点**整行标题**切换（与压缩摘要块一致）；箭头跟着状态走（▸ / ▾）
  head.onclick = () => {
    box.classList.toggle('collapsed');
    chev.textContent = box.classList.contains('collapsed') ? '▸' : '▾';
  };
  return box;
}

function addNotice(level, text) {
  const el = document.createElement('div');
  el.className = 'notice' + (level === 'error' ? ' error' : '');
  el.textContent = text;
  messagesEl().appendChild(el);
  scrollToBottom();
}

function addMemoryNote(payload) {
  // 记忆是全量注入的：平时不打扰，只在"有内容因为太长没进来"时说一句 ——
  // 那意味着模型这一轮确实看不到某些记忆，用户该知道。
  if (!payload || !payload.omitted) return;
  const el = document.createElement('div');
  el.className = 'notice';
  el.textContent =
    t("memory.omitted_note", { count: payload.count, limit: payload.limit, omitted: payload.omitted });
  messagesEl().appendChild(el);
  scrollToBottom();
}

/* 一轮结尾那一行统计（步数 / 用时 / 用量）。
   `forkSeq` = 这一轮 user_message 的 seq：给了就在**同一行的右侧**摆一个「分叉」
   按钮（与"步数 / 缓存命中"那一行对齐）；不给就只是一行字（老日志 / 没有 seq 的轮）。
   注意 `el` 是全局的建节点助手，这里用 `document.createElement` 免得被局部变量遮住。 */
function addMeta(text, forkSeq) {
  const row = document.createElement('div');
  row.className = 'meta';
  const label = document.createElement('span');
  label.className = 'meta-text';
  label.textContent = text;
  row.appendChild(label);
  if (forkSeq != null) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'fork-btn';
    btn.textContent = t("chat.fork");
    btn.title = t("chat.fork_title");
    btn.onclick = (event) => {
      event.stopPropagation();
      forkSession(Number(forkSeq));
    };
    row.appendChild(btn);
  }
  messagesEl().appendChild(row);
  scrollToBottom();
}

/* 上下文压缩块：告诉用户"较早的对话被摘要替代了"，
   否则用户会以为历史凭空消失了。默认收起，点开才看摘要正文。 */
function addSummaryBlock(summary, covered, justHappened) {
  const el = document.createElement('div');
  el.className = 'summary collapsed';
  el.innerHTML = t("chat.summary_html", { covered: Number(covered) || 0, suffix: justHappened ? t("chat.summary_just_now") : '' });
  el.querySelector('.summary-body').textContent = summary || t("chat.empty_summary");
  el.querySelector('.summary-head').onclick = () => el.classList.toggle('collapsed');
  if (justHappened) {
    // 刚压缩完就展开一次，让用户看到发生了什么；随后可手动收起
    el.classList.remove('collapsed');
  }
  // 摘要永远排在最前面：它代表的是最早的对话
  messagesEl().prepend(el);
  scrollToBottom();
}

function beginAssistant() {
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = t("chat.think_html");
  messagesEl().appendChild(el);

  const thinkEl = el.querySelector('.think');
  const thinkBody = el.querySelector('.think-body');
  const bubble = el.querySelector('.bubble');
  const tools = el.querySelector('.tools');

  let thinkBuffer = '';
  let textBuffer = '';
  let thinkTouched = false;
  let finished = false;

  thinkEl.querySelector('.think-head').onclick = () => {
    thinkTouched = true;
    thinkEl.classList.toggle('collapsed');
  };

  const api = {
    reasoning(chunk) {
      thinkEl.classList.remove('hidden');
      thinkBuffer += chunk;
      thinkBody.textContent = thinkBuffer;
      if (!textBuffer) thinkEl.classList.remove('collapsed');
      scrollToBottom();
    },
    text(chunk, quiet) {
      if (!textBuffer && thinkBuffer && !thinkTouched) thinkEl.classList.add('collapsed');
      textBuffer += chunk;
      bubble.innerHTML = renderRich(textBuffer) + (quiet ? '' : '<span class="cursor"></span>');
      scrollToBottom();
    },
    toolCall(payload) {
      // 思考阶段结束（转入调工具）就把思考块折起来 ——
      // 否则"只想不做正文"的那一步会把大段原始思维链一直摊在屏幕上。
      if (thinkBuffer && !thinkTouched) thinkEl.classList.add('collapsed');
      const chip = addToolChip(payload.name);
      chip.dataset.pending = '1';
      chip.querySelector('.tool-text').textContent = shortArgs(payload.arguments);
      scrollToBottom();
    },
    toolResult(payload) {
      const pending = tools.querySelector('.tool[data-pending="1"]');
      fillToolChip(pending, payload, null);
      scrollToBottom();
    },
    finish() {
      if (finished) return;
      finished = true;
      bubble.innerHTML = textBuffer ? renderRich(textBuffer) : '';
      if (!textBuffer && thinkBuffer && !thinkTouched) thinkEl.classList.add('collapsed');
      if (!textBuffer && !thinkBuffer) bubble.innerHTML = t("chat.no_output");
    },
    element: el,
  };
  return api;
}

function shortArgs(raw) {
  if (!raw) return '';
  const text = String(raw).replace(/\s+/g, ' ');
  return text.length > 70 ? text.slice(0, 70) + '…' : text;
}

function addToolChip(name) {
  // 工具卡片必须挂在**当前这一轮**的 assistant 容器里 —— 宽度才和其它卡片一致
  // （`.msg` 是 820px 居中，`.tools` 是它的子节点）。从后往前找最近的 assistant；
  // 碰到 user 就说明这一轮还没有 assistant 容器（助手只调了工具、没有正文/思考，
  // 会话视图里那一步被省略了），那就**自己建一个**。
  // **不能**退回 `.messages`：那会让卡片横跨整个页面（这个 bug 真的出现过）。
  let host = null;
  const kids = messagesEl().children;
  for (let i = kids.length - 1; i >= 0; i -= 1) {
    const kid = kids[i];
    if (!kid || !kid.classList) continue;
    if (kid.classList.contains('user')) break;
    if (kid.classList.contains('assistant')) {
      host = kid.querySelector('.tools') || null;
      break;
    }
  }
  if (!host) {
    const wrapper = document.createElement('div');
    wrapper.className = 'msg assistant';
    host = document.createElement('div');
    host.className = 'tools';
    wrapper.appendChild(host);
    messagesEl().appendChild(wrapper);
  }
  const chip = document.createElement('div');
  chip.className = 'tool collapsed';
  chip.innerHTML = t("chat.tool_chip_html", { name: escapeHtml(name) });
  chip.querySelector('.tool-head').onclick = () => chip.classList.toggle('collapsed');
  host.appendChild(chip);
  return chip;
}

function fillToolChip(chip, payload, argumentsText) {
  if (!chip) return;
  chip.dataset.pending = '';
  chip.classList.toggle('ok', !!payload.ok);
  chip.classList.toggle('fail', !payload.ok);
  chip.querySelector('.tool-text').textContent = payload.display || (payload.ok ? t("tool.done") : t("tool.failed"));
  const detail = [];
  if (argumentsText) detail.push(t("tool.args", { args: argumentsText }));
  detail.push(t("tool.result", { result: JSON.stringify(payload.result ?? {}, null, 2) }));
  chip.querySelector('.tool-detail').textContent = detail.join('\n');
}

function usageText(usage) {
  if (!usage) return '';
  const parts = [`prompt ${usage.prompt_tokens || 0}`, `completion ${usage.completion_tokens || 0}`];
  if (usage.cached_tokens) parts.push(t("usage.cached", { cached: usage.cached_tokens }));
  return parts.join(' · ');
}

function updateUsageHint(usage) {
  $('chat-meta').textContent = usage ? t("usage.this_chat", { total: usage.total_tokens || 0 }) : '';
}

/* ── 当前会话统计条 ─────────────────────────────────────
   轮数与累计用量来自事件日志；速度 = 全部输出 token / 各轮实际耗时之和。
   上下文占用只取最后一次模型请求的 prompt_tokens，不能把多步请求的输入相加。 */
function emptySessionStats() {
  return {
    turns: 0,
    elapsedSeconds: 0,
    contextTokens: 0,
    usage: Object.fromEntries(SESSION_USAGE_FIELDS.map((key) => [key, 0])),
  };
}

function statNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

function normalizeSessionStats(raw) {
  const source = raw || {};
  const usage = source.usage || {};
  const normalized = emptySessionStats();
  normalized.turns = Math.floor(statNumber(source.turns));
  normalized.elapsedSeconds = statNumber(source.elapsed_seconds);
  normalized.contextTokens = Math.floor(statNumber(source.context_tokens));
  for (const key of SESSION_USAGE_FIELDS) normalized.usage[key] = statNumber(usage[key]);
  return normalized;
}

function formatCompactTokens(value) {
  const number = statNumber(value);
  if (number < 1000) return String(Math.round(number));
  const units = ['k', 'M', 'B', 'T'];
  let scaled = number;
  let unit = '';
  for (const candidate of units) {
    if (scaled < 1000) break;
    scaled /= 1000;
    unit = candidate;
  }
  return `${Math.round(scaled * 10) / 10}${unit}`;
}

function formatStatRate(value) {
  return String(Math.round(statNumber(value) * 10) / 10);
}

function formatContextPercent(value) {
  const number = statNumber(value);
  // 有模型且有少量上下文时不能四舍五入成 0%：0% 专门表示未选模型/没有占用。
  return number > 0 && number < 1 ? '<1' : String(Math.round(number));
}

function selectedModelContextWindow() {
  const selected = state.selection || {};
  if (!selected.provider || !selected.model) return 0;
  const entry = ((state.providers && state.providers.models) || []).find((item) => (
    item.provider === selected.provider && item.model === selected.model
  ));
  return entry ? statNumber(entry.context_window) : 0;
}

function renderSessionStats() {
  const bar = $('session-stats');
  if (!bar) return;
  const stats = state.sessionStats || emptySessionStats();
  const usage = stats.usage || {};
  const speed = stats.elapsedSeconds > 0
    ? statNumber(usage.completion_tokens) / stats.elapsedSeconds : 0;
  const cachePercent = statNumber(usage.prompt_tokens) > 0
    ? statNumber(usage.cached_tokens) / statNumber(usage.prompt_tokens) * 100 : 0;
  const contextWindow = selectedModelContextWindow();
  const contextPercent = contextWindow > 0
    ? statNumber(stats.contextTokens) / contextWindow * 100 : 0;

  $('stats-turns').textContent = t('stats.turns', { n: stats.turns });
  $('stats-speed').textContent = t('stats.speed', { value: formatStatRate(speed) });
  $('stats-tokens').textContent = t('stats.tokens', { value: formatCompactTokens(usage.total_tokens) });
  $('stats-tokens').title = t('stats.tokens_title', { value: statNumber(usage.total_tokens) });
  $('stats-cache').textContent = t('stats.cache', { value: Math.round(cachePercent) });
  $('stats-context').textContent = t('stats.context', { value: formatContextPercent(contextPercent) });
  $('stats-context-wrap').title = contextWindow > 0
    ? t('stats.context_title', {
      used: statNumber(stats.contextTokens), window: contextWindow,
    })
    : t('stats.context_none');
  $('context-meter-fill').style.width = `${Math.max(0, Math.min(100, contextPercent))}%`;
}

function setSessionStats(raw) {
  state.sessionStats = normalizeSessionStats(raw);
  renderSessionStats();
}

function recordTurnStats(payload) {
  const frame = payload || {};
  const stats = normalizeSessionStats({
    turns: state.sessionStats.turns,
    elapsed_seconds: state.sessionStats.elapsedSeconds,
    context_tokens: state.sessionStats.contextTokens,
    usage: state.sessionStats.usage,
  });
  const usage = frame.usage || {};
  stats.turns += 1;
  stats.elapsedSeconds += statNumber(frame.elapsed);
  for (const key of SESSION_USAGE_FIELDS) stats.usage[key] += statNumber(usage[key]);
  // 新后端明确给最后一次请求的输入；老后端只有单步用量时才用它兜底。
  // 两者都没有时归零，不能把上一轮的上下文占用冒充成这一轮的。
  if (Object.prototype.hasOwnProperty.call(frame, 'context_tokens')) {
    stats.contextTokens = statNumber(frame.context_tokens);
  } else if (Object.prototype.hasOwnProperty.call(usage, 'prompt_tokens')) {
    stats.contextTokens = statNumber(usage.prompt_tokens);
  } else {
    stats.contextTokens = 0;
  }
  state.sessionStats = stats;
  renderSessionStats();
}

/* ── 图片附件（接口契约见 docs/11-attachments.md）─────────────
   三条入口：📎 选择文件 / 把图片拖到输入区 / 在输入框里 Ctrl+V 粘贴截图。
   选中之后**立刻**上传（POST /api/attachments）：缩略图 src 用的是服务端
   返回的那个 URL，显示的就是落盘、也就是模型将看到的那一版。
   刻意**不用** URL.createObjectURL —— 本地那一版和发给模型的不是同一张，
   用本地图顶替会让人以为"图上去了"，其实未必。 */

const ATTACH_TYPES = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif' };

/* 契约 §7.2 那个 GET 的查询串。上传响应（§7.1）里没有 url 字段、
   会话视图（§7.4）里才有，所以后端给了就用后端的，没给才照契约拼这个
   —— 拼的仍然是"问服务端要那一份落盘的图"，不是拿本地文件顶替。 */
function attachmentUrl(sessionId, id, ext) {
  if (!sessionId || !id) return '';
  return '/api/attachments?session=' + encodeURIComponent(sessionId)
    + '&id=' + encodeURIComponent(id) + '&ext=' + encodeURIComponent(ext || '');
}

/* 附件对象（上传响应的 attachment / 会话视图里的 attachment）→ 扩展名。
   契约里 `file` 的结尾就是 `.<ext>`（由服务端嗅探决定），优先信它；
   没有 `file` 时退回 mime 映射。 */
function attachmentExt(item, fallbackMime) {
  const raw = item || {};
  const fromFile = /\.([A-Za-z0-9]+)\s*$/.exec(String(raw.file || ''));
  if (fromFile) return fromFile[1].toLowerCase();
  return ATTACH_TYPES[raw.mime || fallbackMime || ''] || '';
}

function attachmentSrc(item) {
  const raw = item || {};
  if (raw.url) return raw.url;   // §7.4：前端只用后端给的 url 取图
  return attachmentUrl(state.sessionId, raw.id, attachmentExt(raw, raw.mime));
}

function attachmentLabel(item) {
  const raw = item || {};
  const name = raw.name || t("attachment.image");
  const w = raw.width || raw.stored_width;
  const h = raw.height || raw.stored_height;
  return w && h ? `${name}（${w}×${h}）` : name;
}

/* 把 File / Blob 读成裸 base64（去掉 data URL 的头）。
   用 FileReader 是因为它连老浏览器都有；`file.arrayBuffer()` 那条路
   在自检的 DOM stub 里反而要多造两个桩。 */
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(t("attachment.read_failed", { detail: file && file.name ? `：${file.name}` : '' })));
    reader.onload = () => {
      const text = String(reader.result || '');
      const cut = text.indexOf(',');
      resolve(cut >= 0 ? text.slice(cut + 1) : text);
    };
    reader.readAsDataURL(file);
  });
}

async function uploadAttachment(file) {
  const data_base64 = await readFileAsBase64(file);
  return uploadAttachmentData({
    name: (file && file.name) || 'image',
    mime: (file && file.type) || '',
    data_base64,
  });
}

/* POST /api/attachments。session_id 为 null = 服务端生成一个新会话 id
   并在响应里返回（新对话还没有 id，图片先落地到一个"还没写 .jsonl"的会话目录）。
   一次上传 = 一张图，所以调用方按顺序一张一张 await：多张图的先后顺序
   必须就是用户点选的顺序，并发上传会让"谁先回来谁排前面"。 */
async function uploadAttachmentData({ name, mime, data_base64 }) {
  // 请求飞出去之前先把"这是哪个会话的上传"记下来：响应回来时 state.sessionId
  // 可能已经被用户切走了，那时它代表的是**另一个**会话。
  const requestedSession = state.sessionId;
  const body = await requestJSON('/api/attachments', {
    session_id: requestedSession,
    name,
    mime,
    data_base64,
  });
  // 只有"本来就没有会话、而且到现在还是没有"才采纳服务端生成的新 id。
  // 否则一张慢回来的图会把 state.sessionId 改成旧会话 —— 之后的每条消息
  // 都会发到别的会话（或者又静默开一个新的）里。
  const returned = body && typeof body.session_id === 'string' ? body.session_id : '';
  if (!requestedSession && !state.sessionId && returned) {
    state.sessionId = returned;
  } else if (state.sessionId !== requestedSession) {
    // 上传途中切走了：这张图属于旧会话目录，带不过去，直接丢掉并说一句
    // （静默丢弃会让用户以为图已经加上了）。
    toast(t("attachment.session_changed"));
    return null;
  }
  const raw = body.attachment || {};
  const ext = attachmentExt(raw, mime);
  const item = {
    id: raw.id,
    ext,
    name: raw.name || name,
    mime: raw.mime || mime,
    width: raw.stored_width || raw.width || 0,
    height: raw.stored_height || raw.height || 0,
    bytes: raw.stored_bytes || raw.bytes || 0,
    url: raw.url || attachmentUrl(body.session_id, raw.id, ext),
    warnings: Array.isArray(body.warnings) ? body.warnings.slice() : [],
  };
  return item;
}

/* 后端 400 的原话 + 缺 Pillow 时那句可操作的提示（契约 §6：不做静默降级） */
function attachmentErrorText(err) {
  const need = (err && err.body && err.body.need) || '';
  const text = String((err && err.message) || err);
  return need === 'pillow' ? t("attachment.setup_hint", { msg: text }) : text;
}

function attachmentThumb(item, className) {
  const img = document.createElement('img');
  img.className = className || 'attach-thumb';
  img.setAttribute('src', attachmentSrc(item));
  img.setAttribute('alt', (item && item.name) || t("attachment.image"));
  img.setAttribute('title', attachmentLabel(item));
  img.onclick = () => openLightbox(attachmentSrc(item), (item && item.name) || '');
  return img;
}

/* 缩略图条：待发列表与消息气泡里用的是同一套节点（只差外层 class 与有没有 ✕） */
function attachmentStrip(items) {
  const strip = el('div', 'attach-strip');
  for (const item of items || []) {
    if (!item) continue;
    strip.appendChild(attachmentThumb(item));
  }
  return strip;
}

function renderAttachments() {
  const host = $('composer-attachments');
  clearNode(host);
  const list = state.attachments;
  host.classList.toggle('hidden', !list.length);
  for (const item of list) {
    const box = el('div', 'attach-item');
    box.appendChild(attachmentThumb(item));
    // 契约 §13.7 第 2 句：开关关着时才在**每张**待发缩略图上标一句。
    // 开着的时候一个字都不能有 —— 那会让人以为图片没发出去。
    if (multimodalTextOnly()) {
      box.appendChild(el('div', 'attach-badge', multimodalBadgeText()));
    }
    const remove = el('button', 'attach-remove', '✕');
    remove.setAttribute('type', 'button');
    remove.setAttribute('title', t("attachment.remove_title"));
    remove.onclick = () => removeAttachment(item);
    box.appendChild(remove);
    host.appendChild(box);
  }
  syncAttachEntry();
}

/* 移除只是从**待发列表**里拿掉：磁盘上的那份还在（别的消息可能引用着同一个
   内容哈希，去重之后更不该由前端说删就删）。 */
function removeAttachment(item) {
  state.attachments = state.attachments.filter((entry) => entry !== item);
  renderAttachments();
}

/* 清空。传了 only 就只清那几个（发送成功时清的是"这一轮真的带上去的"）。 */
function clearAttachments(only) {
  if (only && only.length) {
    const sent = new Set(only);
    state.attachments = state.attachments.filter((entry) => !sent.has(entry));
  } else {
    state.attachments = [];
  }
  renderAttachments();
}

function attachmentsAllowed() {
  if (state.busy) {
    // busy 时不做排队、也不默默吞掉：明说为什么不收（契约 §11：行为要可预期）
    toast(t("attachment.busy_add"));
    return false;
  }
  return true;
}

/* 入口主函数：File[] → 一张一张上传。某一张失败**只丢那一张**，
   其余成功的留在待发列表里，错误用后端原话 toast 出来。 */
async function attachFiles(files) {
  const list = Array.from(files || []).filter(Boolean);
  if (!list.length) return;
  if (!attachmentsAllowed()) return;
  const failed = [];
  const warnings = [];
  for (const file of list) {
    try {
      const item = await uploadAttachment(file);
      // 上传途中切了会话：那一张已经被 uploadAttachmentData 丢掉并 toast 过了
      if (!item) continue;
      if (item.warnings.length) warnings.push(...item.warnings);
      state.attachments.push(item);
      renderAttachments();
    } catch (err) {
      failed.push(attachmentErrorText(err));
    }
  }
  if (failed.length) toast(failed.join('；'));
  else if (warnings.length) toast(warnings.join('；'));
}

function isImageFile(file) {
  const type = String((file && file.type) || '');
  // 截图粘贴给的文件名常常是空的，类型也可能缺失 —— 类型不认识的也收下，
  // 由服务端按文件内容嗅探后拒绝（前端不做"看起来像不像图片"的判断）。
  return type === '' || type.startsWith('image/');
}

/* 拖放：只有 .composer（#composer-drop）这一块是落点。
   挂在输入区而不是 window/document，正是为了"拖到窗口别处不该触发"。 */
function onComposerDragOver(event) {
  event.preventDefault();
  if (state.busy) return;
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  $('composer-drop').classList.add('dragover');
}

function onComposerDragLeave(event) {
  // dragleave 在子元素之间移动也会冒出来一次：relatedTarget 还在这一块里时
  // 不该撤掉高亮，否则鼠标从缩略图挪到输入框，高亮就一闪一闪的。
  const zone = $('composer-drop');
  const to = event && event.relatedTarget;
  if (to && typeof zone.contains === 'function' && zone.contains(to)) return;
  zone.classList.remove('dragover');
}

function onComposerDrop(event) {
  event.preventDefault();
  $('composer-drop').classList.remove('dragover');
  if (!attachmentsAllowed()) return;
  const transfer = event.dataTransfer || {};
  attachFiles(transfer.files || []);
}

/* Ctrl+V：截图粘贴是最高频的用法。只在**剪贴板里真的有文件**时接管，
   普通文字粘贴一概不拦。 */
function onComposerPaste(event) {
  const transfer = (event && event.clipboardData) || {};
  const files = Array.from(transfer.files || []).filter(isImageFile);
  if (!files.length) return;
  event.preventDefault();
  if (!attachmentsAllowed()) return;
  attachFiles(files);
}

/* ── 大图预览（灯箱）────────────────────────────────────── */

let lightboxKey = null;

function openLightbox(url, name) {
  if (!url) return;
  $('lightbox-img').setAttribute('src', url);
  $('lightbox-img').setAttribute('alt', name || t("attachment.image"));
  $('lightbox-caption').textContent = name || '';
  $('lightbox').classList.remove('hidden');
  if (!lightboxKey) {
    lightboxKey = (event) => { if (event.key === 'Escape') closeLightbox(); };
    document.addEventListener('keydown', lightboxKey);
  }
}

function closeLightbox() {
  $('lightbox').classList.add('hidden');
  $('lightbox-img').setAttribute('src', '');
  if (lightboxKey) {
    document.removeEventListener('keydown', lightboxKey);
    lightboxKey = null;
  }
}

/* 拖到**别处**（不是输入区）时用的：只把浏览器的默认行为挡掉 —— 默认行为是
   "把这张图当成新页面打开"，会把整段对话连同没发出去的图一起顶掉。
   这一路**不碰附件**：上传只可能由输入区的 drop 触发。
   定义在模块作用域（而不是每次接线时新建一个匿名函数）是有意的：
   同一个函数重复 addEventListener 不会真的挂两遍。 */
function swallowWindowDrop(event) { event.preventDefault(); }

/* 入口三件套的接线。放在一个函数里（而不是散在 init 里），
   这样自检能在不启动整个应用的情况下把"哪个元素挂了哪个监听"验出来。 */
function wireComposerAttachments() {
  renderAttachments();

  const button = $('attach');
  button.onclick = () => {
    if (!attachmentsAllowed()) return;
    $('attach-file').click();
  };

  const picker = $('attach-file');
  picker.onchange = () => {
    // 先**拷成数组**再清空 value：`input.files` 是个跟着控件走的活对象，
    // 清空 value 之后手里那个引用常常就空了（同一个文件连选两次也要能再触发）。
    const files = Array.from(picker.files || []);
    picker.value = '';
    attachFiles(files);
  };

  const zone = $('composer-drop');
  zone.addEventListener('dragover', onComposerDragOver);
  zone.addEventListener('dragleave', onComposerDragLeave);
  zone.addEventListener('drop', onComposerDrop);
  $('input').addEventListener('paste', onComposerPaste);

  window.addEventListener('dragover', swallowWindowDrop);
  window.addEventListener('drop', swallowWindowDrop);

  $('lightbox').onclick = (event) => {
    if (event.target === $('lightbox') || event.target === $('lightbox-img')) closeLightbox();
  };

  syncAttachEntry();
}

/* busy 时 📎 禁用；拖放 / 粘贴在各自的处理函数里挡住并用 toast 明说。 */
function syncAttachEntry() {
  const button = $('attach');
  if (button) button.disabled = !!state.busy;
  if (state.busy) $('composer-drop').classList.remove('dragover');
}

/* ── 发送 ─────────────────────────────────────────────── */

function setBusy(busy) {
  state.busy = busy;
  $('send').classList.toggle('hidden', busy);
  $('stop').classList.toggle('hidden', !busy);
  $('send').disabled = busy;
  syncAttachEntry();
  /* 语言是**进程级**的：正在生成时切语言，同一轮里后续每一步都会用新语言重新组装
     系统提示与工具定义 —— 模型会在一次回答中途看到"系统提示换了一套"。
     所以「设置 → 语言」那两个选项在 busy 时是禁用的（见 renderLanguagePane），
     这里负责"这一轮结束之后**自动恢复可用**"。别的设置项没有这个问题（它们要么只
     影响下一轮，要么本来就是会话级的开关），所以只在这一处动手。 */
  if (state.pane === 'general') renderGeneralPane();
}

async function send() {
  const input = $('input');
  const text = input.value.trim();
  // 「至少一个非空」：只发图不说话也是合法的一轮（契约 §7.3）
  const pending = state.attachments.slice();
  if ((!text && !pending.length) || state.busy) return;
  if (!(state.providers && state.providers.models || []).length) {
    toast(t("chat.no_provider"));
    return;
  }
  const payload = {
    session_id: state.sessionId,
    text,
    provider: state.selection.provider,
    model: state.selection.model,
    effort: state.selection.effort,
    web_search: !!state.webSearch,   // 新会话还没落盘，开关只能随消息传上去
    // 与 web_search 平级、同样随每一条消息传（契约 §13.1）。
    // 它决定**图片怎么进请求**：true = 图片本身发出去；false = 只发服务端 OCR 的文字。
    multimodal: !multimodalTextOnly(),
  };
  // 没图时请求体的形状与从前一致：只有 attachments 是"有图才多出来的"那一个键。
  // 服务端只认 {id, ext, name}：路径它自己按会话目录拼，绝不用前端给的东西。
  if (pending.length) {
    payload.attachments = pending.map((item) => ({ id: item.id, ext: item.ext, name: item.name }));
  }
  // 这一轮是不是"只发文字"那条路（要按**发出去的那个值**记，不是发完再看状态：
  // 中途来的 tools 帧会改 state.multimodal）
  const textOnlyTurn = !payload.multimodal && pending.length > 0;
  let confirmed = false;   // 服务端回过 session 帧 = 这一轮真的开始了
  input.value = '';
  autoGrow();
  setBusy(true);
  addUser(text, pending);
  const ui = beginAssistant();
  try {
    await streamPost('/api/chat', payload, {
      session: (payload) => {
        confirmed = true;
        const frame = payload || {};
        // "这一轮开始之前有没有会话 id"要在采纳新 id **之前**判断：
        // 只有全新会话才拿当前消息当标题（命名/改过名的会话由刷新后的列表说了算）。
        const wasNew = !state.sessionId;
        // 只认非空字符串。null / '' 曾经把会话 id 抹成 null，于是之后每一轮
        // 都会静默开一个**新**会话（在用户看来就是历史突然没了）。
        if (typeof frame.session_id === 'string' && frame.session_id) {
          state.sessionId = frame.session_id;
          if (wasNew) {
            setSessionStats(null);
            $('chat-title').textContent = text
              ? text.slice(0, 40)   // 与侧栏 session_display_title 的 40 字保持一致
              : (pending.length ? t("chat.images_only") : t("chat.new_title"));
          }
        }
        // 服务端确认这一轮真的开始了，才清空待发附件；在那之前（比如 400）
        // 图片一律留着，让用户能重发。
        clearAttachments(pending);
      },
      generation: (payload) => { applySelection(payload); },
      tools: applyToolsFrame,
      step: () => {},
      memory: addMemoryNote,
      reasoning: (payload) => ui.reasoning((payload || {}).text ?? ''),
      text: (payload) => ui.text((payload || {}).text ?? ''),
      tool_call: (payload) => ui.toolCall(payload),
      tool_result: (payload) => ui.toolResult(payload),
      compact: (payload) => addSummaryBlock(payload.summary, payload.covered, true),
      notice: (payload) => {
        const frame = payload || {};
        addNotice(frame.level, frame.text ?? '');
      },
      turn_end: (payload) => {
        // `usage` 可能不在这一帧里（旧后端 / 将来改了帧形状）。**不能直读**：
        // 少一个字段就 TypeError，而它是在 SSE 处理函数里抛的 —— 结果是
        // **这一条流后面所有帧都不再被处理**（界面看着像"卡住了"，其实早就炸了）。
        // 后端目前总是带 usage（web.py 的 turn_end），但这条不该由后端来保证。
        const usage = (payload && payload.usage) || null;
        // `user_seq` 是后端在 turn_end 帧里带回来的这一轮分叉点（老后端没有它 → 不给按钮）
        addMeta(t("meta.steps_line", { steps: payload.steps, reason: payload.stop_reason, elapsed: payload.elapsed })
          + usageText(usage), payload && payload.user_seq);
        recordTurnStats(payload);
        updateUsageHint(state.sessionStats.usage.total_tokens ? state.sessionStats.usage : null);
      },
      // 某一帧的处理函数抛异常时：只报一次，然后继续读后面的帧
      // （见 streamPost 里那条注释）。
      onHandlerError: (err) => {
        addNotice('error', t("notice.stream_handler_failed", { msg: String((err && err.message) || err) }));
      },
    });
  } catch (err) {
    if (err.name === 'AbortError') addNotice('warn', t("notice.stopped"));
    else {
      addNotice('error', String(err.message || err));
      // 契约 §13.5 / §13.8：后端因为"这个模型没声明支持图片"拒收时，
      // 返回体里带 need:"vision"。这一轮唯一能动的就是「多模态」那个按钮 ——
      // 把后端原话照旧显示，同时把它闪一下（不自动替用户改开关：
      // 关掉多模态会让图片不再发出去，那是用户的决定，不是我们的）。
      if (((err.body || {}).need) === 'vision') highlightMultimodalToggle();
    }
  } finally {
    ui.finish();
    setBusy(false);
    state.controller = null;
    await loadSessions().catch(() => {});
    // 「只发文字」那一轮的 OCR 是**服务端在发送那一刻**跑的，前端手上没有它。
    // 为了让用户看得见模型到底收到了什么（契约 §13.7 第 3 句），这一轮结束后
    // 按会话视图把那条用户气泡重画一次 —— 与刷新页面看到的是同一份数据。
    // notice 帧、工具卡片、回复都**不动**（只换那一条用户气泡的节点）。
    if (textOnlyTurn && confirmed) {
      await refreshLastUserMessage(text, pending).catch(() => {});
    }
  }
}

/* 附件列表的"同一批"判据：id 由内容哈希决定（契约 §2），所以同一张图重发
   拿到的就是同一个 id。用它比对"服务端记下的那一条是不是我刚发的这一条"，
   免得在"其实没写进事件"的情形下把**上一条**旧消息重画成新的。 */
function attachmentSignature(items) {
  return (items || []).map((item) => String((item && item.id) || '')).join(',');
}

/* 用会话视图里**最后一条** user 消息重画刚发出的那个气泡。
   先核对身份（文字 + 附件 id 都对得上、而且记的是 modality:"text"），
   对不上就什么都不做 —— 那时屏幕上已经有后端原话的错误提示，
   硬把旧消息刷进来只会制造第二条迷糊账。 */
async function refreshLastUserMessage(text, pending) {
  if (!state.sessionId) return;
  let data;
  try {
    data = await getJSON(`/api/sessions/${encodeURIComponent(state.sessionId)}`);
  } catch (err) {
    addNotice('warn', t("notice.ocr_refresh_failed", { msg: err.message || err })
      + t("notice.ocr_refresh_hint"));
    return;
  }
  const messages = (data && data.messages) || [];
  const lastUser = [...messages].reverse().find((message) => message.role === 'user');
  if (!lastUser || lastUser.modality !== 'text') return;
  if (lastUser.text !== text) return;
  if (attachmentSignature(lastUser.attachments) !== attachmentSignature(pending)) return;

  const fresh = userBubbleNode(lastUser.text, lastUser.attachments, lastUser.modality);
  const users = Array.from(messagesEl().querySelectorAll('.msg'))
    .filter((node) => node.classList.contains('user'));
  const old = users[users.length - 1];
  if (!old || !old.parentNode) { messagesEl().appendChild(fresh); return; }
  old.parentNode.insertBefore(fresh, old);
  old.parentNode.removeChild(old);
  state.stick = true;
  scrollToBottom();
}

function stop() {
  if (state.controller) state.controller.abort();
}

function autoGrow() {
  const input = $('input');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 190) + 'px';
}

/* ── 记忆 ─────────────────────────────────────────────── */

async function loadMemories() {
  state.memories = await getJSON('/api/memories');
  state.memoriesLoaded = true;
  renderMemories();
}

function renderMemories() {
  const data = state.memories;
  if (!data) return;
  const stats = data.stats || {};
  const limit = data.limit || {};
  const injection = data.injection || {};
  const active = stats.active || 0;
  const cap = limit.max_items ?? 50;

  $('memory-stats').textContent = t("memory.stats", { active: active, cap: cap });

  // 上限控件：值、可设范围、以及"现在最低能设到多少"（不能低于已有条数）
  const field = $('memory-limit');
  if (document.activeElement !== field) field.value = cap;
  field.min = limit.min_allowed ?? 0;
  field.max = limit.max_allowed ?? 100;
  $('memory-limit-hint').textContent =
    t("memory.range", { min: limit.min_allowed ?? 0, max: limit.max_allowed ?? 100 })
    + t("memory.lowest_now", { lowest: limit.lowest_now ?? 0 })
    + (active >= cap ? t("memory.full") : '');

  const note = $('memory-injection-note');
  if (injection.omitted) {
    note.className = 'pane-note warn';
    note.textContent =
      t("memory.budget_warn_1", { tokens: injection.token_budget })
      + t("memory.budget_warn_2", { n: injection.omitted });
  } else {
    note.className = 'pane-note';
    note.textContent = '';
  }

  const keyword = $('memory-search').value.trim().toLowerCase();
  const list = $('memory-list');
  list.innerHTML = '';

  const items = (data.items || []).filter((item) => {
    if (!keyword) return true;
    return String(item.content || '').toLowerCase().includes(keyword);
  });

  if (!items.length) {
    list.innerHTML = t("memory.no_match");
    return;
  }

  for (const item of items) {
    const card = document.createElement('div');
    card.className = 'mem-card';
    // 原生 memory_instruction.md 只有 id 与正文，没有可展示的分类/状态/时间戳。
    card.innerHTML = t("memory.card_html", { id: escapeHtml(item.id), content: escapeHtml(item.content), actions: t("memory.delete_btn") });
    card.querySelector('[data-act="edit"]').onclick = () => editMemory(card, item);
    const del = card.querySelector('[data-act="del"]');
    if (del) del.onclick = () => deleteMemory(item);
    list.appendChild(card);
  }
}

async function saveMemoryLimit() {
  const raw = $('memory-limit').value.trim();
  if (raw === '') { toast(t("memory.limit_invalid")); return; }
  const value = Number(raw);
  if (!Number.isInteger(value)) { toast(t("memory.limit_not_int")); return; }
  try {
    const result = await postJSON('/api/memories/limit', { max_items: value });
    toast(t("memory.limit_set", { n: result.limit.max_items }));
    await loadMemories();
  } catch (err) {
    // 服务端的原话最有用（例如"上限太低：现在已经有 12 条记忆…"）
    toast(String(err.message || err));
  }
}

function editMemory(card, item) {
  const content = card.querySelector('.mem-content');
  const original = item.content;
  content.innerHTML = '';
  const area = document.createElement('textarea');
  area.value = original;
  area.rows = 2;
  area.style.cssText = 'width:100%;font:inherit;padding:6px 8px;border-radius:8px;'
    + 'border:1px solid var(--border-strong);background:var(--bg);color:var(--text)';
  const bar = document.createElement('div');
  bar.className = 'mem-actions';
  bar.style.marginTop = '6px';
  bar.innerHTML = t("memory.edit_save_btn")
    + t("memory.edit_cancel_btn");
  content.appendChild(area);
  content.appendChild(bar);
  area.focus();

  bar.querySelector('[data-act="cancel"]').onclick = () => {
    content.textContent = original;
  };
  bar.querySelector('[data-act="save"]').onclick = async () => {
    const next = area.value.trim();
    if (!next) { toast(t("memory.content_required")); return; }
    try {
      await postJSON('/api/memories/update', { id: item.id, content: next });
      toast(t("memory.updated"));
      await loadMemories();
    } catch (err) { toast(String(err.message || err)); }
  };
}

async function deleteMemory(item) {
  if (!confirm(t("memory.delete_confirm", { preview: item.content }))) return;
  try {
    await postJSON('/api/memories/delete', { id: item.id });
    toast(t("memory.deleted_note"));
    await loadMemories();
  } catch (err) { toast(String(err.message || err)); }
}

/* ── Dream：背景档案 + 整理运行 ─────────────────────────── */

/* 这一页有两份数据，而且是**分开拉的**：
   /api/vault 给"现在长什么样、还有多少没整理"（点一次「整理一次」随时会变），
   /api/dreams 给运行历史（一次运行写完就不再变）。
   合成一个请求只会让"跑完之后只刷新其中一半"变得别扭。 */

async function loadVault() {
  state.vault = await getJSON('/api/vault');
  state.vaultLoaded = true;
  renderVaultPane();
  renderDreamMeta();
}

async function loadDreams() {
  const data = await getJSON('/api/dreams');
  state.dreamsLoaded = true;
  renderDreamList(data.runs || []);
  renderDreamMeta();
}

/* ── Dream 页的整理模型（ADR-103）───────────────────────────
   以前整理用的是**聊天页当时选中的那个模型**（runDream 直接把 state.selection
   发上去），所以它既看不见也调不了；而 CLI 走的是全局默认 —— 两边还不是
   同一个答案。现在它是一种**持久设置**（存在 providers.json 的 `dream` 里），
   CLI 与 Web 共用同一份：
     * 空 = 「跟随默认」—— 与加这个设置之前的行为完全一样；
     * 有值 = 明确指定；接口后来被删掉时后端会退回默认（不静默换一家）。 */

/* 当前设置的那一项，形状 `provider|model`；空串 = 跟随默认。 */
function dreamModelValue() {
  const dream = (state.providers && state.providers.dream) || {};
  const provider = String(dream.provider || '');
  const model = String(dream.model || '');
  return provider && model ? `${provider}|${model}` : '';
}

/* 「跟随默认」那一项上写什么：**模型的显示名**（不带接口前缀 —— 下拉列表里
   本来就按接口分了组，那一项被挤窄时先丢掉接口名比丢掉模型名更合理）。
   取不到就退回模型 id；再取不到就只写「跟随默认」。 */
function defaultModelLabel() {
  const data = state.providers || {};
  const def = data.default || {};
  const hit = (data.models || []).find(
    (item) => item.provider === def.provider && item.model === def.model,
  );
  if (hit) return hit.model_label || hit.model;
  return def.model || '';
}

function renderDreamPicker() {
  const select = $('dream-model');
  if (!select) return;
  const models = (state.providers && state.providers.models) || [];
  select.innerHTML = '';
  if (!models.length) {
    // 一个可用模型都没有：与聊天页顶栏那句一致，别让它看起来还能选。
    const option = document.createElement('option');
    option.value = '';
    option.textContent = t('dream.model_none');
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  const fallback = document.createElement('option');
  fallback.value = '';
  const fallbackLabel = defaultModelLabel();
  fallback.textContent = fallbackLabel
    ? t('dream.model_follow_named', { model: fallbackLabel })
    : t('dream.model_follow');
  select.appendChild(fallback);

  // 与顶栏模型选择器同样的分组方式（按接口分 optgroup）
  const groups = new Map();
  for (const entry of models) {
    const key = entry.provider_label || entry.provider;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  const values = [''];
  for (const [groupLabel, list] of groups) {
    const group = document.createElement('optgroup');
    group.label = groupLabel;
    for (const entry of list) {
      const option = document.createElement('option');
      option.value = `${entry.provider}|${entry.model}`;
      option.textContent = entry.model_label || entry.model;
      group.appendChild(option);
      values.push(option.value);
    }
    select.appendChild(group);
  }
  // 存着的值不在列表里（接口 / 模型被删了）：显示「跟随默认」，
  // 与后端的兜底行为一致；**不改写**存储（等用户下一次显式选择）。
  const wanted = dreamModelValue();
  select.value = values.includes(wanted) ? wanted : '';
  select.disabled = false;
}

async function setDreamModel(provider, model) {
  const select = $('dream-model');
  if (select) select.disabled = true;
  try {
    const result = await postJSON('/api/providers/dream', { provider, model });
    state.providers.dream =
      (result && result.dream) || { provider, model, effort: '' };
    toast(t('dream.model_saved'));
  } catch (err) {
    // **失败时不改 state**：下面那句 renderDreamPicker() 会把下拉框退回原值，
    // 免得界面显示一个后端并没有存上的选择。
    toast(t('dream.model_save_failed', { msg: err.message || err }));
  } finally {
    renderDreamPicker();
  }
}

function onDreamModelChange() {
  const raw = $('dream-model').value;
  const [provider, model] = raw ? raw.split('|') : ['', ''];
  // **串行化**：连续改两次时，后一个 POST 必须等前一个落盘才发 ——
  // 否则两个请求的到达顺序不确定，最后存下的可能不是你最后选的那个。
  // 这个 promise 也用于 `runDream()`：整理开始前等它落盘（见那里）。
  const previous = state.dreamModelSaving || Promise.resolve();
  state.dreamModelSaving = previous.then(() => setDreamModel(provider, model));
}

function renderDreamMeta() {
  const runs = state.dreams || 0;
  const pending = ((state.vault || {}).pending) || {};
  const waiting = Number(pending.sessions || 0);
  $('dream-meta').textContent =
    t("dream.meta", { parts: runs ? t("dream.runs_count", { n: runs }) : t("dream.no_runs_short"), n: waiting });
}

async function loadDreamPane() {
  // 两份数据互不依赖：一份拉不到不该把另一份也藏起来
  // （否则运行历史会因为 vault 目录被手工删掉而整片空白）。
  const [vaultResult, runsResult] = await Promise.allSettled([loadVault(), loadDreams()]);
  const failures = [vaultResult, runsResult].filter((item) => item.status === 'rejected');
  if (!failures.length) return;
  const message = String((failures[0].reason && failures[0].reason.message) || failures[0].reason);
  if (runsResult.status === 'rejected' && dreamRunsHost()) {
    dreamRunsHost().innerHTML =
      t("dream.runs_load_failed", { msg: escapeHtml(message) });
  }
  throw new Error(message);
}

/* ── 小工具（只服务这一页） ─────────────────────────────── */

function fmtInt(value) {
  return Number(value || 0).toLocaleString('zh-CN');
}

/* 字符数用原样数字：中文里的"1,024 字"读起来像一千零二十四条，
   而这里说的是字数，加千位分隔反而容易看错。 */
function fmtChars(value) {
  return String(Number(value || 0));
}

/* "2026-10-12T10:16:02" 这种没带时区的本地时间。
   刻意**不走 Date 解析**：带不带时区会让同一串字符在不同浏览器里差几个小时，
   而运行记录里存的本来就是本地墙上时间。 */
function fmtStamp(text) {
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(text || ''));
  return match ? `${match[2]}-${match[3]} ${match[4]}:${match[5]}` : String(text || '');
}

/* 建一个元素：文本一律走 textContent。
   档案里的标题、摘要、标签、正文都是模型写的，**不能用 innerHTML 拼**，
   所以这一页统一从这条路径建 DOM 树，而不是拼字符串再塞回去。 */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clearNode(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

/* 属性区：只画**有值**的那些（后端对缺失字段给的是空串 / 空数组）。
   没有值的属性不占一行 —— 一行"status："比不显示更难看。 */
function attrRow(label, values) {
  const list = (Array.isArray(values) ? values : [values])
    .map((value) => String(value === undefined || value === null ? '' : value).trim())
    .filter((value) => value !== '');
  if (!list.length) return null;
  const row = el('div', 'vault-attr');
  row.appendChild(el('span', 'va-k', label));
  for (const value of list) row.appendChild(el('span', 'chip', value));
  return row;
}

/* frontmatter 里的自定义键（extra）也照原样显示成键值对。 */
/* frontmatter 里的自定义键（extra）也照原样显示成键值对。
   原生的 `relation` 是"列表套映射"、`date` 是嵌套映射 —— 直接 String() 会变成
   `[object Object]`，所以这里递归展开。 */
function extraValue(value) {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map(extraValue).join('、');
  if (typeof value === 'object') {
    return Object.entries(value)
      .map(([key, inner]) => `${key}: ${extraValue(inner)}`)
      .join('；');
  }
  return String(value);
}

function extraRow(key, value) {
  const text = extraValue(value);
  if (!text.trim()) return null;
  const row = el('div', 'vault-attr vault-attr-extra');
  row.appendChild(el('span', 'va-k', key));
  row.appendChild(el('span', 'va-v', text));
  return row;
}

function durationText(run) {
  const started = Date.parse(run.started_at || '');
  const finished = Date.parse(run.finished_at || '');
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) return '';
  const seconds = Math.round((finished - started) / 1000);
  if (seconds < 60) return t("dream.seconds", { n: seconds });
  return t("dream.minutes_seconds", { m: Math.floor(seconds / 60), s: seconds % 60 });
}

/* 状态 → (文案 key, 色调)。这里存的是**key** 而不是文案：状态字是运行期查的，
   存文案会在加载时就冻成当时的语言（切语言后运行记录还留在旧语言）。 */
const RUN_STATUS = {
  running: ['run.status.running', 'warn'],
  completed: ['tool.done', 'ok'],
  error: ['run.status.error', 'err'],
  empty: ['run.status.empty', ''],
  // 进程被杀/断电留下的记录：下一次整理会把它收尾成这个状态（dream.py 的
  // mark_interrupted）。不映射的话界面会把它显示成"运行中"，而那是假的。
  interrupted: ['run.status.interrupted', 'err'],
};

function runStatusChip(status) {
  const [labelKey, tone] = RUN_STATUS[status] || [null, ''];
  const label = labelKey ? t(labelKey) : (status || t('run.status.unknown'));
  return `<span class="chip ${tone}">${escapeHtml(label)}</span>`;
}

/* 一次运行在界面上叫什么。
   `dry-run` **只可能来自历史记录**（预演本身已经随 ADR-072 取消，页面上没有发起入口），
   所以给老的预演记录一个中性的标注 —— 而不是把它显示成"已应用"（那是在说假话），
   也不是让历史记录变成一片看不懂的东西。 */
function modeLabel(mode) {
  return mode === 'dry-run' ? t("dream.mode.dry") : t("dream.mode.apply");
}

/* `skipped` 是后端**压根没调模型**时给的标记（没有待整理的记录）。
   它不该被画成一次失败的运行 —— 那是一分钱都没花的正常结果。 */
function pendingText(pending) {
  const sessions = Number((pending || {}).sessions || 0);
  const events = Number((pending || {}).events || 0);
  if (!sessions) return t("dream.pending_latest");
  return t("dream.pending_counts", { n: sessions, events: fmtInt(events) });
}

/* 日志比游标还短（被手工改短、换过文件、或从旧备份恢复过）：
   后端把这种会话按「从头整理」处理，所以下次整理会多花一点 ——
   这不是正常状态，也不该悄悄发生，所以在待整理那块明说一句。
   字段缺失或为 0 时什么都不显示（"没有异常"不需要占地方）。 */
function staleNoticeHtml(pending) {
  const stale = Number((pending || {}).stale || 0);
  if (!stale) return '';
  return t("dream.stale_notice_open", { n: stale })
    + t("dream.stale_notice_mid")
    + t("dream.stale_notice_close");
}

/* ── 档案正文的极简 markdown 渲染 ───────────────────────────
   档案是**模型写的**，里面完全可能有 <script>。所以这里不用 innerHTML
   塞正文：解析成 token 之后一律 appendChild + textContent 建节点。
   `[[名字]]` 单独成 token，因为它是真正可点的东西（而且指不存在的名字时
   要画成"还没写"而不是一个死链）。支持：`#`~`###`、无序/有序列表、
   `**粗体**`、行内代码、代码块、段落、`> 引用`、`---`。
   刻意不做完整 markdown —— 目标只是"能读"，语法边界越少越不容易出错。 */

function inlineTokens(text) {
  const out = [];
  const src = String(text || '');
  // `![[raw/...]]` 是图片嵌入，必须排在普通 `[[...]]` 前面匹配。
  const re = /!\[\[([^\[\]\n]{1,200})\]\]|\[\[([^\[\]\n]{1,200})\]\]|`([^`\n]+)`|\*\*([^*\n]+)\*\*/g;
  let last = 0;
  let match;
  while ((match = re.exec(src)) !== null) {
    if (match.index > last) out.push({ t: 'text', v: src.slice(last, match.index) });
    if (match[1] !== undefined) out.push({ t: 'embed', v: match[1].trim() });
    else if (match[2] !== undefined) out.push({ t: 'link', v: match[2].trim() });
    else if (match[3] !== undefined) out.push({ t: 'code', v: match[3] });
    else out.push({ t: 'bold', v: match[4] });
    last = re.lastIndex;
  }
  if (last < src.length) out.push({ t: 'text', v: src.slice(last) });
  return out;
}

function mdBlocks(body) {
  const lines = String(body || '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let para = [];
  let list = null;          // { ordered, items: string[] }
  let fence = null;         // { lines: string[] }

  const flushPara = () => {
    if (para.length) { out.push({ t: 'p', text: para.join(' ') }); para = []; }
  };
  const flushList = () => {
    if (list) { out.push({ t: 'list', ordered: list.ordered, items: list.items }); list = null; }
  };
  const flush = () => { flushPara(); flushList(); };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    const fenceMatch = /^\s*(```|~~~)/.exec(line);
    if (fence) {
      if (fenceMatch) { out.push({ t: 'code', text: fence.lines.join('\n') }); fence = null; }
      else fence.lines.push(raw);
      continue;
    }
    if (fenceMatch) { flush(); fence = { lines: [] }; continue; }
    if (!line.trim()) { flush(); continue; }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      out.push({ t: 'h', level: heading[1].length, text: heading[2].trim() });
      continue;
    }
    if (/^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); out.push({ t: 'hr' }); continue; }
    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (quote) { flush(); out.push({ t: 'quote', text: quote[1] }); continue; }
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || ordered) {
      flushPara();
      const kind = bullet ? false : true;
      if (!list || list.ordered !== kind) { flushList(); list = { ordered: kind, items: [] }; }
      list.items.push((bullet ? bullet[1] : ordered[1]).trim());
      continue;
    }
    // 列表/段落里的换行：**留着**（一个 item 里手写的两行就是两行），
    // 只有真正相邻的两行才拼成一段 —— 与 markdown 的"软换行"一致。
    if (list) list.items[list.items.length - 1] += '\n' + line.trim();
    else para.push(line.trim());
  }
  if (fence) out.push({ t: 'code', text: fence.lines.join('\n') });
  flush();
  return out;
}

/* 行内渲染。**加粗里面的东西要再解析一遍**：模型特别爱写 `**[[某个档案]]**`，
   如果 bold 直接当纯文本塞进 <strong>，那条双链就永远渲染不出来（方括号原样
   显示在页面上）—— 而同一段里不加粗的 `[[X]]` 又是好的，所以看起来"有时行有时不行"。

   递归的安全性靠一条不变量：**只拿 token 的内容去递归，而内容严格比原文短**
   （判空那条 `token.v.length < text.length` 是它的守卫）。所以就算以后改了正则
   （比如允许内容里再出现 `*`），也不会在这里转不出来。
   `code` 刻意**不递归**：markdown 的行内代码语义就是"原样显示"，
   `` `[[X]]` `` 必须还是字面量。 */
/* 原生 raw 素材的 URL（ADR-104）。路径本身由后端 `read_raw` 再校验一次，
   前端只做编码 —— 绝不能把查询参数当成"已经校验过了"。 */
function vaultRawUrl(path) {
  return `/api/vault/raw?path=${encodeURIComponent(String(path || ''))}`;
}

function renderMdInline(host, text) {
  const src = String(text === undefined || text === null ? '' : text);
  for (const token of inlineTokens(src)) {
    if (token.t === 'text') { host.appendChild(document.createTextNode(token.v)); continue; }
    if (token.t === 'code') { host.appendChild(el('code', 'ic', token.v)); continue; }
    if (token.t === 'embed') {
      // 只接受 raw/ 下的图片嵌入；别的 `![[...]]` 按文字显示，不猜。
      if (/^raw\/[^/]+\/.+\.(png|jpe?g|webp|gif)$/i.test(token.v)) {
        const img = el('img', 'vault-raw-image');
        img.setAttribute('src', vaultRawUrl(token.v));
        img.setAttribute('alt', token.v.split('/').pop() || 'image');
        img.title = token.v;
        img.onclick = () => openLightbox(vaultRawUrl(token.v), token.v.split('/').pop() || '');
        host.appendChild(img);
      } else {
        host.appendChild(document.createTextNode(`![[${token.v}]]`));
      }
      continue;
    }
    if (token.t === 'bold') {
      const strong = el('strong', '');
      if (token.v.length < src.length) renderMdInline(strong, token.v);
      else strong.textContent = token.v;      // 兜底：真的没变短就只当文本，别递归
      host.appendChild(strong);
      continue;
    }
    // `[[名字]]` / `[[path|别名]]`：后端已经告诉我们目标映射到哪个文件了
    const target = token.v.split('|', 1)[0].trim();
    const label = token.v.includes('|')
      ? token.v.slice(token.v.indexOf('|') + 1).trim() || target
      : target;
    const path = (state.vaultFileLinks || {})[target] || '';
    if (path) {
      const link = el('button', 'vault-link', label);
      link.type = 'button';
      link.title = t("vault.open_link", { name: path });
      link.onclick = () => openVaultFile(path, { canonical: path });
      host.appendChild(link);
    } else {
      const missing = el('span', 'vault-link missing', label);
      missing.title = t("vault.not_written");
      // 「还没写」那一段文字**本身就是入口**：点它就问一句"要建一份叫《X》的吗？"
      // 带路径的目标（`sections/...`）不在这里建：它要么是生成物，要么是拼错了。
      host.appendChild(
        target.includes('/') ? missing : missingLinkCreateWrap(target, missing),
      );
    }
  }
}

/* 「这份档案还没写」→「那现在就建一份」。

   做法是把那条虚线的**文字本身**包进一个透明 <button> 里：看着还是原来那条
   "还没写"（虚线、灰字都留在里面的 span 上），点它却直接进新建态，
   名字与标题都预填成这个名字 —— 用户想要的"手动创建一个指向别处的链接"
   到这里才闭环。

   为什么不把那个 span 直接换成按钮：既有自检正是按"missing 是 span、
   `onclick` 为 null、文字就是这个名字"来盯这条语义的（它盯的是"界面上没有
   假装能读一份不存在的档案"）。包一层两边都成立：可点的是整段虚线文字，
   里面那个 span 一个字节都没变。 */
function missingLinkCreateWrap(name, inner) {
  const button = el('button', 'vault-link-create');
  button.type = 'button';
  button.title = t("vault.missing_link_create", { name: name });
  button.onclick = () => askCreateMissingLink(name);
  button.appendChild(inner);
  return button;
}

function askCreateMissingLink(name) {
  const target = String(name || '').trim();
  if (!target) return;
  // 与"编辑态里换档案"同一套：自检环境没有 confirm 就当同意
  const ask = typeof globalThis.confirm === 'function' ? globalThis.confirm : () => true;
  if (!ask(t("vault.missing_link_ask", { name: target }))) return;
  startVaultCreate(target);
}

function renderVaultBody(host, body) {
  clearNode(host);
  for (const block of mdBlocks(body)) {
    if (block.t === 'h') {
      const node = el(`h${Math.min(3, Math.max(1, block.level))}`, 'vault-h');
      renderMdInline(node, block.text);
      host.appendChild(node);
    } else if (block.t === 'p') {
      const node = el('p', 'vault-p');
      renderMdInline(node, block.text);
      host.appendChild(node);
    } else if (block.t === 'list') {
      const node = el(block.ordered ? 'ol' : 'ul', 'vault-list');
      for (const item of block.items) {
        const li = document.createElement('li');
        const parts = String(item).split('\n');
        parts.forEach((part, index) => {
          if (index) li.appendChild(document.createElement('br'));
          renderMdInline(li, part);
        });
        node.appendChild(li);
      }
      host.appendChild(node);
    } else if (block.t === 'quote') {
      const node = el('blockquote', 'vault-quote');
      renderMdInline(node, block.text);
      host.appendChild(node);
    } else if (block.t === 'code') {
      host.appendChild(el('pre', 'codeblock', block.text));
    } else if (block.t === 'hr') {
      host.appendChild(el('hr', 'vault-hr'));
    }
  }
  if (!host.firstChild) host.appendChild(el('div', 'session-sub', t("vault.no_body")));
}

/* 后端给的 links 里 path 可能是**空串**（`[[某个还不存在的名字]]`）。
   空串时不做成可点链接，说一句"还没写"就够了。 */
function neighborChip(entry, fallbackTitle) {
  const path = entry && entry.path ? String(entry.path) : '';
  const title = String((entry && (entry.title || entry.name)) || fallbackTitle || path || '');
  if (!path) {
    const miss = el('span', 'vault-chip missing', title);
    miss.title = t("vault.not_written");
    return miss;
  }
  const chip = el('button', 'vault-chip', title);
  chip.type = 'button';
  chip.title = path;
  chip.onclick = () => openVaultFile(path, { canonical: path });
  return chip;
}

function neighborGroup(title, entries, fallbackTitle) {
  const rows = (entries || []).filter((entry) => entry);
  if (!rows.length) return null;       // 空的整块不显示
  const group = el('div', 'vault-nb');
  group.appendChild(el('div', 'vnb-title', title));
  const row = el('div', 'vnb-row');
  for (const entry of rows) row.appendChild(neighborChip(entry, fallbackTitle));
  group.appendChild(row);
  return group;
}

/* ── (a) 待整理状态 + 目录树/查看器 + 结构校验 ───────────── */

/* 树的展开状态：把上一次的状态里**现在仍然存在**的目录挑出来，其余走默认。
   这样"重新加载 vault"之后用户折叠过的目录还是折叠的，而整理新写出来的目录
   按默认（展开）出现，不用用户自己去找。
   键用**目录路径**（`sections/entities`）而不是目录名：同名目录在不同层级
   是两份东西，拿名字当键会让点开一个把另一个也打开。 */
function aliveTreeState(nodes) {
  const alive = {};
  const walk = (list, prefix) => {
    for (const node of list || []) {
      if (!node || node.type !== 'dir') continue;
      const name = String(node.name || '');
      const path = prefix ? `${prefix}/${name}` : name;
      if (path in state.vaultTreeOpen) alive[path] = state.vaultTreeOpen[path];
      walk(node.children, path);
    }
  };
  walk(nodes, '');
  return alive;
}

function isDirOpen(path) {
  return state.vaultTreeOpen[path] !== false;   // 没记过 = 默认展开
}

function toggleTreeDir(path) {
  state.vaultTreeOpen[path] = !isDirOpen(path);
  renderVaultTree();
}

function treeFileNode(file) {
  const path = String(file.path || '');
  const row = el('div', 'vault-node vault-node-file');
  if (state.vaultFile && state.vaultFile.canonical === path) row.classList.add('active');
  const kind = file.kind ? el('span', 'chip', String(file.kind)) : el('span', 'chip', t("vault.kind.file"));
  const title = el('span', 'vn-title', String(file.title || path));
  title.title = path;
  const summary = file.summary ? el('span', 'vn-sum', String(file.summary)) : null;
  if (summary) summary.title = String(file.summary);
  row.appendChild(kind);
  row.appendChild(title);
  if (file.is_readme) row.appendChild(el('span', 'chip', t("vault.kind.index")));
  if (summary) row.appendChild(summary);
  row.onclick = () => openVaultFile(path, { canonical: path });
  return row;
}

function treeDirNode(dir, prefix) {
  const name = String(dir.name || '');
  const path = prefix ? `${prefix}/${name}` : name;
  const open = isDirOpen(path);
  const box = el('div', 'vault-node-dir');
  const head = el('div', 'vault-node vault-node-dirhead');
  head.appendChild(el('span', 'vn-caret', open ? '▾' : '▸'));
  head.appendChild(el('span', 'vn-title', name));
  head.appendChild(el('span', 'chip', String(Number(dir.files || 0))));
  head.title = t("vault.dir_label", { name: path, files: Number(dir.files || 0) });
  head.onclick = () => toggleTreeDir(path);
  box.appendChild(head);
  if (open) {
    const kids = el('div', 'vault-kids');
    for (const child of dir.children || []) {
      if (!child) continue;
      kids.appendChild(child.type === 'dir' ? treeDirNode(child, path) : treeFileNode(child));
    }
    if (!kids.firstChild) kids.appendChild(el('div', 'session-sub', t("vault.empty_dir")));
    box.appendChild(kids);
  }
  return box;
}

function renderVaultTree() {
  const host = $('vault-tree');
  if (!host) return;
  // 树是整棵重建的（点击 = 重建一次），所以自己把滚动位置接回去：
  // 目录几十条时，点一下展开就跳回顶部会让人找不到刚才看的那一层。
  const scrolled = host.scrollTop;
  clearNode(host);
  const nodes = ((state.vault || {}).tree) || [];
  if (!nodes.length) {
    host.appendChild(el('div', 'session-sub', t("vault.tree_empty")));
    host.scrollTop = 0;
    return;
  }
  for (const node of nodes) {
    if (!node) continue;
    host.appendChild(node.type === 'dir' ? treeDirNode(node, '') : treeFileNode(node));
  }
  host.scrollTop = scrolled;
}

/* 一次点击先清空旧内容：从 120 字的档案点到一份 20 万字的档案时，
   页面必须等新内容，而不是把旧正文留在屏幕上让人以为点错了。

   在编辑态里点别的档案/双链时**先问一句**：编辑器里的字还在 state 里，
   但用户看不见它们了，不问就换等于把没保存的改动藏起来。 */
function openVaultFile(path, options) {
  const opts = options || {};
  if (state.vaultEdit && state.vaultEdit.dirty) {
    // 自检环境里没有 confirm（DOM stub 只实现 app.js 用到的 API），没有就当"同意"
    const ask = typeof globalThis.confirm === 'function' ? globalThis.confirm : () => true;
    if (!ask(t("vault.unsaved_open") + (state.vaultEdit.draft.title || t("vault.unsaved_name")) + t("vault.unsaved_mid")
      + t("vault.unsaved_confirm"))) {
      return Promise.resolve();
    }
  }
  state.vaultEdit = null;
  state.vaultNote = null;
  state.vaultFile = {
    requested: String(path || ''),
    canonical: String(opts.canonical || path || ''),
    data: null,
    error: null,
    loading: true,
  };
  renderVaultTree();
  renderVaultViewer();
  return loadVaultFile(state.vaultFile.requested);
}

async function loadVaultFile(path) {
  try {
    // path 里有 `/` 和中文，必须编码；后端也按"裸名字"补 entities/ 与 sections/
    const data = await getJSON(`/api/vault/file?path=${encodeURIComponent(path)}`);
    if (!state.vaultFile || state.vaultFile.requested !== String(path)) return;   // 已经点了别的
    state.vaultFile = {
      requested: String(path),
      // 后端回的是真实路径（裸名字会补成 entities/x.md）：高亮和标题都用它
      canonical: String(data.path || path),
      data,
      error: null,
      loading: false,
    };
    state.vaultFileLoaded = true;
  } catch (err) {
    if (!state.vaultFile || state.vaultFile.requested !== String(path)) return;
    // 400 是"这个路径不对"、404 是"没有这个档案"：两种都把后端那句话原样给人看
    // （它会说清是路径非法还是文件不存在），不吞成一句"加载失败"。
    state.vaultFile.loading = false;
    state.vaultFile.error = String((err && err.message) || err);
  }
  renderVaultTree();
  renderVaultViewer();
}

/* 保存/删除成功之后那一句。它是**最新的结构校验结果**的载体：档案变了但
   README 与双链不会自己跟着变，所以"保存成功"之后最该看到的是校验说了什么。 */
function vaultNoteRow(note) {
  const level = note.level || 'ok';
  const row = el('div', 'vault-note ' + level);
  row.appendChild(el('div', 'vn-text', String(note.text || '')));
  const problems = ((note.check || {}).problems) || [];
  if (problems.length) {
    row.appendChild(el('div', 'vn-sub', t("vault.check_problems", { n: problems.length })));
    const list = el('ul', 'vn-list');
    for (const item of problems.slice(0, 5)) list.appendChild(el('li', '', String(item)));
    if (problems.length > 5) {
      list.appendChild(el('li', '', t("vault.check_more", { n: problems.length - 5 })));
    }
    row.appendChild(list);
  }
  return row;
}

/* 查看器头部右下角那三个动作。**README 也允许编辑**（要清空它就重写内容），
   但**不给删除按钮** —— 后端会拒（"README 是索引，不能删"），
   在界面上放一个按了必然失败的按钮是在骗人。 */
function viewerActions(data) {
  const row = el('div', 'vault-view-actions');
  if (data.editable === false) return row;   // 生成物 / 观察层 / 日志：没有编辑按钮
  const edit = el('button', 'mini', t("action.edit"));
  edit.type = 'button';
  edit.title = t("vault.edit_title");
  edit.onclick = () => startVaultEdit();
  row.appendChild(edit);
  if (!data.is_readme) {
    const del = el('button', 'mini danger', t("action.delete"));
    del.type = 'button';
    del.title = t("vault.delete_title");
    del.onclick = () => askDeleteVaultFile(data);
    row.appendChild(del);
  }
  return row;
}

/* 操作条：左边一句"这份档案是从哪来的、能不能改"，右边是动作。
   顺序上先说话再放按钮，读起来才知道那个按钮是干什么的。 */
function vaultActionRow(data) {
  const row = el('div', 'vault-actions');
  const info = el('div', 'vault-actions-info');
  const note = data.is_readme
    ? (data.editable === false
      ? t("vault.action.generated_note")
      : t("vault.action.index_note"))
    : (data.is_generated
      ? t("vault.action.generated_note")
      : (data.editable === false ? t("vault.action.readonly_note") : t("vault.action.file_note")));
  info.appendChild(el('span', '', note));
  row.appendChild(info);
  row.appendChild(viewerActions(data));
  return row;
}

function renderVaultViewer() {
  const host = $('vault-viewer');
  if (!host) return;
  // 编辑态整个接管查看器（左树仍在，可以在别的档案之间跳；跳之前会问一句）
  if (state.vaultEdit) { renderVaultEditor(host); return; }
  const switched = host.dataset ? host.dataset.vaultPath !== (state.vaultFile || {}).canonical : false;
  clearNode(host);
  if (host.dataset) host.dataset.vaultPath = (state.vaultFile || {}).canonical || '';
  // 换了一份档案就从头开始看（否则读长档案读到一半再点短档案，
  // 短档案会"打开就是空的"——其实是滚动条还停在底部）。
  if (switched) host.scrollTop = 0;
  const file = state.vaultFile;
  if (!file) {
    // 那句原文是既有自检的断言目标，**不改**。
    // 但结果提示（"已删除 …，其中 N 份的双链变成了还没写"）要留着 ——
    // 删除之后正好就是"什么都没选中"，那句话不该跟着一起消失。
    if (state.vaultNote) host.appendChild(vaultNoteRow(state.vaultNote));
    host.appendChild(el('div', 'vault-empty', t("vault.viewer_empty")));
    return;
  }
  if (file.error) {
    host.appendChild(el('div', 'vault-error', file.error));
    return;
  }
  const data = file.data;
  if (!data) {
    host.appendChild(el('div', 'vault-empty', t("vault.loading")));
    return;
  }

  // `state.vaultFileLinks` 是 `[[名字]] → 路径` 的映射，供正文里的双链查表
  const links = {};
  for (const link of data.links || []) {
    if (link && link.name && link.path) links[String(link.name)] = String(link.path);
  }
  state.vaultFileLinks = links;

  const head = el('div', 'vault-view-head');
  head.appendChild(el('div', 'vault-view-path', String(data.path || file.canonical || '')));
  head.appendChild(el('div', 'vault-view-title', String(data.title || '')));
  if (data.is_readme) {
    head.appendChild(el('div', 'vault-view-note',
      data.editable === false ? t("vault.action.generated_note") : t("vault.index_note")));
  }
  host.appendChild(head);
  host.appendChild(vaultActionRow(data));
  if (state.vaultNote) host.appendChild(vaultNoteRow(state.vaultNote));

  const attrRows = [
    attrRow('name', data.name),
    attrRow('kind', data.kind),
    attrRow('relation', data.relation),
    attrRow('status', data.status),
    attrRow('created', data.created),
    // 原生用 last_updated；旧档案只有 updated —— 标签跟着实际存在的那个走。
    attrRow(
      data.last_updated ? 'last_updated' : 'updated',
      data.last_updated || data.updated,
    ),
    attrRow('place', data.place),
    attrRow('tags', data.tags || []),
    attrRow('aliases', data.aliases || []),
    attrRow('sources', data.sources || []),
  ].filter(Boolean);
  for (const [key, value] of Object.entries(data.extra || {})) {
    const row = extraRow(key, value);
    if (row) attrRows.push(row);
  }
  if (attrRows.length) {
    const attrs = el('div', 'vault-attrs');
    for (const row of attrRows) attrs.appendChild(row);
    host.appendChild(attrs);
  }

  const total = Number(data.total_chars || 0);
  if (data.truncated) {
    host.appendChild(el('div', 'vault-trunc',
      t("vault.truncated", { shown: fmtChars(data.chars), total: fmtChars(total) })));
  }
  const warnings = data.warnings || [];
  if (warnings.length) {
    const box = el('div', 'vault-warn');
    box.appendChild(el('div', 'vw-title', t("vault.warnings", { n: warnings.length })));
    const list = document.createElement('ul');
    for (const item of warnings) list.appendChild(el('li', '', String(item)));
    box.appendChild(list);
    host.appendChild(box);
  }

  const body = el('div', 'vault-body');
  renderVaultBody(body, data.body || '');
  host.appendChild(body);

  const groups = [
    neighborGroup(t("vault.links_out"), data.links, ''),
    neighborGroup(t("vault.links_in"), data.backlinks, ''),
    neighborGroup(t("vault.links_shared"), data.related, ''),
  ].filter(Boolean);
  for (const group of groups) host.appendChild(group);

  const foot = el('div', 'vault-view-foot');
  foot.textContent = t("vault.chars_total", { chars: fmtChars(total) }) + (data.truncated ? '' : t("vault.shown_full"));
  host.appendChild(foot);
}

/* ── 新建 / 编辑 / 删除（对标 Memory space：档案可以在这里直接改）──

   安全红线与正文渲染一样：**档案正文是模型写的，也是用户写的**，
   所以编辑器里一个字符串都不拼进 innerHTML —— 输入框是 <textarea>/<input>，
   提示文字一律走 el()（textContent）。这样"正文里塞 <script>"最多是一段文字。

   流程上只有一条规矩：**加载时拿到的 hash 要原样带回去**（expect_hash）。
   后端拿它判断"你看的还是不是现在磁盘上那份"，对不上就 409 并把最新内容
   一起回来 —— 那时**绝不能把用户敲进去的字丢掉**，所以草稿一直在 state 里，
   由用户自己选"看最新的（放弃我的）"还是"仍然覆盖（用新 hash 再存一次）"。 */

const VAULT_DIRS = ['entities', 'sections'];
const VAULT_NAME_MAX = 40;   // 与后端 settings.vault_name_max_chars 的默认值一致

/* 交付给后端的 path：目录 + 名字 + .md。
   用户只填名字，扩展名由这里补 —— 让人手打 `.md` 是纯粹的坑。
   名字里带了 `.md` 就不再补（就当用户写全了）。 */
function vaultDraftPath(draft) {
  const dir = VAULT_DIRS.includes(draft.dir) ? draft.dir : VAULT_DIRS[0];
  const name = String(draft.name || '').trim();
  if (!name) return '';
  return `${dir}/${/\.md$/i.test(name) ? name : `${name}.md`}`;
}

/* 从一份完整的文件视图建草稿。tags/aliases 用**逗号分隔的字符串**，
   与「设置 → 接口」里"模型（逗号分隔）"那一栏手感一致。

   正文里的 CRLF 先归成 LF：`<textarea>` 的 value 在浏览器里本来就会把换行
   统一成 LF（HTML 规范如此），所以如果不在这里先归一次，那么"打开一份 CRLF
   的档案、什么都不改就保存"会把它整个文件的行尾换掉 —— 看起来像全文都变了。
   归一次只有一个效果：读进来什么样、写回去还什么样。 */
function vaultDraftFromView(view) {
  return {
    path: String(view.path || ''),
    dir: String(view.path || '').split('/')[0] || 'entities',
    name: String(view.path || '').split('/').pop() || '',
    title: String(view.title || ''),
    kind: String(view.kind || ''),
    relation: String(view.relation || ''),
    status: String(view.status || ''),
    place: String(view.place || ''),
    tags: (view.tags || []).join(', '),
    aliases: (view.aliases || []).join(', '),
    body: String(view.body || '').replace(/\r\n?/g, '\n'),
  };
}

function vaultDraftFromCreate(dir) {
  return {
    path: '', dir, name: '', title: '', kind: '', relation: '', status: '', place: '',
    tags: '', aliases: '', body: '',
  };
}

function splitVaultList(text) {
  return String(text || '').split(/[,，]/).map((part) => part.trim()).filter(Boolean);
}

function vaultEditStatus() {
  const edit = state.vaultEdit;
  if (!edit) return '';
  return edit.mode === 'create'
    ? t("vault.edit_status_new")
    : t("vault.edit_status_editing", { path: edit.draft.path });
}

/* 左边目录树顶上的「新建档案」。它属于树那一栏，所以只建一次。 */
function renderTreeNewButton(pane) {
  const button = el('button', 'mini', t("vault.new_file"));
  button.type = 'button';
  button.title = t("vault.new_file_title");
  button.onclick = () => startVaultCreate();
  pane.appendChild(button);
}

/* 一个"标签 + 输入框"的字段。输入框只有两种：单行 <input> 与多行 <textarea>。
   `root` 是这次的编辑器容器：同 id 的既有节点会被复用（见 reuseOrCreate），
   所以"页面上那个 #vault-kind"与监听输入的那个永远是同一个节点。

   `keyName` 是**写进 frontmatter 的英文键名**。给人看的标签全是中文，键名放到
   输入框的 title 提示里 —— 这样界面是中文的，而"文件里到底写的是什么键"仍然
   查得到（模型与文件格式靠的就是这些英文键，不能跟着文案改）。 */
function vaultField(root, id, label, value, options) {
  const opts = options || {};
  const box = el('div', 'vault-field');
  const multiline = !!opts.multiline;
  const caption = el('label', 'vault-field-label', label);
  caption.setAttribute('for', id);
  box.appendChild(caption);
  const input = reuseOrCreate(root, id, multiline ? 'textarea' : 'input');
  input.id = id;
  input.className = '';
  input.value = String(value === undefined || value === null ? '' : value);
  input.placeholder = opts.placeholder ? String(opts.placeholder) : '';
  input.title = opts.keyName ? t("vault.field_key_hint", { key: opts.keyName }) : '';
  input.disabled = !!opts.disabled;
  if (opts.maxlength) {
    input.maxLength = Number(opts.maxlength);
    input.setAttribute('maxlength', String(opts.maxlength));
  }
  if (multiline) input.rows = String(opts.rows || 3);
  box.appendChild(input);
  if (opts.hint) box.appendChild(el('div', 'vault-field-hint', opts.hint));
  return box;
}

/* 编辑器里那一行就地提示。level 决定颜色；'warn' 用来放"被改过"这种要么
   看清要么覆盖的情形，'err' 放后端原话（路径不合法、没有这份档案…）。 */
function vaultEditNotice(text, level) {
  const row = el('div', 'vault-notice ' + (level || 'err'));
  row.appendChild(el('div', 'vn-text', String(text || '')));
  const edit = state.vaultEdit;
  const conflict = edit && edit.conflict;
  if (conflict) {
    row.appendChild(el('div', 'vn-text',
      t("vault.conflict_lead") + fmtChars((conflict.view || {}).total_chars) + t("vault.conflict_tail")));
    const actions = el('div', 'vault-notice-actions');
    const fresh = el('button', 'mini', t("vault.conflict_take"));
    fresh.type = 'button';
    fresh.onclick = () => takeFreshVaultContent();
    const force = el('button', 'mini danger', t("vault.conflict_overwrite"));
    force.type = 'button';
    force.onclick = () => forceOverwriteVaultFile();
    actions.appendChild(fresh);
    actions.appendChild(force);
    row.appendChild(actions);
  }
  const exists = edit && edit.exists;
  if (exists) {
    const actions = el('div', 'vault-notice-actions');
    const open = el('button', 'mini', t("vault.open_link", { name: exists.path || '' }));
    open.type = 'button';
    open.onclick = () => {
      state.vaultEdit = null;
      state.vaultNote = null;
      openVaultFile(exists.path, { canonical: exists.path });
    };
    actions.appendChild(open);
    row.appendChild(actions);
  }
  return row;
}

/* 编辑器旁边那两句必须被读到的话。它们是"这个功能会怎么坑你"的说明书，
   所以放在编辑器里，而不是藏进某个折叠块。 */
function vaultEditHints() {
  const box = el('div', 'vault-hints');
  const lines = [
    t("vault.edit_hint_1")
      + t("vault.edit_hint_2"),
    t("vault.edit_hint_3")
      + t("vault.edit_hint_4"),
  ];
  for (const line of lines) box.appendChild(el('div', 'vault-hint', line));
  return box;
}

/* ── 「插入链接」─────────────────────────────────────────

   用户的原话是"实在不行加一个『插入链接』功能"。只往正文里塞两个方括号等于
   把语法丢给人记，所以这里**真的列出 vault 里现有的档案**，点一下就插到光标处。

   链接名用 `title` 而不是文件名：服务端解析双链时匹配的是标题 / 别名 / 文件名
   （`_name_to_path`），标题是链接真正指向的东西，而文件名是可以改的。
   列表里两个都显示 —— 插入的是标题，但人常常是按文件名找东西的。

   手打一个名字那个输入框是**必须**的：想指向一份**还不存在**的档案正是正常用法
   （先写链接、以后再补）。只给列表等于把这条路堵死。 */

/* 从已经拿到的目录树里挑出可链接的档案：entities/ 与 sections/ 下的文件，
   含更深的子目录。树还没拿到就返回空数组 —— 那就只剩"手打一个名字"这条路，
   界面里会明确说出来，而不是给一个空白面板。 */
function vaultLinkCandidates() {
  const out = [];
  const walk = (nodes, prefix) => {
    for (const node of nodes || []) {
      if (!node) continue;
      if (node.type === 'dir') {
        const name = String(node.name || '');
        walk(node.children, prefix ? `${prefix}/${name}` : name);
        continue;
      }
      const path = String(node.path || '');
      if (!path) continue;
      if (!(path.startsWith('entities/') || path.startsWith('sections/'))) continue;
      const fileName = path.split('/').pop() || path;
      const title = String(node.title || '').trim() || fileName.replace(/\.md$/i, '');
      out.push({ path, title, fileName });
    }
  };
  walk((state.vault || {}).tree, '');
  return out;
}

/* 正文框里的光标位置。编辑器重画会把 textarea 整个换掉，所以**任何**会触发
   重画的动作（点「插入链接」、点面板里的一份档案）都必须在重画**之前**把它记下来。

   自检用的 DOM stub 没有 selectionStart：读到非数字就当"没有光标信息"，
   插入时追加到末尾（见 insertVaultLinkToken）。 */
function rememberVaultCaret(edit, area) {
  if (!edit || !area) return;
  const start = Number(area.selectionStart);
  if (!Number.isFinite(start)) return;
  const end = Number(area.selectionEnd);
  edit.caret = { start, end: Number.isFinite(end) ? end : start };
}

/* 焦点与光标交回正文框（找的是**重画之后**那个 textarea）。 */
function focusVaultBody(position) {
  const viewer = $('vault-viewer');
  const area = viewer ? viewer.querySelector('.vault-textarea') : null;
  if (!area) return;
  if (typeof area.focus === 'function') area.focus();
  if (Number.isFinite(position) && typeof area.setSelectionRange === 'function') {
    area.setSelectionRange(position, position);
  }
}

/* 把 `[[名字]]` 插进正文：有光标信息就插到光标处（选中了一段就替换掉那一段），
   没有就追加到末尾。插入之后**把焦点与光标交回 textarea** —— 不这么做的话，
   面板收起、焦点留在按钮上，人会以为根本没插进去。 */
function insertVaultLinkToken(rawName) {
  const edit = state.vaultEdit;
  if (!edit) return;
  const name = String(rawName === undefined || rawName === null ? '' : rawName).trim();
  if (!name) { toast(t("vault.link_name_required")); return; }
  if (name.length > 120 || /[\[\]\n]/.test(name)) {
    toast(t("vault.link_name_invalid"));
    return;
  }
  const token = `[[${name}]]`;
  const body = String(edit.draft.body || '');
  let start = body.length;
  let end = body.length;
  const caret = edit.caret;
  if (caret && Number.isFinite(caret.start)) {
    start = Math.min(Math.max(0, caret.start), body.length);
    end = Number.isFinite(caret.end) ? Math.min(Math.max(start, caret.end), body.length) : start;
  }
  edit.draft.body = body.slice(0, start) + token + body.slice(end);
  edit.dirty = true;
  edit.caret = { start: start + token.length, end: start + token.length };
  edit.linkPicker = false;        // 插完就收起面板
  edit.linkName = '';
  renderVaultViewer();            // 正文框按新草稿重画
  focusVaultBody(edit.caret.start);
}

/* 点开「插入链接」之后那块面板：上面手打名字，下面列现有档案。 */
function vaultLinkPicker(host, edit) {
  const box = el('div', 'vault-link-insert');
  box.appendChild(el('div', 'vli-title',
    t("vault.link_picker_lead")
      + t("vault.link_picker_lead2")));

  const manual = el('div', 'vli-manual');
  const input = reuseOrCreate(host, 'vault-link-name', 'input');
  input.id = 'vault-link-name';
  input.className = 'vli-input';
  input.value = String(edit.linkName || '');
  input.placeholder = t("vault.link_placeholder");
  // 这一格没有 <label>（旁边那句话就是它的说明），给读屏软件补一个名字
  input.setAttribute('aria-label', t("vault.link_name_title"));
  input.oninput = () => { edit.linkName = String(input.value || ''); };
  input.onkeydown = (event) => {
    if (event && (event.key === 'Enter' || event.keyCode === 13)) insertVaultLinkToken(input.value);
  };
  manual.appendChild(input);
  const go = el('button', 'mini', t("vault.link_insert"));
  go.id = 'vault-link-do';
  go.type = 'button';
  go.title = t("vault.link_insert_title");
  go.onclick = () => insertVaultLinkToken(input.value);
  manual.appendChild(go);
  const close = el('button', 'mini', t("vault.collapse"));
  close.id = 'vault-link-close';
  close.type = 'button';
  close.onclick = () => { edit.linkPicker = false; renderVaultViewer(); focusVaultBody(); };
  manual.appendChild(close);
  box.appendChild(manual);

  const items = vaultLinkCandidates();
  const list = el('div', 'vli-list');
  if (!items.length) {
    list.appendChild(el('div', 'session-sub', t("vault.link_picker_empty")));
  }
  for (const item of items) {
    const row = el('button', 'mini vli-item', item.title);
    row.type = 'button';
    row.title = t("vault.link_insert_label", { name: item.title, hint: item.path });
    // 文件名与标题不一致时两个都显示：插入的是标题，但人常按文件名找东西
    row.appendChild(el('span', 'vli-path', item.path));
    row.onclick = () => insertVaultLinkToken(item.title);
    list.appendChild(row);
  }
  box.appendChild(list);
  return box;
}

/* 正文框上面那一条工具行（现在只有「插入链接」）。
   面板与按钮都在编辑器容器里，所以点一下重画整个编辑器是安全的：
   草稿（含刚记下的光标位置）都在 state 里，重画不会丢字。 */
function vaultBodyTools(host, edit, area) {
  const row = el('div', 'vault-body-tools');
  const button = el('button', 'mini vault-insert-link', t("vault.insert_link"));
  button.id = 'vault-insert-link';
  button.type = 'button';
  button.title = t("vault.insert_link_title");
  button.onclick = () => {
    // **先**记光标：这一步之后 textarea 会被重画换掉
    rememberVaultCaret(edit, area);
    edit.linkPicker = !edit.linkPicker;
    renderVaultViewer();
    if (edit.linkPicker) focusInEditor('#vault-link-name');
  };
  row.appendChild(button);
  if (edit.linkPicker) row.appendChild(vaultLinkPicker(host, edit));
  return row;
}

function vaultEditorSaveRow() {
  const row = el('div', 'vault-edit-actions');
  const save = el('button', 'btn-primary', state.vaultEdit.busy ? t("action.saving") : t("action.save"));
  save.id = 'vault-save';
  save.type = 'button';
  save.disabled = !!state.vaultEdit.busy;
  save.onclick = () => saveVaultEdit();
  const cancel = el('button', 'btn-ghost', t("action.cancel"));
  cancel.id = 'vault-cancel';
  cancel.type = 'button';
  cancel.disabled = !!state.vaultEdit.busy;
  cancel.onclick = () => cancelVaultEdit();
  row.appendChild(save);
  row.appendChild(cancel);
  return row;
}

/* 编辑器就画在查看器里（右边那一栏）：左树可以继续点，位置也不跳。

   容器用 $('vault-editor') **取**（而不是每次新建一个）：真 DOM 里两者本来就没区别，
   但"取出来、再挂回去"这一段同时让零构建自检的 DOM stub 把问过的 id 与页面上的
   节点重新对上（stub 没有 id 索引，只记得"谁问过这个 id"）。**必须先 detach 再挂**：
   直接 appendChild 一个已经在 viewer 里的节点会变成环（自己装自己），
   下一次 clearNode 就会沿着环转下去。 */
function renderVaultEditor(viewer) {
  const edit = state.vaultEdit;
  clearNode(viewer);
  let host = $('vault-editor');
  if (!host || String(host.tagName).toUpperCase() !== 'DIV') host = el('div', 'vault-editor');
  if (host.parentNode) host.parentNode.removeChild(host);
  clearNode(host);
  host.id = 'vault-editor';
  host.className = 'vault-editor';
  host.dataset.vaultEditMode = edit.mode;
  viewer.appendChild(host);

  const head = el('div', 'vault-editor-head');
  // 这一行也按 id 复用（见 reuseOrCreate）：每次新建一个的话，
  // "名字 → 路径"实时更新就只会改到那个已经不在页面上的旧节点。
  const pathLine = reuseOrCreate(host, 'vault-edit-path', 'div');
  pathLine.id = 'vault-edit-path';
  pathLine.className = 'vault-view-path';
  // 编辑时用**磁盘上那个路径本身**（可能是更深的旧稿目录，重新拼会拼错）；
  // 新建时才由"目录 + 名字"算出来。
  pathLine.textContent = edit.mode === 'create'
    ? (vaultDraftPath(edit.draft) || t("vault.no_path_yet"))
    : edit.draft.path;
  head.appendChild(pathLine);
  head.appendChild(el('div', 'vault-view-title',
    edit.mode === 'create' ? t("vault.new_file") : t("vault.edit_title_label", { name: edit.draft.title || edit.draft.path })));
  head.appendChild(el('div', 'vault-view-note', vaultEditStatus()));
  host.appendChild(head);

  if (edit.note) host.appendChild(vaultEditNotice(edit.note.text, edit.note.level));

  // 载入时被截断过：正文只载入了前 N 字符，**保存会按这份截断内容写回去**。
  // 这是会丢字的事，所以不放进任何折叠块，就摆在正文输入框上面。
  const view = edit.file && edit.file.data;
  if (view && view.truncated) {
    const warn = el('div', 'vault-warn');
    warn.appendChild(el('div', 'vw-title', t("vault.too_long_editor_1")
      + fmtChars(view.chars) + t("vault.too_long_editor_2") + fmtChars(view.total_chars) + t("vault.too_long_editor_3")));
    warn.appendChild(el('div', '', t("vault.too_long_save_1")
      + t("vault.too_long_save_2")));
    host.appendChild(warn);
  }

  const form = el('div', 'vault-form');
  if (edit.mode === 'create') {
    const dirBox = el('div', 'vault-field');
    const dirLabel = el('label', '', t("vault.field_dir"));
    dirLabel.setAttribute('for', 'vault-dir');
    dirBox.appendChild(dirLabel);
    // 同上：只有真的是 <select> 才复用（stub 会为任何 id 造 div）
    const select = reuseOrCreate(host, 'vault-dir', 'select');
    select.id = 'vault-dir';
    for (const dir of VAULT_DIRS) {
      const option = document.createElement('option');
      option.value = dir;
      option.textContent = dir;
      if (dir === edit.draft.dir) option.selected = true;
      select.appendChild(option);
    }
    // 闭包里**不捕获 select 变量**：读值时以事件目标（此刻真的在页面上的那个
    // select）为准 —— 闭包捕获的可能是上一次渲染留下的、已经脱离文档的节点。
    select.onchange = (event) => {
      const target = (event && event.target) || findElIn(host, 'vault-dir') || select;
      edit.draft.dir = String(target.value || VAULT_DIRS[0]);
      edit.dirty = true;
      renderVaultViewer();   // 那行"名字 → 路径"跟着换目录
    };
    dirBox.appendChild(select);
    form.appendChild(dirBox);
    form.appendChild(vaultField(host, 'vault-name', t("vault.field_name"), edit.draft.name, {
      maxlength: VAULT_NAME_MAX,
      placeholder: t("vault.link_placeholder"),
      hint: t("vault.field_name_hint", { max: VAULT_NAME_MAX })
        + t("vault.field_name_rule"),
    }));
  } else {
    form.appendChild(vaultField(host, 'vault-path', t("vault.field_path"),
      edit.draft.path, { disabled: true }));
    form.appendChild(vaultField(host, 'vault-name', t("vault.field_filename"), edit.draft.name, { disabled: true }));
  }
  form.appendChild(vaultField(host, 'vault-title', t("vault.field_title"), edit.draft.title, {
    maxlength: 120, keyName: 'title', placeholder: t("vault.link_placeholder"),
  }));
  form.appendChild(vaultField(host, 'vault-kind', t("vault.field_kind"), edit.draft.kind, {
    keyName: 'kind', placeholder: t("vault.field_kind_placeholder"),
  }));
  form.appendChild(vaultField(host, 'vault-relation', t("vault.field_relation"), edit.draft.relation, {
    keyName: 'relation', placeholder: t("vault.field_relation_placeholder"),
  }));
  form.appendChild(vaultField(host, 'vault-status', t("vault.field_status"), edit.draft.status, {
    keyName: 'status', placeholder: t("vault.field_status_placeholder"),
  }));
  form.appendChild(vaultField(host, 'vault-place', t("vault.field_place"), edit.draft.place, {
    keyName: 'place', placeholder: t("vault.field_place_placeholder"),
  }));
  form.appendChild(vaultField(host, 'vault-tags', t("vault.field_tags"), edit.draft.tags, {
    keyName: 'tags', placeholder: t("vault.field_tags_placeholder"),
  }));
  form.appendChild(vaultField(host, 'vault-aliases', t("vault.field_aliases"), edit.draft.aliases, {
    keyName: 'aliases', placeholder: t("vault.field_aliases_placeholder"),
  }));
  host.appendChild(form);

  // 正文那一栏：自己一行、等宽的 <textarea>（vaultField 的 multiline 形态）
  const bodyBox = vaultField(host, 'vault-body', t("vault.field_body"), edit.draft.body, {
    multiline: true,
    rows: 14,
  });
  bodyBox.classList.add('vault-field-body');
  // 正文元素直接从这个字段盒里取（id 在外层盒子上，正文本体只带 class）
  const area = bodyBox.querySelector('textarea');
  if (area) area.classList.add('vault-textarea');
  // 「插入链接」那一行摆在正文框**上面**：它是往正文里写东西的工具
  host.appendChild(vaultBodyTools(host, edit, area));
  host.appendChild(bodyBox);

  host.appendChild(vaultEditHints());

  // 输入框 → 草稿：**每次 input 都同步**，所以切走再回来（或 409 之后重画）
  // 拿到的都是用户最新的字，而不是载入时那份。
  const single = {
    'vault-title': 'title', 'vault-kind': 'kind', 'vault-relation': 'relation',
    'vault-status': 'status', 'vault-place': 'place',
    'vault-tags': 'tags', 'vault-aliases': 'aliases',
  };
  for (const [id, key] of Object.entries(single)) {
    const field = findElIn(host, id);
    if (!field) continue;
    // 读值是闭包里那个节点自己的 value —— 它就是页面上这个输入框（见 reuseOrCreate）
    field.oninput = () => { edit.draft[key] = String(field.value || ''); edit.dirty = true; };
  }
  const nameField = findElIn(host, 'vault-name');
  if (nameField && edit.mode === 'create') {
    nameField.oninput = () => {
      edit.draft.name = String(nameField.value || '');
      edit.dirty = true;
      // 用户还没自己写过 title 时跟着名字走（否则新建出来的档案标题都是空的）。
      // 只看 title 还是不是"上一个名字"——用户改过 title 之后就不再跟着动。
      const titleField = findElIn(host, 'vault-title');
      if (titleField && (!edit.draft.title || edit.draft.title === edit.autoTitle)) {
        edit.draft.title = edit.draft.name;
        edit.autoTitle = edit.draft.name;
        titleField.value = edit.draft.name;
      }
      // 只更新依赖名字的那一行：重建整个编辑器会把焦点从名字框挪到正文，
      // 用户每敲一个字就丢一次焦点。title 已经在上面原地更新，无需重画。
      const pathLine = findElIn(host, 'vault-edit-path');
      if (pathLine) pathLine.textContent = vaultDraftPath(edit.draft) || t("vault.no_path_yet");
    };
  }
  const pathField = findElIn(host, 'vault-path');
  // 只读：改名 = 删掉再新建（后端没有"改名"这个动作）。disabled 在建字段时就给了，
  // 这里再按"真的那个 input"补一刀。
  if (pathField && String(pathField.tagName).toUpperCase() === 'INPUT') pathField.disabled = true;
  // 正文 → 草稿：**每次 input 都同步**。顺手把光标位置记下来，
  // 这样点「插入链接」时知道该插到哪儿（见 rememberVaultCaret）。
  area.oninput = () => {
    rememberVaultCaret(edit, area);
    edit.draft.body = String(area.value || '');
    edit.dirty = true;
  };
  // 只是移动光标 / 选一段字也要记（点按钮本身会让正文框失焦）
  area.onclick = () => rememberVaultCaret(edit, area);
  area.onkeyup = () => rememberVaultCaret(edit, area);
  area.onselect = () => rememberVaultCaret(edit, area);

  host.appendChild(vaultEditorSaveRow());
  // 焦点放在正文上（而不是标题）：改档案多半就是改正文
  if (typeof area.focus === 'function') area.focus();
}
/* 按 id 找**已经画出来的**节点，只在这个编辑器的 DOM 子树里找。
   刻意不用 `$()`（= getElementById）：
   * 真 DOM 里两者等价，但这里要找的东西本来就应该在刚建的子树里 ——
     找不到就是没画，而不是"到别处去找一个同 id 的节点"；
   * 零构建自检用的 DOM stub 里 getElementById 是"边用边造"的（它没有真 DOM
     的 id 索引，只能临时造一个空 div 顶上）。用 getElementById 会让自检里的
     代码拿着一个"页面外的空 div"当输入框，把真实的错位藏起来。 */
function findElIn(root, id) {
  return root ? root.querySelector(`#${id}`) : null;
}

/* 复用一个同 id 的既有节点（只有当它是"对的标签"时），否则新建。
   这样"页面上的 #vault-kind"与代码手里的那个永远是同一个节点。 */
function reuseOrCreate(root, id, tag) {
  const found = findElIn(root, id);
  if (found && String(found.tagName).toUpperCase() === tag.toUpperCase()) return found;
  return document.createElement(tag);
}

/* 把光标放到编辑器里的某个控件上（新建时是名字那一格）。
   和 findElIn 一样从查看器里找，不用 $()：见那边的说明。 */
function focusInEditor(selector) {
  const viewer = $('vault-viewer');
  const node = viewer ? viewer.querySelector(selector) : null;
  if (node && typeof node.focus === 'function') node.focus();
}

/* ── 进入 / 退出编辑态 ─────────────────────────────────── */

function startVaultEdit() {
  const file = state.vaultFile;
  const view = file && file.data;
  if (!view) { toast(t("vault.edit_need_open")); return; }
  if (view.truncated) {
    // 只读到一半的档案**不能**拿来编辑：保存会把没读到的部分抹掉。
    toast(t("vault.edit_too_long"));
    state.vaultNote = {
      level: 'warn',
      text: t("vault.edit_too_long_1") + fmtChars(view.chars) + t("vault.edit_too_long_2")
        + t("vault.edit_too_long_3")
        + t("vault.edit_too_long_4"),
    };
    renderVaultViewer();
    return;
  }
  state.vaultNote = null;
  state.vaultEdit = {
    mode: 'update',
    draft: vaultDraftFromView(view),
    hash: String(view.hash || ''),
    file: { requested: file.requested, canonical: file.canonical, data: view },
    note: null,
    conflict: null,
    exists: null,
    dirty: false,
    busy: false,
    linkPicker: false,
    linkName: '',
    caret: null,
  };
  renderVaultViewer();
}

/* 进新建态。`name` 可选：从"'这份档案还没写'→现在就建一份"那条路过来时，
   名字与标题都预填好（名字决定路径，标题决定别处的 `[[双链]]` 指向它）。 */
function startVaultCreate(name) {
  state.vaultNote = null;
  const seed = String(name === undefined || name === null ? '' : name).trim();
  state.vaultEdit = {
    mode: 'create',
    draft: vaultDraftFromCreate(VAULT_DIRS[0]),
    hash: '',
    file: null,
    note: null,
    conflict: null,
    exists: null,
    dirty: false,
    busy: false,
    linkPicker: false,
    linkName: '',
    caret: null,
  };
  if (seed) {
    state.vaultEdit.draft.name = seed;
    state.vaultEdit.draft.title = seed;
    state.vaultEdit.autoTitle = seed;   // 之后改名字时标题仍然跟着走
  }
  // 树上看不到"正在编辑哪一份"（新档案还不存在），所以清掉高亮
  state.vaultFile = null;
  state.vaultFileLinks = {};
  renderVaultTree();
  renderVaultViewer();
  // 新建时先填名字（路径是它算出来的）：renderVaultEditor 会把焦点放在正文上，
  // 所以这里显式把光标挪到名字那一格。
  focusInEditor('#vault-name');
}

function vaultEditModeFor(edit) {
  return edit.mode === 'create' ? 'create' : 'update';
}

function positionAfterEditResult(view) {
  return {
    requested: String(view.path || ''),
    canonical: String(view.path || ''),
    data: view,
    error: null,
    loading: false,
  };
}

/* 取消 = 原样退回去看那一份。编辑新建时"原来那一份"不存在，回到引导态。 */
function cancelVaultEdit() {
  const edit = state.vaultEdit;
  state.vaultEdit = null;
  if (edit && edit.file) {
    state.vaultFile = {
      requested: edit.file.requested,
      canonical: edit.file.canonical,
      data: edit.file.data,
      error: null,
      loading: false,
    };
  } else {
    state.vaultFile = null;
    state.vaultFileLinks = {};
  }
  renderVaultTree();
  renderVaultViewer();
}

/* ── 保存 ─────────────────────────────────────────────── */

/* 最终 POST 出去的那份 payload。字段名与后端契约一一对应：
   mode / path / body / title / kind / relation / status / tags / aliases / expect_hash。
   aliases 也一起发（后端 save_note 本来就收）—— 载入时显示了它，
   不发就等于"用户改了 aliases，一保存却被悄悄回滚成原来的"。 */
function vaultEditPayload(edit) {
  return {
    mode: vaultEditModeFor(edit),
    path: vaultEditModeFor(edit) === 'create' ? vaultDraftPath(edit.draft) : edit.draft.path,
    body: String(edit.draft.body || ''),
    title: String(edit.draft.title || '').trim(),
    kind: String(edit.draft.kind || '').trim(),
    relation: String(edit.draft.relation || '').trim(),
    status: String(edit.draft.status || '').trim(),
    place: String(edit.draft.place || '').trim(),
    tags: splitVaultList(edit.draft.tags),
    aliases: splitVaultList(edit.draft.aliases),
    // 载入时那一份的指纹。后端拿它判断"你看的还是不是现在磁盘上那份"。
    expect_hash: edit.hash,
  };
}

async function saveVaultEdit() {
  const edit = state.vaultEdit;
  if (!edit || edit.busy) return;
  // **被截断的档案不许在这里保存**（只警告是不够的）。
  // 编辑器里只有前 20 万字符，保存等于把没读到的那一截**永久删掉** ——
  // 而整理没有回滚（ADR-065），这个损失不可逆。所以拦住，并把人指到别的做法上。
  const loaded = edit.file && edit.file.data;
  if (loaded && loaded.truncated) {
    edit.note = {
      text: t("vault.save_too_long_1") + fmtChars(loaded.chars)
        + t("vault.too_long_editor_2") + fmtChars(loaded.total_chars) + t("vault.too_long_editor_3")
        + t("vault.save_too_long_2")
        + t("vault.save_too_long_3"),
      level: 'err',
    };
    renderVaultViewer();
    return;
  }
  const payload = vaultEditPayload(edit);
  if (!payload.path) {
    edit.note = { text: t("vault.save_need_name"), level: 'err' };
    renderVaultViewer();
    return;
  }
  edit.busy = true;
  edit.note = null;
  edit.conflict = null;
  edit.exists = null;
  renderVaultViewer();

  let view;
  try {
    view = await requestJSON('/api/vault/file', payload);
  } catch (err) {
    edit.busy = false;
    const body = err.body || {};
    const status = err.status || 0;
    if (status === 409 && body.exists) {
      // 新建撞名：不是"内容被改过"，是"已经有这一份了" —— 该让人去看它
      edit.exists = { path: String(body.path || payload.path) };
      edit.note = { text: String(body.error || err.message), level: 'warn' };
    } else if (status === 409 && body.path) {
      // 内容被改过：后端把**最新那份完整视图**一起给回来了（含新 hash）
      edit.conflict = { view: body, hash: String(body.hash || '') };
      edit.note = { text: String(body.error || err.message), level: 'warn' };
    } else {
      // 400（路径/名字/内容不合法）、404（刚被删）、409（整理正在跑）、500：
      // 一律把后端原话摆出来，编辑态原样保留 —— 用户可以改完再存。
      edit.note = {
        text: String(body.error || err.message || t("vault.save_failed")),
        level: status === 409 ? 'warn' : 'err',
      };
    }
    renderVaultViewer();
    return;
  }

  await refreshAfterVaultWrite(view, view.action === 'created' ? 'created' : 'updated');
}

/* 保存成功之后：刷新目录树 + 结构校验面板，并保持/切到正确的那一份。
   **等重刷完再返回**：调用方（保存按钮 / 自检）要能在拿到控制权时就看到
   "新档案已经在树上"。不等的话，界面上会有一瞬间的不一致，自检也会看到假失败。 */
async function refreshAfterVaultWrite(view, action) {
  const path = String(view.path || '');
  state.vaultEdit = null;
  state.vaultFile = positionAfterEditResult(view);
  state.vaultFileLinks = {};
  state.vaultNote = {
    level: 'ok',
    text: (action === 'created'
      ? t("vault.created", { name: path })
      : t("vault.saved", { name: path }))
      + t("vault.saved_note"),
    check: view.check,
  };
  renderVaultTree();
  renderVaultViewer();
  toast(action === 'created' ? t("vault.created_short") : t("vault.saved_short"));
  await reloadVaultAfterWrite();
}

/* 重刷 vault（目录树 + 结构校验 + 待整理）。查看器是复用的，所以
   刚保存的那一份不会被刷掉（state.vaultFile 还在，没选中就还是没选中）。 */
async function reloadVaultAfterWrite() {
  try {
    await loadVault();
  } catch {
    // 刷新失败不该让人以为"保存也没成功"：正文已经落盘了，这里只是没重读到
    state.vaultNote = {
      level: 'warn',
      text: (state.vaultNote ? state.vaultNote.text : '')
        + t("vault.reload_failed"),
    };
    renderVaultViewer();
  }
}

/* 「仍然覆盖保存」：用 409 带回来的**最新 hash** 再存一次。
   用户敲进去的字一个都不动 —— 这正是这个按钮存在的意义。 */
async function forceOverwriteVaultFile() {
  const edit = state.vaultEdit;
  if (!edit || !edit.conflict) return;
  edit.hash = edit.conflict.hash;
  edit.conflict = null;
  edit.note = { text: t("vault.force_overwriting"), level: 'warn' };
  renderVaultViewer();
  await saveVaultEdit();
}

/* 「看最新内容（放弃我的改动）」：草稿**整个**换成最新那份。 */
function takeFreshVaultContent() {
  const edit = state.vaultEdit;
  if (!edit || !edit.conflict) return;
  const view = edit.conflict.view || {};
  edit.draft = vaultDraftFromView(view);
  edit.hash = String(edit.conflict.hash || view.hash || '');
  edit.conflict = null;
  edit.note = { text: t("vault.fresh_taken"), level: 'warn' };
  edit.dirty = false;
  renderVaultViewer();
}

/* ── 删除 ─────────────────────────────────────────────── */

/* 路径 → 给人看的名字。先问这次载入的那份（它有标题），再问目录树。 */
function vaultNameFor(path, extra) {
  const wanted = String(path || '');
  if (!wanted) return '';
  const sources = [extra, (state.vaultFile || {}).data].filter(Boolean);
  for (const source of sources) {
    for (const item of source.backlinks || []) {
      if (item && String(item.path || '') === wanted) {
        return String(item.title || item.name || wanted);
      }
    }
  }
  const walk = (nodes) => {
    for (const node of nodes || []) {
      if (!node) continue;
      if (node.type === 'dir') {
        const found = walk(node.children);
        if (found) return found;
      } else if (String(node.path || '') === wanted) {
        return String(node.title || wanted);
      }
    }
    return '';
  };
  return walk(((state.vault || {}).tree) || []) || wanted;
}

/* 删除确认。**必须**把"谁指向它"列出来：删掉之后那些双链会变成"还没写"，
   那是这次操作真正的代价，不该等删完才从 dangling 里知道。 */
function deleteVaultConfirmBody(data) {
  const backlinks = (data.backlinks || []).filter(Boolean);
  const box = el('div', 'vault-del-confirm');
  box.appendChild(el('p', '', t("vault.delete_confirm_lead", { name: String(data.title || data.path), kind: String(data.path) })));
  if (backlinks.length) {
    box.appendChild(el('p', 'danger-text',
      t("vault.delete_confirm_inbound", { n: backlinks.length })
      + backlinks.map((item) => String(item.title || item.path || '')).join('、')));
  } else {
    box.appendChild(el('p', '', t("vault.delete_confirm_no_inbound")));
  }
  box.appendChild(el('p', 'danger-text',
    t("vault.delete_confirm_note")
    + t("vault.delete_confirm_alt")));
  if (data.body) {
    const details = document.createElement('details');
    details.appendChild(el('summary', '', t("vault.delete_preview")));
    const pre = el('pre', 'vault-del-body', String(data.body));
    details.appendChild(pre);
    box.appendChild(details);
  }
  return box;
}

function askDeleteVaultFile(data) {
  if (!data) return;
  // README 上根本没有删除按钮（后端会拒）；这里再兜一次，免得别处误调
  if (data.is_readme) { toast(t("vault.delete_readme")); return; }
  openDialog({
    title: t("dialog.delete_file.title"),
    node: deleteVaultConfirmBody(data),
    confirmLabel: t("action.delete"),
    confirmClass: 'btn-danger',
    onConfirm: async () => {
      const confirm = $('dialog-confirm');
      if (confirm) { confirm.disabled = true; confirm.textContent = t("action.deleting"); }
      let result;
      try {
        result = await requestJSON('/api/vault/file/delete', {
          path: String(data.path), expect_hash: String(data.hash || ''),
        });
      } catch (err) {
        const body = err.body || {};
        confirm.disabled = false;
        confirm.textContent = t("action.delete");
        if (err.status === 409 && body.path) {
          // 被改过（或整理正在跑）：把后端原话原样说出来，并给出"看最新的"
          state.vaultNote = { level: 'warn', text: String(body.error || err.message) };
          state.vaultFile = positionAfterEditResult(body);
          renderVaultTree();
          renderVaultViewer();
          toast(t("vault.delete_conflict"));
        } else {
          // 400 / 404 / 409（整理中）/ 500 都是后端那句人话
          toast(String(body.error || err.message || t("vault.delete_failed")));
        }
        return;
      }
      // 删掉的那份不能留在屏幕上：回到"没选中"的引导态。
      // **先留一份反链清单**：删除成功会把 state.vaultFile 清掉，之后要按名字
      // 说出"哪几份的双链变成了还没写"，就只能靠删除前那一份的标题。
      const linksBefore = { backlinks: (data.backlinks || []).slice() };
      state.vaultEdit = null;
      state.vaultFile = null;
      state.vaultFileLoaded = false;
      state.vaultFileLinks = {};
      const dangling = (result.dangling || []).map((path) => vaultNameFor(path, linksBefore));
      state.vaultNote = {
        level: 'ok',
        text: t("vault.deleted", { name: String(result.path || data.path) })
          + (dangling.length
            ? t("vault.deleted_backlinks", { n: dangling.length, list: dangling.join('、') })
            : t("vault.deleted_no_backlinks")),
        check: result.check,
      };
      renderVaultTree();
      renderVaultViewer();
      toast(t("toast.deleted"));
      await reloadVaultAfterWrite();
    },
  });
}

/* 浏览器那一块（左树 + 右查看器）。整页重刷时**不重建**这两个容器：
   重建会把滚动位置和展开状态一起丢掉，而它们正是"重新加载之后不丢"要保住的东西。 */
function renderVaultBrowser(host) {
  let browser = host.querySelector('.vault-browser');
  if (!browser) {
    browser = el('div', 'vault-browser');
    const treePane = el('div', 'vault-tree-pane');
    const treeHead = el('div', 'vault-tree-title');
    treeHead.appendChild(el('span', '', t("vault.browser_title")));
    renderTreeNewButton(treeHead);
    treePane.appendChild(treeHead);
    const tree = el('div', 'vault-tree');
    tree.id = 'vault-tree';
    treePane.appendChild(tree);
    const viewer = el('div', 'vault-viewer');
    viewer.id = 'vault-viewer';
    browser.appendChild(treePane);
    browser.appendChild(viewer);
    host.insertBefore(browser, host.firstChild);
  }
  return browser;
}

function renderVaultPane() {
  // 整理模型选择器与 vault 数据无关：档案读不到时它照样该显示
  // （选哪个模型不需要先有档案）。所以放在下面那个 early return **之前**。
  renderDreamPicker();
  const data = state.vault;
  const host = $('dream-list');
  if (!host || !data) return;
  const check = data.check || {};
  const pending = data.pending || {};
  const sessions = Number(pending.sessions || 0);
  const events = Number(pending.events || 0);
  const stale = Number(pending.stale || 0);

  // sessions 为 0 时 stale 一定也是 0（后端只数还待整理的那些会话），
  // 所以这两个分支不会互相打架。
  const summary = sessions
    ? t("vault.pending_sessions_html", { n: sessions })
      + t("vault.pending_events_html", { n: fmtInt(events) })
    : t("vault.pending_latest");
  const pendingChip = stale
    ? t("vault.chip_stale")
    : sessions
      ? t("vault.chip_pending")
      : t("vault.chip_latest");

  const tokens = Number(check.readme_tokens || 0);
  const budget = Number(check.readme_budget || 0);
  const charCap = Number(check.readme_char_cap || 0);
  const readmeChars = Number(check.readme_chars || 0);
  const truncated = !!check.readme_truncated;
  // 超预算必须醒目：README 每轮都进上下文，截掉的那半截等于白写。
  // 原生模式按字符 cap，旧模式按 token —— 单位不能混着说。
  const used = charCap ? readmeChars : tokens;
  const limit = charCap || budget;
  const budgetTone = truncated ? ' err' : limit && used > limit * 0.85 ? ' warn' : '';
  const budgetLine = limit
    ? (charCap
      ? t("vault.readme_chars", { used: fmtInt(readmeChars), cap: fmtInt(charCap) })
      : `README ${fmtInt(tokens)} / ${fmtInt(budget)} token`)
      + (truncated ? t("vault.budget_over") : '')
    : t("vault.readme_missing");
  const filesLine = t("vault.file_count", { files: fmtInt(check.files || 0) })
    + (check.has_readme ? '' : t("vault.no_readme"));
  const pathLine = data.path ? `<code>${escapeHtml(data.path)}</code>` : '';

  const problems = check.problems || [];
  const problemsHtml = problems.length
    ? t("vault.problems_header", { n: problems.length })
      + `<ul>${problems.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul></div>`
    : t("vault.problems_ok");

  // 待整理那块会顺手说一句"日志比进度短"的异常（staleNoticeHtml），
  // 它和整理能不能回滚无关，是独立性校验的一部分，留着。
  host.innerHTML = `
    <div class="vault-card">
      <div class="vault-summary">${pendingChip}${summary}</div>
      <div class="vault-meta${budgetTone}">${escapeHtml(filesLine)} · ${escapeHtml(budgetLine)}`
        + t("vault.pane_html", { path: pathLine ? ' · ' + pathLine : '', problems: problemsHtml, pending: escapeHtml(pendingText(pending)), stale: staleNoticeHtml(pending) });

  // 树/查看器的容器是**复用的**（上面的 innerHTML 只重画下面那两块），
  // 所以重刷之后展开状态与当前选中的档案都还在。
  state.vaultTreeOpen = aliveTreeState(data.tree || []);
  renderVaultBrowser(host);
  renderVaultTree();
  renderVaultViewer();
  // 运行历史的容器是上面那句 innerHTML **新建**的，而 /api/vault 与 /api/dreams 是并行拉的。
  // 真机上见过这个顺序：运行记录是空的时候 dreams 先回来 —— 那一刻容器还不存在，
  // renderDreamList 只能把数据记下就返回，页面上的运行历史于是整片空白（截图里发现的）。
  // 所以这里在容器画好之后**补画一次**，谁先回来都不丢。
  if (state.dreamsLoaded) renderDreamList(state.dreamRuns || []);
  // 危险区挂在这一页**最下面**：它不可逆，不能和「整理一次」挤在同一排里被顺手点到
  renderDreamDangerZone(host);
}

/* ── 危险区：格式化并初始化 dream ─────────────────────────
   （渲染在 renderVaultPane 的最后，也就是这一页的最下面；
     执行逻辑与「整理一次」共用同一条 POST 通道，见下面的 openDialog 用法） */

/* 后端要求一字不差地说出的那个确认词（`POST /api/dream/format` 的 400 响应里带
   `confirm_word`）。这里是**默认值，而且全项目只写一处** ——
   一旦后端回了别的词，下面的失败分支会就地改成后端那个词，提示与门禁都跟着走。
   所以以后改词不会出现"界面说 A、后端要 B"。
   `null` = 还没收到过后端的词，用当前语言的默认词（`格式化` ↔ `format`）；
   **不能**在加载时就把 t() 的结果存下来 —— 那样切语言之后门禁还认旧语言的词。 */
let dreamFormatWord = null;

/* 界面上真正生效的那个词（后端给过就以后端为准）。 */
function formatWord() {
  return dreamFormatWord || t('dream.format.word');
}

/* 为什么把这个按钮埋在最下面：它**不可逆**（清掉档案、运行记录、整理进度），
   而它上面那排是"整理一次"这种日常动作。放在一起迟早有人顺手点到。 */
function renderDreamDangerZone(host) {
  const zone = el('div', 'dream-danger');
  zone.id = 'dream-danger';
  zone.appendChild(el('div', 'danger-title', t("dream.danger_title")));
  const sub = el('div', 'danger-sub');
  // 这一段是我们自己写的静态文案（没有任何模型/用户内容），可以放心用 innerHTML
  sub.innerHTML = t("dream.danger_sub_1")
    + t("dream.danger_sub_2")
    + t("dream.danger_sub_3");
  zone.appendChild(sub);
  const note = el('div', 'danger-note');
  note.innerHTML = t("dream.danger_note_1")
    + t("dream.danger_note_2");
  zone.appendChild(note);

  if (state.dreamFormatResult) zone.appendChild(formatResultNode(state.dreamFormatResult));

  const button = el('button', 'btn-danger dream-format-open', t("dream.danger_button"));
  button.id = 'dream-format';
  button.onclick = () => { askFormatDream().catch((err) => toast(String(err.message || err))); };
  zone.appendChild(button);
  host.appendChild(zone);
  return zone;
}

/* 执行完之后把 before → after 如实摆出来 —— 这是"到底清掉了什么"的唯一凭据。
   `leftovers` 非空必须单独警告：那意味着有文件**没删掉**，得用户自己动手。 */
function formatResultNode(result) {
  const before = result.before || {};
  const after = result.after || {};
  const box = el('div', 'dream-format-result');
  box.appendChild(el('div', 'dfr-title', t("dream.format_done")));
  for (const [label, from, to, unit] of [
    [t("vault.browser_title"), before.vault_files, after.vault_files, t("dream.unit_files")],
    [t("dream.runs"), before.dream_runs, after.dream_runs, t("dream.unit_records")],
    [t("dream.progress"), before.cursor_sessions, after.cursor_sessions, t("dream.unit_sessions")],
  ]) {
    box.appendChild(el('div', 'dfr-row',
      `${label}：${fmtInt(from)} → ${fmtInt(to)} ${unit}`));
  }
  if (result.cursors_cleared !== undefined) {
    box.appendChild(el('div', 'dfr-row', t("dream.format_cursor", { n: fmtInt(result.cursors_cleared) })));
  }
  box.appendChild(el('div', 'dfr-note', result.initialized
    ? t("dream.reinit_done")
      + t("dream.reinit_placeholder_note")
    : t("dream.reinit_skipped")));
  const leftovers = result.leftovers || [];
  if (leftovers.length) {
    const warn = el('div', 'dfr-leftovers');
    warn.appendChild(el('div', 'dfr-leftovers-title',
      t("dream.format_leftovers", { n: leftovers.length })));
    for (const item of leftovers) {
      warn.appendChild(el('div', 'dfr-leftover-path', String(item)));
    }
    box.appendChild(warn);
  }
  return box;
}

/* 点「格式化并初始化 dream」→ 先把要失去的东西列清楚，再让人把确认词打对。 */
async function askFormatDream() {
  // 数字要**当场**重新拉：这一步的全部意义就是"把要失去的东西列清楚"，
  // 拿一屏之前的旧数据来列等于没列。任何一个接口挂了都降级显示 ——
  // 列不出数字也得能让人确认，不能因此把这条路堵死。
  const [vaultBack, runsBack, resetBack] = await Promise.allSettled([
    getJSON('/api/vault'), getJSON('/api/dreams'), getJSON('/api/reset'),
  ]);
  const vault = vaultBack.status === 'fulfilled' ? vaultBack.value : state.vault;
  const runs = runsBack.status === 'fulfilled' ? (runsBack.value.runs || []) : null;
  const reset = resetBack.status === 'fulfilled' ? resetBack.value : null;
  const check = (vault || {}).check || {};
  const pending = (vault || {}).pending || {};
  const runsCount = runs ? runs.length : (runsBack.status === 'rejected' && state.dreamsLoaded
    ? state.dreams : null);

  let expected = formatWord();
  const body = el('div', 'dream-format-body');

  const listOf = (title, rows) => {
    body.appendChild(el('p', 'dft-lead', title));
    const list = el('ul', 'dft-list');
    for (const [label, value, unit] of rows) {
      const item = el('li');
      item.appendChild(el('span', 'dft-k', `${label}：`));
      item.appendChild(el('strong', '', value === null || value === undefined
        ? t("dream.unknown_value") : `${fmtInt(value)} ${unit}`));
      list.appendChild(item);
    }
    body.appendChild(list);
    return list;
  };

  // 会失去什么（能拿到多少列多少）
  listOf(t("dream.will_clear"), [
    [t("vault.browser_title"), check.files, t("dream.unit_files")],
    [t("dream.runs_long"), runsCount, t("dream.unit_records")],
    [t("dream.progress_cursor"), pending.sessions, t("dream.unit_sessions")],
  ]);
  // 不会被碰什么（数字来自 /api/reset 的预览，拿不到就只说名字）
  const keep = listOf(t("dream.will_keep"), [
    [t("reset.row_chats"), reset ? reset.sessions : null, t("dream.unit_sessions")],
    [t("reset.row_memories"), reset ? reset.memories : null, t("reset.unit_active_items")],
    [t("reset.row_providers"), reset ? reset.providers : null, t("reset.unit_items")],
  ]);
  keep.classList.add('dft-keep');
  body.appendChild(el('p', 'danger-text',
    t("dream.why_not_disaster")
    + t("dream.why_not_disaster_2")));

  const ask = el('p', 'dft-ask');
  ask.appendChild(el('span', '', t("dream.ask_prefix")));
  const wordNode = el('code', 'dft-word', expected);
  ask.appendChild(wordNode);
  ask.appendChild(el('span', '', t("dream.ask_suffix")));
  body.appendChild(ask);

  const input = el('input', 'dream-format-input');
  input.type = 'text';
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('spellcheck', 'false');
  input.setAttribute('placeholder', expected);
  body.appendChild(input);
  const errorLine = el('div', 'dream-format-error');
  body.appendChild(errorLine);

  // 门禁：**打对之前「执行格式化」一直是禁用的**（前后空白照后端一样容忍掉）
  const gate = (confirm) => {
    confirm.disabled = String(input.value || '').trim() !== expected;
  };

  openDialog({
    title: t("dream.format.title"),
    node: body,
    confirmLabel: t("dream.format.confirm"),
    onReady: ({ confirm }) => {
      input.oninput = () => gate(confirm);
      gate(confirm);
      input.focus();
    },
    // 失败**不关**对话框：后端原话与"再试一次"要留在原地（400 / 409 都走这里）
    onConfirmError: (err, { confirm }) => {
      const back = err.body || {};
      const word = String(back.confirm_word || '').trim();
      if (word && word !== expected) {
        // 后端说确认词不对 —— 那就以**后端回的那个词**为准，就地更新提示与门禁
        expected = word;
        dreamFormatWord = word;
        wordNode.textContent = word;
        input.setAttribute('placeholder', word);
      }
      errorLine.textContent = String(err.message || err);
      gate(confirm);
    },
    onConfirm: async () => {
      const result = await postJSON('/api/dream/format', {
        confirm: String(input.value || '').trim(),
      });
      applyDreamFormatResult(result);
      toast(t("dream.format_done_lead") + formatResultText(result));
      // 重刷这一页：档案/运行记录/待整理全都变了（结果条从 state 里画出来，不会被刷掉）
      await loadDreamPane().catch(() => {});
    },
  });
}

function formatResultText(result) {
  const before = result.before || {};
  const after = result.after || {};
  return t("dream.format_after_files", { before: fmtInt(before.vault_files), after: fmtInt(after.vault_files) })
    + t("dream.format_after_runs", { before: fmtInt(before.dream_runs), after: fmtInt(after.dream_runs) })
    + t("dream.format_after_progress", { before: fmtInt(before.cursor_sessions), after: fmtInt(after.cursor_sessions) });
}

/* 格式化之后界面上的东西全都对不上了：正在看的那份档案已经被挪走、目录树的展开状态
   没有意义、编辑态里的草稿也没有地方可存。清掉它们，再把结果记进 state
   （renderVaultPane 会把它画出来，所以"清掉了什么"不会被下一次重刷擦掉）。 */
function applyDreamFormatResult(result) {
  state.dreamFormatResult = result;
  state.vaultFile = null;
  state.vaultFileLoaded = false;
  state.vaultFileLinks = {};
  state.vaultTreeOpen = {};
  state.vaultEdit = null;
  state.vaultNote = null;
}

/* ── (b) 运行历史 ───────────────────────────────────────── */

/* 运行历史的容器。**从这一页的容器子树里找**，而不是只按 id 取：
   `#dream-runs` 是 renderVaultPane 用 innerHTML 画出来的，而自检的 DOM stub 没有真 DOM
   的 id 索引（它的 getElementById 是"边用边造"的）—— 只按 id 取，自检看到的就是
   另一个节点，"运行历史到底画没画出来"会验成一句空话（这个坑查看器/编辑器都踩过）。 */
function dreamRunsHost() {
  const list = $('dream-list');
  const inPage = list && list.querySelector ? list.querySelector('#dream-runs') : null;
  return inPage || $('dream-runs');
}

function renderDreamList(runs) {
  state.dreams = runs.length;
  state.dreamRuns = runs;      // 留一份：容器被重画时可以补画（见 renderVaultPane 末尾）
  const host = dreamRunsHost();
  renderDreamMeta();
  // 容器还不存在 = 两份数据在赛跑（`/api/dreams` 先回来了）。**不能在这里放弃**：
  // 数据已经记进 state，renderVaultPane 画完容器会再调一次这个函数。
  if (!host) return;
  if (!runs.length) {
    host.innerHTML = t("dream.none_1")
      + t("dream.none_2");
    return;
  }
  host.innerHTML = '';
  for (const run of runs) {
    const card = document.createElement('div');
    card.className = 'run';
    // 老的 `mode: "dry-run"` 记录照旧显示（历史不改写），但页面上**没有**发起预演的入口
    const dry = run.mode === 'dry-run';
    const mode = dry
      ? t("dream.chip_dry")
      : t("dream.chip_applied");
    const sessions = Number((run.pending || {}).sessions || 0);
    const skipped = run.status === 'empty';
    const changed = skipped
      ? t("dream.no_files_touched")
      : t("dream.files_touched", { n: run.changed || 0 });
    card.innerHTML = t("dream.run_head_html", { when: escapeHtml(fmtStamp(run.started_at)), id: escapeHtml(run.id), mode: mode, status: runStatusChip(run.status), sessions: sessions, changed: changed, summary: escapeHtml(run.summary || t("dream.no_summary_short")), tokens: fmtInt((run.usage || {}).total_tokens) });
    card.querySelector('.run-head').onclick = () => toggleRun(card, run.id);
    host.appendChild(card);
  }
}

async function toggleRun(card, runId) {
  const body = card.querySelector('.run-body');
  if (!body.classList.contains('hidden')) { body.classList.add('hidden'); return; }
  body.classList.remove('hidden');
  if (body.dataset.loaded === runId) return;
  body.innerHTML = t("dream.detail_loading");
  try {
    // 列表里刻意不含 diff / check（可能很长），点开才取
    const run = await getJSON(`/api/dreams/${encodeURIComponent(runId)}`);
    body.dataset.loaded = runId;
    body.innerHTML = renderRunDetail(run);
  } catch (err) {
    body.innerHTML = t("session.load_failed", { msg: escapeHtml(String(err.message || err)) });
  }
}

/* ── (c) 运行详情 ───────────────────────────────────────── */

function renderRunDetail(run) {
  const groups = [];
  const added = (run.diff || {}).added || [];
  const changed = (run.diff || {}).changed || [];
  const removed = (run.diff || {}).removed || [];
  if (added.length) {
    groups.push(t("dream.diff_created")
      + added.map((row) => `<div class="diff-line diff-add">＋ `
        + `<span class="v-path">${escapeHtml(row.path)}</span> `
        + `${escapeHtml(row.title || '')}`
        + `<span class="session-sub">${escapeHtml(row.summary || '')}</span></div>`).join('')
      + '</div>');
  }
  if (changed.length) {
    groups.push(t("dream.diff_updated")
      + changed.map((row) => `<div class="diff-line diff-chg">～ `
        + `<span class="v-path">${escapeHtml(row.path)}</span> `
        + t("dream.diff_chars_both", { from: fmtChars(row.chars_before), to: fmtChars(row.chars_after) })).join('')
      + '</div>');
  }
  if (removed.length) {
    groups.push(t("dream.diff_deleted")
      + removed.map((row) => `<div class="diff-line diff-del">－ `
        + `<span class="v-path">${escapeHtml(row.path)}</span> `
        + t("dream.diff_chars_one", { chars: fmtChars(row.chars) })).join('')
      + '</div>');
  }
  const diffHtml = groups.length
    ? `<div class="diff">${groups.join('')}</div>`
    : t("dream.diff_empty");

  const problems = ((run.check || {}).problems) || [];
  const problemsHtml = problems.length
    ? t("dream.check_after_header", { n: problems.length })
      + `<ul>${problems.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul></div>`
    : t("dream.check_after_ok");

  return diffHtml + renderRunFooter(run) + problemsHtml;
}

function renderRunFooter(run) {
  const entries = Object.entries(run.advanced || {});
  const lines = [];
  lines.push(t("dream.cursor_advanced", { n: entries.length }));
  for (const [sessionId, seq] of entries) {
    lines.push(t("dream.cursor_to", { from: sessionId, to: seq }));
  }
  const usage = run.usage || {};
  const parts = [];
  if (run.stop_reason) parts.push(t("dream.stop_reason", { reason: escapeHtml(run.stop_reason) }));
  const duration = durationText(run);
  if (duration) parts.push(t("dream.elapsed", { duration: duration }));
  if (usage.total_tokens) {
    parts.push(`${fmtInt(usage.total_tokens)} token`
      + `（prompt ${fmtInt(usage.prompt_tokens)} · completion ${fmtInt(usage.completion_tokens)}）`);
  }
  if (run.pending && run.pending.sessions) {
    parts.push(t("dream.candidates", { n: run.pending.sessions, events: fmtInt(run.pending.events) }));
  }
  const summary = run.summary
    ? `<div class="mem-content">${escapeHtml(run.summary)}</div>`
    : t("dream.no_summary");
  const skipNote = run.status === 'empty'
    ? t("dream.run_skipped")
    : '';
  return `${skipNote}${summary}
    <div class="session-sub" style="margin-top:8px">${parts.join(' · ')}</div>
    <div class="diff-field">${escapeHtml(lines.join('\n'))}</div>
    ${renderFinalNote(run)}`;
}

/* 轨迹 JSONL 是**取消回滚之后唯一的追回途径**：每次 `vault_write` 的全文都在里面，
   所以"这份文件当时到底写了什么"永远还能翻出来 —— 只是要自己去翻。
   接口给了 paths（轨迹路径）就用它；没给就按 data/dreams/<run-id>.jsonl 这个约定拼。 */
function tracePathOf(run) {
  const raw = (run || {}).paths;
  const candidates = Array.isArray(raw)
    ? raw.slice()
    : raw && typeof raw === 'object'
      ? Object.values(raw)
      : [raw, (run || {}).trace, (run || {}).trace_path];
  for (const item of candidates) {
    if (typeof item === 'string' && /\.jsonl$/i.test(item)) return item;
  }
  const id = (run || {}).id || '';
  return id ? `data/dreams/${id}.jsonl` : '';
}

/* 整理是单向门：改完不可逆，后端不再为一次运行留快照，而**预演也已经取消了**
   （ADR-072：它照样要真跑一遍模型、照样花钱，给出的却只是"会改什么"的估计；
   用户要的取向是"能重来"，不是"先看一眼"）。所以这个位置**没有按钮**，
   只留下事实与那条唯一的补救路径。 */
function renderFinalNote(run) {
  const trace = tracePathOf(run);
  const traceLine = trace
    ? t("dream.final_trace_lead")
      + `<code class="ic">${escapeHtml(trace)}</code>`
      + t("dream.final_trace_full")
      + t("dream.final_trace_tail")
    : '';
  if (run.mode === 'dry-run') {
    // 历史记录里的老预演：当时确实什么都没改，照实说，但别让它读起来像"还能再发起一次"
    return t("dream.final_dry_1")
      + t("dream.final_dry_2")
      + t("dream.final_dry_3")
      + `${traceLine}</div>`;
  }
  return '<div class="run-final">'
    + t("dream.final_note_undo")
    + t("dream.final_note_1")
    + t("dream.final_note_2")
    + t("dream.final_note_3")
    + `${traceLine}</div>`;
}

/* ── (a) 整理按钮：走既有 SSE 通道 ──────────────────────── */

/* 整理只有一种模式：真整理（预演随 ADR-072 取消）。
   **请求体里绝不能再出现 `dry_run`**：后端见到这个字段会直接 400 —— 那是刻意的。
   一个还缓存着旧页面的浏览器会 POST `dry_run: true` 并**以为不会改动任何东西**，
   后端要是把它忽略掉，那次请求就变成了一次**真整理**（用户以为在预演、实际在写档案）。
   所以前端这边必须一个字都不发；这条也有断言盯着（脚本里会检查请求体的键）。 */
async function runDream() {
  if (state.busy) { toast(t("toast.busy_running")); return; }
  // 必须在任何 await 之前锁住：模型保存还没完成时，连点两次不能发出两轮整理。
  setBusy(true);
  $('dream-run').disabled = true;
  const progress = $('dream-progress');
  progress.classList.remove('hidden');
  progress.textContent = t("dream.starting");
  const append = (line) => {
    progress.textContent += line + '\n';
    progress.scrollTop = progress.scrollHeight;
  };

  try {
    // 刚改完模型就点「整理一次」：等那次保存落盘再用 —— 否则后端会用上一个
    // 模型跑这一轮，而界面上显示的是新选的那个（ADR-103）。
    if (state.dreamModelSaving) await state.dreamModelSaving;
    // 等待期间若外部已经取消这次占用，就不要再发起不可逆的整理。
    if (!state.busy) return;
    const limit = Number($('dream-limit').value) || 3;
    await streamPost('/api/dream', {
      // 整理模型**不再随请求发**：它存在 providers.json 的 `dream` 里，
      // 由后端 / CLI 共用（ADR-103）。请求体只带这一轮的业务参数。
      limit,
    }, {
      // 这几个中间帧（step / tool_call / tool_result / notice / text / reasoning）
      // 与对话页是同一套 emitter 发出来的，所以按对话页的老办法复用即可。
      dream_start: (payload) => {
        const pending = payload.pending || {};
        append(t("dream.run_started", { id: payload.run_id, when: modeLabel(payload.mode) }));
        append(t("dream.candidates", { n: pending.sessions || 0, events: fmtInt(pending.events) }));
        const generation = payload.generation || {};
        if (generation.model) append(t("dream.model", { model: generation.model }));
        for (const item of pending.detail || []) {
          // 整行都是一条文案：引号也用界面语言的（英文界面下不该冒出「」）
          append(t("dream.record_line", {
            id: item.session_id,
            title: item.title || t("dream.untitled"),
          }) + t("dream.new_records", { n: item.new_events, from: item.cursor, to: item.last_seq }));
        }
      },
      step: (payload) => { if (payload.step > 1) append(t("dream.step", { n: payload.step })); },
      tool_call: (payload) => append(`⚙ ${payload.name} ${shortArgs(payload.arguments)}`),
      tool_result: (payload) => append(`  ↳ ${payload.display}`),
      notice: (payload) => append(`! ${payload.text}`),
      dream_done: (payload) => {
        const diff = payload.diff || {};
        if (payload.skipped) {
          // 没有待整理的记录：后端直接返回，一分钱没花。别把它画成失败。
          append(t("dream.skipped"));
          append(payload.summary || t("dream.no_summary_paren"));
          return;
        }
        append(t("dream.done_created", { n: (diff.added || []).length })
          + t("dream.done_updated", { n: (diff.changed || []).length })
          + t("dream.done_deleted", { n: (diff.removed || []).length })
          + ` · ${fmtInt((payload.usage || {}).total_tokens)} token`);
        const advanced = Object.entries(payload.advanced || {}).length;
        append(t("dream.cursor_advanced", { n: advanced }));
        append(payload.summary || t("dream.no_summary_paren"));
      },
      // 同对话页：某一帧画不出来只报一次，别把整条整理流掐断
      onHandlerError: (err) => append(t("dream.error", { msg: (err && err.message) || err })),
    });
  } catch (err) {
    if (err.name === 'AbortError') append(t("notice.stopped"));
    else append(t("dream.error", { msg: err.message || err }));
  } finally {
    setBusy(false);
    $('dream-run').disabled = false;
    await loadDreamPane().catch(() => {});
    await loadMemories().catch(() => {});
  }
}

/* ── 用量 ─────────────────────────────────────────────── */

async function loadUsage() {
  const data = (await getJSON('/api/usage')) || {};
  const total = data.total || {};
  // 接口少给一个 sessions 数组、或某一行没有 usage，不该让整页停在写
  // #usage-body 之前：一条 TypeError 的结果就是空白 / 上一次的旧内容。
  const sessions = Array.isArray(data.sessions) ? data.sessions : [];
  $('usage-meta').textContent = t("usage.chat_count", { n: sessions.length });
  const cells = [
    [t("usage.total"), total.total_tokens], [t("usage.prompt"), total.prompt_tokens],
    [t("usage.completion"), total.completion_tokens], [t("usage.reasoning"), total.reasoning_tokens],
    [t("usage.cache_hit"), total.cached_tokens], [t("usage.cache_miss"), total.cache_miss_tokens],
  ];
  const usageOf = (row) => (row && row.usage) || {};
  const rows = sessions.slice().sort(
    (a, b) => (usageOf(b).total_tokens || 0) - (usageOf(a).total_tokens || 0));

  // 列的含义在两张表里完全一致，所以共用一个行渲染器
  const usageRow = (label, chip, usage) => {
    const u = usage || {};
    return `<tr>
      <td>${escapeHtml(label)}${chip ? ` <span class="chip id">${escapeHtml(chip)}</span>` : ''}</td>
      <td class="num">${u.prompt_tokens || 0}</td>
      <td class="num">${u.completion_tokens || 0}</td>
      <td class="num">${u.cached_tokens || 0}</td>
      <td class="num">${u.total_tokens || 0}</td></tr>`;
  };

  const providerRows = (data.providers || []).map((entry) => usageRow(
    entry.label || entry.provider,
    entry.label === entry.provider ? '' : entry.provider,
    entry.usage)).join('');

  $('usage-body').innerHTML = t("usage.pane_html", {
    prompt: t("usage.prompt"),
    completion: t("usage.completion"),
    cells: cells.map(([k, v]) => `<div class="usage-cell"><div class="k">${k}</div>
        <div class="v">${v || 0}</div></div>`).join(''),
    providers: providerRows || t("usage.empty_rows"),
    rows: rows.map((row) => usageRow(row.id, '', row.usage)).join(''),
  });
}

/* ── 接口（多供应商）───────────────────────────────────── */

/* 上下文上限的写法（ADR-093）：`1000000`、`1m`、`1M`、`1000k`、`200K` 是同一个数。
   k/K = **1000**、m/M = **1000000**（不是 1024：各家标称的 200k / 1M 都是十进制）。
   只有"数字 + 最多一个单位"这一种形式；`1.5M`、`1kk`、`1e6`、`1kb` 一律看不懂。

   这是后端 `parse_token_limit()` 的**同一套规则**（两份实现）：这一份的职责是
   "别发一个后端会静默丢掉、而用户以为存上了的请求"，权威那一份在 Python 里
   （档案也可能被人手改）。两边靠同一批用例钉住。 */
function parseTokenLimit(raw, options = {}) {
  // `allowUnlimited` 只给「设置 → 常规 → 单轮最大输出」用（ADR-101）：`-1` = 不限。
  // 上下文上限那条路径不传它，所以那里 -1 仍然不认（窗口没有"不限"这回事）。
  const allowUnlimited = !!(options && options.allowUnlimited);
  const text = String(raw === undefined || raw === null ? '' : raw).trim();
  const match = /^(-?)([0-9]+)([kKmM]?)$/.exec(text);
  if (!match) return null;
  const unit = match[3].toLowerCase();
  const value = (match[1] === '-' ? -1 : 1)
    * Number(match[2])
    * (unit === 'm' ? 1000000 : unit === 'k' ? 1000 : 1);
  if (allowUnlimited && value === -1) return -1;
  // `Number.isSafeInteger`：`9999999999999m` 这种会溢出成不精确的数，那就不认
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

/* 「支持图片的模型」那一栏的文本 → 列表（providers.json 里存的是列表）。
   逗号和换行都算分隔符（用户从别处粘一列模型名是最常见的用法），
   两头空白去掉、空项丢掉 —— **空串必须得到空列表**：
   `''.split(',')` 是 `['']`，那不是"没填"，那是一个名字为空字符串的模型。
   内置已知情况见 providers 预设：deepseek-flash 支持、deepseek-v4-pro 不支持；
   **留空 = 未知**（不拦，但真发出去可能被模型静默忽略）。 */
function splitModelList(text) {
  return String(text === undefined || text === null ? '' : text)
    .split(/[,\n\r]+/)
    .map((item) => item.trim())
    .filter((item) => item !== '');
}

async function loadProviders() {
  state.providers = await getJSON('/api/providers');
  renderApiPane();
  renderPickers();
  // 接口页增删模型之后，Dream 页那个选择器也要跟着变（它列的是同一批模型）
  renderDreamPicker();
}

function renderApiPane() {
  const data = state.providers;
  if (!data) return;
  const def = data.default || {};
  // 这一行是各子页共用的：慢回来的接口页响应不该把它写到记忆/主题页上
  if (state.pane === 'api') {
    $('settings-meta').textContent = t("api.provider_count", { n: (data.providers || []).length });
  }

  const defaultOptions = (data.models || []).map((m) => {
    const selected = m.provider === def.provider && m.model === def.model ? 'selected' : '';
    return `<option value="${escapeHtml(m.provider)}|${escapeHtml(m.model)}" ${selected}>${escapeHtml(m.label)}</option>`;
  }).join('');

  const presetOptions = (data.presets || []).map((p) =>
    `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label)} — ${escapeHtml(p.base_url)}</option>`
  ).join('');

  const cards = (data.providers || []).map((p) => providerCard(p, def)).join('');

  $('api-body').innerHTML = t("api.pane_html", { defaultCls: defaultOptions ? '' : 'default', defaultOptions: defaultOptions || t("api.no_default_options"), cards: cards || t("api.no_providers"), presets: presetOptions });

  const defaultSelect = $('default-model');
  if (defaultSelect) {
    defaultSelect.onchange = async () => {
      const [provider, model] = defaultSelect.value.split('|');
      try {
        await postJSON('/api/providers/default', { provider, model });
        state.providers.default = { ...(state.providers.default || {}), provider, model };
        toast(t("api.default_set"));
      } catch (err) { toast(String(err.message || err)); }
    };
  }
  $('preset-add').onclick = () => addFromPreset($('preset-pick').value);
  wireProviderCards();
}

/* ── 模型表格（接口卡片里的那一块） ───────────────────────

   一行 = 一个模型的全部属性：id / 上下文上限 / 显示名。

   **为什么不是两个文本框**：早先是「模型（逗号分隔）」+「按模型覆盖上下文上限
   （每行 model=上限）」两个自由文本。改一个忘一个就会留下"id 改了、上限还挂在
   旧 id 上"这种**看不出来**的错，而且没有任何地方会核对两处文本对不对齐。
   Kimi 的接口页也是这张表的样子（Model ID / Context / Display name + 删除/添加），
   见 ADR-092。

   **三列一起提交**（`models` / `model_limits` / `model_labels`），后端照旧接受
   老的两串文本写法 —— 所以这个改动不需要迁移任何已有档案。 */

function modelRowCells(row) {
  const value = (raw) => escapeHtml(raw === undefined || raw === null ? '' : String(raw));
  const context = row.limit === undefined || row.limit === null ? '' : String(row.limit);
  return `<input data-m="id" value="${value(row.id)}" placeholder="deepseek-flash" spellcheck="false">
            <input data-m="context" value="${value(context)}" inputmode="numeric" placeholder="200k">
            <input data-m="label" value="${value(row.label)}" placeholder="${escapeHtml(t("api.model_label_ph"))}">
            <button class="mini danger" data-act="model-del" type="button" title="${escapeHtml(t("api.model_remove"))}">×</button>`;
}

/* 档案里存的是整数（规范形式），所以回填也显示整数：`1M` 会显示成 `1000000`。
   这是刻意的 —— 界面上"你填了什么"与"存下来了什么"必须是同一个值，
   而不是让人怀疑"我的 1M 去哪了"。单位只是**输入**时的便利。 */

function modelRowHtml(row) {
  return `<div class="model-row" data-row>${modelRowCells(row)}</div>`;
}

/* 从档案画出所有行。**一个模型都没有时也画一行空行**：
   否则这张表只剩表头，看起来像是坏了而不是"还没填"。 */
function modelRowsHtml(profile) {
  const models = (profile && profile.models) || [];
  const limits = (profile && profile.model_limits) || {};
  const labels = (profile && profile.model_labels) || {};
  const rows = models
    .map((model) => modelRowHtml({ id: model, limit: limits[model], label: labels[model] }))
    .join('');
  return rows || modelRowHtml({});
}

function providerCard(profile, def) {
  const isDefault = def.provider === profile.id;
  const keyHint = profile.has_key
    ? t("api.current_key", { value: escapeHtml(profile.masked_key) })
    : t("api.local_only");
  return t("api.provider_card_html", {
    defaultCls: isDefault ? 'default' : '',
    id: escapeHtml(profile.id),
    label: escapeHtml(profile.label),
    idText: escapeHtml(profile.id),
    defaultChip: isDefault ? t("api.default_chip") : '',
    noKeyChip: profile.has_key ? '' : t("api.no_key_chip"),
    noToolsChip: profile.supports_tools ? '' : t("api.no_tools_chip"),
    labelValue: escapeHtml(profile.label),
    baseUrl: escapeHtml(profile.base_url),
    keyPlaceholder: profile.has_key ? t("api.key_stored") : t("api.key_paste"),
    keyHint: keyHint,
    // 模型那三列是一张表，**由这里整块画好**（行数随档案变），
    // 模板里只留表头与"添加模型"按钮。
    modelRows: modelRowsHtml(profile),
    visionModels: escapeHtml((profile.vision_models || []).join(', ')),
    thinkingOptions: escapeHtml((profile.thinking_options || []).join(', ')),
    reasoningYes: profile.send_reasoning_content ? 'selected' : '',
    reasoningNo: profile.send_reasoning_content ? '' : 'selected',
    typeOpenai: profile.type === 'anthropic' ? '' : 'selected',
    typeAnthropic: profile.type === 'anthropic' ? 'selected' : '',
    notes: profile.notes ? `<div class="provider-note">${escapeHtml(profile.notes)}</div>` : '',
  });
}

/* 表格的接线：删一行、加一行。**两者都只动 DOM** —— 真值在保存那一刻由
   `readModels()` 从行里读出来，所以"加一行又删掉"不会在别处留下痕迹。 */
function wireModelRows(card) {
  const table = card.querySelector('[data-f="model-table"]');
  if (!table) return;
  const wireRow = (row) => {
    const del = row.querySelector('[data-act="model-del"]');
    if (!del) return;
    del.onclick = () => {
      // 最后一行不删、只清空：留下一张只有表头的表，看起来像是坏了
      if (table.querySelectorAll('.model-row').length <= 1) {
        for (const input of row.querySelectorAll('input')) input.value = '';
        return;
      }
      row.parentNode.removeChild(row);
    };
  };
  for (const row of table.querySelectorAll('.model-row')) wireRow(row);

  const add = card.querySelector('[data-act="model-add"]');
  if (!add) return;
  add.onclick = () => {
    const row = document.createElement('div');
    row.className = 'model-row';
    row.setAttribute('data-row', '');
    row.innerHTML = modelRowCells({});
    wireRow(row);
    table.appendChild(row);
    const first = row.querySelector('[data-m="id"]');
    if (first && typeof first.focus === 'function') first.focus();
  };
}

function wireProviderCards() {
  for (const card of $('api-body').querySelectorAll('.provider-card[data-id]')) {
    const id = card.dataset.id;
    const read = (field) => card.querySelector(`[data-f="${field}"]`).value;
    // 模型表格 → 三个字段（id 列表 / 上限表 / 显示名表）。
    // **校验在这里**，而不是让后端把非法值悄悄丢掉：上下文写错一个字，
    // 后端会当成"没填"、于是静默退回 200k —— 那正是"看起来成功了"的失败。
    const readModels = () => {
      const table = card.querySelector('[data-f="model-table"]');
      const rows = table ? table.querySelectorAll('.model-row') : [];
      const models = [];
      const limits = {};
      const labels = {};
      const fieldOf = (row, name) => {
        const input = row.querySelector(`[data-m="${name}"]`);
        return input ? String(input.value || '').trim() : '';
      };
      for (const row of rows) {
        const modelId = fieldOf(row, 'id');
        const context = fieldOf(row, 'context');
        const label = fieldOf(row, 'label');
        if (!modelId && !context && !label) continue;   // 整行空着 = 还没填
        if (!modelId) throw new Error(t("api.model_id_required"));
        if (models.includes(modelId)) throw new Error(t("api.model_duplicate", { id: modelId }));
        if (context !== '') {
          // `128k` / `1M` 都认（ADR-093）；看不懂就**不发请求** —— 后端对看不懂的
          // 上限是"跳过"，那会静默退回 200k，而用户以为存上了。
          const parsed = parseTokenLimit(context);
          if (parsed === null) {
            throw new Error(t("api.model_context_bad", { id: modelId }));
          }
          limits[modelId] = parsed;
        }
        if (label) labels[modelId] = label;
        models.push(modelId);
      }
      return { models, model_limits: limits, model_labels: labels };
    };
    const profilePayload = (extra) => ({
      id,
      ...extra,
      label: read('label'),
      base_url: read('base_url'),
      thinking_options: read('thinking_options'),
      send_reasoning_content: read('send_reasoning_content') === '1',
      type: read('type'),
      ...readModels(),
      // 契约：这一栏按"列表"提交（逗号或换行分隔的文本 → 列表）。
      // **空串提交成空列表**，绝不是 ['']。
      vision_models: splitModelList(read('vision_models')),
    });
    wireModelRows(card);
    card.querySelector('[data-act="save"]').onclick = async () => {
      try {
        await postJSON('/api/providers/save', {
          ...profilePayload({ preset: id }),
          api_key: read('api_key'),
        });
        toast(t("vault.saved_short"));
        await loadProviders();
      } catch (err) { toast(String(err.message || err)); }
    };
    card.querySelector('[data-act="fetch"]').onclick = async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      button.textContent = t("api.fetching");
      try {
        // 先保存当前输入，否则新填的 key 还没落盘就取不到模型
        await postJSON('/api/providers/save', {
          ...profilePayload({}),
          api_key: read('api_key'),
        });
        const result = await postJSON('/api/providers/fetch-models', { id });
        toast(t("api.fetched_models", { n: result.models.length }));
        await loadProviders();
      } catch (err) {
        toast(t("api.fetch_failed", { msg: err.message || err }));
        button.disabled = false;
        button.textContent = t("api.fetch_models");
      }
    };
    card.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(t("api.delete_confirm", { id: id }))) return;
      try {
        await postJSON('/api/providers/delete', { id });
        toast(t("toast.deleted"));
        await loadProviders();
      } catch (err) { toast(String(err.message || err)); }
    };
  }
}

async function addFromPreset(presetId) {
  const preset = ((state.providers || {}).presets || []).find((p) => p.id === presetId);
  const custom = presetId === '__custom';
  const id = custom ? (prompt(t("api.new_id_prompt")) || '').trim() : presetId;
  if (!id) return;
  try {
    await postJSON('/api/providers/save', {
      id,
      preset: custom ? '' : presetId,
      label: custom ? id : (preset ? preset.label : id),
      base_url: custom ? (prompt(t("api.base_url_prompt")) || '').trim() : undefined,
      thinking_options: custom ? '' : undefined,
    });
    toast(t("api.added_fill_key"));
    await loadProviders();
  } catch (err) { toast(String(err.message || err)); }
}

/* ── 常规面板：主题 + 语言 + 时区，三个子区各自渲染 ─────────── */

function renderGeneralPane() {
  renderThemePane();
  renderLanguagePane();
  renderTimezonePane();
  renderOutputPane();
}

/* ── 主题面板 ─────────────────────────────────────────── */

function renderThemePane() {
  // 刻意**不叫** current：i18n.js 里那个 current()（当前语言）是全局函数，
  // 同名的局部常量会把它在这个函数体里整个遮住。
  const active = currentTheme();
  // 主题名**现取**（t()），不能写成模块级常量表：那种表在加载时就冻成了当时的
  // 语言，切语言之后这一页会继续显示旧语言。
  const options = [
    ['', t("theme.follow_system"), t("theme.follow_system_desc")],
    ['light', t("theme.light"), t("theme.light_desc")],
    ['dark', t("theme.dark"), t("theme.dark_desc")],
  ];
  $('theme-body').innerHTML = `<div class="section-title">${t("settings.general.theme")}</div>`
    + options.map(([value, name, desc]) => `
    <div class="theme-option ${value === active ? 'active' : ''}" data-theme-value="${value}">
      <div><div class="t-name">${name}</div><div class="t-desc">${desc}</div></div>
    </div>`).join('');
  for (const el of $('theme-body').querySelectorAll('.theme-option')) {
    el.onclick = () => { applyTheme(el.dataset.themeValue); renderThemePane(); };
  }
}

/* ── 语言面板（契约 docs/12-i18n.md §5）────────────────────
   两个选项 + 当前值。选中之后：POST /api/settings/language → 成功后 setLanguage()
   → rerenderForLanguage()（**原地重渲染，不刷新页面** —— 用户明确选了这条）。

   为什么 busy 时禁用：语言是**进程级**的，切了之后同一轮对话里后续每一步都会用
   新语言重新组装系统提示与工具定义，模型会在一次回答中途看到"系统提示换了一套"。
   所以"正在生成"时不许切，并在这儿把原因说出来；这一轮结束后 setBusy() 会把这一页
   重画一次（选项自动恢复可点）。 */

/* 当前语言在界面上的名字（中文界面显示「中文」，英文界面显示「Chinese」） */
function languageLabel(id) {
  const found = (I18N.languages || []).find((item) => item.id === id);
  return found ? found.label : id;
}

/* 同一处改两个地方：i18n.js 里的 I18N.lang 与 state.language（设置页靠后者画当前值）。
   setLanguage() 返回规范化之后的值 —— 传进来一个乱七八糟的东西也不会写坏状态。 */
function setLanguageNow(lang) {
  state.language = setLanguage(lang);
  return state.language;
}

function renderLanguagePane() {
  const host = $('language-body');
  if (!host) return;
  clearNode(host);

  const card = el('div', 'reset-card');
  card.appendChild(el('div', 'section-title', t('settings.language.title')));
  card.appendChild(el('p', 'reset-lead', t('settings.language.lead')));

  const options = el('div', 'lang-options');
  const languages = I18N.languages || [];
  for (const item of languages) {
    const active = item.id === state.language;
    // 用真按钮（不是 div）：busy 时要能**真的 disabled**，鼠标点不动、键盘也 tab 不进去。
    const box = el('button', 'theme-option lang-option' + (active ? ' active' : ''));
    box.setAttribute('type', 'button');
    box.dataset.lang = item.id;
    box.disabled = !!state.busy;
    if (!state.busy) box.onclick = () => { chooseLanguage(item.id); };
    const inner = el('div');
    inner.appendChild(el('div', 't-name', item.label));
    inner.appendChild(el('div', 't-desc', t('language.desc.' + item.id)));
    box.appendChild(inner);
    options.appendChild(box);
  }
  if (state.busy) {
    // 说明**只在 busy 时出现**：不 busy 的时候留一句"为什么不能点"只会让人以为出了问题
    const busy = el('div', 'lang-busy');
    busy.id = 'language-busy';
    busy.textContent = t('settings.language.busy');
    options.appendChild(busy);
  }
  card.appendChild(options);
  card.appendChild(el('div', 'hint', t('settings.language.current', { name: languageLabel(state.language) })));
  card.appendChild(el('div', 'hint', t('settings.language.note')));
  host.appendChild(card);
}

/* 点一个语言：**POST 成功之后**才切界面（失败了要如实说，不能界面变了、后端没变）。 */
let languageSaving = false;
async function chooseLanguage(lang) {
  if (languageSaving) return;
  if (state.busy) { toast(t('settings.language.busy')); return; }
  if (lang === state.language) return;
  languageSaving = true;
  try {
    await postJSON('/api/settings/language', { language: lang });
  } catch (err) {
    toast(t('settings.language.failed', { msg: err.message || err }));
    return;
  } finally {
    languageSaving = false;
  }
  setLanguageNow(lang);
  await rerenderForLanguage();
  toast(t('settings.language.saved', { name: languageLabel(lang) }));
}

/* ── 时区（`time` 工具用的偏移，小时）──────────────────────────
   只收整数小时（-12 ~ +14）。选中后 POST /api/settings/timezone → 成功后更新
   state.timezone。时区是**下一轮对话**才生效的设置（runtime 装配时才读），
   所以这里不需要 rerenderForLanguage() 那套原地重渲染。 */

function timezoneLabel(offset) {
  if (offset > 0) return `UTC+${offset}`;
  if (offset < 0) return `UTC${offset}`;
  return 'UTC';
}

function renderTimezonePane() {
  const host = $('timezone-body');
  if (!host) return;
  clearNode(host);

  const card = el('div', 'reset-card');
  card.appendChild(el('div', 'section-title', t('settings.general.timezone')));
  card.appendChild(el('p', 'reset-lead', t('settings.timezone.lead')));

  const select = el('select', 'timezone-select');
  select.id = 'timezone-select';
  for (let h = -12; h <= 14; h += 1) {
    const opt = el('option');
    opt.value = String(h);
    opt.textContent = timezoneLabel(h);
    select.appendChild(opt);
  }
  select.value = String(state.timezone ?? 8);
  select.onchange = () => chooseTimezone(Number(select.value));
  card.appendChild(select);

  const current = state.timezone ?? 8;
  card.appendChild(el('div', 'hint', t('settings.timezone.current', { zone: timezoneLabel(current) })));
  card.appendChild(el('div', 'hint', t('settings.timezone.note')));
  host.appendChild(card);
}

let timezoneSaving = false;
async function chooseTimezone(offset) {
  if (timezoneSaving) return;
  if (offset === (state.timezone ?? 8)) return;
  timezoneSaving = true;
  try {
    const result = await postJSON('/api/settings/timezone', { timezone: offset });
    state.timezone = result.timezone;
    toast(t('settings.timezone.saved', { zone: timezoneLabel(state.timezone) }));
  } catch (err) {
    toast(t('settings.timezone.failed', { msg: err.message || err }));
  } finally {
    timezoneSaving = false;
  }
  renderTimezonePane();
}

/* ── 单轮最大输出（token；-1 = 不限，ADR-101）──────────────────
   与语言不同，它是**下一轮对话**装配 Runtime 时才读的，所以生成中也能改。
   接受 `5000` / `5k` / `1M`；`-1` = 不限。前端先解析一次（别发一个后端会
   静默丢掉的请求），权威那一份仍在后端（`parse_token_limit`）。 */

function outputLimitLabel(value) {
  return Number(value) === -1 ? t('settings.output.unlimited') : String(value);
}

function renderOutputPane() {
  const host = $('output-body');
  if (!host) return;
  clearNode(host);

  const card = el('div', 'reset-card');
  card.appendChild(el('div', 'section-title', t('settings.general.output')));
  card.appendChild(el('p', 'reset-lead', t('settings.output.lead')));

  const row = el('div', 'toolbar');
  const input = el('input', 'num');
  input.type = 'text';   // 允许 `5k` / `-1`：type=number 会把它们当成非法输入
  input.id = 'output-limit-input';
  input.value = String(state.maxOutputTokens);
  input.placeholder = '5000';
  row.appendChild(input);
  const save = el('button', 'btn-ghost', t('settings.output.save'));
  save.type = 'button';
  save.id = 'output-limit-save';
  save.onclick = () => { saveOutputLimit(input.value); };
  row.appendChild(save);
  card.appendChild(row);

  card.appendChild(el('div', 'hint',
    t('settings.output.current', { value: outputLimitLabel(state.maxOutputTokens) })));
  card.appendChild(el('div', 'hint', t('settings.output.note')));
  host.appendChild(card);
}

let outputSaving = false;
async function saveOutputLimit(raw) {
  if (outputSaving) return;
  const parsed = parseTokenLimit(raw, { allowUnlimited: true });
  if (parsed === null) { toast(t('settings.output.invalid')); return; }
  outputSaving = true;
  try {
    const result = await postJSON('/api/settings/output-limit', {
      max_output_tokens: parsed,
    });
    state.maxOutputTokens = result.max_output_tokens ?? parsed;
    toast(t('settings.output.saved', {
      value: outputLimitLabel(state.maxOutputTokens),
    }));
  } catch (err) {
    toast(t('settings.output.failed', { msg: err.message || err }));
  } finally {
    outputSaving = false;
  }
  renderOutputPane();
}

/* 这一轮之后该重跑的东西（契约 §5 的第 1~5 项，一项都不能少）：
     1. applyStaticTexts()（含 document.title）；
     2. 会话列表、顶部品牌行、模型/档位选择器、两个开关与它们的提示句；
     3. 输入框占位符、附件区、发送/停止按钮；
     4. 当前设置子页（记忆 / Dream / 用量 / 接口 / 工具 / 提示词 / 常规 / 重置）；
     5. 当前会话的消息流 —— 用 /api/sessions/<id> 重新渲染，这样消息里那些
        **后端产出的标签**（步数/用量/工具简报/压缩摘要）也跟着换语言。
        重放不出历史里的临时 notice 是已知代价（它们是当时那一刻的话）。
   刻意**不刷新页面**：用户明确选了"原地重渲染"这条路。 */
async function rerenderForLanguage() {
  // 0. 提示词的编辑态属于**上一门语言的那段文本**：语言一换，正在编辑的那一条
  //    与它的草稿就都失效了（留着会把中文正文填进英文卡片里）。
  //    契约 §10 本来就不做"记住编辑到哪"，这里直接收起来。
  state.promptEdit = null;
  state.promptNote = null;
  // 1. 静态字（含 <title> 与 <html lang>）
  applyStaticTexts();
  if (state.setupGateOpen) renderSetupGate();
  // 2. 侧栏与会话列表 + 品牌行 + 选择器 + 两个开关（+ 附件区那句提示）
  renderSessions();
  renderBrandAndHint();
  renderPickers();
  // 「跟随默认（接口 · 模型）」那一项的字也是文案：切语言时必须重画
  renderDreamPicker();
  renderWebToggle();
  renderMultimodalToggle();
  // 3. 输入框占位符在 index.html 上（data-i18n-placeholder，第 1 步已经填过），
  //    附件区与"发送/停止"由这几处重画/重取。
  renderAttachments();
  $('send').textContent = t('chat.send');
  $('stop').textContent = t('chat.stop');
  // 4. 当前设置子页（复用路由那条路径：它知道每个子页该调哪个渲染函数）
  if (state.view === 'settings') showSettings(state.pane);
  // 5. 当前会话的消息流
  await rerenderMessages();
}

/* 用会话视图把消息流**整条重画**一遍（不新建 DOM 之外的东西、不动输入框）。
   没有会话时画回"空对话"那一屏 —— 它也是一段文案。 */
async function rerenderMessages() {
  const host = messagesEl();
  const sessionId = state.sessionId;
  if (!sessionId) {
    host.innerHTML = emptyChatHtml();
    $('chat-title').textContent = t('chat.new_title');
    setSessionStats(null);
    return;
  }
  let data;
  try {
    data = await getJSON(`/api/sessions/${encodeURIComponent(sessionId)}`);
  } catch {
    // 拉不到就保持原样：这时候把消息流清空只会让人以为对话丢了
    return;
  }
  // 等待期间用户可能已经切到 B：A 的慢响应不能清空或覆盖 B 的消息流。
  if (state.sessionId !== sessionId) return;
  host.innerHTML = '';
  setSessionStats(data && data.stats);
  const messages = (data && data.messages) || [];
  if (!messages.length) { host.innerHTML = emptyChatHtml(); return; }
  for (const message of messages) renderStoredMessage(message);
  updateUsageHint(data && data.stats ? state.sessionStats.usage : (data && data.usage) || null);
  state.stick = true;
  scrollToBottom();
}

/* ── 工具（联网）───────────────────────────────────────── */

function toolsPaneHtml(data) {
  const warning = (!data.has_tavily_key && data.search_backend === 'tavily')
    ? t("tools.missing_key_chip")
    : '';
  const backends = (data.backends || []).map((item) => `
    <div class="theme-option ${item.id === data.search_backend ? 'active' : ''}"
         data-backend="${escapeHtml(item.id)}">
      <div><div class="t-name">${escapeHtml(item.label)}${
        item.id === 'tavily' && data.search_backend === 'tavily' ? ' ' + warning : ''}</div>
      <div class="t-desc">${escapeHtml(item.notes)}</div></div>
    </div>`).join('');

  const keyHint = data.has_tavily_key
    ? t("tools.current_key", { masked: escapeHtml(data.masked_tavily_key) })
    : t("tools.no_key");

  return t("tools.pane_html", { enabledSelected: data.enabled ? 'selected' : '', disabledSelected: data.enabled ? '' : 'selected', backends: backends, keyHint: keyHint, searchCount: data.search_count, maxFetchChars: data.max_fetch_chars, privateOff: data.allow_private_hosts ? '' : 'selected', privateOn: data.allow_private_hosts ? 'selected' : '', clearDisabled: data.has_tavily_key ? '' : 'disabled' });
}

function readToolsForm(fallback) {
  const picked = $('tools-body').querySelector('.theme-option.active');
  return {
    enabled: $('tools-enabled').value === '1',
    search_backend: picked ? picked.dataset.backend : (fallback.search_backend || 'bing'),
    search_count: $('tools-count').value || 5,
    max_fetch_chars: $('tools-maxchars').value || 8000,
    allow_private_hosts: $('tools-private').value === '1',
    // 刻意**不带** tavily_api_key：key 只在**明确动作**（回车 / 点「保存」）时提交。
    // 否则"粘了 key 但没回车、只是顺手改了别的字段"会把它一并存掉 ——
    // 那正是"失焦等于没填"这条规矩要防的事。
  };
}

/* 回车 = 按「保存」。
   **对着空框按回车等于没按**：回车永远不会清掉东西 ——
   想重打一遍却手滑按了回车，不该把已存的 key 弄没。
   清空是「清空」按钮的专职（见 clearTavilyKey）。 */
async function commitTavilyKey() {
  const field = $('tools-tavily-key');
  if (!field) return;
  if (!(field.value || '').trim()) return;   // 空框回车：什么都不做
  await saveToolsPane();
}

/* 「清空」：抹掉已保存的 key。**只有这个按钮会清 key**。 */
async function clearTavilyKey() {
  if (!confirm(t("tools.clear_confirm"))) return;
  const status = $('tools-status');
  try {
    const saved = await postJSON('/api/tools/save', { clear_tavily_key: true });
    state.tools = (saved && saved.web) || state.tools;
    toast(t("tools.cleared"));
    await loadToolsPane();
    const fresh = $('tools-status');
    if (fresh) fresh.textContent = t("tools.cleared_check");
  } catch (err) {
    if (status) status.textContent = t("tools.clear_failed", { msg: err.message || err });
  }
}

/* 保存（按钮）。商定语义：**覆盖式保存框里的非空字段**。
   key 也走这条路：填了就存（否则"粘了 key、点了保存、却没生效"是个很自然的坑），
   但**空值不会清 key** —— 清空是「清空」按钮的专职。
   一个显示不了 key 的按钮，不该因为它是空的就把用户的 key 删掉。 */
async function saveToolsPane({ silent = false } = {}) {
  const payload = readToolsForm(state.tools || {});
  const keyField = $('tools-tavily-key');
  const typedKey = keyField ? (keyField.value || '').trim() : '';
  // 自动保存 / 试搜前的静默保存绝不捎带 key；只有 Enter 或「保存」走非 silent 路径。
  if (!silent && typedKey) payload.tavily_api_key = typedKey;
  const saved = await postJSON('/api/tools/save', payload);
  state.tools = (saved && saved.web) || state.tools;
  if (silent) {
    const status = $('tools-status');
    if (status) status.textContent = t("tools.autosaved");
    // 重新拉一次配置，让"缺 key"之类提示立刻更新；不整页重绘，避免抢焦点
    state.config = await getJSON('/api/config').catch(() => state.config);
    renderWebToggle();
    return state.tools;
  }
  toast(t("vault.saved_short"));
  await loadToolsPane();
  return state.tools;
}

async function loadToolsPane() {
  // 注意：/api/tools 返回的是 { web: {...} }，别把外层当成配置本身
  // （踩过：忘了取 .web，结果总开关显示"关闭"、后端列表和数字输入全是空的）
  const payload = await getJSON('/api/tools');
  const data = (payload && payload.web) || {};
  state.tools = data;
  $('tools-body').innerHTML = toolsPaneHtml(data);

  const autoSave = () => {
    saveToolsPane({ silent: true }).catch((err) => {
      const status = $('tools-status');
      if (status) status.textContent = t("tools.save_failed", { msg: err.message || err });
    });
  };

  for (const el of $('tools-body').querySelectorAll('.theme-option')) {
    el.onclick = () => {
      for (const other of $('tools-body').querySelectorAll('.theme-option')) {
        other.classList.toggle('active', other === el);
      }
      autoSave();   // 选了后端就立刻存，别让用户以为"选了就算数了"
    };
  }
  // 其余字段：change 在文本框失焦、下拉立即触发 —— 都发生在用户"改完"之后，
  // 所以不会一边打字一边重绘抢焦点。
  // **tavily key 不在这里**：它只认回车（见下面那个 keydown）。
  for (const el of $('tools-body').querySelectorAll(
    '#tools-enabled, #tools-count, #tools-maxchars, #tools-private')) {
    el.addEventListener('change', autoSave);
  }
  $('tools-tavily-key').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();   // 别让回车触发任何默认行为
    commitTavilyKey();
  });
  $('tools-save').onclick = () => saveToolsPane();
  $('tools-clear-key').onclick = () => clearTavilyKey();
  $('tools-test').onclick = async (event) => {
    const button = event.currentTarget;
    const query = $('tools-test-query').value.trim();
    if (!query) { toast(t("tools.test_need_query")); return; }
    button.disabled = true;
    $('tools-test-out').textContent = t("tools.test_searching");
    try {
      // 先保存，否则试的是"上一次的配置"
      await saveToolsPane({ silent: true });
      const result = await postJSON('/api/tools/test', { query, count: 3 });
      const lines = (result.results || []).map(
        (item, index) => `${index + 1}. ${item.title}\n   ${item.url}`);
      $('tools-test-out').innerHTML = [
        result.note ? `⚠ ${escapeHtml(result.note)}` : t("tools.test_backend", { backend: escapeHtml(result.backend) }),
        ...lines.map((line) => escapeHtml(line)),
      ].join('<br>') || t("tools.test_no_results");
    } catch (err) {
      $('tools-test-out').textContent = t("tools.test_failed", { msg: err.message || err });
    } finally {
      button.disabled = false;
    }
  };
}

/* ── 提示词（契约 docs/13-prompts-page.md）───────────────
   把"我们到底跟模型说了什么"摊开：全部提示词（含后台整理用的那两条）与全部
   工具定义。数据来自只读的 GET /api/prompts —— 它按**当前语言**取好了一切
   （提示词原文、工具描述、`when` / `condition` 那几句人话）。

   文案分工（契约 §4）：服务端给的那几个字符串**这里一个字都不翻**（它们已经
   是当前语言了，与 vault 页的 `problems` 同一条规矩）；前端只翻自己的外壳 ——
   页首说明、状态徽标、字符数、参数表头、复制按钮。 */

/* 状态徽标的文案。三种值见契约 §2.2，**这里不解释语义**：
   它只回答"按当前配置，这条现在会不会被用上"。 */
function promptStateLabel(state) {
  if (state === 'off') return t('prompts.state.off');
  if (state === 'session') return t('prompts.state.session');
  return t('prompts.state.on');
}

/* 徽标颜色只跟状态有关：生效=ok / 未注册=err / 会话级=warn。
   契约里 `off` 的界面词是「未注册」（不是"关闭"）—— 它说的是"现在不会发出去"。 */
function promptStateChip(state) {
  const kind = state === 'off' ? 'err' : (state === 'session' ? 'warn' : 'ok');
  return el('span', `chip ${kind} prompts-state`, promptStateLabel(state));
}

/* 服务端给的字段可能是 undefined / null（半截负载）。直接 `.trim()` 会当场
   TypeError，而这一页是"只读地看一眼"，不该因为一个字段缺失整页空白。 */
function textOf(value) {
  return typeof value === 'string' ? value : '';
}

/* 「▸ 原文」那种折叠块：默认折叠，点标题整行开合（与 OCR 文字块同一个形状）。
   **刻意用字符串直接建 DOM**（不是 innerHTML）—— 提示词原文里有 `{}`、`**`、
   `<` 这些东西，它们都是**正文**，不是模板。 */
function promptDetails(summary, bodyNode) {
  const box = el('div', 'prompt-details collapsed');
  const head = el('button', 'pd-head');
  head.setAttribute('type', 'button');
  const chev = el('span', 'chev', '▸');
  head.appendChild(chev);
  head.appendChild(el('span', 'pd-label', summary));
  head.onclick = () => {
    const open = box.classList.toggle('collapsed');
    chev.textContent = open ? '▸' : '▾';
  };
  box.appendChild(head);
  const body = el('div', 'pd-body');
  body.appendChild(bodyNode);
  box.appendChild(body);
  return box;
}

/* 复制按钮：用**本次卡片子树**里的节点，不用 `$()`（见 dom-stub README 那条：
   stub 的 getElementById 是"边用边造"的，按 id 取会取到另一个节点）。 */
function copyButton(getText, card) {
  const button = el('button', 'btn-ghost prompts-copy', t('prompts.copy'));
  button.setAttribute('type', 'button');
  button.onclick = async () => {
    const ok = await copyText(getText());
    if (!ok) {
      // 失败必须说一句人话，不能静默（契约 §5 第 5 条）
      toast(t('prompts.copy_failed'));
      return;
    }
    toast(t('prompts.copied'));
    if (!card) return;
    button.textContent = t('prompts.copied');
    setTimeout(() => { button.textContent = t('prompts.copy'); }, 1600);
  };
  return button;
}

/* 参数名 → 类型。parameters.properties 里每一项都有 type（没有就如实写
   "any" —— 那正是"模型看到的那一份"里写的）。 */
function paramType(spec) {
  if (!spec || typeof spec !== 'object') return 'any';
  const type = spec.type;
  if (typeof type === 'string' && type) return type;
  if (Array.isArray(type) && type.length) return type.join(' | ');
  return 'any';
}

/* ── 可编辑（契约 docs/14-editable-prompts.md §8）────────────
   这一页从"只读地看一眼"变成"能改"，所以多了三件事：一条自己的编辑态、
   那条"已修改"的标记与它旁边的「复原」、以及保存之后**必须说清楚后果**的
   那几行字（warnings 是成功但有代价，不是错误）。 */

/* 这一份负载是哪门语言的。**以服务端回的 `language` 为准**（页面上的正文就是
   它那门语言的），字段缺失时才退回界面自己的那一份镜子 —— 拿错语言的 id 去写，
   正是"改中文把英文带坏"那条硬要求的反面。 */
function promptsLanguageOf(data) {
  return textOf(data && data.language) || state.language || 'zh';
}

/* 这门语言改过几条。优先用 `overrides`（契约 §4.1 给的就是它，两门语言各有清单）；
   它缺了才退回 `summary.overridden`（那个只数当前语言）。 */
function promptOverrideCount(data, language) {
  const overrides = (data && data.overrides) || {};
  const list = overrides[language];
  if (Array.isArray(list)) return list.length;
  const summary = (data && data.summary) || {};
  if (language === promptsLanguageOf(data) && typeof summary.overridden === 'number') {
    return summary.overridden;
  }
  return 0;
}

/* 这一页说话时用的语言名：中文界面「中文 / 英文」，英文界面「Chinese / English」。
   **刻意不用 languageLabel()** —— 那是"界面语言选项"的名字（中文界面里写作
   「中文 / English」），而这里说的是"哪一门提示词"，中文界面上写「英文」才顺。
   两个名字各是一个 key，写死成分支而不是拼字符串：契约 §8.1 那条"每个 key 都要有
   引用点"的守卫扫的正是 `t('…')` 这一种写法，拼出来的 key 它会看不见。 */
function promptLanguageName(language) {
  if (language === 'zh') return t('prompts.overrides_name.zh');
  if (language === 'en') return t('prompts.overrides_name.en');
  return languageLabel(language);   // 没见过的语言：退回通用那份标签
}

/* 页首那行"两门语言各改了几条"。
   **两门都要写出来，0 也写** —— 用户最担心的就是"改中文把英文带坏了"，
   那就让他在同一屏里看到英文那一门的数字。语言清单与顺序照 I18N.languages
   （与设置 → 语言 那一页同一份），不跟着响应里键的顺序走：少一门语言时
   这里会如实写出 0，而不是整段消失。 */
function promptOverridesText(data) {
  const parts = (I18N.languages || []).map((item) => t('prompts.overrides_lang', {
    name: promptLanguageName(item.id),
    n: promptOverrideCount(data, item.id),
  }));
  return t('prompts.overrides_summary', { parts: parts.join(' · ') });
}

/* 按 id 找那一条（当前负载里的）。找不到就返回 null —— 调用方据此退回整页重画。 */
function promptItem(id) {
  return itemsOf(state.prompts && state.prompts.groups)
    .find((item) => textOf(item && item.id) === id) || null;
}

/* 卡片上那行 warn（契约 §8 第 1 条最后一段）：`warnings` 与 `placeholders_missing`
   合起来只占**一行** —— 这是"保存成功了，但有后果"，所以用 warn 色，不是错误色。
   服务端那句 warning 里通常已经把缺了哪个占位符写进去了；`placeholders_missing`
   只补它**没提到**的那些，免得同一件事在一行里说两遍。 */
function promptWarnText(item) {
  const warnings = ((item && item.warnings) || []).map((line) => String(line));
  const joined = warnings.join(' · ');
  const missing = ((item && item.placeholders_missing) || []).map((name) => String(name));
  const extra = missing.filter((name) => !joined.includes(`{${name}}`));
  if (extra.length) {
    warnings.push(t('prompts.placeholders_missing', {
      list: extra.map((name) => `{${name}}`).join(', '),
    }));
  }
  return warnings.join(' · ');
}

/* 「编辑」按下去之后那张卡片里的东西：一个 textarea + 三句提示 + 保存 / 取消。
   三句提示是契约 §8 第 1 条点名的（各自一个 key），缺一句这段路就不完整：
   ① 哪些占位符会被填（别删）、② 缓存会重建（一次性的钱）、③ 改过的条目会冻结。 */
function promptEditorNode(item) {
  const id = textOf(item && item.id);
  const box = el('div', 'pc-editor');
  const area = el('textarea', 'pc-editor-area');
  area.value = state.promptEdit ? textOf(state.promptEdit.draft) : textOf(item && item.text);
  // 实时把草稿存回 state：保存失败要重画这一张卡片时，用户敲的字不能丢
  area.oninput = () => {
    if (state.promptEdit && state.promptEdit.id === id) state.promptEdit.draft = area.value;
  };
  box.appendChild(area);

  const placeholders = ((item && item.placeholders) || []).map((name) => String(name));
  const hints = el('div', 'pc-hints');
  hints.appendChild(el('div', 'pc-hint', t('prompts.editor_hint_placeholders',
    { list: placeholders.length ? placeholders.join(', ') : '—' })));
  hints.appendChild(el('div', 'pc-hint', t('prompts.editor_hint_cache')));
  hints.appendChild(el('div', 'pc-hint', t('prompts.editor_hint_frozen')));
  box.appendChild(hints);

  // 后端 400 的那句人话**原样**留在这儿（不关编辑器、不自己编一句"保存失败"）
  const error = state.promptEdit ? textOf(state.promptEdit.error) : '';
  if (error) box.appendChild(el('div', 'pc-editor-error', t('prompts.save_failed', { error })));

  const actions = el('div', 'pc-actions');
  const cancel = el('button', 'btn-ghost pc-cancel', t('prompts.cancel'));
  cancel.setAttribute('type', 'button');
  cancel.onclick = () => cancelPromptEdit(id);
  const save = el('button', 'btn-primary pc-save', t('prompts.save'));
  save.setAttribute('type', 'button');
  // 把**这个** textarea 交给保存：它是这一屏上用户真正在敲的那个节点
  save.onclick = () => savePromptEdit(id, area);
  actions.appendChild(save);
  actions.appendChild(cancel);
  box.appendChild(actions);
  return box;
}

/* 这一张卡片的内容。抽成"往一个已存在的 card 里画"是为了能**只重画这一张**：
   开/关编辑器与保存失败都不该把整页重画一遍 —— 那会把分组的开合重置回默认
   （契约 §10：折叠状态是零 JS 状态的，我们不记它），于是用户正编辑的那一条
   会被自己所在的折叠组收起来，看不见了。 */
function fillPromptCard(card, item) {
  const id = textOf(item && item.id);
  const overridden = !!(item && item.overridden);
  const editing = !!state.promptEdit && state.promptEdit.id === id;

  const head = el('div', 'pc-head');
  // 标题就是 id（标识符不翻译，契约 §2.1）
  head.appendChild(el('div', 'pc-title', id || textOf(item && item.title)));
  head.appendChild(promptStateChip(item && item.state));
  // 「已修改」标记：这一条现在用的是用户写的那一版（不是内置原文）
  if (overridden) head.appendChild(el('span', 'chip warn pc-edited', t('prompts.edited')));
  // 字符数缺失时**不写 0**（那是在替一份半截负载下结论），直接不写这一格
  if (item && typeof item.chars === 'number') {
    head.appendChild(el('span', 'pc-chars', t('prompts.chars', { n: item.chars })));
  }
  // 编辑态里这两个按钮收起来：正在编辑时它们只会让人以为"还能再开一个编辑器"
  if (!editing) {
    const edit = el('button', 'btn-ghost pc-edit', t('prompts.edit'));
    edit.setAttribute('type', 'button');
    edit.onclick = () => startPromptEdit(id, card);
    head.appendChild(edit);
    if (overridden) {
      const revert = el('button', 'btn-ghost pc-revert', t('prompts.revert'));
      revert.setAttribute('type', 'button');
      revert.onclick = () => askRevertPrompt(id);
      head.appendChild(revert);
    }
  }
  head.appendChild(copyButton(() => textOf(item && item.text), card));
  card.appendChild(head);

  const when = textOf(item && item.when);
  if (when) card.appendChild(el('div', 'pc-when', when));
  const condition = textOf(item && item.condition);
  if (condition) card.appendChild(el('div', 'pc-cond', condition));
  const placeholders = (item && Array.isArray(item.placeholders)) ? item.placeholders : [];
  if (placeholders.length) {
    card.appendChild(el('div', 'pc-ph', t('prompts.placeholders', { list: placeholders.join(', ') })));
  }

  const warn = promptWarnText(item);
  if (warn) card.appendChild(el('div', 'pc-warning', warn));

  if (editing) {
    card.appendChild(promptEditorNode(item));
    return;
  }
  const pre = el('pre', 'prompt-text', textOf(item && item.text));
  // 被改过时，标题改叫「当前生效」—— 它旁边就要摆上内置原文好对比（契约 §8 第 1 条）
  card.appendChild(promptDetails(overridden ? t('prompts.current_text') : t('prompts.text'), pre));
  if (overridden) {
    card.appendChild(promptDetails(t('prompts.builtin_text'),
      el('pre', 'prompt-text prompt-builtin', textOf(item && item.default_text))));
  }
}

/* 一条提示词。
   `data-prompt-id` 是**稳定钩子**（契约 §5 第 4 条）：按 id 定位这一条。 */
function promptCard(item) {
  const card = el('div', 'prompt-card');
  card.dataset.promptId = textOf(item && item.id);
  fillPromptCard(card, item);
  return card;
}

/* 只重画这一张卡片（不是整页）。找不到那一条（负载换过了）就退回整页重画。 */
function updatePromptCard(card, id) {
  const item = promptItem(id);
  if (!card || !item) { renderPrompts(); return; }
  clearNode(card);
  fillPromptCard(card, item);
}

/* 「编辑」：把这一条切成编辑器。**不整页重画**（理由见 fillPromptCard 上那段注释）。 */
function startPromptEdit(id, card) {
  const item = promptItem(id);
  if (!item) { renderPrompts(); return; }
  const previous = state.promptEdit ? state.promptEdit.id : null;
  state.promptEdit = { id, draft: textOf(item.text), error: null };
  // 收起来的那一条、以及要变成编辑器的那一条，都**按 id 在页面上当场找**：
  // 闭包里抓住的 `card` 是渲染那一刻的节点，而这中间可能已经有过一次整页重画
  // （切语言、保存之后都会重画）—— 往一个已经不在页面上的节点里画等于没画
  // （dom-stub README 里那条"按 id 取到的必须与页面上是同一个节点"就是这件事）。
  if (previous && previous !== id) {
    const other = promptCardById(previous);
    if (other) updatePromptCard(other, previous);
  }
  updatePromptCard(promptCardById(id) || card, id);
}

/* 「取消」：把这一条退回非编辑态，草稿丢掉（用户自己按的取消）。 */
function cancelPromptEdit(id) {
  if (state.promptEdit && state.promptEdit.id === id) state.promptEdit = null;
  const card = promptCardById(id);
  if (card) updatePromptCard(card, id);
  else renderPrompts();
}

/* 页面上那一张卡片（按 data-prompt-id 找）。重画会换节点，所以每次当场找。 */
function promptCardById(id) {
  const host = $('prompts-body');
  if (!host) return null;
  let found = null;
  const visit = (node) => {
    if (found || !node || !node.children) return;
    for (const child of node.children) {
      if (child.dataset && child.dataset.promptId === id) { found = child; return; }
      visit(child);
    }
  };
  visit(host);
  return found;
}

/* 「保存」。**只发这一次请求**，成不成以后端为准：
   * 200 → 关掉编辑器、把那行结论留在页首，然后**重新拉一次** —— 覆盖之后
     `overridden` / `warnings` / 两门语言的 `overrides` 计数都变了，靠本地
     改一个布尔值去猜，猜错了就是"界面说改了、其实没改"；
   * 400 → **原话**留在编辑器里，编辑器不关（改完还能再试），草稿一个字不丢。 */
async function savePromptEdit(id, area) {
  const edit = state.promptEdit;
  if (!edit || edit.id !== id) return;
  // 以**这一屏上那个 textarea** 为准（它才是用户敲的东西）；拿不到才退回草稿
  const text = area && typeof area.value === 'string' ? area.value : textOf(edit.draft);
  edit.draft = text;
  edit.error = null;
  const language = promptsLanguageOf(state.prompts);
  try {
    const result = await postJSON('/api/prompts/update', { language, id, text });
    // 只关闭发起这次保存的编辑器。等待期间新开的 B（即使 id 相同）有自己的
    // 未保存草稿，不能被 A 的完成响应清掉；后面的整页刷新会照常保留它。
    if (state.promptEdit === edit) state.promptEdit = null;
    // `stored: false` = 这段文本与内置一模一样，所以没存成自定义（契约 §4.2）：
    // 那不是失败，但**必须说出来** —— 否则用户会一直找不到那条「已修改」标记。
    state.promptNote = (result && result.stored === false)
      ? { kind: 'warn', text: t('prompts.save_identical') }
      : { kind: 'ok', text: t('prompts.saved') };
    await loadPrompts();
  } catch (err) {
    const message = String((err && err.message) || err);
    if (state.promptEdit === edit) state.promptEdit.error = message;
    const card = promptCardById(id);
    if (card) updatePromptCard(card, id);
    else renderPrompts();
  }
}

/* 「复原」这一条：**二次确认**，而且确认文案里必须写明复原的是**当前语言**这条
   （契约 §8 第 1 条）—— 用户心里的问题是"会不会把另一门语言也带走"。 */
function askRevertPrompt(id) {
  const language = promptsLanguageOf(state.prompts);
  const message = t('prompts.revert_confirm', { id, language: promptLanguageName(language) });
  if (!confirm(message)) return undefined;
  return revertPrompts(language, id);
}

/* 「把这门语言的提示词全部复原」：写清语言名与**条数**（契约 §8 第 2 条）。 */
function askRevertAllPrompts(language, count) {
  if (!count) return undefined;   // 没改动时那个按钮是禁用的，这里再兜一道
  const message = t('prompts.revert_all_confirm', {
    language: promptLanguageName(language),
    n: count,
  });
  if (!confirm(message)) return undefined;
  return revertPrompts(language, null);
}

/* 复原（`id` 缺省 = 这门语言的全部）。成功之后照旧**重新拉一次**：这一页的
   标记、警告与两门语言的计数都变了，本地猜不如问服务端。 */
async function revertPrompts(language, id) {
  try {
    const result = await postJSON('/api/prompts/revert', id ? { language, id } : { language });
    const cleared = result && typeof result.cleared === 'number' ? result.cleared : 0;
    state.promptEdit = null;
    state.promptNote = id
      ? { kind: 'ok', text: t('prompts.revert_done', { id }) }
      : { kind: 'ok', text: t('prompts.revert_all_done', { language: promptLanguageName(language), n: cleared }) };
    await loadPrompts();
  } catch (err) {
    state.promptNote = { kind: 'err', text: t('prompts.revert_failed', {
      error: String((err && err.message) || err),
    }) };
    renderPrompts();
  }
}

/* 「把这门语言的提示词全部复原」（契约 §8 第 2 条）。
   它在**页首右侧**，与摘要那几行同一行（用户要求：原来夹在分节标题与分组之间，
   位置不显眼）。它是 `btn-danger`（红色）：一次动一整门，属于危险动作。
   这门语言一条都没改时它是**禁用**的（没有东西可复原），并说明为什么 ——
   一个能点、点了却什么都不做的按钮比没有更糟。 */
function promptsRevertAllNode(data) {
  const language = promptsLanguageOf(data);
  const count = promptOverrideCount(data, language);
  const row = el('div', 'prompts-actions');
  const button = el('button', 'btn-danger prompts-revert-all', t('prompts.revert_all'));
  button.setAttribute('type', 'button');
  button.dataset.language = language;   // 稳定钩子：图上/自检里认"这门语言"是哪个
  button.disabled = count === 0;
  if (count === 0) button.title = t('prompts.revert_all_none');
  button.onclick = () => askRevertAllPrompts(language, count);
  row.appendChild(button);
  return row;
}

/* 页首那几行 + 右侧的「全部复原」。左边是可伸缩的文字列（三行统计），
   右边是固定宽度的按钮 —— 窄屏时按钮不会把文字挤成一条竖线（`.prompts-head`
   在 style.css 里 flex-wrap，按钮整块换到下一行）。 */
function promptsHeadNode(lines, data) {
  const head = el('div', 'prompts-head');
  const text = el('div', 'prompts-head-text');
  for (const line of lines) text.appendChild(line);
  head.appendChild(text);
  head.appendChild(promptsRevertAllNode(data));
  return head;
}

/* 覆盖文件坏了（`store_error` 非空）时页首那条醒目警告：文件读不出来，
   现在用的是**内置原文**（契约 §8 第 4 条）。它必须比别的东西都靠前 ——
   这一页上的每一段正文这时候都不是用户以为的那一份。 */
function promptsStoreErrorNode(data) {
  const error = textOf(data && data.store_error);
  if (!error) return null;
  return el('div', 'prompts-store-error', t('prompts.store_error', { error }));
}

/* 上一次保存 / 复原的结果（留在页首，见 state.promptNote 那段注释）。 */
function promptsNoteNode() {
  const note = state.promptNote;
  if (!note) return null;
  return el('div', `prompt-note ${note.kind || 'ok'}`, textOf(note.text));
}

function makeTable(headers, rows) {
  const table = el('table', 'prompt-params');
  const headRow = el('tr');
  for (const header of headers) headRow.appendChild(el('th', '', header));
  table.appendChild(headRow);
  for (const cells of rows) {
    const row = el('tr');
    for (const cell of cells) row.appendChild(el('td', '', cell));
    table.appendChild(row);
  }
  return table;
}

/* 一个工具。`data-tool-id` 与提示词的 `data-prompt-id` 是同一类钩子。 */
function toolCard(item) {
  const name = textOf(item && item.name) || textOf(item && item.id);
  const card = el('div', 'prompt-card tool-card');
  card.dataset.toolId = name;

  const head = el('div', 'pc-head');
  head.appendChild(el('div', 'pc-title tool-name', name));
  head.appendChild(promptStateChip(item && item.state));
  card.appendChild(head);

  const condition = textOf(item && item.condition);
  if (condition) card.appendChild(el('div', 'pc-cond', condition));
  const description = textOf(item && item.description);
  if (description) card.appendChild(el('p', 'tool-desc', description));

  const parameters = (item && item.parameters && typeof item.parameters === 'object')
    ? item.parameters : {};
  const properties = (parameters.properties && typeof parameters.properties === 'object')
    ? parameters.properties : {};
  const required = Array.isArray(item && item.required)
    ? item.required.map((key) => String(key))
    : (Array.isArray(parameters.required) ? parameters.required.map((key) => String(key)) : []);
  const rows = Object.keys(properties).map((key) => {
    const spec = properties[key] || {};
    const note = textOf(spec && spec.description);
    return [key, paramType(spec), required.includes(key) ? t('prompts.required_yes') : '—', note];
  });

  card.appendChild(el('div', 'pc-sub', t('prompts.params')));
  card.appendChild(rows.length
    ? makeTable([t('prompts.col_name'), t('prompts.col_type'),
      t('prompts.col_required'), t('prompts.col_desc')], rows)
    : el('div', 'pc-when', t('prompts.no_params')));

  let raw;
  try {
    raw = JSON.stringify(parameters, null, 2);
  } catch {
    raw = String(parameters);
  }
  card.appendChild(promptDetails(t('prompts.raw_json'), el('pre', 'prompt-text', raw)));
  return card;
}

/* 页面上按状态各有多少条（提示词 + 工具一起数）。这是**界面自己数出来的**，
   与第二行那个 `{on}` 不是一回事：那个用的是服务端的 `summary.tools_on`
   （它只数工具）。两个数字口径不同，两个函数各自管一句话，别混着用。 */
function promptTally(items) {
  let on = 0;
  let session = 0;
  let off = 0;
  for (const item of items) {
    if ((item && item.state) === 'off') off += 1;
    else if ((item && item.state) === 'session') session += 1;
    else on += 1;
  }
  return { on, session, off, total: items.length };
}
/* 页首那行总统计（契约 §5 第 2 条：分组折起来之后**保留** —— 全折叠时它是
   唯一一眼能看全的东西）。口径与分组 summary 那条**刻意不同**：「生效 / 未注册」
   在这里总是写出来（总账上 0 也是有意义的读数），会话级只在非 0 时写。
   三段之间统一用 ` · ` 连 —— 分隔符**不写进文案**（`prompts.tally_*` 里只有
   "生效 {n} 条"），否则分组那边"为 0 的整段不显示"就没法干净地省掉
   中间 / 开头 / 末尾的那一个分隔符。 */
function promptTallyText(tally) {
  return [
    t('prompts.tally_on', { n: tally.on }),
    t('prompts.tally_off', { n: tally.off }),
    tally.session ? t('prompts.tally_session', { n: tally.session }) : '',
  ].filter((part) => part !== '').join(' · ');
}

/* 分组 summary 里的状态统计：**为 0 的那几种不显示**（契约 §5 第 2 条）——
   那是给"这一组里有些什么"扫一眼用的，不是总账。空分组得到空串，
   调用方据此不画这一段（不画"生效 0 条"那种噪音）。 */
function groupTallyText(items) {
  const tally = promptTally(items);
  return [
    tally.on ? t('prompts.tally_on', { n: tally.on }) : '',
    tally.off ? t('prompts.tally_off', { n: tally.off }) : '',
    tally.session ? t('prompts.tally_session', { n: tally.session }) : '',
  ].filter((part) => part !== '').join(' · ');
}

/* 一个分组（提示词 5 组 / 工具 2 组）。返回**一个整块的卡片**，不是一串散节点。

   它是一个**原生 `<details>`，默认不带 `open`**（契约 §5 第 2 条；用户明确要求：
   就算每条提示词的原文都折着，光分组标题与说明加起来也把页面拉到八千多像素）。
   于是 summary 就是那张横条：标题 + 条目数 + **本组的状态统计**；
   说明句与全部条目都在折叠体里。开合交给浏览器 —— 这里**一行 JS 状态都不加**，
   所以分组之间天然互不影响（没有手风琴），也不做"全部展开"（那等于把长度问题
   请回来）。`data-prompt-id` / `data-tool-id` 依旧在里面：折的是可见性，不是元素。 */
function promptsGroupNode(group, kind) {
  const items = (group && Array.isArray(group.items)) ? group.items : [];
  const card = el('details', 'prompts-group');
  const head = el('summary', 'pg-head');
  head.appendChild(el('span', 'pg-title', textOf(group && group.title)));
  head.appendChild(el('span', 'pg-note-count',
    t('prompts.group_count', { n: items.length })));
  const tally = groupTallyText(items);
  if (tally) head.appendChild(el('span', 'pg-tally', tally));
  card.appendChild(head);

  const body = el('div', 'pg-body');
  const note = textOf(group && group.note);
  if (note) body.appendChild(el('div', 'pg-note', note));
  if (!items.length) {
    body.appendChild(el('div', 'pc-when', t('prompts.none')));
  } else {
    for (const item of items) {
      body.appendChild(kind === 'tools' ? toolCard(item) : promptCard(item));
    }
  }
  card.appendChild(body);
  return card;
}

function emptyPrompts() {
  return { language: '', summary: {}, groups: [], tools: [] };
}

/* 页首那段说明：`prompts.intro` 里带 `<strong>` / `<code>`（契约 §4 明说这个 key
   可以走 data-i18n-html），所以这里只能走 innerHTML —— 用 el() 的 textContent
   会把标签原样显示出来（真浏览器里拍到过 `<strong>真正发给模型的东西</strong>`）。
   **前提与 index.html 上的 data-i18n-html 完全一样**：这段 HTML 全是我们自己写的
   文案，里面一个外部数据都没有（提示词原文、工具描述那些一律走 textContent）。 */
function promptsIntroNode() {
  const node = el('p', 'prompts-intro');
  node.innerHTML = t('prompts.intro');
  return node;
}

/* 整页的两段（提示词 / 工具定义），**返回节点数组**而不是一棵挂在临时 div 下
   的树：DOM 没有"把子节点整批搬家"的写法（`appendChild` 会**移动**节点），
   所以拼一个临时容器再搬反而容易写出一个边搬边改的循环。数组最直接。 */
function promptsBodyNodes(data) {
  const nodes = [promptsIntroNode()];

  // 提示词：整页跟随界面语言，所以"这一份是哪来的"必须写在页面上
  const groups = Array.isArray(data && data.groups) ? data.groups : [];
  const tools = Array.isArray(data && data.tools) ? data.tools : [];
  nodes.push(el('div', 'section-title', t('prompts.section_prompts')));
  if (!groups.length) {
    nodes.push(el('div', 'pc-when', t('prompts.none')));
  } else {
    for (const group of groups) nodes.push(promptsGroupNode(group, 'prompts'));
  }

  nodes.push(el('div', 'section-title prompts-tools-title', t('prompts.section_tools')));
  if (!tools.length) {
    nodes.push(el('div', 'pc-when', t('prompts.none')));
  } else {
    for (const group of tools) nodes.push(promptsGroupNode(group, 'tools'));
  }
  return nodes;
}

/* 数据是**跟着语言取的**（`/api/prompts` 回的那一份就是当前语言那一套），
   所以切语言要重新拉一次，而不是拿旧数据重画 —— 否则页面上会中英混排。
   拉失败如实说（不静默留旧文案）。

   `promptsSeq`（每拉一次 +1）是给**并发**用的：切语言那一刻可能正好有一次在飞
   （`showSettings` 里那句没有 await，用户还能连点两次切语言）。没有它的话，
   先发出去的那次回来得晚就会把**新语言的**负载覆盖成旧语言的，页面上出现
   中英混排 —— 而这种"看起来换了一半"的错最难查。只认最新那一次的响应。 */
let promptsSeq = 0;
async function loadPrompts() {
  const host = $('prompts-body');
  if (!host) return undefined;
  promptsSeq += 1;
  const seq = promptsSeq;
  showPromptsLoading();
  let data;
  try {
    data = await getJSON('/api/prompts');
  } catch (err) {
    if (seq !== promptsSeq) return undefined;   // 已经有更新的一次在飞了，别覆盖它
    state.prompts = null;
    // 拉不到这一页的负载时，编辑态也一起作废：留着一个指向"已经画不出来的那一条"
    // 的草稿，下一次进这一页会凭空出现一个编辑器。
    state.promptEdit = null;
    clearNode(host);
    host.appendChild(promptsIntroNode());
    host.appendChild(el('div', 'pc-when',
      t('prompts.load_error', { error: String((err && err.message) || err) })));
    return undefined;
  }
  if (seq !== promptsSeq) return undefined;     // 同上：迟到的旧响应直接丢掉
  state.prompts = data;
  renderPrompts();
  return state.prompts;
}

/* 第一次进来时那句「加载中…」（契约 §4 要求有这个 key）。它只在**页面上还什么都
   没有**时出现 —— 切语言那种"重新拉一次"的情况下，把已经画好的内容擦掉换成一句
   "加载中"只会闪一下，观感比留着旧的那一版更差。 */
function showPromptsLoading() {
  const host = $('prompts-body');
  if (!host || host.textContent.trim() !== '') return;
  host.appendChild(el('div', 'pc-when', t('prompts.loading')));
}

function renderPrompts() {
  const host = $('prompts-body');
  if (!host) return;
  const data = state.prompts || emptyPrompts();
  clearNode(host);
  // 覆盖文件坏了就说在最前面（契约 §8 第 4 条）：这一页上每一段正文这时候
  // 都不是用户以为的那一份，藏在下面是骗人。
  const storeError = promptsStoreErrorNode(data);
  if (storeError) host.appendChild(storeError);
  const summary = (data && data.summary) || {};
  // 摘要行的两个数字**用服务端给的**（`/api/prompts` 的 summary）：它数的是
  // "列出来了多少条"，比界面自己再数一遍可靠。缺了就退回本地长度。
  const prompts = summary.prompts === undefined
    ? countItems(data.groups) : summary.prompts;
  const tools = summary.tools === undefined
    ? countItems(data.tools) : summary.tools;
  const toolsOn = summary.tools_on === undefined
    ? promptTally(toolsOf(data)).on : summary.tools_on;
  // 页首那一块：左边三行统计，右边「把这门语言的提示词全部复原」（红色，
  // 见 promptsHeadNode）。三行**合成一组**塞进去，这样它才和按钮同一行。
  host.appendChild(promptsHeadNode([
    el('div', 'pane-note', t('prompts.summary', { prompts, tools })),
    // 第二行：上一条说"有多少"，这一条说"现在哪些会被用上"（三个状态各多少条）。
    // `{on}` 用的是服务端的 `summary.tools_on` —— 契约 §2.2 的 on 就是它。
    el('div', 'pc-when', `${t('prompts.on', { on: toolsOn })}${
      promptTallyText(promptTally(allItems(data)))}`),
    // 第三行：**两门语言各改了几条**（契约 §8 第 3 条）。用户在担心"改中文把英文
    // 带坏了"，那就把英文那一门的数字摆在同一条线上（0 也写出来）。
    el('div', 'pc-when prompts-overrides', promptOverridesText(data)),
  ], data));
  const note = promptsNoteNode();
  if (note) host.appendChild(note);
  for (const node of promptsBodyNodes(data)) host.appendChild(node);
}

function countItems(groups) {
  return itemsOf(groups).length;
}

/* data.tools 是**分组**数组，这里把它摊成工具条目（统计用）。 */
function toolsOf(data) {
  return itemsOf(data && data.tools);
}

function itemsOf(groups) {
  const out = [];
  for (const group of (groups || [])) {
    for (const item of ((group && group.items) || [])) out.push(item);
  }
  return out;
}

function allItems(data) {
  return [...itemsOf(data && data.groups), ...itemsOf(data && data.tools)];
}

/* ── 重置（危险区）─────────────────────────────────────── */

/* 一个极简对话框。整个应用有两处用它：恢复出厂设置、以及「格式化并初始化 dream」
   （后者的门禁更多：要当场列清单、要把确认词打对才解锁、失败要留在原地）。
   它恰好是最危险的两个操作，所以宁可单独写一个也不要用 window.confirm
   （原生确认框塞不下"到底要删掉多少东西"）。

   `html` 是**我们自己写的模板**（里面只允许出现 escapeHtml 过的值）；
   档案的标题/正文/路径是模型与用户写的，那种内容一律走 `node`
   （已经建好的 DOM 树，字符串只会被 textContent 收下）。

   * `onReady({ confirm, close })`：body 挂上去之后、确认按钮接线之前调用 ——
     用来设"确认按钮一开始是禁用的"这种初始状态、给输入框挂 oninput；
   * `onConfirmError(err, { confirm, close })`：给了它，确认失败时**不关**对话框
     （错误原话留在原地，可以改完再试）；没给就照旧"关掉并把异常抛出去"。 */
function openDialog({
  title, html, node, confirmLabel, confirmClass, onConfirm,
  onReady, onConfirmError,
}) {
  $('dialog-title').textContent = title;
  const body = $('dialog-body');
  clearNode(body);
  if (node) body.appendChild(node);
  else body.innerHTML = html;
  const confirm = $('dialog-confirm');
  confirm.textContent = confirmLabel;
  confirm.className = confirmClass || 'btn-danger';
  confirm.disabled = false;
  $('dialog').classList.remove('hidden');

  const close = () => {
    $('dialog').classList.add('hidden');
    confirm.onclick = null;
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);

  $('dialog-cancel').onclick = close;
  $('dialog').onclick = (event) => { if (event.target.id === 'dialog') close(); };
  if (onReady) onReady({ confirm, close });
  confirm.onclick = async () => {
    confirm.disabled = true;
    confirm.textContent = t("dialog.working");
    try {
      await onConfirm();
    } catch (err) {
      if (!onConfirmError) {
        close();
        throw err;
      }
      confirm.textContent = confirmLabel;
      confirm.disabled = false;
      onConfirmError(err, { confirm, close });
      return;
    }
    close();
  };
}

function formatByteSize(bytes) {
  const value = bytes || 0;
  return value < 1024 * 1024
    ? `${(value / 1024).toFixed(1)} KB`
    : `${(value / 1024 / 1024).toFixed(2)} MB`;
}

function resetSummaryRows(data) {
  const size = formatByteSize(data.bytes);
  return [
    [t("reset.row_chats"), t("usage.chat_count", { n: data.sessions || 0 })],
    [t("reset.row_memories"), t("reset.unit_active", { n: data.memories || 0 })],
    ['Dream', t("reset.unit_dream_runs", { n: data.dream_runs || 0 })],
    [t("vault.browser_title"), t("reset.unit_files", { n: data.vault_files || 0 })],
    [t("reset.row_attachments"), t("reset.unit_files", { n: data.attachment_files || 0 })
      + ` · ${formatByteSize(data.attachment_bytes)}`],
    [t("reset.row_prompt_overrides"), `${fmtInt(data.prompt_overrides || 0)} ${t("reset.unit_items")}`],
    [t("reset.row_providers_short"), t("reset.unit_providers_with_keys", { n: data.providers || 0 })],
    [t("reset.row_disk"), size],
  ];
}

/* 重置成功之后：后端的东西**整个换了一遍**，界面里每一份缓存都过期了。
   只清会话/记忆/档案是不够的 —— `providers.json` 与 `tools.json` 也一起被删了，
   而右上角的模型选择器是从 `state.providers` 画的：
   不重拉就会继续列着**已经不存在的接口**，`state.selection` 还指着其中一个，
   下一次发送直接拿一个已删除的 provider 去请求（真实踩过：重置后不刷新页面就发消息）。

   两件事都要做，缺一不可：
   * **清本地缓存**（否则重进某个子页会拿旧数据先画一遍：比如 Dream 页会用旧
     `dreamRuns` 补画一次已经清空的运行历史）；
   * **重新拉一次**（选择器的**内容**来自后端，光清本地是拉不回来的）。
     同 `saveToolsPane` 里那句"重新拉一次配置"的理由。 */
async function refreshAfterFactoryReset() {
  state.sessionId = null;
  state.sessions = [];
  // 附件目录随会话一起被删了：待发列表里的缩略图已经指向不存在的文件
  clearAttachments();
  state.memories = null;
  state.memoriesLoaded = false;
  state.vault = null;
  state.vaultLoaded = false;
  // 档案已经全删了：右边那份正文与树上的展开状态都不能留着
  state.vaultFile = null;
  state.vaultFileLoaded = false;
  state.vaultFileLinks = {};
  state.vaultTreeOpen = {};
  state.vaultEdit = null;
  state.vaultNote = null;
  state.dreams = 0;
  state.dreamRuns = null;
  state.dreamsLoaded = false;
  state.dreamFormatResult = null;
  state.tools = null;          // 工具配置也被删了，下次进那个子页要重新拉
  // 提示词那一页的负载是**跟着语言取的**：重置之后语言本身没变，但这一页
  // 显示的是"当前配置下哪些会生效"，配置整个换过了 —— 缓存必须作废，
  // 否则再进那一页会先画一版已经不成立的 on/off。
  state.prompts = null;
  // 提示词的编辑态与那行结论也跟着作废：data/ 整个没了，草稿还留着只会画出
  // 一个"正在编辑一条已经不存在的覆盖"的假象。
  state.promptEdit = null;
  state.promptNote = null;

  // 接口配置没了：重拉才知道"现在一个接口都没有"
  state.config = await getJSON('/api/config').catch(() => state.config);
  // `data/` 整个没了，`settings.json` 里的时区与单轮最大输出也回到种子值：
  // 界面镜子必须跟着回默认，否则重置之后进「常规」还会显示旧值。
  if (state.config) {
    state.timezone = state.config.timezone ?? 8;
    state.maxOutputTokens = state.config.max_output_tokens ?? 5000;
  }
  state.providers = await getJSON('/api/providers')
    .catch(() => ({ providers: [], models: [], presets: [], default: {} }));
  renderPickers();   // 没有可用模型时它自己会落到"（没有可用模型…）"，并顺手修好 state.selection
  renderWebToggle();
  renderBrandAndHint();
  await loadSessions().catch(() => {});
  // 屏幕上那个会话已经不存在了：不清掉的话，下一条消息看起来就像在接着它
  // 继续聊（标题 / 消息 / 顶栏用量都还是旧的）。newChat() 同时把两个
  // 会话级开关恢复成新对话的默认值。
  newChat();
}

/* 顶部品牌行。`init()` 与"重置之后"共用；返回值是"有没有可用模型"，
   供 init() 决定全新机器要不要直接打开接口设置。输入区旧提示已整体移除。 */
function renderBrandAndHint() {
  const config = state.config || {};
  // 侧栏第二行显示**版本号**（ADR-096）。它来自 `/api/config` 的 `version`，
  // 而那个值读自 `docs/version.txt` —— 界面上没有第二处写死版本的地方。
  // 模型名不再放这里：它已经在顶栏的选择器里，而"你跑的是哪一个版本"没有别处可看。
  const version = String(config.version || '').trim();
  $('brand-sub').textContent = version
    ? t("brand.version", { version })
    : t("brand.version_unknown");
  return ((state.providers && state.providers.models) || []).length;
}

async function loadResetPane() {
  const data = await getJSON('/api/reset');
  const rows = resetSummaryRows(data);
  const empty = !data.sessions && !data.memories && !data.dream_runs && !data.providers
    && !data.vault_files && !data.attachment_files && !data.attachment_bytes
    && !data.prompt_overrides;
  $('reset-body').innerHTML = t("reset.pane_html", { rows: rows.map(([k, v]) => `<tr><td>${k}</td><td>${escapeHtml(String(v))}</td></tr>`).join(''), dataDir: escapeHtml(data.data_dir || ''), disabled: empty ? 'disabled' : '', label: empty ? t("reset.nothing_to_clear") : t("reset.factory_reset") });

  const start = $('reset-start');
  if (start && !empty) {
    start.onclick = () => openDialog({
      title: t("reset.confirm_title"),
      html: t("reset.confirm_html", { items: rows.map(([k, v]) => `<li>${k}：<strong>${escapeHtml(String(v))}</strong></li>`).join('') }),
      confirmLabel: t("reset.delete_forever"),
      // 400/500 以前会"关掉对话框 + 控制台一条 unhandled rejection"，
      // 用户什么都看不到。这个动作不可逆，失败必须当场说出后端的原话。
      onConfirmError: (err) => {
        toast(t("reset.failed", { msg: String((err && err.message) || err) }));
      },
      onConfirm: async () => {
        const result = await postJSON('/api/reset', {});
        const deleted = result.deleted || {};
        toast(t("reset.done_lead", { sessions: deleted.sessions || 0 })
          + t("reset.done_tail", { memories: deleted.memories || 0, providers: deleted.providers || 0 }));
        if (result.leftover) {
          toast(t("reset.leftover_warning", { path: result.leftover }));
        }
        await refreshAfterFactoryReset();
        showChat();          // 旧会话已经不存在了，回到对话页
        navigate('chat');
      },
    });
  }
}

/* ── start.cmd 的 venv 引导门 ─────────────────────────────
   轮询是串行的：上一个状态请求结束后才安排下一个。安装器不复用聊天的
   AbortController / busy，也不需要 SSE；刷新页面只会重新接上当前状态。 */

let setupGateWired = false;

function setupErrorMessage(err) {
  const code = err && err.body && err.body.code;
  const translated = {
    invalid_mirror: 'setup.invalid_mirror',
    install_busy: 'setup.install_busy',
    consent_required: 'setup.consent_required',
    not_active: 'setup.not_active',
  };
  if (code && translated[code]) return t(translated[code]);
  return t('setup.action_failed', { msg: String((err && err.message) || err) });
}

function setSetupGateOpen(open) {
  const changed = state.setupGateOpen !== open;
  state.setupGateOpen = open;
  $('startup-gate').classList.toggle('hidden', !open);
  $('app').inert = open;
  $('app').setAttribute('aria-hidden', open ? 'true' : 'false');
  if (open && changed) {
    const target = state.setup && state.setup.state === 'consent' ? $('setup-start') : $('setup-card');
    if (target && typeof target.focus === 'function') target.focus();
  }
}

function renderSetupGate() {
  const status = state.setup || { state: 'checking', progress: {} };
  const phase = status.state || 'checking';
  const downloading = ['preparing', 'downloading', 'paused', 'verifying', 'finalizing', 'cancelling'].includes(phase);
  const consent = phase === 'consent' || phase === 'error';
  $('setup-loading').classList.toggle('hidden', phase !== 'checking');
  $('setup-consent-panel').classList.toggle('hidden', !consent);
  $('setup-download-panel').classList.toggle('hidden', !downloading);

  const network = $('setup-network-error');
  network.textContent = state.setupNetworkError || '';
  network.classList.toggle('hidden', !state.setupNetworkError);

  const integrity = status.integrity || {};
  const missing = integrity.missing || [];
  $('setup-missing').textContent = t('setup.missing', {
    // 内部 issue code 只供诊断/测试，不把 import_failed 这类英文代号倒给用户。
    items: missing.length ? missing.join(', ') : t('setup.missing_generic'),
  });
  const failure = $('setup-error');
  const failureText = state.setupActionError || state.setupNetworkError || (phase === 'error'
    ? t('setup.failed', { msg: status.error || status.error_code || 'unknown' })
    : '');
  failure.textContent = failureText;
  failure.classList.toggle('hidden', !failureText);

  const phaseKeys = {
    preparing: 'setup.preparing',
    downloading: 'setup.downloading',
    paused: 'setup.paused',
    verifying: 'setup.verifying',
    finalizing: 'setup.finalizing',
    cancelling: 'setup.cancelling',
  };
  if (phaseKeys[phase]) $('setup-download-title').textContent = t(phaseKeys[phase]);
  $('setup-source').textContent = status.source === 'mirror'
    ? t('setup.source_mirror') : t('setup.source_official');
  const progress = status.progress || {};
  const bar = $('setup-progress');
  if (typeof progress.percent === 'number' && Number.isFinite(progress.percent)) {
    bar.value = Math.max(0, Math.min(100, progress.percent));
    $('setup-progress-text').textContent = t('setup.progress_percent', {
      value: Math.round(progress.percent),
    });
  } else {
    // 没有 pip 的真实测量就保持浏览器原生 indeterminate，绝不编一个百分比。
    bar.removeAttribute('value');
    $('setup-progress-text').textContent = phase === 'paused'
      ? t('setup.paused') : t('setup.progress_waiting');
  }
  $('setup-log').textContent = String(progress.message || '');
  const actionFailure = $('setup-action-error');
  const actionText = downloading ? (state.setupActionError || state.setupNetworkError || '') : '';
  actionFailure.textContent = actionText;
  actionFailure.classList.toggle('hidden', !actionText);

  $('setup-start').disabled = !!state.setupActionBusy;
  $('setup-skip').disabled = !!state.setupActionBusy;
  $('setup-pause').textContent = phase === 'paused' ? t('setup.resume') : t('setup.pause');
  $('setup-pause').disabled = !!state.setupActionBusy || !['downloading', 'paused'].includes(phase);
  $('setup-cancel').disabled = !!state.setupActionBusy
    || !['preparing', 'downloading', 'paused', 'verifying'].includes(phase);
}

async function setupMutation(kind) {
  if (state.setupActionBusy) return;
  state.setupActionBusy = true;
  state.setupActionError = '';
  state.setupNetworkError = '';
  renderSetupGate();
  try {
    let result;
    if (kind === 'start') {
      result = await postJSON('/api/setup/start', {
        consent: true,
        mirror: $('setup-mirror').value.trim(),
      });
    } else if (kind === 'skip') {
      result = await postJSON('/api/setup/skip', {});
    } else if (kind === 'pause') {
      const paused = state.setup && state.setup.state === 'paused';
      result = await postJSON(paused ? '/api/setup/resume' : '/api/setup/pause', {});
    } else {
      result = await postJSON('/api/setup/cancel', {});
    }
    state.setup = result;
  } catch (err) {
    state.setupActionError = setupErrorMessage(err);
  } finally {
    state.setupActionBusy = false;
    renderSetupGate();
  }
}

function wireSetupGate() {
  if (setupGateWired) return;
  setupGateWired = true;
  $('setup-start').onclick = () => setupMutation('start');
  $('setup-skip').onclick = () => setupMutation('skip');
  $('setup-pause').onclick = () => setupMutation('pause');
  $('setup-cancel').onclick = () => setupMutation('cancel');
}

function setupStatusDelay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function runSetupGate() {
  wireSetupGate();
  setSetupGateOpen(true);
  for (;;) {
    try {
      const status = await getJSON('/api/setup/status');
      state.setupNetworkError = '';
      state.setup = status;
      const language = status.language || 'zh';
      if (language !== current()) {
        setLanguageNow(language);
        applyStaticTexts();
      }
      renderSetupGate();
      if (status.can_chat) {
        setSetupGateOpen(false);
        return status;
      }
    } catch (err) {
      // 页面已经由本机静态服务打开时，短暂连接失败不等于 pip 失败；保留现有
      // 阶段并自动重试。真正的安装错误来自状态接口，不会和网络错误混为一谈。
      state.setupNetworkError = t('setup.network_retry', { msg: String(err.message || err) });
      if (!state.setup) state.setup = { state: 'checking', progress: {} };
      renderSetupGate();
      await setupStatusDelay(1000);
      continue;
    }
    await setupStatusDelay(400);
  }
}

/* ── 初始化 ───────────────────────────────────────────── */

async function init() {
  initTheme();
  // 先接通启动门：普通 --web 立即 ready；start.cmd 则在这里完成完整性检查、
  // 用户选择与可选下载。只有 ready / bypassed 才会继续触碰聊天应用 API。
  const setupResult = await runSetupGate();
  // 语言必须在**画任何东西之前**定下来：下面每一句 t() 都按它取文案。
  // 接口没就绪（还没有 language 字段）时 normalize() 会退回 zh —— 与后端默认一致。
  try {
    state.config = await getJSON('/api/config');
    state.providers = await getJSON('/api/providers');
  } catch (err) {
    toast(t("init.backend_unreachable") + (err.message || err));
    return;
  }
  setLanguageNow((state.config || {}).language || 'zh');
  // 时区从 /api/config 读初值（默认 8 = UTC+8），设置 → 常规 里改
  state.timezone = (state.config || {}).timezone ?? 8;
  // 单轮最大输出从 /api/config 读初值（默认 5000；-1 = 不限），设置 → 常规 里改
  state.maxOutputTokens = (state.config || {}).max_output_tokens ?? 5000;
  // 静态字（含 <title> 与 <html lang>）：先把页面壳换成该显示的语言，再往下画
  applyStaticTexts();
  // 「有没有可用模型」这个判据由 renderBrandAndHint 一起算好返回（见它的注释）
  const usable = renderBrandAndHint();

  const defaults = state.providers.default || {};
  if (defaults.provider && defaults.model) applySelection(defaults);
  else renderPickers();
  // 整理模型（ADR-103）：与聊天顶栏那个选择器是**两个独立的选择**，
  // 初始值来自 /api/providers 的 `dream`（空 = 跟随默认）。
  renderDreamPicker();
  renderWebToggle();
  renderMultimodalToggle();   // 默认开：不依赖任何 config，页面上先亮起来

  await loadSessions().catch(() => {});
  window.addEventListener('hashchange', () => { applyRoute(); });
  // 全新机器（一个接口都没有）：直接把接口页推到眼前，
  // 否则新用户会在对话页面对一个不能用的输入框发呆。
  if (!usable && (location.hash === '' || location.hash === '#chat')) {
    navigate('settings', 'api');
  } else {
    await applyRoute();
  }
  // 顶栏标题：有会话时它是**会话标题**（数据），没有会话时它是界面文案。
  // index.html 上刻意不给它 data-i18n —— 带上就会被 applyStaticTexts 冲成"新对话"，
  // 把一个正在看的会话标题盖掉。所以这里按当前语言补一次。
  if (!state.sessionId) $('chat-title').textContent = t('chat.new_title');

  $('new-chat').onclick = newChat;
  $('send').onclick = send;
  $('stop').onclick = stop;
  wireComposerAttachments();
  $('input').addEventListener('input', autoGrow);
  $('input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  $('settings-entry').onclick = () => navigate('settings', 'memory');
  $('settings-close').onclick = () => navigate('chat');
  for (const item of document.querySelectorAll('.settings-nav-item')) {
    item.onclick = () => navigate('settings', item.dataset.pane);
  }
  $('pick-model').onchange = () => {
    const [provider, model] = $('pick-model').value.split('|');
    state.selection.provider = provider;
    state.selection.model = model;
    renderEffortPicker();
    renderSessionStats();
    persistSelection();
  };
  $('pick-effort').onchange = () => {
    state.selection.effort = $('pick-effort').value;
    persistSelection();
  };
  $('toggle-web-search').onclick = () => setWebSearch(!state.webSearch);
  // busy 时也让它可切：切了影响的是**下一条**消息（与联网开关同一个语义，契约 §13.7-6）
  $('toggle-multimodal').onclick = toggleMultimodal;
  $('memory-search').addEventListener('input', renderMemories);
  $('memory-reload').onclick = loadMemories;
  $('memory-limit-save').onclick = saveMemoryLimit;
  $('memory-limit').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') saveMemoryLimit();
  });
  $('dream-reload').onclick = () => {
    state.dreamFormatResult = null;   // 刷新 = 把上一次格式化的结果条收起来
    loadDreamPane().catch((err) => toast(String(err.message || err)));
  };
  $('usage-reload').onclick = loadUsage;
  // 整理模型：改了就存（**不影响聊天页那个选择器**，两者是独立的设置）
  $('dream-model').onchange = onDreamModelChange;
  $('dream-run').onclick = () => {
    // 二次确认说的是**现在这件事的代价**：单向、没有预演、补救只有清空重来
    if (confirm(t("dream.confirm_1")
      + t("dream.confirm_2")
      + t("dream.confirm_3")
      + t("dream.confirm_4"))) {
      runDream();
    }
  };
  $('messages').addEventListener('scroll', () => {
    const el = $('messages');
    state.stick = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  });
  $('input').focus();
  if (setupResult && setupResult.state === 'bypassed') {
    toast(t('setup.continued_without'));
  }
}

/* `globalThis.__HARNESS_NO_INIT__` 只有自检脚本会设：它让这个文件能被当成
   一个"函数库"载入（在 vm 里跑真的 app.js，然后直接调 renderVaultPane 这类
   函数），而不必先伪造一整个后端。生产环境没人设这个变量，所以行为不变。 */
if (!globalThis.__HARNESS_NO_INIT__) init();
