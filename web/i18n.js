'use strict';

/* O₃ᶻᵒⁿᵉ · 界面文案（zh / en）
   ─────────────────────────────────────────────────────────
   零构建：这是普通 <script>，在 app.js **之前**加载（index.html 里那一行）。
   它只做三件事：
     1. 装两份完整目录：I18N.zh / I18N.en；
     2. 给出 t(key, vars) / current() / setLanguage(lang) / applyStaticTexts(root)；
     3. 把 index.html 上带 data-i18n* 的静态字填成当前语言（切语言时也是它负责原地重填）。

   约定（谁改都按这个来）：

   * **中文（I18N.zh）的值是从 app.js / index.html 的原文里取出来的**，不是重打的 ——
     默认语言下界面输出与加 i18n 之前逐字节一致，所以那些盯着中文文案的自检一条都不用改。
   * key 命名：`<区域>.<名字>`，区域 = 界面部件（session / dialog / chat / memory / dream /
     vault / api / tools / theme / usage / reset / toggle / picker / settings / action /
     time / notice / attachment / ocr / language / static）。
   * 占位符写成 `{name}`，由 t(key, {name: value}) 替换；**两边的占位符集合必须一致**。
   * app.js 里用模板字符串拼的 HTML 片段**整体算一个 key**（HTML 留在文案里）：
     这样两种语言下 DOM 结构与空白完全一致，中英不会各自漂移。
     带 HTML 的值，zh / en 两侧的标签序列必须一模一样。
   * index.html 的静态字走四种标记：
       data-i18n              纯文本（textContent）
       data-i18n-html         语句里含 <strong>/<code>，只能走 innerHTML
       data-i18n-title        title 属性
       data-i18n-placeholder  placeholder 属性
     标记里的文字同时留在 HTML 里当"JS 还没跑"时的兜底，applyStaticTexts 会按当前语言覆盖它。
   * 缺 key **不许静默返回空串**：t() 会返回 key 本身并 console.warn（契约 §6.9）。
     所以漏翻会以"界面上出现 key"的形式被看见，而不是一片空白。
   * 语言清单 I18N.languages 里的 label 也是文案：中文界面「中文 / English」，
     英文界面「Chinese / English」。
   * 后端产出的文字（notice / 工具 display / 提示词）由 harness/i18n.py 负责，
     这里一个字都不碰 —— 前端只翻自己写的那些。 */

