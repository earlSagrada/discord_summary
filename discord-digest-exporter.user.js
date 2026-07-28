// ==UserScript==
// @name         Discord Channel Digest Exporter
// @namespace    local.discord.digest
// @version      0.1
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
    hoursBack: 24,        // 往回抓多少小时
    limitByHours: true,   // true = 导出时按 hoursBack 过滤；false = 导出全部已采集消息
    autoScroll: false,    // false = 被动模式（你自己滚，脚本只记录）；true = 脚本自动滚
    scrollRatio: 0.75,    // 每次向上滚动视口高度的比例
    scrollDelayMs: 800,   // 每次滚动后等待渲染 / 加载的时间
    stagnantLimit: 6,     // 连续多少轮没有新消息就停
    stripImageResize: true, // 去掉 media.discordapp.net 的 width/height 参数以拿原图
  };

  const store = new Map(); // messageId -> record

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

  function oldestTs() {
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

  function startPassive() {
    const list = document.querySelector('[data-list-id="chat-messages"]') || document.body;
    const obs = new MutationObserver(() => {
      harvest();
      updatePanel();
    });
    obs.observe(list, { childList: true, subtree: true });
    setInterval(() => { harvest(); updatePanel(); }, 1500);
  }

  // ────────────────────────── 输出格式化 ──────────────────────────

  function finalize(limitByHours = CFG.limitByHours) {
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

  function exportAll() {
    const rows = finalize(CFG.limitByHours);
    if (!rows.length) return alert('还没采集到消息。');
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '');
    const { text, imgs } = toCompactText(rows);
    download(`discord-${stamp}.json`, JSON.stringify(rows, null, 2), 'application/json');
    download(`discord-${stamp}.txt`, text);
    if (imgs.length) download(`discord-${stamp}-images.txt`, imgs.join('\n'));
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
      '<div id="dg-count" style="margin-bottom:8px">已采集 0 条</div>' +
      '<label style="display:flex;align-items:center;gap:6px;margin-bottom:8px;cursor:pointer">' +
      '<input id="dg-limit" type="checkbox" checked style="margin:0" />' +
      '<span>仅导出最近 hoursBack</span></label>' +
      '<button id="dg-scroll" style="width:100%;margin-bottom:6px">自动向上滚动采集</button>' +
      '<button id="dg-export" style="width:100%;margin-bottom:6px">导出</button>' +
      '<button id="dg-reset" style="width:100%">清空</button>';
    document.body.appendChild(panel);
    for (const b of panel.querySelectorAll('button')) {
      b.style.cssText = b.style.cssText +
        ';background:#4e5058;color:#fff;border:0;border-radius:4px;padding:5px;cursor:pointer';
    }
    panel.querySelector('#dg-scroll').onclick = autoScrollCollect;
    panel.querySelector('#dg-export').onclick = exportAll;
    panel.querySelector('#dg-reset').onclick = () => { store.clear(); updatePanel(); };
    panel.querySelector('#dg-limit').checked = !!CFG.limitByHours;
    panel.querySelector('#dg-limit').onchange = (e) => {
      CFG.limitByHours = !!e.target.checked;
    };
  }

  function updatePanel() {
    const el = panel && panel.querySelector('#dg-count');
    if (el) el.textContent = `已采集 ${store.size} 条`;
  }

  // ────────────────────────── 启动 ──────────────────────────

  buildPanel();
  startPassive();
  if (CFG.autoScroll) autoScrollCollect();

  window.__digest = { store, harvest, exportAll, diagnose, finalize, CFG };
  console.log('[digest] 已就绪。被动模式：自己往上滚，脚本会记录。window.__digest 可手动调用。');
})();
