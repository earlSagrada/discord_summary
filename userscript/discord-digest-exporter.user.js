// ==UserScript==
// @name         Discord Channel Digest Exporter
// @namespace    local.discord.digest
// @version      0.5
// @description  从渲染后的 DOM 抓取当前频道的聊天记录，保留引用、图片和 embed，导出 JSON / 精简文本 / 图片 URL 清单
// @match        https://discord.com/channels/*
// @match        https://ptb.discord.com/channels/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  // ─────────────────────────── 配置 ───────────────────────────
  const CFG = {
    hoursBack: 1,         // 往回抓多少小时（自动导出用 1h 小窗口，减少重叠；首次回填可临时改大）
    limitByHours: true,   // true = 导出时按 hoursBack 过滤；false = 导出全部已采集消息
    autoScroll: false,    // false = 被动模式（你自己滾，脚本只记录）；true = 脚本自动滾
    autoExportMin: 15,    // 自动导出间隔（分钟）；开启“定时导出”后按时钟对齐每隔这么久跑一次
    exportLeadSec: 60,    // 导出时刻提前于“计划任务时刻”多少秒（默认早 1 分钟，让文件先落地）
    scrollRatio: 0.75,    // 每次向上滾动视口高度的比例
    scrollDelayMs: 800,   // 每次滾动后等待渲染 / 加载的时间
    stagnantLimit: 6,     // 连续多少轮没有新消息就停
    stripImageResize: true, // 去掉 media.discordapp.net 的 width/height 参数以拿原图
    // 开启“定时导出”后，每次都会依次跳转到下面这些频道采集，然后各自导出一份，最后回到你原来的频道。
    autoChannels: [
      { name: 'tradingroom', guildId: '1459447243747889345', channelId: '1459466706132013170' },
      { name: 'frank',       guildId: '1459447243747889345', channelId: '1469820012859625524' },
    ],
    navTimeoutMs: 15000,  // 切换频道后等待其加载的最长时间
    navSettleMs: 1500,    // 频道切换到位后，额外等待消息渲染的时间
    // 输出频道（就是我们推送简报的那个频道）。停摆恢复后，脚本会读它「最后一条消息的时间」，
    // 当作「上次交付到哪了」的基准，把各源频道向上滚动补采到那个时间点。
    outputChannel: { guildId: '1536800706332336198', channelId: '1536800864403198025' },
    maxBackfillHours: 12, // 停摆补采最多回看多少小时
  };

  let autoTimer = null;   // 自动导出的 setTimeout 句柄
  let autoRunning = false; // 自动导出正在跑（避免重入）
  let lastExportAt = null; // 上次成功自动导出的时间（用于面板显示 + 停摆告警 + 补采基准）
  let lastExportCount = 0; // 上次自动导出实际写出的消息条数（各频道之和）

  // localStorage 持久化：跨「标签页被浏览器丢弃后重载」也能记住状态，从而补采停摆缺口
  const LS_LAST = 'digest_lastExportAt';
  const LS_AUTO = 'digest_autoOn';
  function saveLastExport() {
    try { if (lastExportAt) localStorage.setItem(LS_LAST, lastExportAt.toISOString()); } catch (_) {}
  }
  function loadLastExport() {
    try { const v = localStorage.getItem(LS_LAST); if (v) lastExportAt = new Date(v); } catch (_) {}
  }
  function saveAutoFlag(on) {
    try { localStorage.setItem(LS_AUTO, on ? '1' : '0'); } catch (_) {}
  }
  function autoWasOn() {
    try { return localStorage.getItem(LS_AUTO) === '1'; } catch (_) { return false; }
  }

  // 每个频道独立一份 store：channelId -> Map(messageId -> record)
  const stores = new Map();

  function currentChannelId() {
    const m = location.pathname.match(/\/channels\/[^/]+\/(\d+)/);
    return m ? m[1] : null;
  }

  function storeFor(chId) {
    const key = chId || '_unknown';
    if (!stores.has(key)) stores.set(key, new Map());
    return stores.get(key);
  }

  // 当前频道对应的 store
  const currentStore = () => storeFor(currentChannelId());

  // ───────────────────────── DOM 工具 ─────────────────────────

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const BLOCK_TAGS = new Set(['DIV', 'P', 'LI', 'PRE', 'BLOCKQUOTE', 'BR']);

  /** 提取文本，同时把自定义表情的 :name: 还原出来（innerText 会丢掉它们） */
  function readText(node) {
    if (!node) return '';
    let out = '';
    for (const n of node.childNodes) {
      if (n.nodeType === Node.TEXT_NODE) {
        out += n.nodeValue;
      } else if (n.nodeName === 'IMG') {
        out += n.getAttribute('alt') || '';
      } else if (n.nodeName === 'BR') {
        out += '\n';
      } else {
        out += readText(n);
        if (BLOCK_TAGS.has(n.nodeName)) out += '\n';
      }
    }
    return out;
  }

  const clean = (s) => (s || '').replace(/\n{3,}/g, '\n\n').trim();

  function findScroller() {
    const list = document.querySelector('[data-list-id="chat-messages"]');
    let el = list;
    while (el && el !== document.body) {
      const st = getComputedStyle(el);
      if (/(auto|scroll)/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 50) return el;
      el = el.parentElement;
    }
    return document.querySelector('[class*="messagesWrapper"] [class*="scroller"]');
  }

  function messageNodes() {
    return document.querySelectorAll('li[id^="chat-messages-"]');
  }

  // ─────────────────────── 单条消息的解析 ───────────────────────

  function authorOf(li) {
    // 引用预览里也有 username，必须排除掉
    for (const c of li.querySelectorAll('[class*="username"]')) {
      if (c.closest('[id^="message-reply-context-"]')) continue;
      const t = clean(readText(c));
      if (t) return t;
    }
    return null; // 分组消息（连发）没有 header，稍后按时间向前填充
  }

  function replyOf(li) {
    const ctx = li.querySelector('[id^="message-reply-context-"]');
    if (!ctx) return null;
    const author = clean(readText(ctx.querySelector('[class*="username"]')));
    const text = clean(
      readText(ctx.querySelector('[class*="repliedTextPreview"]') || ctx.querySelector('[id^="message-content-"]'))
    );
    if (!author && !text) return null;
    return { author, text: text.slice(0, 120) };
  }

  function normalizeUrl(u) {
    if (!CFG.stripImageResize) return u;
    try {
      const url = new URL(u);
      url.searchParams.delete('width');
      url.searchParams.delete('height');
      return url.toString();
    } catch (_) {
      return u;
    }
  }

  function mediaOf(li) {
    const urls = new Set();
    const push = (u) => {
      if (u && !u.startsWith('data:') && !u.startsWith('blob:')) urls.add(normalizeUrl(u));
    };
    li.querySelectorAll('a[class*="originalLink"]').forEach((a) => push(a.href));
    li.querySelectorAll('img[class*="lazyImg"], img[data-role="img"], img[class*="embedImage"], img[class*="embedThumbnail"]')
      .forEach((img) => push(img.currentSrc || img.src));
    li.querySelectorAll('video, video source').forEach((v) => push(v.src));
    li.querySelectorAll('[class*="attachment"] a[href]').forEach((a) => push(a.href));
    return [...urls];
  }

  function embedsOf(li) {
    const nodes = li.querySelectorAll('article[class*="embed"], [class*="embedWrapper"]');
    return [...nodes]
      .map((e) => ({
        author: clean(readText(e.querySelector('[class*="embedAuthorName"]'))),
        title: clean(readText(e.querySelector('[class*="embedTitle"]'))),
        desc: clean(readText(e.querySelector('[class*="embedDescription"]'))),
        url: e.querySelector('a[class*="embedTitleLink"]')?.href || '',
      }))
      .filter((x) => x.author || x.title || x.desc);
  }

  function parse(li) {
    const id = li.id.split('-').pop();
    const timeEl = li.querySelector('time[datetime]');
    return {
      id,
      ts: timeEl ? timeEl.getAttribute('datetime') : null,
      author: authorOf(li),
      text: clean(readText(li.querySelector('[id^="message-content-"]'))),
      reply: replyOf(li),
      media: mediaOf(li),
      embeds: embedsOf(li),
    };
  }

  // ────────────────────────── 采集循环 ──────────────────────────

  function harvest() {
    const store = currentStore();
    let added = 0;
    for (const li of messageNodes()) {
      const id = li.id.split('-').pop();
      const rec = parse(li);
      const prev = store.get(id);
      // 重复看到同一条时，保留信息更完整的那份
      if (!prev || (!prev.author && rec.author) || rec.media.length > prev.media.length) {
        if (!prev) added++;
        store.set(id, prev ? { ...prev, ...rec, author: rec.author || prev.author } : rec);
      }
    }
    return added;
  }

  function oldestTs(store = currentStore()) {
    let min = null;
    for (const r of store.values()) {
      if (!r.ts) continue;
      if (!min || r.ts < min) min = r.ts;
    }
    return min;
  }

  async function autoScrollCollect() {
    const scroller = findScroller();
    if (!scroller) return alert('找不到消息滚动容器，Discord 可能改版了。运行 __digest.diagnose() 看看。');
    const cutoff = new Date(Date.now() - CFG.hoursBack * 3600 * 1000).toISOString();
    let stagnant = 0;
    harvest();
    while (stagnant < CFG.stagnantLimit) {
      const oldest = oldestTs();
      if (oldest && oldest < cutoff) break;
      if (scroller.scrollTop <= 1) stagnant++;
      scroller.scrollTop -= scroller.clientHeight * CFG.scrollRatio;
      await sleep(CFG.scrollDelayMs);
      const added = harvest();
      stagnant = added > 0 ? 0 : stagnant + 1;
      updatePanel();
    }
    updatePanel();
  }

  let passiveObserver = null;
  function attachObserver() {
    if (passiveObserver) { passiveObserver.disconnect(); passiveObserver = null; }
    const list = document.querySelector('[data-list-id="chat-messages"]');
    if (!list) return;
    passiveObserver = new MutationObserver(() => {
      harvest();
      updatePanel();
    });
    passiveObserver.observe(list, { childList: true, subtree: true });
  }

  function startPassive() {
    attachObserver();
    setInterval(() => { harvest(); updatePanel(); }, 1500);
    // Discord 是 SPA，切换频道不整页刷新；监测 URL 变化以切换 store 并重挂观察器
    let lastCh = currentChannelId();
    setInterval(() => {
      const now = currentChannelId();
      if (now !== lastCh) {
        lastCh = now;
        attachObserver();
        harvest();
        updatePanel();
      }
    }, 1000);
  }

  // ────────────────────────── 输出格式化 ──────────────────────────

  function finalize(limitByHours = CFG.limitByHours, store = currentStore()) {
    const rows = [...store.values()]
      .filter((r) => r.ts)
      .sort((a, b) => (a.ts < b.ts ? -1 : 1));
    // 分组消息向前填充作者
    let last = null;
    for (const r of rows) {
      if (r.author) last = r.author;
      else r.author = last || '(unknown)';
    }
    if (!limitByHours) return rows;
    const cutoff = new Date(Date.now() - CFG.hoursBack * 3600 * 1000).toISOString();
    return rows.filter((r) => r.ts >= cutoff);
  }

  function toCompactText(rows) {
    const imgs = [];
    const idxOf = (u) => {
      const i = imgs.indexOf(u);
      if (i >= 0) return i + 1;
      imgs.push(u);
      return imgs.length;
    };
    const lines = [];
    let lastDay = '';
    for (const r of rows) {
      const d = new Date(r.ts);
      const day = d.toISOString().slice(0, 10);
      if (day !== lastDay) { lines.push(`\n===== ${day} =====`); lastDay = day; }
      const hm = d.toTimeString().slice(0, 5);
      lines.push(`${hm} ${r.author}`);
      if (r.reply) lines.push(`  ↩ 回复 ${r.reply.author}:「${r.reply.text}」`);
      if (r.text) r.text.split('\n').forEach((l) => l.trim() && lines.push(`  ${l.trim()}`));
      for (const e of r.embeds) {
        const parts = [e.author, e.title, e.desc].filter(Boolean).join(' / ');
        if (parts) lines.push(`  [EMBED] ${parts.slice(0, 400)}`);
      }
      for (const m of r.media) lines.push(`  [IMG#${idxOf(m)}]`);
    }
    lines.push('\n===== 图片清单 =====');
    imgs.forEach((u, i) => lines.push(`IMG#${i + 1}\t${u}`));
    return { text: lines.join('\n'), imgs };
  }

  function download(name, content, type = 'text/plain;charset=utf-8') {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([content], { type }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  // 导出指定频道（chId）已采集的消息；不传则导出当前频道
  // opts.sinceISO 存在时，导出该时间点以后的全部消息（用于停摆补采）；否则按 CFG.limitByHours。
  function exportChannel(chId, opts) {
    const silent = opts && opts.silent;
    const label = (opts && opts.label) || chId || 'unknown';
    const sinceISO = opts && opts.sinceISO;
    const store = storeFor(chId);
    let rows = sinceISO
      ? finalize(false, store).filter((r) => r.ts >= sinceISO)
      : finalize(CFG.limitByHours, store);
    if (!rows.length) {
      if (silent) { console.log(`[digest] 自动导出：${label} 尚无消息，跳过。`); return 0; }
      alert(`频道 ${label} 还没采集到消息。`);
      return 0;
    }
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '');
    const tag = `${label}-${chId || 'unknown'}`;
    const { text, imgs } = toCompactText(rows);
    download(`discord-${tag}-${stamp}.json`, JSON.stringify(rows, null, 2), 'application/json');
    download(`discord-${tag}-${stamp}.txt`, text);
    if (imgs.length) download(`discord-${tag}-${stamp}-images.txt`, imgs.join('\n'));
    if (silent) console.log(`[digest] 自动导出 ${label} @ ${stamp}，${rows.length} 条${sinceISO ? '（补采）' : ''}。`);
    return rows.length;
  }

  // “② 立即导出”按钮：导出当前所在频道
  function exportAll(opts) {
    return exportChannel(currentChannelId(), { ...(opts || {}), label: '当前频道' });
  }

  // ──────────────────── 频道跳转 & 多频道自动导出 ────────────────────

  // 在 Discord 内部做 SPA 跳转（不整页刷新，避免丢失内存里已采集的数据）
  async function navigateToChannel(guildId, channelId) {
    const target = `/channels/${guildId}/${channelId}`;
    if (currentChannelId() !== channelId) {
      // 优先点侧边栏里的真实链接；找不到则退回 history + popstate
      const link = document.querySelector(`a[href="${target}"]`);
      if (link) {
        link.click();
      } else {
        history.pushState({}, '', target);
        window.dispatchEvent(new PopStateEvent('popstate'));
      }
    }
    // 等到 URL 切到位且消息列表渲染出来
    const start = Date.now();
    while (Date.now() - start < CFG.navTimeoutMs) {
      if (currentChannelId() === channelId && document.querySelector('[data-list-id="chat-messages"]')) {
        attachObserver();
        await sleep(CFG.navSettleMs);
        return true;
      }
      await sleep(300);
    }
    return false;
  }

  // 读「输出频道」最后一条消息的时间（= 我们上次交付简报的时间）。best-effort，失败返回 null。
  // 只读时间戳，不采集（不污染 store）。
  async function readOutputLastTs() {
    const oc = CFG.outputChannel;
    if (!oc || !oc.channelId) return null;
    const ok = await navigateToChannel(oc.guildId, oc.channelId);
    if (!ok) { console.warn('[digest] 读不到输出频道，跳过补采基准。'); return null; }
    await sleep(CFG.navSettleMs);
    let maxTs = null;
    for (const li of messageNodes()) {
      const t = li.querySelector('time[datetime]');
      const ts = t && t.getAttribute('datetime');
      if (ts && (!maxTs || ts > maxTs)) maxTs = ts;
    }
    return maxTs;
  }

  // 在当前频道向上滚动，把历史补采到 sinceISO（不早于 12h 硬上限），供停摆恢复用
  async function backfillCurrentTo(sinceISO) {
    const scroller = findScroller();
    if (!scroller) return;
    const hardStop = new Date(Date.now() - CFG.maxBackfillHours * 3600 * 1000).toISOString();
    const target = sinceISO > hardStop ? sinceISO : hardStop;
    let stagnant = 0;
    harvest();
    while (stagnant < CFG.stagnantLimit) {
      const oldest = oldestTs();
      if (oldest && oldest <= target) break;
      if (scroller.scrollTop <= 1) stagnant++;
      scroller.scrollTop -= scroller.clientHeight * CFG.scrollRatio;
      await sleep(CFG.scrollDelayMs);
      const added = harvest();
      stagnant = added > 0 ? 0 : stagnant + 1;
      updatePanel();
    }
  }

  // 依次跳到 CFG.autoChannels 的每个频道采集，再各自导出，最后回到原频道。
  // 若检测到停摆（距上次导出 > 2 个周期），先读输出频道最后消息时间为基准，
  // 各源频道向上滚动补采到该时间点（最多 12h），再导出这段完整区间。
  async function autoExportRun(opts) {
    const silent = opts && opts.silent;
    if (autoRunning) { console.log('[digest] 上一次自动导出还没跑完，本次跳过。'); return; }
    autoRunning = true;
    const originPath = location.pathname; // 记住原来所在位置，跑完回去
    const originMatch = originPath.match(/\/channels\/([^/]+)\/(\d+)/);
    try {
      const nowMs = Date.now();
      const lsLast = lastExportAt ? lastExportAt.getTime() : null;
      const stalled = lsLast && (nowMs - lsLast > CFG.autoExportMin * 2 * 60 * 1000);
      let sinceISO = null;
      if (stalled) {
        // 基准优先用输出频道最后一条消息（= 上次真正交付的时间）；读不到则退回上次导出时间
        let baseMs = lsLast;
        const outTs = await readOutputLastTs();
        if (outTs) baseMs = Date.parse(outTs);
        const hardStopMs = nowMs - CFG.maxBackfillHours * 3600 * 1000;
        sinceISO = new Date(Math.max(baseMs, hardStopMs)).toISOString();
        console.log(`[digest] 检测到停摆，本次补采自 ${sinceISO}（最多 ${CFG.maxBackfillHours}h）。`);
      }

      for (const ch of CFG.autoChannels) {
        const ok = await navigateToChannel(ch.guildId, ch.channelId);
        if (!ok) { console.warn(`[digest] 无法切换到 ${ch.name}（${ch.channelId}），跳过。`); continue; }
        if (sinceISO) {
          await backfillCurrentTo(sinceISO);   // 停摆：向上滚动补采缺口
        } else {
          // 正常：采集当前可见消息（多采几次，等惰性渲染）
          harvest(); await sleep(600);
          harvest(); await sleep(600);
          harvest();
        }
        updatePanel();
      }
      // 回到原频道
      if (originMatch) {
        await navigateToChannel(originMatch[1], originMatch[2]);
      }
      // 各频道分别导出（停摆时导出整段缺口区间）
      let wrote = 0;
      for (const ch of CFG.autoChannels) {
        wrote += exportChannel(ch.channelId, { silent, label: ch.name, sinceISO }) || 0;
      }
      lastExportAt = new Date();
      lastExportCount = wrote;
      saveLastExport();
    } finally {
      autoRunning = false;
      updatePanel();
    }
  }

  // 定时导出：标签页保持打开，按“时钟对齐”自动跑多频道采集+导出
  // 导出时刻 = 每个 autoExportMin 整点边界（如 :00/:15/:30/:45）提前 exportLeadSec 秒，
  // 这样文件在本地「计划任务」触发前就落地了。用 setTimeout 逐次重排，避免 setInterval 漂移。
  function msUntilNextExport() {
    const now = Date.now();
    const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
    const base = dayStart.getTime();
    const step = CFG.autoExportMin * 60 * 1000;
    const lead = CFG.exportLeadSec * 1000;
    let k = Math.ceil((now - base + lead) / step);
    let target = base + k * step - lead;
    while (target <= now + 500) { k += 1; target = base + k * step - lead; }
    return target - now;
  }

  function scheduleNextExport() {
    const ms = msUntilNextExport();
    autoTimer = setTimeout(() => {
      autoExportRun({ silent: true });
      scheduleNextExport();
    }, ms);
    const at = new Date(Date.now() + ms);
    console.log(`[digest] 下次自动导出：${at.toLocaleTimeString()}（计划任务前 ${CFG.exportLeadSec}s）`);
  }

  function toggleAuto(force) {
    const on = typeof force === 'boolean' ? force : !autoTimer;
    if (on && !autoTimer) {
      scheduleNextExport();
      saveAutoFlag(true);
      console.log(`[digest] 自动导出已开启（时钟对齐，每 ${CFG.autoExportMin} 分钟）；覆盖频道：${CFG.autoChannels.map((c) => c.name).join('、')}。需要立刻导出可点「②立即导出」。`);
    } else if (!on && autoTimer) {
      clearTimeout(autoTimer);
      autoTimer = null;
      saveAutoFlag(false);
      console.log('[digest] 自动导出已关闭。');
    }
    const btn = panel && panel.querySelector('#dg-auto');
    if (btn) btn.textContent = autoTimer ? `③ 定时导出：开（每${CFG.autoExportMin}分钟·对齐）` : '③ 定时导出：关';
    return !!autoTimer;
  }

  // ────────────────────────── 自检 ──────────────────────────

  function diagnose() {
    const checks = {
      'scroller': findScroller() ? 1 : 0,
      'li[id^=chat-messages-]': messageNodes().length,
      '[id^=message-content-]': document.querySelectorAll('[id^="message-content-"]').length,
      '[class*=username]': document.querySelectorAll('[class*="username"]').length,
      'time[datetime]': document.querySelectorAll('time[datetime]').length,
      '[id^=message-reply-context-]': document.querySelectorAll('[id^="message-reply-context-"]').length,
    };
    console.table(checks);
    console.log('任何一项为 0 说明该选择器已失效，需要在 F12 里重新确认 Discord 的 DOM 结构。');
    return checks;
  }

  // ────────────────────────── 悬浮面板 ──────────────────────────

  let panel;
  function buildPanel() {
    panel = document.createElement('div');
    panel.style.cssText =
      'position:fixed;right:16px;bottom:16px;z-index:99999;background:#1e1f22;color:#dbdee1;' +
      'font:12px/1.5 system-ui;padding:10px 12px;border-radius:8px;border:1px solid #3f4147;' +
      'box-shadow:0 4px 16px rgba(0,0,0,.4);min-width:180px';
    panel.innerHTML =
      '<div style="font-weight:600;margin-bottom:6px">📥 聊天导出器</div>' +
      '<div id="dg-count" style="margin-bottom:8px">已采集 0 条消息</div>' +
      '<div id="dg-last" style="margin-bottom:8px;font-size:11px;color:#949ba4">定时导出：未开启</div>' +
      '<label style="display:flex;align-items:center;gap:6px;margin-bottom:8px;cursor:pointer" ' +
      'title="勾选=只导出最近这段时间的消息；取消=导出所有已采集的消息">' +
      '<input id="dg-limit" type="checkbox" checked style="margin:0" />' +
      `<span>仅导出最近 ${CFG.hoursBack} 小时</span></label>` +
      '<button id="dg-scroll" style="width:100%;margin-bottom:6px" ' +
      'title="脚本模拟往上滚动，把更早的历史消息也采集进来（被动模式下你也可以自己滚）">' +
      '① 自动往上滚·补历史</button>' +
      '<button id="dg-export" style="width:100%;margin-bottom:6px" ' +
      'title="立刻把已采集的消息导出成 3 个文件（JSON / 精简文本 / 图片清单）到下载目录">' +
      '② 立即导出（下载文件）</button>' +
      '<button id="dg-auto" style="width:100%;margin-bottom:6px" ' +
      'title="开启后每隔固定时间自动导出一次；标签页需保持打开，关掉就停">' +
      '③ 定时导出：关</button>' +
      '<button id="dg-reset" style="width:100%" ' +
      'title="清空已采集的消息缓存并从 0 重新计数；不会删除已下载的文件">' +
      '清空计数（重新采集）</button>';
    document.body.appendChild(panel);
    for (const b of panel.querySelectorAll('button')) {
      b.style.cssText = b.style.cssText +
        ';background:#4e5058;color:#fff;border:0;border-radius:4px;padding:5px;cursor:pointer';
    }
    panel.querySelector('#dg-scroll').onclick = autoScrollCollect;
    panel.querySelector('#dg-export').onclick = () => exportAll();
    panel.querySelector('#dg-auto').onclick = () => toggleAuto();
    panel.querySelector('#dg-reset').onclick = () => { currentStore().clear(); updatePanel(); };
    panel.querySelector('#dg-limit').checked = !!CFG.limitByHours;
    panel.querySelector('#dg-limit').onchange = (e) => {
      CFG.limitByHours = !!e.target.checked;
    };
  }

  function updatePanel() {
    const el = panel && panel.querySelector('#dg-count');
    if (!el) return;
    const chId = currentChannelId();
    const total = [...stores.values()].reduce((n, s) => n + s.size, 0);
    el.textContent = `本频道 ${currentStore().size} 条 · 合计 ${total} 条` + (chId ? `（ch ${chId}）` : '');

    const last = panel.querySelector('#dg-last');
    if (!last) return;
    if (!autoTimer && !lastExportAt) {
      last.textContent = '定时导出：未开启';
      last.style.color = '#949ba4';
      return;
    }
    if (!lastExportAt) {
      last.textContent = '定时导出：已开启，等待首次导出…';
      last.style.color = '#949ba4';
      return;
    }
    const ageMin = (Date.now() - lastExportAt.getTime()) / 60000;
    const hhmm = lastExportAt.toLocaleTimeString();
    // 超过 2 个导出周期没动静 → 判定停摆，标红提醒
    const stale = ageMin > CFG.autoExportMin * 2 + 1;
    last.textContent = `上次导出 ${hhmm}（${Math.round(ageMin)}分钟前 · ${lastExportCount}条）`
      + (stale ? ' ⚠停摆?' : '');
    last.style.color = stale ? '#f23f42' : '#949ba4';
  }

  // 停摆看门狗：每分钟检查一次，若长时间没有自动导出就在控制台告警（多因标签页被后台丢弃/电脑睡眠）
  function startStallWatchdog() {
    setInterval(() => {
      updatePanel();
      if (!autoTimer || !lastExportAt) return;
      const ageMin = (Date.now() - lastExportAt.getTime()) / 60000;
      if (ageMin > CFG.autoExportMin * 2 + 1) {
        console.warn(`[digest] ⚠ 已 ${Math.round(ageMin)} 分钟没有自动导出。`
          + '常见原因：此标签页被浏览器后台丢弃、或电脑进入睡眠。'
          + '把 Discord 标签页保持前台/固定(pin)，或检查系统睡眠设置。');
      }
    }, 60 * 1000);
  }

  // ────────────────────────── 启动 ──────────────────────────

  // 等 Discord 的聊天列表容器挂载完毕后再初始化，避免在 React 启动阶段插入 DOM 导致白屏
  function waitForDiscord(callback, maxWaitMs = 30000) {
    const start = Date.now();
    const timer = setInterval(() => {
      const ready =
        document.querySelector('[data-list-id="chat-messages"]') ||
        document.querySelector('[class*="messagesWrapper"]') ||
        document.querySelector('[class*="chat-"]');
      if (ready) {
        clearInterval(timer);
        callback();
      } else if (Date.now() - start > maxWaitMs) {
        clearInterval(timer);
        // 超时后降级直接启动，避免永远等不到
        callback();
      }
    }, 500);
  }

  window.__digest = {
    stores, storeFor, currentStore, currentChannelId,
    harvest, exportAll, exportChannel, autoExportRun, navigateToChannel,
    toggleAuto, diagnose, finalize, CFG,
  };

  waitForDiscord(() => {
    buildPanel();
    startPassive();
    startStallWatchdog();
    loadLastExport();             // 恢复上次导出时间（跨标签页丢弃/重载），用于停摆补采
    updatePanel();
    if (autoWasOn()) {            // 之前开着定时导出 → 重载后自动恢复（并补采停摆缺口）
      toggleAuto(true);
      console.log('[digest] 侦测到之前已开启定时导出 → 自动恢复。若刚经历停摆，下一轮会自动向上滚动补采。');
    }
    if (CFG.autoScroll) autoScrollCollect();
    console.log('[digest] 已就绪。被动模式：自己往上滾，脚本会记录。window.__digest 可手动调用（例：__digest.toggleAuto(true) 开启每 15min 自动导出）。');
  });
})();