const I18N = {
  /* 当前语言。初始一律 zh；app.js 的 init() 拿到 /api/config 的 language 之后再 setLanguage()。 */
  lang: 'zh',

  /* ── 中文（zh）───────────────────────────────────────
     值是**从 app.js / index.html 的原文里取出来的**，一个字都没重打：
     默认语言下界面输出与这一轮改动之前逐字节一致。 */
  zh: {
  "time.just_now": '刚刚',   // app.js:115
  "time.minutes_ago": `{n} 分钟前`,   // app.js:116
  "time.hours_ago": `{n} 小时前`,   // app.js:117
  "time.days_ago": `{n} 天前`,   // app.js:118
  "theme.follow_system": '跟随系统',   // app.js:203
  "theme.light": '浅色',   // app.js:203
  "theme.dark": '深色',   // app.js:203
  "session.list_recent": '最近会话',   // app.js:236
  "session.list_settings": '会话',   // app.js:245
  "session.load_failed": `<div class="session-sub">加载失败：{msg}</div>`,   // app.js:262
  "session.open_failed": '会话加载失败：{msg}',   // app.js:451
  "session.none": '<div class="session-sub" style="padding:8px 6px">还没有对话</div>',   // app.js:307
  "session.item": `
      <div class="session-title">{title}</div>
      <div class="session-sub">{turns} 轮 · {time}</div>
      <div class="session-actions">
        <button class="session-act" data-act="rename" title="重命名">✎</button>
        <button class="session-act" data-act="delete" title="删除这个对话">✕</button>
      </div>`,   // app.js:313
  "dialog.rename.title": '重命名对话',   // app.js:336
  "dialog.rename.body": `<p>给这个对话起个名字（留空就恢复成自动取的第一句话）：</p>
      <input id="rename-input" type="text" maxlength="60"
             value="{title}"
             style="width:100%;margin-top:10px;padding:8px 10px;border-radius:8px;
                    border:1px solid var(--border-strong);background:var(--bg);
                    color:var(--text);font-family:inherit;font-size:13.5px">`,   // app.js:337
  "action.save": '保存',   // app.js:343
  "toast.renamed": '已重命名',   // app.js:351
  "toast.rename_restored": '已恢复自动命名',   // app.js:351
  "toast.rename_failed": `重命名失败：{msg}`,   // app.js:352
  "dialog.delete_chat.title": '删除这个对话？',   // app.js:371
  "dialog.delete_chat.body": `<p>将删除对话 <strong>{title}</strong>
      （{turns} 轮）。</p>
      <p class="danger-text">只删这一条对话。记忆、接口配置和其他对话都不受影响。</p>`,   // app.js:372
  "action.delete": '删除',   // app.js:375
  "toast.deleted": '已删除',   // app.js:379
  "toast.delete_failed": `删除失败：{msg}`,   // app.js:386
  "toast.busy_running": '正在生成中，先停止或等待结束',   // app.js:392
  "meta.steps_line": `步数 {steps} · {reason} · {elapsed}s · `,   // app.js:439
  "chat.empty_html": `
    <div class="empty" id="chat-empty">
      <div class="empty-title">开始一段对话</div>
      <div class="empty-sub">模型会自己决定记住什么。记忆跨会话保留，可以随时整理。</div>
    </div>`,   // app.js:451
  "chat.new_title": '新对话',   // app.js:456
  "picker.no_models": '<option value="">（没有可用模型，去「设置 → 接口」添加）</option>',   // app.js:498
  "picker.effort_none": '<option value="">thinking：不下发</option>',   // app.js:541
  "picker.effort_option": 'thinking：{value}',   // app.js:597
  "toast.save_model_failed": `保存模型选择失败：{msg}`,   // app.js:563
  "toggle.web.on": '联网搜索：开',   // app.js:580
  "toggle.web.off": '联网搜索：关',   // app.js:580
  "toggle.web.disabled": '联网功能在「设置 → 工具」里被关掉了',   // app.js:582
  "toggle.web.unavailable": '搜索后端不可用（被关掉或缺 key），去「设置 → 工具」看看',   // app.js:584
  "toggle.web.on_title": '这个会话允许模型自己搜索；点击关闭',   // app.js:586
  "toggle.web.off_title": '这个会话不允许模型自己搜索（打开用户给的链接仍然可以）；点击开启',   // app.js:587
  "toast.save_web_failed": `保存联网开关失败：{msg}`,   // app.js:598
  "toolcfg.button": '工具',   // index.html（顶栏按钮）
  "toolcfg.title_attr": '配置这个会话允许模型使用哪些工具（没有会话时，编辑的是新会话的默认配置）',   // index.html
  "toolcfg.title_session": '这个会话可用的工具',   // app.js openToolConfig
  "toolcfg.title_default": '新会话默认可用的工具',   // app.js openToolConfig
  "toolcfg.target_session": '正在编辑：当前会话。改动只影响这一个会话。',   // app.js renderToolConfigPopup
  "toolcfg.target_default": '正在编辑：默认配置。每个新会话出生时会复制这一份。',   // app.js renderToolConfigPopup
  "toolcfg.search_placeholder": '搜索工具名…',   // index.html
  "toolcfg.empty": '没有匹配的工具',   // app.js renderToolConfigPopup
  "toolcfg.close": '完成',   // index.html
  "toolcfg.enabled_count": '已启用 {n}/{total}',   // app.js renderToolConfigCount
  "toast.save_toolcfg_failed": `保存工具配置失败：{msg}`,   // app.js closeToolConfig
  "toast.load_toolcfg_failed": `读取工具配置失败：{msg}`,   // app.js openToolConfig
  "multimodal.badge": '将只发送识别出的文字',   // app.js:612
  "multimodal.ocr_label": '多模态已关 · 只发送了下面的识别文本',   // app.js:615
  "toggle.multimodal.on": '多模态：开',   // app.js:630
  "toggle.multimodal.off": '多模态：关',   // app.js:630
  "toggle.multimodal.on_title": '图片会随请求直接发给模型；点一下改成"只发本机识别出的文字"',   // app.js:632
  "toggle.multimodal.off_title": '多模态已关：图片只在本机做 OCR，只把识别出的文字发给模型，图片本身不会发出去；点一下改回发图',   // app.js:633
  "toast.save_multimodal_failed": `保存多模态开关失败：{msg}`,   // app.js:646
  "ocr.failed": `识别失败：{msg}`,   // app.js:757
  "ocr.no_text": '没有识别到任何文字。',   // app.js:759
  "ocr.no_record": '这一张没有留下识别记录。',   // app.js:760
  "ocr.count_images": `{n} 张图`,   // app.js:785
  "ocr.count_chars": `{n} 字`,   // app.js:786
  "ocr.count_failed": `{n} 张识别失败`,   // app.js:787
  "ocr.failed_short": '识别失败',   // app.js:787
  "ocr.count_empty": `{n} 张没有文字`,   // app.js:788
  "ocr.empty_short": '没有识别到文字',   // app.js:788
  "ocr.count_norec": `{n} 张没有识别记录`,   // app.js:789
  "ocr.norec_short": '没有识别记录',   // app.js:789
  "attachment.image": '图片',   // app.js:823
  "attachment.image_label": `【图片 {n}：{name}】`,   // app.js:824
  "memory.omitted_note": `⟲ 记忆 {count}/{limit} 条，其中 {omitted} 条因长度未进上下文`,   // app.js:856
  "chat.summary_html": `
    <div class="summary-head">
      <span class="chev">▸</span>
      <span>上下文已压缩</span>
      <span class="summary-note">较早的 {covered} 条消息已折叠成摘要{suffix}</span>
    </div>
    <div class="summary-body"></div>`,   // app.js:874
  "chat.empty_summary": '(空摘要)',   // app.js:882
  "chat.think_html": `
    <div class="think hidden"><div class="think-head"><span class="chev">▸</span>深度思考</div>
      <div class="think-body"></div></div>
    <div class="tools"></div>
    <div class="bubble"></div>`,   // app.js:896
  "chat.no_output": '<span style="color:var(--muted)">（无输出）</span>',   // app.js:951
  "chat.tool_chip_html": `
    <div class="tool-head"><span class="tool-dot"></span>
      <span class="tool-name">{name}</span>
      <span class="tool-text">运行中…</span></div>
    <div class="tool-detail"></div>`,   // app.js:970
  "tool.done": '完成',   // app.js:985
  "tool.failed": '失败',   // app.js:985
  "tool.args": `参数：{args}`,   // app.js:987
  "tool.result": `结果：{result}`,   // app.js:988
  "usage.cached": `缓存命中 {cached}`,   // app.js:995
  "usage.this_chat": `本会话 {total} token`,   // app.js:1000
  "stats.turns": `{n} 轮`,
  "stats.speed": `{value} tok/s`,
  "stats.tokens": `{value} tok`,
  "stats.tokens_title": `本会话累计 {value} token`,
  "stats.cache": `缓存命中 {value}%`,
  "stats.context": `{value}%`,
  "stats.context_title": `当前模型上下文：{used} / {window} token`,
  "stats.context_none": `未选择模型，上下文使用率显示 0%`,
  "attachment.read_failed": `读不到文件{detail}`,   // app.js:1051
  "attachment.setup_hint": `{msg}（先跑 run.cmd --setup）`,   // app.js:1102
  "attachment.remove_title": '从待发列表里移除（服务器上的文件不动）',   // app.js:1140
  "attachment.busy_add": '正在生成中，先等这一轮结束（或点「停止」）再加图片',   // app.js:1184
  "attachment.session_changed": '上传期间切换了会话，这张图已丢弃；请在新会话里重新添加',   // app.js:1133
  "chat.no_provider": '还没有可用的接口，先去「设置 → 接口」添加一个',   // app.js:1361
  "chat.images_only": '（图片）',   // app.js:1396
  "chat.fork": '分叉',   // app.js:806
  "chat.fork_title": '从这里分叉：新建一个只保留这条消息之前上下文的独立对话（原对话不变）',   // app.js:807
  "toast.forked": '已分叉出一个新的独立对话',   // app.js:478
  "toast.fork_failed": `分叉失败：{msg}`,   // app.js:481
  "notice.stopped": '已停止。',   // app.js:1423
  "notice.stream_handler_failed": `这一帧没能画出来（{msg}）；后面的内容会继续接收。`,   // app.js:1480
  "notice.ocr_refresh_failed": `这一轮只发送了 OCR 文字，但会话视图没刷新出来（{msg}）。`,   // app.js:1464
  "notice.ocr_refresh_hint": '刷新页面就能看到模型实际收到的文字。',   // app.js:1465
  "memory.stats": `有效 {active}/{cap} 条`,   // app.js:1513
  "memory.range": `范围 {min}–{max}`,   // app.js:1521
  "memory.lowest_now": `，现在最低可设到 {lowest}`,   // app.js:1522
  "memory.full": ' · 已满，模型现在记不了新东西',   // app.js:1523
  "memory.budget_warn_1": `⚠ 记忆总量超过注入预算（{tokens} token），`,   // app.js:1529
  "memory.budget_warn_2": `有 {n} 条不会进入模型上下文 —— 调小上限或精简内容`,   // app.js:1530
  "memory.no_match": '<div class="session-sub">没有匹配的记忆</div>',   // app.js:1549
  "memory.card_html": `
      <div class="mem-head">
        <span class="chip id">mem:{id}</span>
      </div>
      <div class="mem-content">{content}</div>
      <div class="mem-foot">
        <span class="mem-actions">
          <button class="mini" data-act="edit">编辑</button>
          {actions}
        </span>
      </div>`,   // app.js:1558
  "memory.limit_invalid": '请填一个 0 到 100 之间的整数',   // app.js:1580
  "memory.limit_not_int": '上限必须是整数',   // app.js:1582
  "memory.limit_set": `记忆上限已设为 {n}`,   // app.js:1585
  "memory.edit_save_btn": '<button class="mini" data-act="save">保存</button>',   // app.js:1605
  "memory.edit_cancel_btn": '<button class="mini" data-act="cancel">取消</button>',   // app.js:1606
  "memory.content_required": '内容不能为空',   // app.js:1616
  "memory.updated": '已更新',   // app.js:1619
  "memory.delete_confirm": `删除这条记忆？\n\n{preview}`,   // app.js:1626
  "memory.deleted_note": '已从 memory_instruction.md 移除',   // app.js:1629
  "dream.meta": `{parts} · 待整理 {n} 个会话`,   // app.js:1660
  "dream.runs_load_failed": `<div class="session-sub">运行历史加载失败：{msg}</div>`,   // app.js:1672
  "dream.seconds": `{n} 秒`,   // app.js:1740
  "dream.minutes_seconds": `{m} 分 {s} 秒`,   // app.js:1741
  "run.status.running": '运行中',   // app.js:1745
  "run.status.error": '出错',   // app.js:1747
  "run.status.empty": '无可整理',   // app.js:1748
  "run.status.interrupted": '已中断',   // app.js:1751
  "run.status.unknown": '未知',   // app.js:1755
  "dream.mode.dry": '预演（旧功能）',   // app.js:1764
  "dream.mode.apply": '整理',   // app.js:1764
  "dream.pending_latest": '待整理：已经整理到最新',   // app.js:1772
  "dream.pending_counts": `待整理 {n} 个会话 · {events} 条记录`,   // app.js:1773
  "dream.stale_notice_open": `<div class="vault-stale">⚠ 其中 {n} 个会话的日志比整理进度还短`,   // app.js:1783
  "dream.stale_notice_mid": '（被改过或换过），已按从头整理处理 —— 下次整理会多花一点，',   // app.js:1784
  "dream.stale_notice_close": '不这么做那些记录会永久读不到。</div>',   // app.js:1785
  "vault.open_link": `打开 {name}`,   // app.js:1892
  "vault.not_written": '这个档案还没写',   // app.js:1897
  "vault.missing_link_create": `《{name}》这份档案还没写，点一下现在建一份`,   // app.js:1918
  "vault.missing_link_ask": `《{name}》这份档案还没写，要现在建一份吗？`,   // app.js:1929
  "vault.no_body": '（这份档案没有正文）',   // app.js:1966
  "vault.kind.file": '档案',   // app.js:2032
  "vault.kind.index": '索引',   // app.js:2039
  "vault.dir_label": `{name}（{files} 份档案）`,   // app.js:2054
  "vault.empty_dir": '（空目录）',   // app.js:2063
  "vault.tree_empty": '还没有档案。点「整理一次」让模型把会话记录压成档案。',   // app.js:2078
  "vault.unsaved_open": '「',   // app.js:2109
  "vault.unsaved_name": '这份档案',   // app.js:2099
  "vault.unsaved_mid": '」还没保存，',   // app.js:2099
  "vault.unsaved_confirm": '换了就会丢掉你敲进去的内容。继续？',   // app.js:2100
  "vault.check_problems": `结构校验有 {n} 处问题：`,   // app.js:2151
  "vault.check_more": `…还有 {n} 条（见下面「结构校验」那块）`,   // app.js:2155
  "action.edit": '编辑',   // app.js:2167
  "vault.edit_title": '改这份档案的正文与元信息',   // app.js:2169
  "vault.delete_title": '删除这份档案（会先让你确认，并列出谁指向它）',   // app.js:2175
  "vault.action.index_note": '这是索引，模型每一轮都能看到它；可以直接改它（删除会被后端拒绝）。',   // app.js:2188
  "vault.action.generated_note": '这是 harness 生成/维护的文件（README / index / earlier_months），只读；改 sections / entities 后会自动重建。',   // app.js:2188
  "vault.action.readonly_note": '这份文件只读（观察层 / 运行日志 / 旁路导出），不能在这里改。',   // app.js:2188
  "vault.action.file_note": '这份档案模型下一轮就会看到。改它不会自动更新 README 与别处的双链。',   // app.js:2189
  "vault.viewer_empty": '从左边点一个档案看看。',   // app.js:2212
  "vault.loading": '读取中…',   // app.js:2221
  "vault.index_note": '这是索引，模型每一轮都能看到它。',   // app.js:2236
  "vault.truncated": `文件太长，这里只显示了前 {shown} 字符（全文 {total} 字符）。`,   // app.js:2264
  "vault.warnings": `这份档案有 {n} 处警告：`,   // app.js:2269
  "vault.links_out": '它指向',   // app.js:2281
  "vault.links_in": '谁指向它',   // app.js:2282
  "vault.links_shared": '共同指向别处',   // app.js:2283
  "vault.chars_total": `共 {chars} 字符`,   // app.js:2288
  "vault.shown_full": ' · 已显示全文',   // app.js:2288
  "vault.edit_status_new": '新建档案 · 保存后模型下一轮就能看到它',   // app.js:2353
  "vault.edit_status_editing": `正在编辑 {path}`,   // app.js:2354
  "vault.new_file": '新建档案',   // app.js:2359
  "vault.new_file_title": '在 entities/ 或 sections/ 下写一份新档案',   // app.js:2361
  "vault.field_key_hint": `写进 frontmatter 的键名：{key}`,   // app.js:2385
  "vault.conflict_lead": '最新内容（',   // app.js:2406
  "vault.conflict_tail": ' 字符）不会自动合并过来。',   // app.js:2406
  "vault.conflict_take": '看最新内容（放弃我的改动）',   // app.js:2408
  "vault.conflict_overwrite": '仍然覆盖保存（用最新 hash 再存一次）',   // app.js:2411
  "vault.edit_hint_1": '你改的档案，模型下一轮就能看到它；但 README 索引与别处的双链**不会自动更新**',   // app.js:2439
  "vault.edit_hint_2": ' —— 结构校验会提示"没有被 README 提到（孤儿档案）"之类的问题。',   // app.js:2440
  "vault.edit_hint_3": '整理正在运行时不能改档案（后端会直接拒绝）。等它跑完（几秒到几十秒）再保存 —— ',   // app.js:2441
  "vault.edit_hint_4": '否则整理会按它开跑时读到的内容把你刚写的东西盖掉。',   // app.js:2442
  "vault.link_name_required": '先写一个链接名',   // app.js:2516
  "vault.link_name_invalid": '链接名不能有方括号或换行，且不超过 120 个字符',   // app.js:2518
  "vault.link_picker_lead": '从下面挑一份现有档案，或手打一个名字 —— 名字可以指向还没写的档案，',   // app.js:2543
  "vault.link_picker_lead2": '先写链接、以后再补是正常用法。',   // app.js:2544
  "vault.link_placeholder": '例如：小林',   // app.js:2551
  "vault.link_name_title": '要插入的链接名（可以是一份还不存在的档案）',   // app.js:2553
  "vault.link_insert": '插入',   // app.js:2559
  "vault.link_insert_title": '把 [[这个名字]] 插到正文的光标处',   // app.js:2562
  "vault.collapse": '收起',   // app.js:2565
  "vault.link_picker_empty": '目录树里还没有档案 —— 那就手打一个名字。',   // app.js:2575
  "vault.link_insert_label": `插入 [[{name}]]（{hint}）`,   // app.js:2580
  "vault.insert_link": '插入链接',   // app.js:2595
  "vault.insert_link_title": '把 [[标题]] 插到正文的光标处（正文框里没有光标信息就追加到末尾）',   // app.js:2598
  "action.saving": '保存中…',   // app.js:2613
  "action.cancel": '取消',   // app.js:2618
  "vault.no_path_yet": '（还没定路径）',   // app.js:2656
  "vault.edit_title_label": `编辑：{name}`,   // app.js:2660
  "vault.too_long_editor_1": '这份档案太长：编辑器里只有前 ',   // app.js:2671
  "vault.too_long_editor_2": ' 字符（全文 ',   // app.js:2672
  "vault.too_long_editor_3": ' 字符）。',   // app.js:2672
  "vault.too_long_save_1": '现在保存会**按你看到的这一份**写回去，',   // app.js:2673
  "vault.too_long_save_2": '超出的部分会丢。要改这种档案，请用别的编辑器直接改 data/vault 下的文件。',   // app.js:2674
  "vault.field_dir": '放在哪个目录',   // app.js:2681
  "vault.field_name": '名字（不用打 .md）',   // app.js:2704
  "vault.field_name_hint": `上限 {max} 个字符（含 .md，后端也按这条校验）；`,   // app.js:2707
  "vault.field_name_rule": '`< > : " | ? *`、Windows 保留设备名（NUL、CON…）、三层路径都会被后端拒绝。',   // app.js:2708
  "vault.field_path": '路径（不能改，改名请删掉再新建）',   // app.js:2711
  "vault.field_filename": '文件名（只读）',   // app.js:2713
  "vault.field_title": '标题',   // app.js:2715
  "vault.field_kind": '类型',   // app.js:2718
  "vault.field_kind_placeholder": '例如：person / project / section',   // app.js:2719
  "vault.field_relation": '关系',   // app.js:2721
  "vault.field_relation_placeholder": '例如：用户本人、同事',   // app.js:2722
  "vault.field_status": '状态',   // app.js:2724
  "vault.field_status_placeholder": '例如：活跃 / 进行中',   // app.js:2725
  "vault.field_place": '地点',   // app.js:2726
  "vault.field_place_placeholder": '例如：北京中关村学院',   // app.js:2726
  "vault.field_tags": '标签（逗号分隔）',   // app.js:2727
  "vault.field_tags_placeholder": '例如：用户, 同学',   // app.js:2728
  "vault.field_aliases": '别名（逗号分隔）',   // app.js:2730
  "vault.field_aliases_placeholder": '例如：林, 小林同学',   // app.js:2731
  "vault.field_body": '正文（markdown 原文）',   // app.js:2736
  "vault.edit_need_open": '先打开一份档案再编辑',   // app.js:2832
  "vault.edit_too_long": '这份档案太长，这里只读到了前一段，不能在这里编辑',   // app.js:2835
  "vault.edit_too_long_1": '这份档案超过查看上限，界面里只有前 ',   // app.js:2838
  "vault.edit_too_long_2": ' 字符，',   // app.js:2838
  "vault.edit_too_long_3": '所以**不提供编辑** —— 在这里保存会把没读到的部分抹掉。',   // app.js:2839
  "vault.edit_too_long_4": '请用别的编辑器直接改 data/vault 下的文件。',   // app.js:2840
  "vault.save_too_long_1": '这份档案太长，编辑器里只有前 ',   // app.js:2962
  "vault.save_too_long_2": '在这里保存会把没读到的部分删掉，所以不允许保存 —— ',   // app.js:2964
  "vault.save_too_long_3": '请直接用别的编辑器改 data/vault 下的那份文件。',   // app.js:2965
  "vault.save_need_name": '先填个名字 —— 档案的路径就是它。',   // app.js:2973
  "vault.save_failed": '保存失败',   // app.js:3002
  "vault.created": `已新建 {name}。`,   // app.js:3024
  "vault.saved": `已保存 {name}（模型下一轮就能看到它）。`,   // app.js:3025
  "vault.saved_note": 'README 索引与别处的双链没有自动更新，下面是**现在的**结构校验结果。',   // app.js:3026
  "vault.created_short": '已新建',   // app.js:3031
  "vault.saved_short": '已保存',   // app.js:3031
  "vault.reload_failed": '（目录树与结构校验这次没刷新成功，点上面「刷新」再试一次。）',   // app.js:3045
  "vault.force_overwriting": '正在用最新版本覆盖保存…',   // app.js:3058
  "vault.fresh_taken": '已换成最新内容（你刚才的改动被放弃了）。',   // app.js:3071
  "vault.delete_confirm_lead": `要删除 {name}（{kind}）。`,   // app.js:3110
  "vault.delete_confirm_inbound": `有 {n} 份档案指向它，删掉之后那些链接会变成"还没写"：`,   // app.js:3113
  "vault.delete_confirm_no_inbound": '现在没有别的档案指向它（删掉不会让任何双链失效）。',   // app.js:3116
  "vault.delete_confirm_note": '删除会直接抹掉这个文件，没有回收站：vault 不为它留快照。',   // app.js:3119
  "vault.delete_confirm_alt": '（如果只是想清空内容，改成编辑再保存更好。）',   // app.js:3120
  "vault.delete_preview": '看一眼现在的正文',   // app.js:3123
  "vault.delete_readme": 'README 是索引，不能删 —— 要清空它就重写内容',   // app.js:3134
  "dialog.delete_file.title": '删除这份档案？',   // app.js:3136
  "action.deleting": '删除中…',   // app.js:3142
  "vault.delete_conflict": '这份档案在你要删之前被改过了，先看一眼最新内容',   // app.js:3158
  "vault.delete_failed": '删除失败',   // app.js:3161
  "vault.deleted": `已删除 {name}。`,   // app.js:3176
  "vault.deleted_backlinks": `有 {n} 份档案的双链变成了"还没写"：{list}`,   // app.js:3178
  "vault.deleted_no_backlinks": '没有别的档案指向它。',   // app.js:3179
  "vault.browser_title": '背景档案',   // app.js:3198
  "vault.pending_sessions_html": `还有 <span class="v-big">{n}</span> 个会话待整理 · `,   // app.js:3226
  "vault.pending_events_html": `<span class="v-big">{n}</span> 条记录`,   // app.js:3227
  "vault.pending_latest": '已经整理到最新',   // app.js:3228
  "vault.chip_stale": '<span class="chip warn">待整理 · 有异常</span>',   // app.js:3230
  "vault.chip_pending": '<span class="chip warn">待整理</span>',   // app.js:3232
  "vault.chip_latest": '<span class="chip ok">最新</span>',   // app.js:3233
  "vault.budget_over": '（超预算，注入时会被截断）',   // app.js:3241
  "vault.readme_chars": 'README {used} / {cap} 字符',   // app.js:3242
  "vault.readme_missing": '还没读到 README',   // app.js:3242
  "vault.file_count": `{files} 份档案`,   // app.js:3243
  "vault.no_readme": ' · 没有 README（模型看不到索引）',   // app.js:3244
  "vault.problems_header": `<div class="vault-problems warn"><strong>结构校验有 {n} 处问题</strong>`,   // app.js:3249
  "vault.problems_ok": '<div class="vault-problems ok">结构校验通过</div>',   // app.js:3251
  "vault.pane_html": `{path}</div>
      {problems}
      <div class="vault-sub">{pending}</div>
      {stale}
    </div>
    <div class="section-title" id="dream-runs-title">运行历史</div>
    <div id="dream-runs"></div>`,   // app.js:3259
  "dream.format.word": '格式化',   // app.js:3290
  "dream.danger_title": '危险区 · 格式化并初始化 dream',   // app.js:3297
  "dream.danger_sub_1": '清掉 <strong>背景档案</strong>、<strong>运行记录</strong>、',   // app.js:3300
  "dream.danger_sub_2": '<strong>整理进度</strong>这三样，然后初始化一个空的 vault 骨架。',   // app.js:3301
  "dream.danger_sub_3": '只动 dream 这一层：<strong>对话历史、记忆条目、接口配置与 API key 一个字节都不碰</strong>。',   // app.js:3302
  "dream.danger_note_1": '档案是会话的<strong>投影</strong> —— 重新整理一遍就能再做出来',   // app.js:3305
  "dream.danger_note_2": '（代价是再花一次整理的钱）；但<strong>手写的内容不会回来</strong>。',   // app.js:3306
  "dream.danger_button": '格式化并初始化 dream',   // app.js:3311
  "dream.format_done": '格式化完成（只动了 dream 这一层）',   // app.js:3325
  "dream.unit_files": '份',   // app.js:3327
  "dream.runs": '运行记录',   // app.js:3328
  "dream.unit_records": '条',   // app.js:3328
  "dream.progress": '整理进度',   // app.js:3329
  "dream.unit_sessions": '个会话',   // app.js:3329
  "dream.format_cursor": `清掉的游标：{n} 个会话`,   // app.js:3335
  "dream.reinit_done": '已经重新初始化了一个空的 vault 骨架（占位 README + sections/ + entities/）',   // app.js:3338
  "dream.reinit_placeholder_note": '—— 占位 README 不会被注入，整理一轮之后它才会有内容。',   // app.js:3339
  "dream.reinit_skipped": '没有重新初始化 vault 骨架 —— 下次整理时它会自己建。',   // app.js:3340
  "dream.format_leftovers": `有 {n} 处没删掉，请手动删：`,   // app.js:3345
  "dream.unknown_value": '（这次没读到，未知）',   // app.js:3380
  "dream.will_clear": '将要清掉（不可逆）：',   // app.js:3388
  "dream.runs_long": '整理运行记录',   // app.js:3390
  "dream.progress_cursor": '整理进度（游标）',   // app.js:3391
  "dream.will_keep": '不会被碰：',   // app.js:3394
  "reset.row_chats": '对话历史',   // app.js:3395
  "reset.row_memories": '记忆条目',   // app.js:3396
  "reset.unit_active_items": '条有效',   // app.js:3396
  "reset.row_providers": '接口配置与 key',   // app.js:3397
  "reset.unit_items": '个',   // app.js:3397
  "dream.why_not_disaster": '为什么这不是灾难：档案是会话的投影，重新整理一遍就能再做出来',   // app.js:3401
  "dream.why_not_disaster_2": '（代价是再花一次整理的钱）；但手写的内容不会回来。',   // app.js:3402
  "dream.ask_prefix": '请输入「',   // app.js:3405
  "dream.ask_suffix": '」以确认：',   // app.js:3408
  "dream.format.title": '格式化并初始化 dream？',   // app.js:3426
  "dream.format.confirm": '执行格式化',   // app.js:3428
  "dream.format_done_lead": '已格式化并初始化 dream：',   // app.js:3453
  "dream.format_after_files": `档案 {before} → {after} · `,   // app.js:3463
  "dream.format_after_runs": `运行记录 {before} → {after} · `,   // app.js:3464
  "dream.format_after_progress": `进度 {before} → {after}`,   // app.js:3465
  "dream.none_1": '<div class="session-sub">还没有运行记录。点上面的「整理一次」跑第一轮',   // app.js:3502
  "dream.none_2": ' —— 整理是单向的，没有预演。</div>',   // app.js:3503
  "dream.chip_dry": '<span class="chip dry">预演（旧功能）</span>',   // app.js:3513
  "dream.chip_applied": '<span class="chip">已应用</span>',   // app.js:3514
  "dream.no_files_touched": '没动档案',   // app.js:3518
  "dream.files_touched": `改动 {n} 份档案`,   // app.js:3519
  "dream.run_head_html": `
      <div class="run-head">
        <span class="run-when">{when}</span>
        <span class="run-id">{id}</span>
        {mode}{status}
        <span class="chip">{sessions} 个会话</span>
        <span class="chip">{changed}</span>
        <span class="run-summary">{summary}</span>
        <span class="chip">{tokens} token</span>
      </div>
      <div class="run-body hidden"></div>`,   // app.js:3520
  "dream.detail_loading": '<div class="session-sub">加载中…</div>',   // app.js:3541
  "dream.diff_created": '<div class="diff-group"><div class="diff-group-title">新建</div>',   // app.js:3560
  "dream.diff_updated": '<div class="diff-group"><div class="diff-group-title">更新</div>',   // app.js:3568
  "dream.diff_chars_both": `{from} → {to} 字符</div>`,   // app.js:3571
  "dream.diff_deleted": '<div class="diff-group"><div class="diff-group-title">删除</div>',   // app.js:3575
  "dream.diff_chars_one": `{chars} 字符</div>`,   // app.js:3578
  "dream.diff_empty": '<div class="session-sub">（本次没有改动任何档案）</div>',   // app.js:3583
  "dream.check_after_header": `<div class="vault-problems warn"><strong>这次运行之后的结构校验：{n} 处问题</strong>`,   // app.js:3587
  "dream.check_after_ok": '<div class="vault-problems ok">这次运行之后结构校验通过</div>',   // app.js:3589
  "dream.cursor_advanced": `游标推进 {n} 个会话`,   // app.js:3597
  "dream.cursor_to": `{from} → 第 {to} 条`,   // app.js:3599
  "dream.stop_reason": `结束原因 {reason}`,   // app.js:3603
  "dream.elapsed": `耗时 {duration}`,   // app.js:3605
  "dream.candidates": `候选 {n} 个会话 · {events} 条记录`,   // app.js:3611
  "dream.no_summary": '<div class="session-sub">(模型没有留下总结)</div>',   // app.js:3615
  "dream.run_skipped": '<div class="run-skip">没有待整理的记录，这次**没有调用模型**（省了一次钱）。</div>',   // app.js:3617
  "dream.final_trace_lead": ` 想追回某份文件当时写了什么，可以去看完整轨迹 `,   // app.js:3649
  "dream.final_trace_full": `（里面存着每一次 <code class="ic">vault_write</code> 的全文）`,   // app.js:3651
  "dream.final_trace_tail": ' —— 这是取消回滚之后唯一的追回途径。',   // app.js:3652
  "dream.final_dry_1": '<div class="run-final">这是一次<strong>预演（旧功能，已经取消）</strong>：',   // app.js:3656
  "dream.final_dry_2": '当时没有改动任何档案 —— 上面这些差异只是给你看的，跑完就丢掉了，游标也没有动。',   // app.js:3657
  "dream.final_dry_3": '现在要重来只有一条路：用「格式化并初始化 dream」清空重跑。',   // app.js:3658
  "dream.final_note_undo": '<strong>整理结果不可撤销：没有回滚，也没有预演。</strong>',   // app.js:3662
  "dream.final_note_1": ' vault 不会为这次运行留下可还原的副本 —— 写进去的就是写进去了。',   // app.js:3663
  "dream.final_note_2": ' 如果这一轮改坏了，只能用「格式化并初始化 dream」清空重来',   // app.js:3664
  "dream.final_note_3": '（那会连同运行记录与整理进度一起清掉，并再花一次整理的钱）。',   // app.js:3665
  "dream.starting": '开始整理…（会真正写档案并推进游标，而且不可撤销）\n',   // app.js:3683
  "dream.run_started": `运行 {id}（{when}）`,   // app.js:3700
  "dream.model": `模型 {model}`,   // app.js:3703
  // ── Dream 页的整理模型（ADR-103）──────────────────────────
  "dream.model_follow": '跟随默认',   // app.js:1891
  "dream.model_follow_named": `跟随默认（{model}）`,   // app.js:1893
  "dream.model_none": '（没有可用模型，先去「接口」页添加）',   // app.js:1878
  "dream.model_saved": '整理模型已保存',   // app.js:1943
  "dream.model_save_failed": `保存整理模型失败：{msg}`,   // app.js:1946
  "dream.new_records": ` {n} 条新记录（游标 {from} → 末尾 {to}）`,   // app.js:3706
  // 待整理会话那一行**整行**是一条文案：引号也是各语言自己的（英文界面下不该冒出「」）
  "dream.record_line": `· {id}「{title}」`,   // app.js:3731
  "dream.untitled": '(无标题)',   // app.js:3733
  "dream.step": `· 第 {n} 步`,   // app.js:3709
  "dream.skipped": '\n没有待整理的记录，这次没有调用模型（不花钱）。',   // app.js:3717
  "dream.no_summary_paren": '(无总结)',   // app.js:3718
  "dream.done_created": `\n完成：新建 {n} 份 · `,   // app.js:3721
  "dream.done_updated": `更新 {n} 份 · `,   // app.js:3722
  "dream.done_deleted": `删除 {n} 份`,   // app.js:3723
  "dream.error": `出错：{msg}`,   // app.js:3732
  "usage.chat_count": `{n} 个会话`,   // app.js:3746
  "usage.total": '总 token',   // app.js:3748
  "usage.prompt": '输入 token',   // app.js:3776
  "usage.completion": '输出 token',   // app.js:3777
  "usage.reasoning": '思考 token',   // app.js:3749
  "usage.cache_hit": '缓存命中',   // app.js:3750
  "usage.cache_miss": '缓存未命中',   // app.js:3750
  "usage.pane_html": `
    <div class="usage-card"><div class="usage-grid">
      {cells}
    </div></div>
    <div class="usage-card">
      <div class="section-title">按接口分家</div>
      <table class="usage-table">
        <thead><tr><th>接口</th><th class="num">{prompt}</th><th class="num">{completion}</th>
        <th class="num">缓存命中</th><th class="num">总计</th></tr></thead>
        <tbody>{providers}</tbody>
      </table>
    </div>
    <div class="usage-card">
      <div class="section-title">逐对话</div>
      <table class="usage-table">
        <thead><tr><th>会话</th><th class="num">{prompt}</th><th class="num">{completion}</th>
        <th class="num">缓存命中</th><th class="num">总计</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>`,   // app.js:3768
  "api.provider_count": `{n} 个接口`,   // app.js:3817
  "api.pane_html": `
    <div class="section-title">新会话默认</div>
    <div class="provider-card {defaultCls}">
      <div class="provider-fields">
        <div class="field wide">
          <label>默认模型</label>
          <select id="default-model">{defaultOptions}</select>
          <div class="hint">新开的对话默认用它。单个会话可以在右上角随时切换，不影响这里。</div>
        </div>
      </div>
    </div>

    <div class="section-title">已配置的接口</div>
    {cards}

    <div class="add-provider">
      <select id="preset-pick">{presets}<option value="__custom">自定义（自己填 base_url）</option></select>
      <button class="btn-primary" id="preset-add">添加这个接口</button>
      <span class="hint" style="font-size:11.5px;color:var(--muted)">
        添加后填入 API Key，再点卡片上的「获取模型列表」。
      </span>
    </div>

    <div class="provider-note">
      所有 key 只存在本机的 <code class="ic">data/providers.json</code>，不会上传到任何地方。
      模型列表刻意不预填：点「获取模型列表」会去调该供应商的 <code class="ic">/models</code>，
      拿到的才是真实在售的模型。
    </div>`,   // app.js:3830
  "api.default_set": '已设为新会话默认',   // app.js:3866
  "api.current_key": `当前：{value}（留空表示不改）`,   // app.js:3877
  "api.local_only": '只存在本机的 data/providers.json',   // app.js:3878
  "api.provider_card_html": `
  <div class="provider-card {defaultCls}" data-id="{id}">
    <div class="provider-head">
      <span class="p-label">{label}</span>
      <span class="chip id">{idText}</span>
      {defaultChip}
      {noKeyChip}
      {noToolsChip}
      <span class="p-actions">
        <button class="mini" data-act="fetch">获取模型列表</button>
        <button class="mini" data-act="save">保存</button>
        <button class="mini danger" data-act="del">删除</button>
      </span>
    </div>
    <div class="provider-fields">
      <div class="field"><label>名称</label>
        <input data-f="label" value="{labelValue}"></div>
      <div class="field"><label>API Base URL</label>
        <input data-f="base_url" value="{baseUrl}"></div>
      <div class="field"><label>API Key</label>
        <input data-f="api_key" type="password" autocomplete="off"
               placeholder="{keyPlaceholder}">
        <div class="hint">{keyHint}</div></div>
      <div class="field wide"><label>模型</label>
        <div class="model-table" data-f="model-table">
          <div class="model-head">
            <span>模型 ID</span><span>上下文上限</span><span>显示名（可选）</span><span></span>
          </div>
          {modelRows}
        </div>
        <button class="mini model-add" data-act="model-add" type="button">＋ 添加模型</button>
        <div class="hint">
          「上下文上限」可以写 <code class="ic">128k</code> 或 <code class="ic">1M</code>
          （k = 1000，M = 1000k）；到它的 75% 会自动 compact，<strong>不填就是 200k</strong>。
          同一家里不同模型的窗口差别很大（8k 到 1M 都有），所以只能按模型填。
          <br>「显示名」只影响下拉框里怎么显示，<strong>不填就用模型 id</strong>。
        </div></div>
      <div class="field"><label>协议</label>
        <select data-f="type">
          <option value="openai" {typeOpenai}>OpenAI 兼容（绝大多数）</option>
          <option value="anthropic" {typeAnthropic}>Anthropic 兼容（Kimi Code、Claude）</option>
        </select></div>
      <div class="field"><label>思考档位（逗号分隔，留空 = 不下发）</label>
        <input data-f="thinking_options" value="{thinkingOptions}"></div>
      <div class="field"><label>回传历史思维链</label>
        <select data-f="send_reasoning_content">
          <option value="1" {reasoningYes}>是（DeepSeek 要求）</option>
          <option value="0" {reasoningNo}>否（OpenAI 等更严格）</option>
        </select></div>
      <div class="field wide"><label>支持图片的模型（逗号或换行分隔）</label>
        <textarea data-f="vision_models" rows="2"
                  placeholder="deepseek-flash">{visionModels}</textarea>
        <div class="hint">
          这里列出的模型才允许<strong>直接发送图片</strong>（「多模态」开着时用）。<br>
          <strong>留空 = 未知</strong>：不拦，但真发出去可能被模型静默忽略。<br>
          当前内置的已知情况：<code class="ic">deepseek-flash</code> 支持、
          <code class="ic">deepseek-v4-pro</code> 不支持。
        </div></div>
    </div>
    {notes}
  </div>`,   // app.js:3879
  "api.fetching": '获取中…',   // app.js:3967
  "api.fetched_models": `拿到 {n} 个模型`,   // app.js:3975
  "api.fetch_failed": `获取失败：{msg}`,   // app.js:3978
  "api.fetch_models": '获取模型列表',   // app.js:3980
  "api.delete_confirm": `删除接口「{id}」及其 key？`,   // app.js:3984
  "api.new_id_prompt": '给这个接口起个 id（字母数字，例如 myapi）',   // app.js:3997
  "api.base_url_prompt": 'Base URL（例如 https://api.example.com/v1）',   // app.js:4004
  "api.added_fill_key": '已添加，请填入 API Key',   // app.js:4007
  "api.model_label_ph": '例如 DeepSeek V4.1 Flash',
  "api.model_remove": '删除这一行',
  "api.model_id_required": '这一行填了别的、却没填模型 ID —— 补上，或者整行清空。',
  "api.model_duplicate": `模型 ID「{id}」填了两次。`,
  "api.model_context_bad": `「{id}」的上下文上限要写正整数，或者带 k / M 的数（例如 128k、1M；k = 1000，M = 1000k）。`,
  "theme.follow_system_desc": '跟随 Windows 的浅色 / 深色设置',   // app.js:4017
  "theme.light_desc": '始终使用浅色界面',   // app.js:4018
  "theme.dark_desc": '始终使用深色界面',   // app.js:4019
  "tools.missing_key_chip": '<span class="chip" style="color:var(--err)">缺 key —— 搜索用不了</span>',   // app.js:4034
  "tools.current_key": `当前已存：{masked}`,   // app.js:4045
  "tools.no_key": '当前还没有 key。Tavily 有免费额度，去 tavily.com 注册一个。',   // app.js:4046
  "tools.pane_html": `
    <div class="reset-card">
      <div class="section-title">联网工具</div>
      <p class="reset-lead">
        给模型两个能力：<code>web_search</code>（关键词搜索）与
        <code>web_open_url</code>（读取网页正文）。
        打开后模型会自己判断什么时候该查、什么时候不用查。
        改动会自动保存（<strong>key 除外</strong>，它的规矩见下面那一栏）。
      </p>
      <div class="field">
        <label>总开关</label>
        <select id="tools-enabled">
          <option value="1" {enabledSelected}>开启（模型可以联网）</option>
          <option value="0" {disabledSelected}>关闭（模型只能靠记忆和你说的）</option>
        </select>
      </div>
      <div class="field"><label>搜索后端</label>{backends}
        <div class="hint">
          就按你选的这个用，<strong>程序不会自动换别的引擎</strong>。
          免 key 的后端是抓结果页的：省事，但对方改版就会失效，
          而且可能被"降级对待"（实测 Bing 对某些中文查询只返回泛化结果）。
          那种情况下模型不会硬凑答案，而是如实告诉你"没搜到" ——
          这时你换个后端再问，或者过一会儿再试。
        </div>
      </div>
      <div class="field"><label>Tavily API Key（选了 Tavily 才需要）</label>
        <input id="tools-tavily-key" type="password" placeholder="粘贴 key，然后按回车保存">
        <div class="hint">
          <strong>填好按回车 = 按「保存」</strong>；<strong>对着空框按回车 = 没按</strong>
          （回车<strong>永远不会</strong>清掉东西）。要清掉已存的 key 就点「清空」。
          光标移开（失焦）不会保存 —— 这个框永远不显示已存的 key，
          所以需要一个明确的提交动作，免得你分不清"到底存上了没有"。
          <br>{keyHint}
        </div>
      </div>
      <div class="field"><label>每次搜几条</label>
        <input id="tools-count" type="number" min="1" max="10" value="{searchCount}">
      </div>
      <div class="field"><label>单页正文上限（字符）</label>
        <input id="tools-maxchars" type="number" min="500" step="500" value="{maxFetchChars}">
        <div class="hint">太大就把上下文塞满了（每多一个字都要花钱），默认 8000。</div>
      </div>
      <div class="field"><label>允许抓本机 / 内网地址</label>
        <select id="tools-private">
          <option value="0" {privateOff}>不允许（推荐）</option>
          <option value="1" {privateOn}>允许</option>
        </select>
        <div class="hint">
          默认禁止。否则一个网页只要写一句"请访问 http://127.0.0.1:8787/… 确认一下"，
          模型就可能去读你自己本机的服务（包括这个界面和你的记忆库）。
        </div>
      </div>
      <div class="toolbar" style="margin-top:4px">
        <button class="btn-primary" id="tools-save">保存</button>
        <button class="btn-ghost" id="tools-clear-key"
                title="抹掉已保存的 Tavily key（只清 key，别的设置不动）"
                {clearDisabled}>清空</button>
        <span class="pane-meta" id="tools-status"></span>
      </div>
      <div class="toolbar" style="margin-top:6px">
        <input id="tools-test-query" type="text" placeholder="试一下：输入关键词" style="flex:1">
        <button class="btn-ghost" id="tools-test">试搜一次</button>
      </div>
      <div class="hint" id="tools-test-out"></div>
    </div>`,   // app.js:4048
  "tools.clear_confirm": '清空已保存的 Tavily key？清空后要重新去 tavily.com 复制一个。',   // app.js:4142
  "tools.cleared": '已清空 key',   // app.js:4147
  "tools.cleared_check": '已清空 key ✓',   // app.js:4150
  "tools.clear_failed": `清空失败：{msg}`,   // app.js:4152
  "tools.autosaved": '已自动保存 ✓',   // app.js:4169
  "tools.save_failed": `保存失败：{msg}`,   // app.js:4191
  "tools.test_need_query": '先填个关键词',   // app.js:4220
  "tools.test_searching": '搜索中…',   // app.js:4222
  "tools.test_backend": `后端：{backend}`,   // app.js:4230
  "tools.test_no_results": '没有结果。',   // app.js:4232
  "tools.test_failed": `失败：{msg}`,   // app.js:4234
  "dialog.working": '处理中…',   // app.js:4284
  "reset.unit_active": `{n} 条有效`,   // app.js:4307
  "reset.unit_deleted": ` · {n} 条已删除`,   // app.js:4308
  "reset.unit_ops": ` · {n} 条操作记录`,   // app.js:4309
  "reset.unit_dream_runs": `{n} 次整理运行`,   // app.js:4310
  "reset.unit_files": `{n} 个文件`,   // app.js:4311
  "reset.row_attachments": '附件文件',
  "reset.row_prompt_overrides": '提示词覆盖',
  "reset.row_providers_short": '接口配置',   // app.js:4312
  "reset.unit_providers_with_keys": `{n} 个（含 API key）`,   // app.js:4312
  "reset.row_disk": '占用空间',   // app.js:4313
  "brand.version": 'v{version}',   // 侧栏第二行：版本号（ADR-096）
  "brand.version_unknown": '版本未知',
  "reset.pane_html": `
    <div class="reset-card">
      <div class="section-title">恢复出厂设置</div>
      <p class="reset-lead">
        把本机保存的一切抹掉，回到刚装好、还没用过的状态。
        <strong>此操作无法撤销。</strong>
      </p>
      <table class="reset-table">
        <tbody>{rows}</tbody>
      </table>
      <div class="hint reset-note">
        数据目录：<code>{dataDir}</code><br>
        <strong>接口配置（含 API key）也会一起删掉。</strong>
        key 只存在这个目录里，本项目不预置任何 key，
        所以重置后就是真正的空状态 —— 需要用的时候自己重新添加一个。
      </div>
      <button class="btn-danger" id="reset-start" {disabled}>
        {label}
      </button>
    </div>`,   // app.js:4381
  "reset.confirm_title": '确认恢复出厂设置？',   // app.js:4405
  "reset.confirm_html": `
        <p>将永久删除本机保存的全部内容：</p>
        <ul>{items}</ul>
        <p class="danger-text">删除后无法恢复，包括已配置的 API key。</p>`,   // app.js:4406
  "reset.delete_forever": '永久删除',   // app.js:4410
  "reset.done_lead": `已恢复出厂设置：清掉 {sessions} 个会话、`,   // app.js:4414
  "reset.done_tail": `{memories} 条记忆、{providers} 个接口`,   // app.js:4415
  "reset.failed": `恢复出厂设置失败：{msg}`,   // app.js:5340
  "reset.leftover_warning": `注意：有文件没能删掉，残留在 {path}`,   // app.js:4417
  "init.backend_unreachable": '无法连接后端：',   // app.js:4435
  "dream.confirm_1": '整理是单向的、不可撤销，而且没有预演（预演已取消）。\n\n',   // app.js:4501
  "dream.confirm_2": '如果这一轮改坏了，只能用「格式化并初始化 dream」清空重来 —— ',   // app.js:4502
  "dream.confirm_3": '那会连同运行记录与整理进度一起清掉，并再花一次整理的钱。\n\n',   // app.js:4503
  "dream.confirm_4": '确定现在整理？',   // app.js:4504
  "session.untitled": '(空会话)',   // app.js:313
  "chat.summary_just_now": '（刚刚发生）',   // app.js:857
  "memory.deleted_chip": '<span class="chip">已删除</span>',   // app.js:1526
  "memory.delete_btn": '<button class="mini danger" data-act="del">删除</button>',   // app.js:1526
  "dream.runs_count": `{n} 次运行`,   // app.js:1616
  "dream.no_runs_short": '还没有运行记录',   // app.js:1616
  "dream.no_summary_short": '(无总结)',   // app.js:3470
  "usage.empty_rows": '<tr><td colspan="5" class="session-sub">还没有用量</td></tr>',   // app.js:3710
  "api.no_default_options": '<option value="">（先添加接口并「获取模型列表」）</option>',   // app.js:3751
  "api.no_providers": '<div class="session-sub">还没有接口。下面挑一个预设添加。</div>',   // app.js:3751
  "api.default_chip": '<span class="chip insight">默认</span>',   // app.js:3773
  "api.no_key_chip": '<span class="chip" style="color:var(--err)">缺 key</span>',   // app.js:3773
  "api.no_tools_chip": '<span class="chip" style="color:var(--warn)">不支持工具 → 记忆会失效</span>',   // app.js:3773
  "api.key_stored": '已保存',   // app.js:3773
  "api.key_paste": '粘贴你的 key',   // app.js:3773
  "reset.nothing_to_clear": '已经没有可清空的数据了',   // app.js:4157
  "reset.factory_reset": '恢复出厂设置',   // app.js:4157
  "app.title": "O₃ᶻᵒⁿᵉ",
  "nav.new_chat": "新对话",
  "nav.settings": "设置",
  "picker.model_title": "选择接口与模型",
  "picker.effort_title": "思考档位",
  "toggle.web.title": "这个会话是否允许模型自己联网搜索",
  "toggle.multimodal.title": "图片是直接发给模型，还是只发本地识别出的文字",
  "chat.empty_title": "开始一段对话",
  "chat.empty_sub": "模型会自己决定记住什么。记忆跨会话保留，可以随时整理。",
  "attachment.attach_title": "添加图片（也可以拖进来，或直接粘贴截图）",
  "chat.input_placeholder": "说点什么…（Enter 发送，Shift+Enter 换行，可直接粘贴截图）",
  "chat.send": "发送",
  "chat.stop": "停止",
  "settings.title": "设置",
  "settings.back": "返回对话",
  "settings.nav.memory": "记忆",
  "settings.nav.dream": "Dream",
  "settings.nav.usage": "用量",
  "settings.nav.api": "接口",
  "settings.nav.tools": "工具",
  // 提示词（契约 docs/13-prompts-page.md §4）。这一页的**内容**（提示词原文、
  // 工具描述、每组/每条的 when 与 condition）全是服务端按当前语言取好的，
  // 这里只管外壳：侧栏项、页首说明、状态徽标、字符数、参数表头、复制按钮。
  "settings.nav.prompts": "提示词",
  "prompts.intro": `当前语言下<strong>真正发给模型的东西</strong>：全部提示词（含只在后台整理时用的那两条）与全部工具定义。
      记忆条目、README 与历史是 harness 每轮按当时的状态拼进去的，不在这里 ——
      要看完整的一次请求，用 <code>run.cmd --show-prompt</code>，或在界面上重放那条会话。`,
  "prompts.section_prompts": "提示词",
  "prompts.section_tools": "工具定义",
  "prompts.summary": `共 {prompts} 条提示词 · {tools} 个工具定义`,
  "prompts.on": `其中 {on} 个工具的当前状态是「生效」；`,
  // 这三条**不带分隔符**：分组 summary 里"为 0 的那种不显示"，把 ` · ` 写进文案
  // 就省不掉中间/开头/末尾那一个（app.js 里那两处 join(' · ') 负责连接）
  "prompts.tally_on": `生效 {n} 条`,
  "prompts.tally_off": `未注册 {n} 条`,
  "prompts.tally_session": `会话级 {n} 条`,
  "prompts.state.on": "生效",
  "prompts.state.off": "未注册",
  "prompts.state.session": "会话级",
  "prompts.chars": `{n} 字符`,
  "prompts.placeholders": `占位符：{list}`,
  "prompts.text": "提示词原文",
  "prompts.params": "参数（JSON Schema）",
  "prompts.raw_json": "原始 JSON",
  "prompts.required_yes": "必填",
  "prompts.no_params": "没有参数。",
  "prompts.col_name": "参数",
  "prompts.col_type": "类型",
  "prompts.col_required": "必填",
  "prompts.col_desc": "说明",
  "prompts.group_count": `{n} 条`,
  "prompts.copy": "复制",
  "prompts.copied": "已复制",
  "prompts.copy_failed": "复制不了：这个环境不允许脚本写剪贴板。选中上面的文字手动复制即可。",
  "prompts.loading": "加载中…",
  "prompts.load_error": `加载失败：{error}`,
  "prompts.none": "（这里什么都没有）",
  // ── 可编辑（契约 docs/14-editable-prompts.md §8.1 的必需清单）─────────────
  // 这一块的语言隔离是**看得见**的：页首那条 `prompts.overrides_summary` 把
  // 两门语言各改了几条并排写出来（0 也写），「复原」的确认文案里也点明只动
  // 当前语言。用户心里的问题就是"改中文会不会把英文带坏"。
  "prompts.overrides_summary": `两门语言各存各的（改一门不影响另一门）：{parts}`,
  "prompts.overrides_name.zh": "中文",
  "prompts.overrides_name.en": "英文",
  "prompts.overrides_lang": `{name}已改 {n} 条`,
  "prompts.edit": "编辑",
  "prompts.cancel": "取消",
  "prompts.edited": "已修改",
  "prompts.revert": "复原",
  "prompts.revert_all": "把这门语言的提示词全部复原",
  "prompts.revert_all_none": "这门语言现在没有改动，没有可复原的东西",
  "prompts.revert_confirm": `把「{id}」恢复成内置原文？

只复原「当前语言（{language}）」的这 1 条：另一门语言一个字都不会动。`,
  "prompts.revert_all_confirm": `把 {language} 里改过的 {n} 条提示词全部恢复成内置原文？

只碰 {language} 这一门：另一门语言一个字都不会动。`,
  "prompts.revert_done": `已复原「{id}」：这一条现在用的是内置原文`,
  "prompts.revert_all_done": `已复原 {language} 的 {n} 条改动：它们现在用的都是内置原文`,
  "prompts.revert_failed": `复原失败：{error}`,
  "prompts.save": "保存",
  "prompts.saved": "已保存：这一条现在用的是你写的那一版",
  "prompts.save_identical": "这段文本与内置一模一样，所以没有存成自定义 —— 等于没改",
  "prompts.save_failed": `保存失败：{error}`,
  "prompts.editor_hint_placeholders": `占位符 {list}：它们会被填进正文（记忆、时间、背景档案之类），别删掉。`,
  "prompts.editor_hint_cache": "保存之后，下一条消息的提示词缓存会重建一次（一次性的钱）。",
  "prompts.editor_hint_frozen": "改过的条目会冻结：内置原文将来改进了它也不会自动跟进 —— 想跟进就点「复原」。",
  "prompts.builtin_text": "内置原文",
  "prompts.current_text": "当前生效",
  "prompts.placeholders_missing": `缺少占位符 {list}`,
  "prompts.store_error": `警告：提示词覆盖文件读不出来，现在用的是内置原文，你改过的东西一条都没生效 —— {error}`,
  "settings.nav.general": "常规",
  "settings.nav.reset": "重置",
  "memory.limit_label": "上限",
  "memory.limit_save": "保存上限",
  "memory.search_placeholder": "搜索记忆内容",
  "memory.show_deleted": "显示已删除",
  "action.refresh": "刷新",
  "dream.run_once": "整理一次",
  "dream.session_limit": "会话上限",
  "dream.model_label": "整理模型",
  "dream.model_title": "用哪个模型整理背景档案（只影响整理，不影响对话）",
  "dream.note_1": `
              dream 把会话记录整理成<strong>背景档案</strong>：<code>data/vault/README.md</code>
              ＋ <code>sections/</code> 段落 ＋ <code>entities/</code> 实体。
              它<strong>不改写记忆条目</strong>（那是你明确要求记住的那一套）。
              「整理一次」会真正写档案并推进游标，而且<strong>不可撤销</strong>：
              整理是单向门，不留快照、没有回滚。
              <strong>预演已经取消了</strong>（它要真跑一遍模型、照样花钱，给出的却只是"会改什么"的估计）。
              如果某一轮改坏了，补救的办法是下面「危险区」里的
              <strong>格式化并初始化 dream</strong>：清空之后重新整理一遍就能再做出来，
              代价是再花一次整理的钱 —— 但手写的内容不会回来。
            `,
  "dream.note_2": `
              档案可以直接在这里<strong>新建 / 编辑 / 删除</strong>：
              左树顶上有「新建档案」，右边每一份的右上角有「编辑 / 删除」。
              保存时界面会带上"你看着的那一版"的指纹；如果模型在你编辑期间改过它，
              后端会拒一次并把最新内容摆出来，由你决定放弃还是覆盖。
              删除 README 会被后端拒绝（它是索引），所以 README 上没有删除按钮。
            `,
  "dream.note_3": `
              手改<strong>不会自动更新 README 索引与别处的双链</strong> ——
              结构校验会提示"没有被 README 提到"之类的问题。
              另外，整理正在运行时不能改档案（后端会直接拒绝），等它跑完再试。
            `,
  "dialog.cancel": "取消",
  "dialog.confirm": "确认",
  "settings.language.title": "界面语言",
  "settings.language.lead": "这里选界面的语言。切换立刻生效、不刷新页面；后端的提示、工具描述与系统提示也会跟着换。",
  "settings.language.current": "当前语言：{name}",
  "settings.language.saved": "已切换到 {name}",
  "settings.language.failed": "切换语言失败：{msg}",
  "settings.language.note": "语言存在 data/settings.json 里；HARNESS_LANGUAGE 是默认值。",
  "language.name.zh": "中文",
  "language.name.en": "English",
  "language.desc.zh": "中文界面 + 中文系统提示与工具描述",
  "language.desc.en": "英文界面 + 英文系统提示与工具描述",
  "settings.language.busy": "正在生成回答，等这一轮结束再切换 —— 语言是进程级的，中途切会让模型在同一次回答里换一套系统提示。",
  "settings.general.theme": "主题",
  "settings.general.timezone": "时区",
  "settings.timezone.lead": "`time` 工具返回的时间用哪个时区（偏移小时，范围 -12 ~ +14）。",
  "settings.timezone.current": "当前时区：{zone}",
  "settings.timezone.saved": "时区已改为 {zone}",
  "settings.timezone.failed": "改时区失败：{msg}",
  "settings.timezone.note": "存在 data/settings.json 里；改完下一轮对话生效。",
  "settings.general.output": "单轮最大输出",
  "settings.output.lead": "每一轮最多让模型输出多少 token。改完下一轮对话生效。",
  "settings.output.save": "保存",
  "settings.output.current": "当前：{value}",
  "settings.output.saved": "单轮最大输出已改为 {value}",
  "settings.output.failed": "保存失败：{msg}",
  "settings.output.invalid": "要填一个整数：-1（不限）或 ≥1；也可以写 5k / 1M。",
  "settings.output.unlimited": "不限",
  "settings.output.note": "接受 5000 / 5k / 1M（k = 1000、M = 1000000）；-1 = 不限。存在 data/settings.json 里。",
  "setup.loading_title": "正在启动",
  "setup.checking": "正在检查本地运行环境…",
  "setup.network_retry": "暂时读不到启动状态，正在重试：{msg}",
  "setup.consent_title": "下载完整运行环境？",
  "setup.consent_body": "完整环境包含联网、图片与本地 OCR 依赖。没有发现完整的 .venv；你可以现在下载，也可以直接进入聊天。",
  "setup.missing": "缺少或不可用：{items}",
  "setup.missing_generic": "依赖不完整",
  "setup.mirror_label": "PyPI 镜像地址（可选）",
  "setup.mirror_placeholder": "留空使用 https://pypi.org/simple",
  "setup.mirror_note": "镜像只用于本次 pip 下载；留空时使用官方 PyPI。拒绝或取消不会记住选择，下次启动仍会检查。",
  "setup.download": "下载并安装",
  "setup.skip": "暂不下载，继续聊天",
  "setup.preparing": "正在准备虚拟环境…",
  "setup.downloading": "正在下载完整环境",
  "setup.verifying": "正在验证安装…",
  "setup.finalizing": "正在完成安装…",
  "setup.cancelling": "正在撤销本次下载…",
  "setup.paused": "下载已暂停",
  "setup.pause": "暂停下载",
  "setup.resume": "继续下载",
  "setup.cancel": "退出下载并继续",
  "setup.cancel_note": "退出会停止 pip，删除本次下载的内容，然后进入不使用完整 venv 的聊天界面。",
  "setup.progress_waiting": "已启动；pip 尚未报告可用百分比。",
  "setup.progress_percent": "{value}%",
  "setup.source_official": "下载源：官方 PyPI",
  "setup.source_mirror": "下载源：自定义镜像",
  "setup.failed": "安装未完成：{msg}",
  "setup.action_failed": "操作未完成：{msg}",
  "setup.continued_without": "已继续进入聊天；完整 venv 仍不完整，下次启动会再次询问。",
  "setup.invalid_mirror": "镜像地址必须是有效的 HTTP(S) URL，且不能带账号或密码。",
  "setup.install_busy": "另一个启动窗口正在安装，请在那个窗口继续。",
  "setup.consent_required": "请先确认下载。",
  "setup.not_active": "当前状态不能执行这个操作，请查看最新进度。",
  },

  /* ── English (en) ───────────────────────────────────
     `{name}` 是占位符，由 t(key, vars) 替换；缺 key 时 t() 返回 key 本身并 console.warn。
     带 HTML 的值与 zh 侧的标签序列**必须一致**（build 脚本会机械校验）。 */
  en: {
  "time.just_now": "just now",   // app.js:115
  "time.minutes_ago": "{n} minutes ago",   // app.js:116
  "time.hours_ago": "{n} hours ago",   // app.js:117
  "time.days_ago": "{n} days ago",   // app.js:118
  "theme.follow_system": "Follow system",   // app.js:203
  "theme.light": "Light",   // app.js:203
  "theme.dark": "Dark",   // app.js:203
  "session.list_recent": "Recent chats",   // app.js:236
  "session.list_settings": "Chats",   // app.js:245
  "session.load_failed": "<div class=\"session-sub\">Failed to load: {msg}</div>",   // app.js:262
  "session.open_failed": "Failed to load chat: {msg}",   // app.js:451
  "session.none": "<div class=\"session-sub\" style=\"padding:8px 6px\">No conversations yet</div>",   // app.js:307
  "session.item": `
      <div class="session-title">{title}</div>
      <div class="session-sub">{turns} turns · {time}</div>
      <div class="session-actions">
        <button class="session-act" data-act="rename" title="Rename">✎</button>
        <button class="session-act" data-act="delete" title="Delete this chat">✕</button>
      </div>`,   // app.js:313
  "dialog.rename.title": "Rename chat",   // app.js:336
  "dialog.rename.body": `<p>Name this chat (leave it empty to go back to the automatically taken first line):</p>
      <input id="rename-input" type="text" maxlength="60"
             value="{title}"
             style="width:100%;margin-top:10px;padding:8px 10px;border-radius:8px;
                    border:1px solid var(--border-strong);background:var(--bg);
                    color:var(--text);font-family:inherit;font-size:13.5px">`,   // app.js:337
  "action.save": "Save",   // app.js:343
  "toast.renamed": "Renamed",   // app.js:351
  "toast.rename_restored": "Restored the automatic name",   // app.js:351
  "toast.rename_failed": "Rename failed: {msg}",   // app.js:352
  "dialog.delete_chat.title": "Delete this chat?",   // app.js:371
  "dialog.delete_chat.body": `<p>This deletes the chat <strong>{title}</strong>
      ({turns} turns).</p>
      <p class="danger-text">Only this one conversation is deleted. Memories, provider settings and other chats are unaffected.</p>`,   // app.js:372
  "action.delete": "Delete",   // app.js:375
  "toast.deleted": "Deleted",   // app.js:379
  "toast.delete_failed": "Delete failed: {msg}",   // app.js:386
  "toast.busy_running": "Still generating — stop it or wait for it to finish",   // app.js:392
  "meta.steps_line": "{steps} steps · {reason} · {elapsed}s · ",   // app.js:439
  "chat.empty_html": `
    <div class="empty" id="chat-empty">
      <div class="empty-title">Start a conversation</div>
      <div class="empty-sub">The model decides for itself what to remember. Memories persist across chats and can be tidied up anytime.</div>
    </div>`,   // app.js:451
  "chat.new_title": "New chat",   // app.js:456
  "picker.no_models": "<option value=\"\">(No model available — add one in Settings → Providers)</option>",   // app.js:498
  "picker.effort_none": "<option value=\"\">thinking: not sent</option>",   // app.js:541
  "picker.effort_option": "thinking: {value}",   // app.js:597
  "toast.save_model_failed": "Failed to save the model choice: {msg}",   // app.js:563
  "toggle.web.on": "Web search: on",   // app.js:580
  "toggle.web.off": "Web search: off",   // app.js:580
  "toggle.web.disabled": "Web access is switched off in Settings → Tools",   // app.js:582
  "toggle.web.unavailable": "Search backend unavailable (switched off or missing key) — have a look at Settings → Tools",   // app.js:584
  "toggle.web.on_title": "This chat lets the model search on its own; click to turn it off",   // app.js:586
  "toggle.web.off_title": "This chat does not let the model search on its own (opening a link you hand it still works); click to turn it on",   // app.js:587
  "toast.save_web_failed": "Failed to save the web switch: {msg}",   // app.js:598
  "toolcfg.button": "Tools",   // index.html (toolbar button)
  "toolcfg.title_attr": "Choose which tools the model can use in this chat (with no chat open, edits the default for new chats)",   // index.html
  "toolcfg.title_session": "Tools available in this chat",   // app.js openToolConfig
  "toolcfg.title_default": "Tools available in new chats",   // app.js openToolConfig
  "toolcfg.target_session": "Editing: this chat. Changes apply only here.",   // app.js renderToolConfigPopup
  "toolcfg.target_default": "Editing: the default config. Every new chat is born as a copy of it.",   // app.js renderToolConfigPopup
  "toolcfg.search_placeholder": "Search tools…",   // index.html
  "toolcfg.empty": "No matching tools",   // app.js renderToolConfigPopup
  "toolcfg.close": "Done",   // index.html
  "toolcfg.enabled_count": "{n}/{total} enabled",   // app.js renderToolConfigCount
  "toast.save_toolcfg_failed": "Failed to save tool config: {msg}",   // app.js closeToolConfig
  "toast.load_toolcfg_failed": "Failed to load tool config: {msg}",   // app.js openToolConfig
  "multimodal.badge": "Only the recognised text will be sent",   // app.js:612
  "multimodal.ocr_label": "Multimodal is off · only the recognised text below was sent",   // app.js:615
  "toggle.multimodal.on": "Multimodal: on",   // app.js:630
  "toggle.multimodal.off": "Multimodal: off",   // app.js:630
  "toggle.multimodal.on_title": "Images go straight to the model with the request; click to switch to \"send only the text recognised on this machine\"",   // app.js:632
  "toggle.multimodal.off_title": "Multimodal is off: images are OCR'd locally and only the recognised text is sent to the model, the image itself is not sent; click to send images again",   // app.js:633
  "toast.save_multimodal_failed": "Failed to save the multimodal switch: {msg}",   // app.js:646
  "ocr.failed": "Recognition failed: {msg}",   // app.js:757
  "ocr.no_text": "No text was recognised.",   // app.js:759
  "ocr.no_record": "No recognition record was kept for this one.",   // app.js:760
  "ocr.count_images": "{n} images",   // app.js:785
  "ocr.count_chars": "{n} chars",   // app.js:786
  "ocr.count_failed": "{n} failed",   // app.js:787
  "ocr.failed_short": "recognition failed",   // app.js:787
  "ocr.count_empty": "{n} with no text",   // app.js:788
  "ocr.empty_short": "no text recognised",   // app.js:788
  "ocr.count_norec": "{n} with no record",   // app.js:789
  "ocr.norec_short": "no record",   // app.js:789
  "attachment.image": "Image",   // app.js:823
  "attachment.image_label": "[Image {n}: {name}]",   // app.js:824
  "memory.omitted_note": "⟲ Memory {count}/{limit} items, {omitted} of them did not enter the context because of length",   // app.js:856
  "chat.summary_html": `
    <div class="summary-head">
      <span class="chev">▸</span>
      <span>Context compacted</span>
      <span class="summary-note">{covered} earlier messages were folded into a summary{suffix}</span>
    </div>
    <div class="summary-body"></div>`,   // app.js:874
  "chat.empty_summary": "(empty summary)",   // app.js:882
  "chat.think_html": `
    <div class="think hidden"><div class="think-head"><span class="chev">▸</span>Deep thinking</div>
      <div class="think-body"></div></div>
    <div class="tools"></div>
    <div class="bubble"></div>`,   // app.js:896
  "chat.no_output": "<span style=\"color:var(--muted)\">(no output)</span>",   // app.js:951
  "chat.tool_chip_html": `
    <div class="tool-head"><span class="tool-dot"></span>
      <span class="tool-name">{name}</span>
      <span class="tool-text">Running…</span></div>
    <div class="tool-detail"></div>`,   // app.js:970
  "tool.done": "Done",   // app.js:985
  "tool.failed": "Failed",   // app.js:985
  "tool.args": "Arguments: {args}",   // app.js:987
  "tool.result": "Result: {result}",   // app.js:988
  "usage.cached": "{cached} cached",   // app.js:995
  "usage.this_chat": "{total} token in this chat",   // app.js:1000
  "stats.turns": "{n} turns",
  "stats.speed": "{value} tok/s",
  "stats.tokens": "{value} tok",
  "stats.tokens_title": "{value} token used in this chat",
  "stats.cache": "Cache hit {value}%",
  "stats.context": "{value}%",
  "stats.context_title": "Current model context: {used} / {window} token",
  "stats.context_none": "No model selected; context usage is 0%",
  "attachment.read_failed": "Could not read the file{detail}",   // app.js:1051
  "attachment.setup_hint": "{msg} (run run.cmd --setup first)",   // app.js:1102
  "attachment.remove_title": "Remove from the pending list (the file on the server is left alone)",   // app.js:1140
  "attachment.busy_add": "Still generating — wait for this turn to end (or hit “Stop”) before adding images",   // app.js:1184
  "attachment.session_changed": "The chat changed while uploading, so this image was discarded; add it again in the new chat.",   // app.js:1133
  "chat.no_provider": "No provider is available yet — add one in Settings → Providers",   // app.js:1361
  "chat.images_only": "(images)",   // app.js:1396
  "chat.fork": "Fork",   // app.js:806
  "chat.fork_title": "Fork here: create a new independent chat that keeps only the context before this message (the original chat stays unchanged)",   // app.js:807
  "toast.forked": "Forked into a new independent chat",   // app.js:478
  "toast.fork_failed": `Fork failed: {msg}`,   // app.js:481
  "notice.stopped": "Stopped.",   // app.js:1423
  "notice.stream_handler_failed": "This frame could not be rendered ({msg}); the rest of the stream keeps coming.",   // app.js:1480
  "notice.ocr_refresh_failed": "This turn sent only OCR text, but the session view did not refresh ({msg}).",   // app.js:1464
  "notice.ocr_refresh_hint": "Reload the page to see the text the model actually received.",   // app.js:1465
  "memory.stats": "{active}/{cap} active",   // app.js:1513
  "memory.range": "Range {min}–{max}",   // app.js:1521
  "memory.lowest_now": ", and the lowest you can set right now is {lowest}",   // app.js:1522
  "memory.full": " · full, the model cannot store anything new right now",   // app.js:1523
  "memory.budget_warn_1": "⚠ Total memory exceeds the injection budget ({tokens} token), ",   // app.js:1529
  "memory.budget_warn_2": "{n} items will not enter the model context — lower the cap or trim the content",   // app.js:1530
  "memory.no_match": "<div class=\"session-sub\">No matching memories</div>",   // app.js:1549
  "memory.card_html": `
      <div class="mem-head">
        <span class="chip id">mem:{id}</span>
      </div>
      <div class="mem-content">{content}</div>
      <div class="mem-foot">
        <span class="mem-actions">
          <button class="mini" data-act="edit">Edit</button>
          {actions}
        </span>
      </div>`,   // app.js:1558
  "memory.limit_invalid": "Enter an integer between 0 and 100",   // app.js:1580
  "memory.limit_not_int": "The cap must be an integer",   // app.js:1582
  "memory.limit_set": "Memory cap set to {n}",   // app.js:1585
  "memory.edit_save_btn": "<button class=\"mini\" data-act=\"save\">Save</button>",   // app.js:1605
  "memory.edit_cancel_btn": "<button class=\"mini\" data-act=\"cancel\">Cancel</button>",   // app.js:1606
  "memory.content_required": "Content cannot be empty",   // app.js:1616
  "memory.updated": "Updated",   // app.js:1619
  "memory.delete_confirm": `Delete this memory?

{preview}`,   // app.js:1626
  "memory.deleted_note": "Removed from memory_instruction.md",   // app.js:1629
  "dream.meta": "{parts} · {n} chats to tidy up",   // app.js:1660
  "dream.runs_load_failed": "<div class=\"session-sub\">Failed to load run history: {msg}</div>",   // app.js:1672
  "dream.seconds": "{n} s",   // app.js:1740
  "dream.minutes_seconds": "{m} min {s} s",   // app.js:1741
  "run.status.running": "Running",   // app.js:1745
  "run.status.error": "Failed",   // app.js:1747
  "run.status.empty": "Nothing to tidy",   // app.js:1748
  "run.status.interrupted": "Interrupted",   // app.js:1751
  "run.status.unknown": "unknown",   // app.js:1755
  "dream.mode.dry": "Dry run (legacy)",   // app.js:1764
  "dream.mode.apply": "Tidy up",   // app.js:1764
  "dream.pending_latest": "To tidy up: already up to date",   // app.js:1772
  "dream.pending_counts": "{n} chats to tidy up · {events} records",   // app.js:1773
  "dream.stale_notice_open": "<div class=\"vault-stale\">⚠ {n} chats have a log shorter than the dream cursor",   // app.js:1783
  "dream.stale_notice_mid": "(changed or replaced), so it is treated as a from-scratch tidy — the next run costs a bit more, ",   // app.js:1784
  "dream.stale_notice_close": "if we did not do that, those records would stay unreadable forever.</div>",   // app.js:1785
  "vault.open_link": "Open {name}",   // app.js:1892
  "vault.not_written": "This file has not been written yet",   // app.js:1897
  "vault.missing_link_create": "“{name}” has not been written yet — click to create it now",   // app.js:1918
  "vault.missing_link_ask": "“{name}” has not been written yet. Create it now?",   // app.js:1929
  "vault.no_body": "(this file has no body)",   // app.js:1966
  "vault.kind.file": "file",   // app.js:2032
  "vault.kind.index": "index",   // app.js:2039
  "vault.dir_label": "{name} ({files} files)",   // app.js:2054
  "vault.empty_dir": "(empty directory)",   // app.js:2063
  "vault.tree_empty": "No files yet. Hit “Tidy up once” and let the model compress the chat log into files.",   // app.js:2078
  "vault.unsaved_open": "“",   // app.js:2109
  "vault.unsaved_name": "this file",   // app.js:2099
  "vault.unsaved_mid": "” has not been saved yet, ",   // app.js:2099
  "vault.unsaved_confirm": "switching will throw away what you typed. Continue?",   // app.js:2100
  "vault.check_problems": "The structure check found {n} issues:",   // app.js:2151
  "vault.check_more": "…and {n} more (see the “Structure check” block below)",   // app.js:2155
  "action.edit": "Edit",   // app.js:2167
  "vault.edit_title": "Edit the body and metadata of this file",   // app.js:2169
  "vault.delete_title": "Delete this file (you are asked first, and everyone pointing at it is listed)",   // app.js:2175
  "vault.action.index_note": "This is the index, the model sees it every turn; you can edit it directly (deleting it is refused by the backend).",   // app.js:2188
  "vault.action.generated_note": "This file is generated/maintained by the harness (README / index / earlier_months) and is read-only; edit sections / entities and it rebuilds automatically.",   // app.js:2188
  "vault.action.readonly_note": "This file is read-only (observations / run log / auxiliary export) and cannot be edited here.",   // app.js:2188
  "vault.action.file_note": "The model will see this file on the next turn. Editing it does not update the README or backlinks elsewhere automatically.",   // app.js:2189
  "vault.viewer_empty": "Click a file on the left to look at it.",   // app.js:2212
  "vault.loading": "Loading…",   // app.js:2221
  "vault.index_note": "This is the index, the model sees it every turn.",   // app.js:2236
  "vault.truncated": "The file is too long, only the first {shown} characters are shown here ({total} in total).",   // app.js:2264
  "vault.warnings": "This file has {n} warnings:",   // app.js:2269
  "vault.links_out": "Links to",   // app.js:2281
  "vault.links_in": "Linked from",   // app.js:2282
  "vault.links_shared": "Also linked from",   // app.js:2283
  "vault.chars_total": "{chars} chars in total",   // app.js:2288
  "vault.shown_full": " · full text shown",   // app.js:2288
  "vault.edit_status_new": "New file · the model will see it on the next turn once saved",   // app.js:2353
  "vault.edit_status_editing": "Editing {path}",   // app.js:2354
  "vault.new_file": "New file",   // app.js:2359
  "vault.new_file_title": "Write a new file under entities/ or sections/",   // app.js:2361
  "vault.field_key_hint": "Key written into the frontmatter: {key}",   // app.js:2385
  "vault.conflict_lead": "The latest content (",   // app.js:2406
  "vault.conflict_tail": " chars) will not be merged in automatically.",   // app.js:2406
  "vault.conflict_take": "Show the latest content (discard my edits)",   // app.js:2408
  "vault.conflict_overwrite": "Overwrite anyway (save again with the latest hash)",   // app.js:2411
  "vault.edit_hint_1": "The file you edit will be visible to the model on the next turn; but the README index and backlinks elsewhere are **not updated automatically**",   // app.js:2439
  "vault.edit_hint_2": " — the structure check will report things like \"not mentioned by the README (orphan file)\".",   // app.js:2440
  "vault.edit_hint_3": "Files cannot be edited while a tidy-up is running (the backend refuses outright). Wait for it to finish (seconds to tens of seconds) before saving — ",   // app.js:2441
  "vault.edit_hint_4": "otherwise the tidy-up overwrites what you just wrote with what it read when it started.",   // app.js:2442
  "vault.link_name_required": "Write a link name first",   // app.js:2516
  "vault.link_name_invalid": "A link name cannot contain square brackets or newlines and must be at most 120 characters",   // app.js:2518
  "vault.link_picker_lead": "Pick an existing file below, or type a name — a name may point at a file that has not been written yet; ",   // app.js:2543
  "vault.link_picker_lead2": "writing the link first and filling it in later is normal usage.",   // app.js:2544
  "vault.link_placeholder": "e.g. Xiaolin",   // app.js:2551
  "vault.link_name_title": "Name of the link to insert (may be a file that does not exist yet)",   // app.js:2553
  "vault.link_insert": "Insert",   // app.js:2559
  "vault.link_insert_title": "Insert [[this name]] at the cursor in the body",   // app.js:2562
  "vault.collapse": "Collapse",   // app.js:2565
  "vault.link_picker_empty": "There are no files in the tree yet — just type a name.",   // app.js:2575
  "vault.link_insert_label": "Insert [[{name}]] ({hint})",   // app.js:2580
  "vault.insert_link": "Insert link",   // app.js:2595
  "vault.insert_link_title": "Insert [[title]] at the cursor in the body (appended at the end when the body box has no cursor)",   // app.js:2598
  "action.saving": "Saving…",   // app.js:2613
  "action.cancel": "Cancel",   // app.js:2618
  "vault.no_path_yet": "(no path chosen yet)",   // app.js:2656
  "vault.edit_title_label": "Editing: {name}",   // app.js:2660
  "vault.too_long_editor_1": "This file is too long: the editor only holds the first ",   // app.js:2671
  "vault.too_long_editor_2": " characters (out of ",   // app.js:2672
  "vault.too_long_editor_3": " characters).",   // app.js:2672
  "vault.too_long_save_1": "Saving now writes back **exactly what you see**, ",   // app.js:2673
  "vault.too_long_save_2": "and anything beyond that is lost. To edit a file like this, use another editor on the files under data/vault.",   // app.js:2674
  "vault.field_dir": "Which directory",   // app.js:2681
  "vault.field_name": "Name (no need to type .md)",   // app.js:2704
  "vault.field_name_hint": "At most {max} characters (including .md, the backend checks the same rule); ",   // app.js:2707
  "vault.field_name_rule": "`< > : \" | ? *`, Windows reserved device names (NUL, CON…), and three-level paths are all refused by the backend.",   // app.js:2708
  "vault.field_path": "Path (cannot be changed; to rename, delete and create a new one)",   // app.js:2711
  "vault.field_filename": "File name (read-only)",   // app.js:2713
  "vault.field_title": "Title",   // app.js:2715
  "vault.field_kind": "Kind",   // app.js:2718
  "vault.field_kind_placeholder": "e.g. person / project / section",   // app.js:2719
  "vault.field_relation": "Relation",   // app.js:2721
  "vault.field_relation_placeholder": "e.g. the user, a colleague",   // app.js:2722
  "vault.field_status": "Status",   // app.js:2724
  "vault.field_status_placeholder": "e.g. active / in progress",   // app.js:2725
  "vault.field_place": "Place",   // app.js:2726
  "vault.field_place_placeholder": "e.g. Beijing Zhongguancun Academy",   // app.js:2726
  "vault.field_tags": "Tags (comma separated)",   // app.js:2727
  "vault.field_tags_placeholder": "e.g. user, classmate",   // app.js:2728
  "vault.field_aliases": "Aliases (comma separated)",   // app.js:2730
  "vault.field_aliases_placeholder": "e.g. Lin, Xiaolin",   // app.js:2731
  "vault.field_body": "Body (raw markdown)",   // app.js:2736
  "vault.edit_need_open": "Open a file before editing",   // app.js:2832
  "vault.edit_too_long": "This file is too long, only the first part was read here, so it cannot be edited here",   // app.js:2835
  "vault.edit_too_long_1": "This file exceeds the viewing limit, the UI only holds the first ",   // app.js:2838
  "vault.edit_too_long_2": " characters, ",   // app.js:2838
  "vault.edit_too_long_3": "so **editing is not offered** — saving here would wipe the part that was never read.",   // app.js:2839
  "vault.edit_too_long_4": "Please use another editor on the files under data/vault.",   // app.js:2840
  "vault.save_too_long_1": "This file is too long, the editor only holds the first ",   // app.js:2962
  "vault.save_too_long_2": "saving here would delete the part that was never read, so saving is not allowed — ",   // app.js:2964
  "vault.save_too_long_3": "please edit that file under data/vault directly with another editor.",   // app.js:2965
  "vault.save_need_name": "Fill in a name first — it becomes the path of the file.",   // app.js:2973
  "vault.save_failed": "Save failed",   // app.js:3002
  "vault.created": "Created {name}.",   // app.js:3024
  "vault.saved": "Saved {name} (the model will see it on the next turn).",   // app.js:3025
  "vault.saved_note": "The README index and backlinks elsewhere were not updated automatically; below is the **current** structure check result.",   // app.js:3026
  "vault.created_short": "Created",   // app.js:3031
  "vault.saved_short": "Saved",   // app.js:3031
  "vault.reload_failed": "(the file tree and structure check did not refresh this time — hit “Refresh” above and try again.)",   // app.js:3045
  "vault.force_overwriting": "Overwriting with the latest version…",   // app.js:3058
  "vault.fresh_taken": "Switched to the latest content (your edits were discarded).",   // app.js:3071
  "vault.delete_confirm_lead": "About to delete {name} ({kind}).",   // app.js:3110
  "vault.delete_confirm_inbound": "{n} files point at it; after deleting, those links become \"not written yet\":",   // app.js:3113
  "vault.delete_confirm_no_inbound": "No other file points at it right now (deleting it breaks no backlinks).",   // app.js:3116
  "vault.delete_confirm_note": "Deleting wipes the file outright, there is no recycle bin: the vault keeps no snapshot of it.",   // app.js:3119
  "vault.delete_confirm_alt": "(If you only want to empty it, editing and saving is the better move.)",   // app.js:3120
  "vault.delete_preview": "Take a look at the current body",   // app.js:3123
  "vault.delete_readme": "The README is the index and cannot be deleted — to empty it, rewrite its content",   // app.js:3134
  "dialog.delete_file.title": "Delete this file?",   // app.js:3136
  "action.deleting": "Deleting…",   // app.js:3142
  "vault.delete_conflict": "This file was changed after you started deleting it — have a look at the latest content first",   // app.js:3158
  "vault.delete_failed": "Delete failed",   // app.js:3161
  "vault.deleted": "Deleted {name}.",   // app.js:3176
  "vault.deleted_backlinks": "{n} files now have a \"not written yet\" backlink: {list}",   // app.js:3178
  "vault.deleted_no_backlinks": "No other file pointed at it.",   // app.js:3179
  "vault.browser_title": "Vault",   // app.js:3198
  "vault.pending_sessions_html": "There are <span class=\"v-big\">{n}</span> chats to tidy up · ",   // app.js:3226
  "vault.pending_events_html": "<span class=\"v-big\">{n}</span> records",   // app.js:3227
  "vault.pending_latest": "already up to date",   // app.js:3228
  "vault.chip_stale": "<span class=\"chip warn\">To tidy up · has issues</span>",   // app.js:3230
  "vault.chip_pending": "<span class=\"chip warn\">To tidy up</span>",   // app.js:3232
  "vault.chip_latest": "<span class=\"chip ok\">Up to date</span>",   // app.js:3233
  "vault.budget_over": "(over budget, truncated on injection)",   // app.js:3241
  "vault.readme_chars": "README {used} / {cap} characters",   // app.js:3242
  "vault.readme_missing": "README not read yet",   // app.js:3242
  "vault.file_count": "{files} files",   // app.js:3243
  "vault.no_readme": " · no README (the model cannot see the index)",   // app.js:3244
  "vault.problems_header": "<div class=\"vault-problems warn\"><strong>Structure check found {n} issues</strong>",   // app.js:3249
  "vault.problems_ok": "<div class=\"vault-problems ok\">Structure check passed</div>",   // app.js:3251
  "vault.pane_html": `{path}</div>
      {problems}
      <div class="vault-sub">{pending}</div>
      {stale}
    </div>
    <div class="section-title" id="dream-runs-title">Run history</div>
    <div id="dream-runs"></div>`,   // app.js:3259
  "dream.format.word": "format",   // app.js:3290
  "dream.danger_title": "Danger zone · format and reinitialise dream",   // app.js:3297
  "dream.danger_sub_1": "Clears <strong>vault files</strong>, <strong>run records</strong> and ",   // app.js:3300
  "dream.danger_sub_2": "<strong>dream progress</strong>, then initialises an empty vault skeleton.",   // app.js:3301
  "dream.danger_sub_3": "Only the dream layer is touched: <strong>chat history, memories, provider settings and API keys are not touched at all</strong>.",   // app.js:3302
  "dream.danger_note_1": "Vault files are a <strong>projection</strong> of your chats — running dream again can rebuild them",   // app.js:3305
  "dream.danger_note_2": "(costing one more dream run); but <strong>anything you wrote by hand will not come back</strong>.",   // app.js:3306
  "dream.danger_button": "Format and reinitialise dream",   // app.js:3311
  "dream.format_done": "Formatting done (only the dream layer was touched)",   // app.js:3325
  "dream.unit_files": "files",   // app.js:3327
  "dream.runs": "Run records",   // app.js:3328
  "dream.unit_records": "records",   // app.js:3328
  "dream.progress": "Dream progress",   // app.js:3329
  "dream.unit_sessions": "chats",   // app.js:3329
  "dream.format_cursor": "Cursor entries cleared: {n} chats",   // app.js:3335
  "dream.reinit_done": "An empty vault skeleton was reinitialised (placeholder README + sections/ + entities/)",   // app.js:3338
  "dream.reinit_placeholder_note": "— the placeholder README is not injected, it only gets content after one tidy-up run.",   // app.js:3339
  "dream.reinit_skipped": "The vault skeleton was not reinitialised — it will create itself on the next tidy-up.",   // app.js:3340
  "dream.format_leftovers": "{n} things could not be deleted, please remove them by hand:",   // app.js:3345
  "dream.unknown_value": "(not read this time, unknown)",   // app.js:3380
  "dream.will_clear": "About to be cleared (irreversible):",   // app.js:3388
  "dream.runs_long": "Dream run records",   // app.js:3390
  "dream.progress_cursor": "Dream progress (cursor)",   // app.js:3391
  "dream.will_keep": "Not touched:",   // app.js:3394
  "reset.row_chats": "Chat history",   // app.js:3395
  "reset.row_memories": "Memories",   // app.js:3396
  "reset.unit_active_items": "active items",   // app.js:3396
  "reset.row_providers": "Provider settings and keys",   // app.js:3397
  "reset.unit_items": "items",   // app.js:3397
  "dream.why_not_disaster": "Why this is not a disaster: vault files are a projection of your chats, so one more tidy-up rebuilds them",   // app.js:3401
  "dream.why_not_disaster_2": "(costing one more dream run); but anything you wrote by hand will not come back.",   // app.js:3402
  "dream.ask_prefix": "Type “",   // app.js:3405
  "dream.ask_suffix": "” to confirm:",   // app.js:3408
  "dream.format.title": "Format and reinitialise dream?",   // app.js:3426
  "dream.format.confirm": "Run the formatting",   // app.js:3428
  "dream.format_done_lead": "Dream was formatted and reinitialised:",   // app.js:3453
  "dream.format_after_files": "Vault files {before} → {after} · ",   // app.js:3463
  "dream.format_after_runs": "Run records {before} → {after} · ",   // app.js:3464
  "dream.format_after_progress": "Progress {before} → {after}",   // app.js:3465
  "dream.none_1": "<div class=\"session-sub\">No runs yet. Hit “Tidy up once” above to do a first run",   // app.js:3502
  "dream.none_2": " — dream is one-way, there is no dry run.</div>",   // app.js:3503
  "dream.chip_dry": "<span class=\"chip dry\">Dry run (legacy)</span>",   // app.js:3513
  "dream.chip_applied": "<span class=\"chip\">Applied</span>",   // app.js:3514
  "dream.no_files_touched": "no file touched",   // app.js:3518
  "dream.files_touched": "{n} files changed",   // app.js:3519
  "dream.run_head_html": `
      <div class="run-head">
        <span class="run-when">{when}</span>
        <span class="run-id">{id}</span>
        {mode}{status}
        <span class="chip">{sessions} chats</span>
        <span class="chip">{changed}</span>
        <span class="run-summary">{summary}</span>
        <span class="chip">{tokens} token</span>
      </div>
      <div class="run-body hidden"></div>`,   // app.js:3520
  "dream.detail_loading": "<div class=\"session-sub\">Loading…</div>",   // app.js:3541
  "dream.diff_created": "<div class=\"diff-group\"><div class=\"diff-group-title\">Created</div>",   // app.js:3560
  "dream.diff_updated": "<div class=\"diff-group\"><div class=\"diff-group-title\">Updated</div>",   // app.js:3568
  "dream.diff_chars_both": "{from} → {to} chars</div>",   // app.js:3571
  "dream.diff_deleted": "<div class=\"diff-group\"><div class=\"diff-group-title\">Deleted</div>",   // app.js:3575
  "dream.diff_chars_one": "{chars} chars</div>",   // app.js:3578
  "dream.diff_empty": "<div class=\"session-sub\">(this run changed no vault file)</div>",   // app.js:3583
  "dream.check_after_header": "<div class=\"vault-problems warn\"><strong>Structure check after this run: {n} issues</strong>",   // app.js:3587
  "dream.check_after_ok": "<div class=\"vault-problems ok\">Structure check passed after this run</div>",   // app.js:3589
  "dream.cursor_advanced": "Cursor advanced {n} chats",   // app.js:3597
  "dream.cursor_to": "{from} → record {to}",   // app.js:3599
  "dream.stop_reason": "Stop reason {reason}",   // app.js:3603
  "dream.elapsed": "Took {duration}",   // app.js:3605
  "dream.candidates": "{n} candidate chats · {events} records",   // app.js:3611
  "dream.no_summary": "<div class=\"session-sub\">(the model left no summary)</div>",   // app.js:3615
  "dream.run_skipped": "<div class=\"run-skip\">There was nothing to tidy up, so **no model was called** this time (no money spent).</div>",   // app.js:3617
  "dream.final_trace_lead": "  to recover what a file said at the time, you can look at the full trace ",   // app.js:3649
  "dream.final_trace_full": "(they keep the full text of every <code class=\"ic\">vault_write</code>)",   // app.js:3651
  "dream.final_trace_tail": " — that is the only way to recover it now that rollback is gone.",   // app.js:3652
  "dream.final_dry_1": "<div class=\"run-final\">This was a <strong>dry run (a legacy feature, now removed)</strong>:",   // app.js:3656
  "dream.final_dry_2": "No vault file was changed at the time — the differences above were only shown to you, discarded once the run ended, and the cursor did not move either.",   // app.js:3657
  "dream.final_dry_3": "To redo it now there is only one way: clear everything with “Format and reinitialise dream” and run again.",   // app.js:3658
  "dream.final_note_undo": "<strong>Dream results cannot be undone: there is no rollback and no dry run.</strong>",   // app.js:3662
  "dream.final_note_1": " the vault keeps no restorable copy of this run — what was written is written.",   // app.js:3663
  "dream.final_note_2": " If a run made a mess, the only way back is to clear everything with “Format and reinitialise dream”",   // app.js:3664
  "dream.final_note_3": "(that also drops the run records and the dream progress, and costs one more dream run).",   // app.js:3665
  "dream.starting": `Starting the tidy-up… (it really writes files and advances the cursor, and it cannot be undone)
`,   // app.js:3683
  "dream.run_started": "Run {id} ({when})",   // app.js:3700
  "dream.model": "Model {model}",   // app.js:3703
  // ── Dream page: which model does the tidy-up (ADR-103) ──
  "dream.model_follow": "Follow the default",   // app.js:1891
  "dream.model_follow_named": "Follow the default ({model})",   // app.js:1893
  "dream.model_none": "(No models available — add one under Providers)",   // app.js:1878
  "dream.model_saved": "Tidy-up model saved",   // app.js:1943
  "dream.model_save_failed": "Failed to save the tidy-up model: {msg}",   // app.js:1946
  "dream.new_records": " {n} new records (cursor {from} → end {to})",   // app.js:3706
  "dream.record_line": '· {id} "{title}"',   // app.js:3731
  "dream.untitled": "(untitled)",   // app.js:3733
  "dream.step": "· step {n}",   // app.js:3709
  "dream.skipped": `
There was nothing to tidy up, so no model was called this time (no money spent).`,   // app.js:3717
  "dream.no_summary_paren": "(no summary)",   // app.js:3718
  "dream.done_created": `
Done: {n} created · `,   // app.js:3721
  "dream.done_updated": "{n} updated · ",   // app.js:3722
  "dream.done_deleted": "{n} deleted",   // app.js:3723
  "dream.error": "Error: {msg}",   // app.js:3732
  "usage.chat_count": "{n} chats",   // app.js:3746
  "usage.total": "Total token",   // app.js:3748
  "usage.prompt": "prompt",   // app.js:3776
  "usage.completion": "completion",   // app.js:3777
  "usage.reasoning": "Reasoning token",   // app.js:3749
  "usage.cache_hit": "Cache hit",   // app.js:3750
  "usage.cache_miss": "Cache miss",   // app.js:3750
  "usage.pane_html": `
    <div class="usage-card"><div class="usage-grid">
      {cells}
    </div></div>
    <div class="usage-card">
      <div class="section-title">By provider</div>
      <table class="usage-table">
        <thead><tr><th>Provider</th><th class="num">{prompt}</th><th class="num">{completion}</th>
        <th class="num">cached</th><th class="num">total</th></tr></thead>
        <tbody>{providers}</tbody>
      </table>
    </div>
    <div class="usage-card">
      <div class="section-title">Per chat</div>
      <table class="usage-table">
        <thead><tr><th>Chat</th><th class="num">{prompt}</th><th class="num">{completion}</th>
        <th class="num">cached</th><th class="num">total</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>`,   // app.js:3768
  "api.provider_count": "{n} providers",   // app.js:3817
  "api.pane_html": `
    <div class="section-title">New chat defaults</div>
    <div class="provider-card {defaultCls}">
      <div class="provider-fields">
        <div class="field wide">
          <label>Default model</label>
          <select id="default-model">{defaultOptions}</select>
          <div class="hint">New conversations use this by default. A single chat can be switched anytime in the top bar, which does not affect this.</div>
        </div>
      </div>
    </div>

    <div class="section-title">Configured providers</div>
    {cards}

    <div class="add-provider">
      <select id="preset-pick">{presets}<option value="__custom">Custom (fill in base_url yourself)</option></select>
      <button class="btn-primary" id="preset-add">Add this provider</button>
      <span class="hint" style="font-size:11.5px;color:var(--muted)">
        Add it, then fill in the API key and hit “Fetch model list” on the card.
      </span>
    </div>

    <div class="provider-note">
      All keys live only in <code class="ic">data/providers.json</code> on this machine and are never uploaded anywhere.
      The model list is deliberately not pre-filled: “Fetch model list” calls the provider's <code class="ic">/models</code>,
      so what you get is what is actually for sale.
    </div>`,   // app.js:3830
  "api.default_set": "Set as the new-chat default",   // app.js:3866
  "api.current_key": "Current: {value} (empty means unchanged)",   // app.js:3877
  "api.local_only": "Stored only on this machine, in data/providers.json",   // app.js:3878
  "api.provider_card_html": `
  <div class="provider-card {defaultCls}" data-id="{id}">
    <div class="provider-head">
      <span class="p-label">{label}</span>
      <span class="chip id">{idText}</span>
      {defaultChip}
      {noKeyChip}
      {noToolsChip}
      <span class="p-actions">
        <button class="mini" data-act="fetch">Fetch model list</button>
        <button class="mini" data-act="save">Save</button>
        <button class="mini danger" data-act="del">Delete</button>
      </span>
    </div>
    <div class="provider-fields">
      <div class="field"><label>Name</label>
        <input data-f="label" value="{labelValue}"></div>
      <div class="field"><label>API Base URL</label>
        <input data-f="base_url" value="{baseUrl}"></div>
      <div class="field"><label>API Key</label>
        <input data-f="api_key" type="password" autocomplete="off"
               placeholder="{keyPlaceholder}">
        <div class="hint">{keyHint}</div></div>
      <div class="field wide"><label>Models</label>
        <div class="model-table" data-f="model-table">
          <div class="model-head">
            <span>Model ID</span><span>Context</span><span>Display name (optional)</span><span></span>
          </div>
          {modelRows}
        </div>
        <button class="mini model-add" data-act="model-add" type="button">+ Add model</button>
        <div class="hint">
          The <strong>context</strong> accepts <code class="ic">128k</code> or
          <code class="ic">1M</code> (k = 1000, M = 1000k); auto-compact kicks in at 75% of it,
          and <strong>empty means 200k</strong>. Models from the same vendor differ a lot
          (8k to 1M), so this can only be set per model.
          <br>The <strong>display name</strong> only changes how the model shows up in the picker;
          <strong>empty means the model ID</strong>.
        </div></div>
      <div class="field"><label>Protocol</label>
        <select data-f="type">
          <option value="openai" {typeOpenai}>OpenAI compatible (the vast majority)</option>
          <option value="anthropic" {typeAnthropic}>Anthropic compatible (Kimi Code, Claude)</option>
        </select></div>
      <div class="field"><label>Thinking levels (comma separated, empty = do not send)</label>
        <input data-f="thinking_options" value="{thinkingOptions}"></div>
      <div class="field"><label>Send back historical reasoning</label>
        <select data-f="send_reasoning_content">
          <option value="1" {reasoningYes}>Yes (DeepSeek requires it)</option>
          <option value="0" {reasoningNo}>No (OpenAI and others are stricter)</option>
        </select></div>
      <div class="field wide"><label>Image-capable models (comma or newline separated)</label>
        <textarea data-f="vision_models" rows="2"
                  placeholder="deepseek-flash">{visionModels}</textarea>
        <div class="hint">
          Only the models listed here may <strong>send images directly</strong> (used while “Multimodal” is on).<br>
          <strong>Empty = unknown</strong>: nothing is blocked, but the model may silently ignore a real image.<br>
          What is known right now: <code class="ic">deepseek-flash</code> is supported,
          <code class="ic">deepseek-v4-pro</code> is not.
        </div></div>
    </div>
    {notes}
  </div>`,   // app.js:3879
  "api.fetching": "Fetching…",   // app.js:3967
  "api.fetched_models": "Got {n} models",   // app.js:3975
  "api.fetch_failed": "Fetch failed: {msg}",   // app.js:3978
  "api.fetch_models": "Fetch model list",   // app.js:3980
  "api.delete_confirm": "Delete provider “{id}” and its key?",   // app.js:3984
  "api.new_id_prompt": "Give this provider an id (letters and digits, e.g. myapi)",   // app.js:3997
  "api.base_url_prompt": "Base URL (e.g. https://api.example.com/v1)",   // app.js:4004
  "api.added_fill_key": "Added — now fill in the API Key",   // app.js:4007
  "api.model_label_ph": "e.g. DeepSeek V4.1 Flash",
  "api.model_remove": "Remove this row",
  "api.model_id_required": "This row has something in it but no model ID — fill it in, or clear the whole row.",
  "api.model_duplicate": "Model ID “{id}” appears twice.",
  "api.model_context_bad": "Context for “{id}” must be a whole number, optionally with a k / M suffix (e.g. 128k, 1M; k = 1000, M = 1000k).",
  "theme.follow_system_desc": "Follow the light / dark setting of Windows",   // app.js:4017
  "theme.light_desc": "Always use the light interface",   // app.js:4018
  "theme.dark_desc": "Always use the dark interface",   // app.js:4019
  "tools.missing_key_chip": "<span class=\"chip\" style=\"color:var(--err)\">No key — search unavailable</span>",   // app.js:4034
  "tools.current_key": "Currently stored: {masked}",   // app.js:4045
  "tools.no_key": "No key stored yet. Tavily has a free tier — sign up at tavily.com.",   // app.js:4046
  "tools.pane_html": `
    <div class="reset-card">
      <div class="section-title">Web tools</div>
      <p class="reset-lead">
        Gives the model two abilities: <code>web_search</code> (keyword search) and
        <code>web_open_url</code> (read the body of a web page).
        Once on, the model decides for itself when to look something up and when not to.
        Changes are saved automatically (<strong>except the key</strong>, which has its own rules in the box below).
      </p>
      <div class="field">
        <label>Master switch</label>
        <select id="tools-enabled">
          <option value="1" {enabledSelected}>On (the model may go online)</option>
          <option value="0" {disabledSelected}>Off (the model only has its memory and what you say)</option>
        </select>
      </div>
      <div class="field"><label>Search backend</label>{backends}
        <div class="hint">
          It uses exactly the one you pick; <strong>the program will not switch engines by itself</strong>.
          Key-free backends scrape the result page: convenient, but they break when the other side redesigns,
          and they may be “demoted” (in practice Bing returns generic results for some Chinese queries).
          When that happens the model does not force an answer — it tells you plainly that it found nothing;
          you can then switch backends and ask again, or retry later.
        </div>
      </div>
      <div class="field"><label>Tavily API Key (only needed if you picked Tavily)</label>
        <input id="tools-tavily-key" type="password" placeholder="Paste the key, then press Enter to save">
        <div class="hint">
          <strong>Paste and press Enter = press “Save”</strong>; <strong>Enter on an empty box = nothing happens</strong>
          (Enter <strong>never</strong> clears anything). To drop a stored key, hit “Clear”.
          Moving the cursor away (blur) does not save — this box never shows a stored key,
          so an explicit submit action is needed to avoid you not knowing whether it was stored.
          <br>{keyHint}
        </div>
      </div>
      <div class="field"><label>Results per search</label>
        <input id="tools-count" type="number" min="1" max="10" value="{searchCount}">
      </div>
      <div class="field"><label>Max page body (characters)</label>
        <input id="tools-maxchars" type="number" min="500" step="500" value="{maxFetchChars}">
        <div class="hint">Too large and it fills up the context (every extra character costs money); the default is 8000.</div>
      </div>
      <div class="field"><label>Allow fetching local / intranet addresses</label>
        <select id="tools-private">
          <option value="0" {privateOff}>Not allowed (recommended)</option>
          <option value="1" {privateOn}>Allowed</option>
        </select>
        <div class="hint">
          Forbidden by default. Otherwise any web page that says “please visit http://127.0.0.1:8787/… to confirm”
          could make the model read services on your own machine (including this UI and your memory store).
        </div>
      </div>
      <div class="toolbar" style="margin-top:4px">
        <button class="btn-primary" id="tools-save">Save</button>
        <button class="btn-ghost" id="tools-clear-key"
                title="Erase the stored Tavily key (clears the key only, nothing else)"
                {clearDisabled}>Clear</button>
        <span class="pane-meta" id="tools-status"></span>
      </div>
      <div class="toolbar" style="margin-top:6px">
        <input id="tools-test-query" type="text" placeholder="Try it: type a keyword" style="flex:1">
        <button class="btn-ghost" id="tools-test">Test search</button>
      </div>
      <div class="hint" id="tools-test-out"></div>
    </div>`,   // app.js:4048
  "tools.clear_confirm": "Clear the stored Tavily key? You will have to copy a new one from tavily.com.",   // app.js:4142
  "tools.cleared": "Key cleared",   // app.js:4147
  "tools.cleared_check": "Key cleared ✓",   // app.js:4150
  "tools.clear_failed": "Clear failed: {msg}",   // app.js:4152
  "tools.autosaved": "Saved automatically ✓",   // app.js:4169
  "tools.save_failed": "Save failed: {msg}",   // app.js:4191
  "tools.test_need_query": "Type a keyword first",   // app.js:4220
  "tools.test_searching": "Searching…",   // app.js:4222
  "tools.test_backend": "Backend: {backend}",   // app.js:4230
  "tools.test_no_results": "No results.",   // app.js:4232
  "tools.test_failed": "Failed: {msg}",   // app.js:4234
  "dialog.working": "Working…",   // app.js:4284
  "reset.unit_active": "{n} active",   // app.js:4307
  "reset.unit_deleted": " · {n} deleted",   // app.js:4308
  "reset.unit_ops": " · {n} operations",   // app.js:4309
  "reset.unit_dream_runs": "{n} dream runs",   // app.js:4310
  "reset.unit_files": "{n} files",   // app.js:4311
  "reset.row_attachments": "Attachment files",
  "reset.row_prompt_overrides": "Prompt overrides",
  "reset.row_providers_short": "Provider settings",   // app.js:4312
  "reset.unit_providers_with_keys": "{n} (API keys included)",   // app.js:4312
  "reset.row_disk": "Disk usage",   // app.js:4313
  "brand.version": "v{version}",   // sidebar line 2: version (ADR-096)
  "brand.version_unknown": "version unknown",
  "reset.pane_html": `
    <div class="reset-card">
      <div class="section-title">Factory reset</div>
      <p class="reset-lead">
        Erase everything stored on this machine and go back to a freshly installed, unused state.
        <strong>This cannot be undone.</strong>
      </p>
      <table class="reset-table">
        <tbody>{rows}</tbody>
      </table>
      <div class="hint reset-note">
        Data directory: <code>{dataDir}</code><br>
        <strong>Provider settings (including API keys) are deleted as well.</strong>
        Keys exist only in this directory and this project ships none,
        so after a reset you are genuinely empty — add one again when you need it.
      </div>
      <button class="btn-danger" id="reset-start" {disabled}>
        {label}
      </button>
    </div>`,   // app.js:4381
  "reset.confirm_title": "Confirm factory reset?",   // app.js:4405
  "reset.confirm_html": `
        <p>This permanently deletes everything stored on this machine:</p>
        <ul>{items}</ul>
        <p class="danger-text">It cannot be recovered afterwards, including the API keys you configured.</p>`,   // app.js:4406
  "reset.delete_forever": "Delete forever",   // app.js:4410
  "reset.done_lead": "Factory reset done: deleted {sessions} chats, ",   // app.js:4414
  "reset.done_tail": "{memories} memories and {providers} providers",   // app.js:4415
  "reset.failed": "Factory reset failed: {msg}",   // app.js:5340
  "reset.leftover_warning": "Note: some files could not be deleted and are left in {path}",   // app.js:4417
  "init.backend_unreachable": "Could not reach the backend: ",   // app.js:4435
  "dream.confirm_1": `Tidying up is one-way, cannot be undone, and has no dry run (the dry run was removed).

`,   // app.js:4501
  "dream.confirm_2": "If a run makes a mess, the only way back is to clear everything with “Format and reinitialise dream” — ",   // app.js:4502
  "dream.confirm_3": `that also drops the run records and the dream progress, and costs one more dream run.

`,   // app.js:4503
  "dream.confirm_4": "Tidy up now?",   // app.js:4504
  "session.untitled": "(untitled chat)",   // app.js:313
  "chat.summary_just_now": " (just now)",   // app.js:857
  "memory.deleted_chip": "<span class=\"chip\">Deleted</span>",   // app.js:1526
  "memory.delete_btn": "<button class=\"mini danger\" data-act=\"del\">Delete</button>",   // app.js:1526
  "dream.runs_count": "{n} runs",   // app.js:1616
  "dream.no_runs_short": "No runs yet",   // app.js:1616
  "dream.no_summary_short": "(no summary)",   // app.js:3470
  "usage.empty_rows": "<tr><td colspan=\"5\" class=\"session-sub\">No usage yet</td></tr>",   // app.js:3710
  "api.no_default_options": "<option value=\"\">(add a provider and hit “Fetch model list” first)</option>",   // app.js:3751
  "api.no_providers": "<div class=\"session-sub\">No providers yet. Pick a preset below to add one.</div>",   // app.js:3751
  "api.default_chip": "<span class=\"chip insight\">default</span>",   // app.js:3773
  "api.no_key_chip": "<span class=\"chip\" style=\"color:var(--err)\">no key</span>",   // app.js:3773
  "api.no_tools_chip": "<span class=\"chip\" style=\"color:var(--warn)\">no tool support → memory will not work</span>",   // app.js:3773
  "api.key_stored": "stored",   // app.js:3773
  "api.key_paste": "paste your key",   // app.js:3773
  "reset.nothing_to_clear": "There is nothing left to clear",   // app.js:4157
  "reset.factory_reset": "Factory reset",   // app.js:4157
  "app.title": "O₃ᶻᵒⁿᵉ",
  "nav.new_chat": "New chat",
  "nav.settings": "Settings",
  "picker.model_title": "Choose provider and model",
  "picker.effort_title": "Reasoning effort",
  "toggle.web.title": "Whether this chat lets the model search the web on its own",
  "toggle.multimodal.title": "Whether images are sent to the model directly, or only the text recognised on this machine",
  "chat.empty_title": "Start a conversation",
  "chat.empty_sub": "The model decides for itself what to remember. Memories persist across chats and can be tidied up anytime.",
  "attachment.attach_title": "Add an image (you can also drag one in, or paste a screenshot)",
  "chat.input_placeholder": "Say something… (Enter to send, Shift+Enter for a newline, paste a screenshot directly)",
  "chat.send": "Send",
  "chat.stop": "Stop",
  "settings.title": "Settings",
  "settings.back": "Back to chat",
  "settings.nav.memory": "Memory",
  "settings.nav.dream": "Dream",
  "settings.nav.usage": "Usage",
  "settings.nav.api": "Providers",
  "settings.nav.tools": "Tools",
  // Prompts page (contract docs/13-prompts-page.md §4). Everything **on** the page
  // (prompt bodies, tool descriptions, each group's note, each item's when/condition)
  // comes from the server already translated; these keys are the shell only.
  "settings.nav.prompts": "Prompts",
  "prompts.intro": `What is <strong>really sent to the model</strong> in the current language: every prompt (including the two only used by the background tidy-up) and every tool definition.
      Memory items, the README and the history are assembled turn by turn from the current state, so they are not listed here —
      to see one complete request, use <code>run.cmd --show-prompt</code>, or replay that chat in the UI.`,
  "prompts.section_prompts": "Prompts",
  "prompts.section_tools": "Tool definitions",
  "prompts.summary": `{prompts} prompts · {tools} tool definitions`,
  "prompts.on": `{on} of those tools are currently in effect; `,
  // Separator-free on purpose: a group summary hides the states that are 0, and a
  // ` · ` baked into the text cannot be dropped from the middle/start/end
  // (app.js joins these two places with ' · ').
  "prompts.tally_on": `{n} in effect`,
  "prompts.tally_off": `{n} not registered`,
  "prompts.tally_session": `{n} session-level`,
  "prompts.state.on": "In effect",
  "prompts.state.off": "Not registered",
  "prompts.state.session": "Session-level",
  "prompts.chars": `{n} chars`,
  "prompts.placeholders": `Placeholders: {list}`,
  "prompts.text": "Prompt text",
  "prompts.params": "Parameters (JSON Schema)",
  "prompts.raw_json": "Raw JSON",
  "prompts.required_yes": "required",
  "prompts.no_params": "No parameters.",
  "prompts.col_name": "Parameter",
  "prompts.col_type": "Type",
  "prompts.col_required": "Required",
  "prompts.col_desc": "Description",
  "prompts.group_count": `{n} items`,
  "prompts.copy": "Copy",
  "prompts.copied": "Copied",
  "prompts.copy_failed": "Could not copy: this environment does not let scripts write to the clipboard. Select the text above and copy it by hand.",
  "prompts.loading": "Loading…",
  "prompts.load_error": `Failed to load: {error}`,
  "prompts.none": "(nothing here)",
  // ── Editable prompts (contract docs/14-editable-prompts.md §8.1) ──────────
  // Language isolation is meant to be **visible**: the header line writes out how
  // many entries each language changed (including 0), and the revert confirmation
  // says which single language it touches.
  "prompts.overrides_summary": `Each language is stored separately (editing one never touches the other): {parts}`,
  "prompts.overrides_name.zh": "Chinese",
  "prompts.overrides_name.en": "English",
  "prompts.overrides_lang": `{name}: {n} changed`,
  "prompts.edit": "Edit",
  "prompts.cancel": "Cancel",
  "prompts.edited": "Edited",
  "prompts.revert": "Revert",
  "prompts.revert_all": "Revert every prompt of this language",
  "prompts.revert_all_none": "This language has no changes, so there is nothing to revert",
  "prompts.revert_confirm": `Restore “{id}” to the built-in text?

Only this single entry of the current language ({language}) is reverted: the other language is left untouched.`,
  "prompts.revert_all_confirm": `Restore all {n} changed prompts of {language} to the built-in text?

Only {language} is touched: the other language is left untouched.`,
  "prompts.revert_done": `Reverted “{id}”: this entry uses the built-in text again`,
  "prompts.revert_all_done": `Reverted {n} changes in {language}: they all use the built-in text again`,
  "prompts.revert_failed": `Revert failed: {error}`,
  "prompts.save": "Save",
  "prompts.saved": "Saved: this entry now uses the version you wrote",
  "prompts.save_identical": "This text is identical to the built-in one, so it was not stored as an override — nothing changed",
  "prompts.save_failed": `Save failed: {error}`,
  "prompts.editor_hint_placeholders": `Placeholders {list}: they get filled into the text (memory, time, vault files and so on), so do not delete them.`,
  "prompts.editor_hint_cache": "After saving, the prompt cache is rebuilt for the next message (a one-off cost).",
  "prompts.editor_hint_frozen": "An edited entry is frozen: if the built-in text is improved later, this one will not follow — press “Revert” to follow it.",
  "prompts.builtin_text": "Built-in text",
  "prompts.current_text": "Currently in effect",
  "prompts.placeholders_missing": `Missing placeholders {list}`,
  "prompts.store_error": `Warning: the prompt override file cannot be read, so the built-in text is in use and none of your changes took effect — {error}`,
  "settings.nav.general": "General",
  "settings.nav.reset": "Reset",
  "memory.limit_label": "Cap",
  "memory.limit_save": "Save cap",
  "memory.search_placeholder": "Search memory content",
  "memory.show_deleted": "Show deleted",
  "action.refresh": "Refresh",
  "dream.run_once": "Tidy up once",
  "dream.session_limit": "Chat limit",
  "dream.model_label": "Tidy-up model",
  "dream.model_title": "Which model consolidates the background archives (tidy-up only; chat is unaffected)",
  "dream.note_1": `
              dream compresses chat logs into <strong>vault files</strong>: <code>data/vault/README.md</code>
              + <code>sections/</code> entries + <code>entities/</code> entities.
              It does <strong>not rewrite memory items</strong> (those are the ones you explicitly asked it to remember).
              “Tidy up once” really writes files and advances the cursor, and it is <strong>irreversible</strong>:
              tidying up is a one-way door — no snapshots, no rollback.
              <strong>The dry run has been removed</strong> (it ran the model for real and cost the same, while only estimating what would change).
              If a run makes a mess, the remedy is <strong>Format and reinitialise dream</strong> in the danger zone below:
              clear it and tidy up again to rebuild it, at the cost of one more dream run — but anything written by hand will not come back.
            `,
  "dream.note_2": `
              Vault files can be <strong>created / edited / deleted</strong> right here:
              “New file” at the top of the tree on the left, and “Edit / Delete” at the top right of each file on the right.
              When saving, the UI sends the fingerprint of the version you are looking at; if the model changed it while you were editing,
              the backend refuses once and lays out the latest content, and you decide whether to discard or overwrite.
              Deleting the README is refused by the backend (it is the index), so the README has no delete button.
            `,
  "dream.note_3": `
              Editing by hand <strong>does not update the README index or backlinks elsewhere automatically</strong> —
              the structure check will report things like "not mentioned by the README".
              Also, vault files cannot be edited while a tidy-up is running (the backend refuses outright); wait for it to finish and try again.
            `,
  "dialog.cancel": "Cancel",
  "dialog.confirm": "Confirm",
  "settings.language.title": "Interface language",
  "settings.language.lead": "Choose the language of the interface here. The switch takes effect immediately, without reloading the page; backend notices, tool descriptions and system prompts follow along.",
  "settings.language.current": "Current language: {name}",
  "settings.language.saved": "Switched to {name}",
  "settings.language.failed": "Failed to switch language: {msg}",
  "settings.language.note": "The language is stored in data/settings.json; HARNESS_LANGUAGE provides the default.",
  "language.name.zh": "Chinese",
  "language.name.en": "English",
  "language.desc.zh": "Chinese interface, Chinese system prompts and tool descriptions",
  "language.desc.en": "English interface, English system prompts and tool descriptions",
  "settings.language.busy": "An answer is being generated; wait for this turn to end before switching — the language is process-wide, so switching mid-answer would change the system prompt the model sees.",
  "settings.general.theme": "Theme",
  "settings.general.timezone": "Timezone",
  "settings.timezone.lead": "Which timezone the `time` tool uses (offset in hours, from -12 to +14).",
  "settings.timezone.current": "Current timezone: {zone}",
  "settings.timezone.saved": "Timezone changed to {zone}",
  "settings.timezone.failed": "Failed to change timezone: {msg}",
  "settings.timezone.note": "Stored in data/settings.json; takes effect on the next turn.",
  "settings.general.output": "Max output per turn",
  "settings.output.lead": "The most tokens the model may produce in one turn. Takes effect on the next turn.",
  "settings.output.save": "Save",
  "settings.output.current": "Current: {value}",
  "settings.output.saved": "Per-turn max output changed to {value}",
  "settings.output.failed": "Failed to save: {msg}",
  "settings.output.invalid": "Enter an integer: -1 (unlimited) or >= 1; 5k / 1M shorthand is accepted.",
  "settings.output.unlimited": "unlimited",
  "settings.output.note": "Accepts 5000 / 5k / 1M (k = 1000, M = 1000000); -1 = unlimited. Stored in data/settings.json.",
  "setup.loading_title": "Starting up",
  "setup.checking": "Checking the local runtime…",
  "setup.network_retry": "Startup status is temporarily unavailable; retrying: {msg}",
  "setup.consent_title": "Download the complete runtime?",
  "setup.consent_body": "The complete runtime includes web, image, and local OCR dependencies. No intact .venv was found. Download it now, or continue directly to chat.",
  "setup.missing": "Missing or unusable: {items}",
  "setup.missing_generic": "Dependencies are incomplete",
  "setup.mirror_label": "PyPI mirror URL (optional)",
  "setup.mirror_placeholder": "Leave blank for https://pypi.org/simple",
  "setup.mirror_note": "The mirror applies only to this pip download; blank uses official PyPI. Declining or cancelling is not remembered, so the next startup checks again.",
  "setup.download": "Download and install",
  "setup.skip": "Continue to chat without downloading",
  "setup.preparing": "Preparing the virtual environment…",
  "setup.downloading": "Downloading the complete runtime",
  "setup.verifying": "Verifying the installation…",
  "setup.finalizing": "Finishing the installation…",
  "setup.cancelling": "Discarding this download…",
  "setup.paused": "Download paused",
  "setup.pause": "Pause download",
  "setup.resume": "Resume download",
  "setup.cancel": "Quit download and continue",
  "setup.cancel_note": "Quitting stops pip, discards content downloaded this time, and opens chat without the complete venv.",
  "setup.progress_waiting": "Started; pip has not reported a usable percentage yet.",
  "setup.progress_percent": "{value}%",
  "setup.source_official": "Source: official PyPI",
  "setup.source_mirror": "Source: custom mirror",
  "setup.failed": "Installation did not complete: {msg}",
  "setup.action_failed": "The action did not complete: {msg}",
  "setup.continued_without": "Chat is open without the complete venv. It is still incomplete, so the next startup will ask again.",
  "setup.invalid_mirror": "Enter a valid HTTP(S) mirror URL without a username or password.",
  "setup.install_busy": "Another startup window is installing the runtime; continue in that window.",
  "setup.consent_required": "Confirm the download first.",
  "setup.not_active": "This action is unavailable in the current state; check the latest progress.",
  },
};

/* ── 语言清单 ─────────────────────────────────────────── */

const LANGUAGE_IDS = ['zh', 'en'];

/* 两个选项的标签本身也是文案：中文界面「中文 / English」，英文界面「Chinese / English」。
   写成 getter 而不是常量数组 —— 常量会在加载时就被冻成当时的语言。 */
Object.defineProperty(I18N, 'languages', {
  enumerable: true,
  get() {
    return LANGUAGE_IDS.map((id) => ({ id, label: t(`language.name.${id}`) }));
  },
});

/* 取值容错，口径与后端 harness/i18n.py 的 normalize() 一致：
   `en` / `en-US` / `English` → en；`zh` / `zh-CN` / `中文` → zh；
   认不出来的一律退回默认值 zh（坏值不该让界面起不来）。 */
function normalizeLanguage(value) {
  const text = String(value === undefined || value === null ? '' : value).trim().toLowerCase();
  if (text === 'en' || text.startsWith('en-') || text === 'english' || text === '英文') return 'en';
  return 'zh';
}

/* 当前语言。app.js 每次要用语言时都现取，不缓存。 */
function current() {
  return I18N.lang;
}

/* 切语言。**只改这一层**：写进 data/settings.json 是 app.js 那一侧的事（POST 成功后才调这里）。 */
function setLanguage(lang) {
  I18N.lang = normalizeLanguage(lang);
  return I18N.lang;
}

/* 查当前语言。v1.0 的口径：
     * 命中 → 替换 {name} 占位符后返回；
     * 缺 key → 返回 **key 本身** 并 console.warn（绝不返回空串：空白是最难查的那种漏翻）。 */
function t(key, vars) {
  const catalog = I18N[I18N.lang] || I18N.zh;
  const text = catalog[key];
  if (typeof text !== 'string') {
    console.warn(`[i18n] missing key: ${key} (lang=${I18N.lang})`);
    return key;
  }
  if (!vars) return text;
  return text.replace(/\{([A-Za-z_$][\w$]*)\}/g, (whole, name) => (
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole
  ));
}

/* 把 index.html 上的静态字填成当前语言。
   四种标记见文件头的约定；`root` 省略时就是整篇文档。
   这样"换语言"不必重建整页（用户明确选了原地重渲染、不刷新）。 */
function applyStaticTexts(root) {
  const host = root || document;
  for (const node of host.querySelectorAll('[data-i18n]')) {
    node.textContent = t(node.getAttribute('data-i18n'));
  }
  for (const node of host.querySelectorAll('[data-i18n-html]')) {
    // 内容全部来自本文件（我们自己的文案），没有任何用户数据 —— 与 app.js 里那些
    // "自己写的模板"同一个前提。档案正文那种模型/用户写的东西**永远**不走这条路。
    node.innerHTML = t(node.getAttribute('data-i18n-html'));
  }
  for (const node of host.querySelectorAll('[data-i18n-title]')) {
    node.title = t(node.getAttribute('data-i18n-title'));
  }
  for (const node of host.querySelectorAll('[data-i18n-placeholder]')) {
    node.setAttribute('placeholder', t(node.getAttribute('data-i18n-placeholder')));
  }
  // 标签页标题与 <html lang> 也跟着走（合同 §5：applyStaticTexts 含 document.title）
  document.title = t('app.title');
  const html = document.documentElement;
  if (html) html.lang = current() === 'en' ? 'en' : 'zh-CN';
}

/* 顶层的 t / current / setLanguage / applyStaticTexts 是给 app.js 直接用的
   （同一个全局词法作用域里的普通脚本），这里再挂一份到 I18N 上，
   让"只认 I18N.*"的调用方也能用。 */
I18N.t = t;
I18N.current = current;
I18N.setLanguage = setLanguage;
I18N.applyStaticTexts = applyStaticTexts;
I18N.normalize = normalizeLanguage;
globalThis.I18N = I18N;
