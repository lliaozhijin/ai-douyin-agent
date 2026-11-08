"use strict";
/* AI也要刷抖音（aiWannaTikTok）—— 界面逻辑。所有数据通过 window.pywebview.api 和 Python 交互。 */

/* ---------- 整体等比缩放 ----------
   界面按固定的设计尺寸（1360 × 860）排版，窗口变大变小时整体按比例放大缩小，
   不会因为窗口变窄而重新排版、换行或错位。窗口比例和设计稿不同时，多出来的部分留白居中。 */
const DESIGN_W = 1360;
const DESIGN_H = 860;

function fitStage() {
  const w = window.innerWidth || DESIGN_W;
  const h = window.innerHeight || DESIGN_H;
  const scale = Math.min(w / DESIGN_W, h / DESIGN_H);
  const root = document.documentElement;
  root.style.zoom = String(scale);
  // 缩放后，可用高度（按设计尺寸算）= 窗口高度 / 比例；宽度固定为设计宽度
  root.style.setProperty("--stage-h", `${Math.max(DESIGN_H, Math.floor(h / scale))}px`);
}
fitStage();
window.addEventListener("resize", fitStage);

const S = {
  page: "dashboard",
  evPage: 1,                 // 仪表盘“她刚刚在做什么”的第几页
  hist: { day: "", group: "", page: 1, data: null },  // 历史记录页
  st: null,
  msgFilter: "all",
  msgs: [],
  counts: {},
  selMsg: null,
  providers: null,
  tasks: null,
  selProv: "anthropic",
  rules: null,
  persona: null,
  tryLog: [],
  busy: false,
  drafts: null,
  selDraft: null,
  draft: null,
  pet: null,
  calib: null,
  friends: null,
  uiSeq: null,
};

/* ---------- 小工具 ---------- */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const HEART = '<svg class="heart" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/></svg>';
const ICON = {
  dashboard: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="9" rx="2.5"/><rect x="14" y="3" width="7" height="5" rx="2.5"/><rect x="14" y="12" width="7" height="9" rx="2.5"/><rect x="3" y="16" width="7" height="5" rx="2.5"/></svg>',
  inbox: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-5 4z"/></svg>',
  persona: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/></svg>',
  models: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l3 3"/></svg>',
  rules: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/></svg>',
  settings: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
  drafts: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/></svg>',
  pet: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="9" r="5"/><path d="M8 14l-2 7M16 14l2 7M9 21h6"/><circle cx="10.2" cy="8.5" r=".6" fill="currentColor"/><circle cx="13.8" cy="8.5" r=".6" fill="currentColor"/></svg>',
  history: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 7v5l3 2"/></svg>',
  logo: '<span class="logo-word">Rain</span>',
  check: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>',
};
const PAGES = [
  ["dashboard", "仪表盘"], ["history", "历史记录"], ["inbox", "消息中心"], ["drafts", "作品"], ["persona", "人设"], ["pet", "桌宠"],
  ["models", "模型与 API Key"], ["rules", "规则与预算"], ["settings", "设置"],
];

/* 提示框：返回 Promise，点确认为 true，点取消、按 Esc 或点遮罩为 false */
function dialog({ title, body, html, ok = "知道了", cancel = "取消", wide = false, urgent = false }) {
  let closeFn = null;
  const p = new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "modal-backdrop";
    wrap.innerHTML = `
      <div class="modal${wide ? " wide" : ""}${urgent ? " urgent" : ""}" role="${urgent ? "alertdialog" : "dialog"}" aria-modal="true" aria-labelledby="modal-title">
        ${urgent ? `<div class="urgent-tag">需要你亲自处理</div>` : ""}
        ${html ? "" : `<img src="assets/mascot.svg" alt="" class="modal-mascot cute-only">`}
        <h2 id="modal-title">${esc(title)}</h2>
        ${html ? `<div class="modal-body">${html}</div>` : `<p>${esc(body)}</p>`}
        <div class="actions" style="justify-content:flex-end">
          ${cancel ? `<button type="button" class="btn" data-modal="0">${esc(cancel)}</button>` : ""}
          <button type="button" class="btn btn-primary" data-modal="1">${esc(ok)}</button>
        </div>
      </div>`;
    const close = (v) => { document.removeEventListener("keydown", onKey); wrap.remove(); resolve(v); };
    closeFn = () => close(false);
    const onKey = (e) => { if (e.key === "Escape") close(false); };
    wrap.addEventListener("click", (e) => {
      const b = e.target.closest("[data-modal]");
      if (b) close(b.dataset.modal === "1");
      else if (e.target === wrap) close(false);
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(wrap);
    wrap.querySelector('[data-modal="1"]').focus();
  });
  p.close = () => closeFn && closeFn();  // 让程序可以主动关掉弹窗（比如验证已经完成）
  return p;
}

/* ---------- 需要你亲自处理的事：手机验证码 / 滑块验证 —— 弹窗提醒，完成后自动关闭 ---------- */
let shownAttention = 0;
let attentionDialog = null;

function handleAttention(att) {
  if (att && att.id !== shownAttention) {
    shownAttention = att.id;
    if (attentionDialog) attentionDialog.close();
    const d = dialog({ title: att.title, body: att.body, ok: "去浏览器里处理", cancel: "知道了", urgent: true });
    attentionDialog = d;
    d.then((go) => {
      if (attentionDialog === d) attentionDialog = null;
      if (go) call("browser_window", true);
    });
  } else if (!att && attentionDialog) {
    attentionDialog.close();
    attentionDialog = null;
    toast("验证完成啦，她继续干活～");
  }
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), isErr ? 4500 : 2200);
}

async function call(name, ...args) {
  const r = await window.pywebview.api[name](...args);
  if (!r || !r.ok) {
    const msg = (r && r.error) || "出错了";
    toast(msg, true);
    throw new Error(msg);
  }
  return r.data;
}

function avatar(size) {
  const url = S.st && S.st.persona && S.st.persona.avatar_url;
  const src = url || "assets/mascot.svg";
  return `<div class="avatar${size >= 80 ? " lg" : ""}" style="width:${size}px;height:${size}px"><img src="${esc(src)}" alt=""></div>`;
}

function greet() {
  const h = new Date().getHours();
  if (h < 6) return "夜深啦～";
  if (h < 11) return "早上好呀～";
  if (h < 14) return "中午好呀～";
  if (h < 18) return "下午好呀～";
  return "晚上好呀～";
}

const hhmm = (ts) => (ts || "").slice(11, 16);

/* ---------- 侧边栏 ---------- */
function renderSide() {
  const st = S.st;
  if (!st) return;
  const p = st.persona;
  const pending = (st.counts && st.counts.pending_count) || 0;
  const nav = PAGES.map(([k, label]) => `
    <button type="button" data-act="nav" data-page="${k}" ${S.page === k ? 'aria-current="page"' : ""}>
      ${ICON[k]}${label}${k === "inbox" && pending ? `<span class="badge">${pending}</span>` : ""}${k === "drafts" && (st.wishes || []).length ? `<span class="badge">${st.wishes.length}</span>` : ""}
    </button>`).join("");
  const logged = st.agent.logged_in;
  $("#side").innerHTML = `
    <div class="brand">
      <div class="brand-logo" aria-hidden="true">${ICON.logo}</div>
      <div><div class="brand-name">${esc(st.app.name)}</div><div class="brand-sub">v${esc(st.app.version)} · 本地运行<span class="cute-only"> ♡</span></div></div>
    </div>
    <div class="nav">${nav}</div>
    <div class="side-card">
      <div class="who">${avatar(46)}
        <div style="min-width:0"><div class="name">${esc(p.name)}</div>
        <div class="${logged ? "login-ok" : "login-no"}">● ${logged ? "抖音已登录" : "抖音未登录"}</div></div>
      </div>
      ${p.signature ? `<div class="sig">${esc(p.signature)}</div>` : ""}
      <div class="login-row">
        <button type="button" data-act="login">${logged ? "已登录 ✓" : "扫码登录"}</button>
        <button type="button" data-act="logout" ${logged ? "" : "disabled"}>退出登录</button>
      </div>
    </div>`;
}

/* ---------- 仪表盘 ---------- */
const EVENT_PILL = {
  like: ["brand", "点赞"], collect: ["brand", "收藏"], comment: ["brand", "点赞 + 评论"], reply_at: ["ok", "回复 @"], reply_dm: ["ok", "回复私信"],
  comment_like: ["brand", "赞评论"], comment_reply: ["ok", "回复评论"], thread_reply: ["ok", "接着聊"],
  at_friend: ["brand", "@好友"], publish_wish: ["warn", "想发作品"], published: ["ok", "发布作品"],
  skip: ["neutral", "划走"], pause: ["bad", "暂停"], info: ["neutral", "提示"], error: ["bad", "出错"],
};

function eventPill(e) {
  let [cls, label] = EVENT_PILL[e.kind] || ["neutral", e.kind];
  if (["like", "collect", "comment"].includes(e.kind)) {
    const m = /^((?:点赞|收藏|评论)(?: \+ (?:点赞|收藏|评论))*) @/.exec(e.title || "");
    if (m) label = m[1];
  }
  if (e.kind === "skip" && e.title.startsWith("放进")) { cls = "warn"; label = "待你处理"; }
  else if (e.kind === "skip" && e.title.startsWith("没有回复")) { label = "跳过"; }
  else if (e.kind === "skip" && e.title.startsWith("看完")) { label = "看完"; }
  return `<span class="pill ${cls}">${label}</span>`;
}

/* “她正在看”的 4 个画面格：截到的显示真实画面，没截的说明原因 */
function frameTiles(frames) {
  const shots = (frames || []).filter((u) => typeof u === "string" && u.startsWith("data:image/"));
  return [0, 1, 2, 3].map((i) => shots[i]
    ? `<div class="shot"><img src="${esc(shots[i])}" alt="视频画面 ${i + 1}"></div>`
    : `<div>${i === 0 ? "截图中…" : "感兴趣才截"}</div>`).join("");
}

/* ---------- 详情：她评论了什么 / 别人说了什么、她回了什么 ---------- */
const DETAIL_KINDS = {
  comments: "她今天发的评论", comment_replies: "她今天回复别人的评论", comment_likes: "她今天赞过的评论",
  replies_at: "她今天回复的 @", replies_dm: "她今天回复的私信", likes: "她今天点赞的视频", collects: "她今天收藏的视频",
  thread_replies: "她今天接着聊的评论", at_friends: "她今天 @ 好友的视频", publishes: "她今天发布的作品",
};

function detailHTML(d) {
  const v = d.video && (d.video.title || d.video.url) ? `
    <div class="dt-video"><span>视频：${esc(d.video.title || "（无文案）")}${d.video.author ? ` · @${esc(d.video.author)}` : ""}</span>
      ${d.video.url ? `<button type="button" class="btn btn-sm" data-act="open-video" data-url="${esc(d.video.url)}">打开视频</button>` : ""}</div>` : "";
  const parts = (d.parts || []).map((p) => {
    if (p.note) return `<p class="dt-note"><b>${esc(p.who)}：</b>${esc(p.text)}</p>`;
    if (p.me) return `<div class="me">${avatar(36)}<div class="dt-col"><span class="dt-who">${esc(p.who)}</span>
      ${p.text ? `<div class="bubble">${esc(p.text)}</div>` : ""}</div></div>`;
    return `<div class="them"><span class="letter" aria-hidden="true">${esc((p.who || "?").slice(0, 1))}</span>
      <div class="dt-col"><span class="dt-who">${esc(p.who)}</span><div class="bubble">${esc(p.text)}</div></div></div>`;
  }).join("");
  return `<div class="dt">${v}<div class="chat">${parts}</div><div class="dt-time">${esc(d.time || "")}</div></div>`;
}

async function showEventDetail(ref, title) {
  if (ref.startsWith("draft:")) { S.selDraft = Number(ref.slice(6)); await showPage("drafts"); return; }
  const d = await call("get_detail", ref);
  if (d) await dialog({ title, html: detailHTML(d), ok: "关闭", cancel: "", wide: true });
}

async function showActivity(kind) {
  const list = await call("list_activity", kind);
  if (!list) return;
  const html = list.length ? list.map(detailHTML).join("") : `<p class="empty">今天还没有哦</p>`;
  await dialog({ title: `${DETAIL_KINDS[kind] || "详情"} · ${list.length}`, html, ok: "关闭", cancel: "", wide: true });
}

function renderDashboard() {
  const st = S.st;
  const a = st.agent;
  const u = Object.fromEntries(st.usage.map((x) => [x.key, x]));
  const replies = (u.replies_at?.used || 0) + (u.replies_dm?.used || 0);
  const tiles = st.usage.map((x) => {
    const inner = `
      <span class="k">${esc(x.label)}${DETAIL_KINDS[x.key] ? `<span class="more">详情 ›</span>` : ""}</span>
      <div class="v"><b>${x.key === "cost" ? "¥" + Number(x.used).toFixed(2) : x.used}</b><span>/ ${x.key === "cost" ? "¥" + x.limit : x.limit}</span></div>
      <div class="bar${x.key === "cost" ? " cost" : ""}"><i style="width:${x.pct}%"></i></div>`;
    return DETAIL_KINDS[x.key]
      ? `<button type="button" class="tile tile-btn" data-act="activity" data-k="${x.key}" title="点开看今天的详情">${inner}</button>`
      : `<div class="tile">${inner}</div>`;
  }).join("");
  const ev = st.events || { items: [], page: 1, pages: 1, total: 0 };
  S.evPage = ev.page;
  const feed = ev.items.length ? ev.items.map((e) => eventItem(e)).join("")
    : `<li class="empty">还没有动态，点右上角“开始刷视频”让她开始刷视频吧</li>`;
  const feedPager = pager(ev.page, ev.pages, "ev-page") +
    `<button type="button" class="btn btn-sm btn-link" data-act="nav" data-page="history">${ev.total > ev.shown ? `更早的 ${ev.total - ev.shown} 条在历史记录里` : "按日期查看历史记录"} ›</button>`;
  const todo = st.todo.length ? st.todo.map((m) => `
    <div class="todo-item"><span>${{ at: "@", reply: "评论" }[m.kind] || "私信"} ${esc(m.user_name)}：${esc((m.reason || "").slice(0, 30))}</span>
    <button type="button" class="btn btn-sm" data-act="goto-msg" data-id="${m.id}">去看看</button></div>`).join("")
    : `<p class="empty">暂时没有需要你处理的事</p>`;
  const cur = a.current || {};
  const stateCls = { running: "ok", resting: "neutral", captcha: "bad", paused: "warn", error: "bad", login: "warn" }[a.state] || "neutral";
  const rb = st.rules_brief;
  const nextInbox = a.running
    ? ` · 私信${rb.dm_reply_mode === "5min" ? "每 5 分钟回一次" : "秒回"} · ${a.next_at_s > 0 ? `${Math.ceil(a.next_at_s / 60)} 分钟后去看 @` : "马上去看 @"}`
    : "";

  $("#main").innerHTML = `
    <header class="page-head">
      <div><h1>仪表盘</h1><p>活跃时段 ${esc(st.rules_brief.active_start)}–${esc(st.rules_brief.active_end)}${nextInbox}</p></div>
      <div class="actions">
        <span class="pill big ${stateCls}">● ${esc(a.label)}</span>
        <button type="button" class="btn" data-act="check-inbox">立即检查 @ 和私信</button>
        ${a.running ? `<button type="button" class="btn btn-primary" data-act="pause">让她休息一下</button>`
                    : `<button type="button" class="btn btn-primary" data-act="start">开始刷视频</button>`}
      </div>
    </header>
    ${a.alert ? `<div class="alert" role="alert"><span>${esc(a.alert)}</span><button type="button" class="btn btn-sm" data-act="dismiss">知道了</button></div>` : ""}
    ${!st.models_ready ? `<div class="alert"><span>还没有配置模型：先去“模型与 API Key”填好 API Key 并分配模型，她才能开始思考哦</span><button type="button" class="btn btn-sm" data-act="nav" data-page="models">去配置</button></div>` : ""}
    <section class="hello" aria-label="问候和心情">
      ${avatar(112)}
      <div class="bubble-box">
        <span class="hi">${greet()}</span>
        <span>今天我已经刷了 ${u.videos?.used || 0} 个视频、点了 ${u.likes?.used || 0} 个赞、回复了 ${replies} 条消息！${st.todo.length ? `有 ${st.todo.length} 件事想请你帮忙看看哦` : "今天也要元气满满"}<span class="cute-only"> ♡</span></span>
        ${a.detail ? `<span style="font-size:13px;color:var(--muted)">${esc(a.detail)}</span>` : ""}
      </div>
      ${moodCard(st.mood)}
    </section>
    ${(st.wishes || []).length ? `<div class="alert wish-alert"><span>💌 您的小可爱想发作品：《${esc(st.wishes[0].title || "未命名")}》${st.wishes.length > 1 ? ` 等 ${st.wishes.length} 个` : ""}</span><button type="button" class="btn btn-sm btn-primary" data-act="goto-draft" data-id="${st.wishes[0].id}">去看看</button></div>` : ""}
    <section class="tiles" aria-label="今日用量">${tiles}</section>
    <div class="row">
      <section class="card grow-3"><h2>${HEART}她刚刚在做什么</h2><ul class="feed">${feed}</ul>
        <div class="pager-row">${feedPager}</div></section>
      <div class="col grow-2">
        <section class="card"><h2>${HEART}她正在看</h2>
          ${cur.title ? `<div class="frames">${frameTiles(cur.frames)}</div>
            <div><b>${esc(cur.title)}</b><div style="color:var(--muted);font-size:13px">@${esc(cur.author)}</div></div>
            <div style="color:var(--brand-dark);font-size:13px">${esc(cur.step || "")}</div>` : `<p class="empty">还没开始看视频</p>`}
        </section>
        <section class="card"><h2>${HEART}需要你帮忙 · ${st.todo.length}</h2>${todo}</section>
      </div>
    </div>`;
}

/* 一条操作记录（仪表盘和历史记录共用）；withDate 时时间前面带日期 */
function eventItem(e, withDate = false) {
  const when = withDate ? `${esc((e.ts || "").slice(5, 10))} ${esc(hhmm(e.ts))}` : esc(hhmm(e.ts));
  return `
    <li><span class="t">${when}</span>${eventPill(e)}
      ${e.ref
        ? `<button type="button" class="body body-btn" data-act="event-detail" data-ref="${esc(e.ref)}" data-title="${esc(e.title)}">
             <span>${esc(e.title)}<span class="more">详情 ›</span></span>${e.detail ? `<span>${esc(e.detail)}</span>` : ""}</button>`
        : `<div class="body"><span>${esc(e.title)}</span>${e.detail ? `<span>${esc(e.detail)}</span>` : ""}</div>`}</li>`;
}

/* 翻页条：‹ 上一页  第 2 / 10 页  下一页 › */
function pager(page, pages, act) {
  if (pages <= 1) return `<span class="pager-info">共 1 页</span>`;
  return `<div class="pager" role="navigation" aria-label="翻页">
    <button type="button" class="btn btn-sm" data-act="${act}" data-p="1" ${page <= 1 ? "disabled" : ""} aria-label="第一页">«</button>
    <button type="button" class="btn btn-sm" data-act="${act}" data-p="${page - 1}" ${page <= 1 ? "disabled" : ""}>‹ 上一页</button>
    <span class="pager-info">第 ${page} / ${pages} 页</span>
    <button type="button" class="btn btn-sm" data-act="${act}" data-p="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页 ›</button>
    <button type="button" class="btn btn-sm" data-act="${act}" data-p="${pages}" ${page >= pages ? "disabled" : ""} aria-label="最后一页">»</button>
  </div>`;
}

/* ---------- 时间选择：时、分各是一个数字块，点开后直接点某个数字 ---------- */
function timeField(id, value) {
  const [h, m] = String(value || "00:00").split(":").map((x) => String(Number(x) || 0).padStart(2, "0"));
  return `<div class="tp" data-tp="${id}" role="group" aria-labelledby="${id}-lab">
    <input type="hidden" id="${id}" value="${h}:${m}">
    <button type="button" class="tp-part" data-act="tp-open" data-id="${id}" data-part="h" aria-haspopup="true" aria-expanded="false" aria-label="小时：${h}">${h}</button>
    <span class="tp-colon" aria-hidden="true">:</span>
    <button type="button" class="tp-part" data-act="tp-open" data-id="${id}" data-part="m" aria-haspopup="true" aria-expanded="false" aria-label="分钟：${m}">${m}</button>
    <span class="tp-unit">${Number(h) < 12 ? "上午" : "下午"}</span>
  </div>`;
}

function closeTimePops(except) {
  document.querySelectorAll(".tp-pop").forEach((p) => { if (p !== except) p.remove(); });
  document.querySelectorAll(".tp-part[aria-expanded='true']").forEach((b) => b.setAttribute("aria-expanded", "false"));
}

function openTimePop(btn) {
  const wrap = btn.closest(".tp");
  const part = btn.dataset.part;
  const had = wrap.querySelector(`.tp-pop[data-part="${part}"]`);
  closeTimePops();
  if (had) return;                     // 再点一次就是收起
  const input = wrap.querySelector("input[type=hidden]");
  const [h, m] = input.value.split(":");
  const cur = part === "h" ? h : m;
  const max = part === "h" ? 24 : 60;
  const nums = Array.from({ length: max }, (_, i) => String(i).padStart(2, "0"));
  const pop = document.createElement("div");
  pop.className = `tp-pop ${part === "h" ? "tp-hours" : "tp-mins"}`;
  pop.dataset.part = part;
  pop.setAttribute("role", "listbox");
  pop.setAttribute("aria-label", part === "h" ? "选择小时" : "选择分钟");
  pop.innerHTML = `<div class="tp-title">${part === "h" ? "选小时" : "选分钟"}</div><div class="tp-grid">${nums.map((n) =>
    `<button type="button" class="tp-num${n === cur ? " on" : ""}" role="option" aria-selected="${n === cur}" data-act="tp-pick" data-v="${n}">${n}</button>`).join("")}</div>`;
  wrap.appendChild(pop);
  btn.setAttribute("aria-expanded", "true");
  (pop.querySelector(".tp-num.on") || pop.querySelector(".tp-num")).focus();
}

function pickTime(numBtn) {
  const pop = numBtn.closest(".tp-pop");
  const wrap = numBtn.closest(".tp");
  const input = wrap.querySelector("input[type=hidden]");
  let [h, m] = input.value.split(":");
  if (pop.dataset.part === "h") h = numBtn.dataset.v; else m = numBtn.dataset.v;
  input.value = `${h}:${m}`;
  const [bh, bm] = wrap.querySelectorAll(".tp-part");
  bh.textContent = h; bh.setAttribute("aria-label", `小时：${h}`);
  bm.textContent = m; bm.setAttribute("aria-label", `分钟：${m}`);
  wrap.querySelector(".tp-unit").textContent = Number(h) < 12 ? "上午" : "下午";
  const back = pop.dataset.part === "h" ? bh : bm;
  closeTimePops();
  back.focus();
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

document.addEventListener("mousedown", (e) => { if (!e.target.closest(".tp")) closeTimePops(); });
document.addEventListener("keydown", (e) => {
  const pop = e.target.closest && e.target.closest(".tp-pop");
  if (e.key === "Escape" && document.querySelector(".tp-pop")) {
    const btn = document.querySelector(".tp-part[aria-expanded='true']");
    closeTimePops(); if (btn) btn.focus(); return;
  }
  if (!pop || !e.target.classList.contains("tp-num")) return;
  const all = [...pop.querySelectorAll(".tp-num")];
  const i = all.indexOf(e.target);
  const cols = pop.classList.contains("tp-hours") ? 6 : 10;
  const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols }[e.key];
  if (step) { e.preventDefault(); const j = Math.min(all.length - 1, Math.max(0, i + step)); all[j].focus(); }
});

/* ---------- 历史记录：按日期、按类型查看她做过的所有操作 ---------- */
const HIST_GROUPS = [["", "全部"], ["interact", "点赞 / 收藏 / 评论"], ["reply", "回复 @ / 私信"], ["watch", "看完 / 跳过"],
  ["publish", "作品"], ["system", "提示和出错"]];

async function loadHistory() {
  const h = S.hist;
  h.data = await call("get_history", h.day, h.group, h.page);
  h.page = h.data.page;
}

function shiftDay(day, n) {
  const d = new Date(day + "T12:00:00");
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}

function renderHistory() {
  const h = S.hist, d = h.data || { items: [], page: 1, pages: 1, total: 0, days: [] };
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const days = d.days || [];
  const chips = days.slice(0, 14).map((x) => `<button type="button" class="chip${h.day === x.day ? " on" : ""}" data-act="h-day" data-day="${esc(x.day)}">${esc(x.day.slice(5))}<small>${x.n}</small></button>`).join("");
  const list = d.items.length ? d.items.map((e) => eventItem(e, !h.day)).join("")
    : `<li class="empty">${h.day ? `${esc(h.day)} 这天没有${h.group ? "这类" : ""}记录` : "还没有记录"}</li>`;
  $("#main").innerHTML = `
    <header class="page-head">
      <div><h1>历史记录</h1><p>她做过的所有操作都在这里，可以按日期和类型查看。一共 ${days.reduce((s, x) => s + x.n, 0)} 条，最早从 ${esc(days.length ? days[days.length - 1].day : today)} 开始。</p></div>
    </header>
    <section class="card">
      <div class="hist-bar">
        <div class="hist-date">
          <button type="button" class="btn btn-sm" data-act="h-shift" data-n="-1" ${h.day ? "" : "disabled"} aria-label="前一天">‹ 前一天</button>
          <input type="date" id="h-date" class="input" value="${esc(h.day)}" max="${today}" aria-label="选择日期">
          <button type="button" class="btn btn-sm" data-act="h-shift" data-n="1" ${h.day && h.day < today ? "" : "disabled"} aria-label="后一天">后一天 ›</button>
          <button type="button" class="btn btn-sm${h.day === today ? " btn-primary" : ""}" data-act="h-day" data-day="${today}">今天</button>
          <button type="button" class="btn btn-sm${!h.day ? " btn-primary" : ""}" data-act="h-day" data-day="">全部日期</button>
        </div>
        <label class="hist-kind"><span>类型</span>
          <select id="h-group" class="input">${HIST_GROUPS.map(([k, v]) => `<option value="${k}" ${h.group === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      </div>
      ${chips ? `<div class="chips" aria-label="最近有记录的日期">${chips}</div>` : ""}
      <p class="hint" style="margin:6px 0 0">${h.day ? esc(h.day) : "全部日期"} · ${HIST_GROUPS.find((g) => g[0] === h.group)[1]} · 共 ${d.total} 条</p>
      <ul class="feed">${list}</ul>
      <div class="pager-row">${pager(d.page, d.pages, "h-page")}</div>
    </section>`;
  $("#h-date").addEventListener("change", async (e) => { h.day = e.target.value || ""; h.page = 1; await loadHistory(); renderHistory(); });
  $("#h-group").addEventListener("change", async (e) => { h.group = e.target.value; h.page = 1; await loadHistory(); renderHistory(); });
}

/* 她现在的心情：表情、名字、强度、原因、她心里的一句话 */
function moodCard(m) {
  if (!m) return "";
  const hist = (m.history || []).slice(0, 4).map((h) => `<span>${esc(h.t)} ${esc(MOOD_LABEL[h.mood] || h.mood)}</span>`).join("");
  return `<div class="mood-card" style="--mc:${esc(m.color)}" title="愉快度 ${m.valence} · 精力 ${m.energy}">
    <div class="mood-top"><span class="mood-emoji" aria-hidden="true">${m.emoji}</span>
      <div><div class="mood-k">她现在的心情</div><div class="mood-name">${esc(m.label)}</div></div></div>
    <div class="mood-bar" aria-label="心情强度 ${m.intensity}%"><i style="width:${m.intensity}%"></i></div>
    <div class="mood-line">“${esc(m.line || "")}”</div>
    <div class="mood-why">${esc(m.reason || "")}</div>
    ${hist ? `<div class="mood-hist">${hist}</div>` : ""}
  </div>`;
}
const MOOD_LABEL = { happy: "开心", excited: "兴奋", clingy: "想撒娇", grumpy: "小脾气", sulky: "委屈", sleepy: "犯困", bored: "无聊", calm: "平静" };

/* ---------- 消息中心 ---------- */
const MSG_STATUS = {
  replied: ["ok", "已自动回复"], manual: ["ok", "已手动处理"], pending: ["warn", "待你处理"],
  skipped_sensitive: ["neutral", "跳过 · 敏感"], skipped_limit: ["neutral", "跳过 · 额度用完"], skipped_gone: ["neutral", "跳过 · 视频已删除"], skipped_old: ["neutral", "跳过 · 之前收到的"], uncertain: ["warn", "可能已发出"],
  failed: ["bad", "发送失败"], blocked: ["neutral", "不回复此人"], skipped_scope: ["neutral", "跳过 · 没关注她"],
};

/* 私信对象的头像：读到了抖音头像就显示，读不到（或图片加载失败）显示名字首字 */
function userAvatar(m, size = 44) {
  const letter = esc((m.user_name || "?").slice(0, 1));
  const src = m.user && m.user.avatar;
  if (!src) return `<span class="letter" aria-hidden="true">${letter}</span>`;
  return `<span class="u-avatar" style="width:${size}px;height:${size}px" aria-hidden="true">
    <img src="${esc(src)}" alt="" referrerpolicy="no-referrer" onerror="this.parentNode.classList.add('broken');this.remove()"><b>${letter}</b></span>`;
}

function displayName(m) {
  return (m.user && m.user.nickname) || m.user_name || "";
}

function followText(u) {
  if (!u || u.follows_me === undefined || u.follows_me === null || u.follows_me < 0) return "还不确定有没有关注她";
  return u.follows_me ? "关注了她" : "没有关注她";
}

async function loadMessages() {
  const d = await call("get_messages", S.msgFilter);
  S.msgs = d.items;
  S.counts = d.counts;
  if (S.selMsg && !S.msgs.find((m) => m.id === S.selMsg)) S.selMsg = null;
  if (!S.selMsg && S.msgs.length) S.selMsg = S.msgs[0].id;
}

const KIND_TEXT = { at: "评论里 @ 了她", reply: "回复了她的评论", dm: "私信" };

function renderInbox() {
  const c = S.counts || {};
  const tabs = [["all", "全部", c.all_count], ["at", "评论区", c.at_count], ["dm", "私信", c.dm_count],
    ["pending", "待你处理", c.pending_count], ["skipped", "已跳过", c.skipped_count]]
    .map(([k, l, n]) => `<button type="button" role="tab" aria-selected="${S.msgFilter === k}" data-act="msg-filter" data-f="${k}">${l} · ${n || 0}</button>`).join("");
  const list = S.msgs.length ? S.msgs.map((m) => {
    const [cls, label] = MSG_STATUS[m.status] || ["neutral", m.status];
    return `<button type="button" class="msg-item" data-act="msg-sel" data-id="${m.id}" ${S.selMsg === m.id ? 'aria-current="true"' : ""}>
      ${userAvatar(m)}
      <span style="display:flex;flex-direction:column;gap:4px;min-width:0;flex:1">
        <span class="who"><b>@${esc(displayName(m))} · ${KIND_TEXT[m.kind] || "私信"}</b><span>${esc(hhmm(m.created_at))}</span></span>
        ${m.user && m.user.douyin_id ? `<span class="txt">抖音号：${esc(m.user.douyin_id)}</span>` : ""}
        <span class="txt">“${esc((m.content || "").slice(0, 60))}”</span>
        <span class="pill ${cls}" style="align-self:flex-start">${label}</span>
      </span></button>`;
  }).join("") : `<p class="empty">还没有消息</p>`;

  const m = S.msgs.find((x) => x.id === S.selMsg);
  let detail = `<p class="empty">选一条消息看看</p>`;
  if (m) {
    const [cls, label] = MSG_STATUS[m.status] || ["neutral", m.status];
    detail = `
      <div class="actions" style="justify-content:space-between"><h2>${HEART}@${esc(displayName(m))} ${{ at: "在评论里 @ 了她", reply: "回复了她的评论" }[m.kind] || "发来私信"}</h2><span class="pill ${cls}">${label}</span></div>
      ${m.kind === "dm" ? `
      <div class="u-card">
        ${userAvatar(m, 56)}
        <div class="u-info">
          <b>${esc(displayName(m))}</b>
          <span>抖音号：${esc((m.user && m.user.douyin_id) || "还没读到")}</span>
          <span>私信列表里的名字：${esc(m.user_name)} · ${followText(m.user)}</span>
        </div>
        ${m.user && m.user.url ? `<button type="button" class="btn btn-sm" data-act="open-video" data-url="${esc(m.user.url)}">打开主页</button>` : ""}
      </div>` : ""}
      ${m.video_title ? `<div style="padding:12px 14px;background:var(--bubble-them);border-radius:16px">视频：${esc(m.video_title)}</div>` : ""}
      <div class="chat">
        <div class="them">${userAvatar(m, 40)}<div class="bubble">${esc(m.content)}</div></div>
        ${m.reply ? `<div class="me">${avatar(40)}<div class="bubble">${esc(m.reply)}</div></div>` : ""}
      </div>
      ${m.reason ? `<p style="margin:0;color:var(--warn-text)">${m.status === "replied" ? "" : "没有自动发出："}${esc(m.reason)}</p>` : ""}
      <dl class="meta">
        <div><dt>类型</dt><dd>${{ at: "评论 @", reply: "回复她的评论" }[m.kind] || "私信"}</dd></div>
        <div><dt>收到时间</dt><dd>${esc(m.created_at)}</dd></div>
        <div><dt>花费</dt><dd>¥${Number(m.cost || 0).toFixed(3)} · ${m.tokens || 0} token</dd></div>
      </dl>
      <div class="field"><label for="manual-text">我来手动回复（会用她的账号发出去）</label>
        <textarea id="manual-text" class="input" rows="2" placeholder="输入你想回复的内容…">${esc(m.status === "pending" ? (m.reply || "") : "")}</textarea></div>
      <div class="actions">
        <button type="button" class="btn btn-primary" data-act="msg-do" data-do="manual">发送手动回复</button>
        ${m.video_url ? `<button type="button" class="btn" data-act="msg-do" data-do="open">在抖音中打开</button>` : ""}
        <button type="button" class="btn" data-act="msg-do" data-do="done">标记为已处理</button>
        <button type="button" class="btn btn-danger" data-act="msg-do" data-do="block">以后不回复此人</button>
      </div>`;
  }
  $("#main").innerHTML = `
    <header class="page-head"><div><h1>消息中心</h1><p>谁 @ 了她、谁给她发了私信，她是怎么回的，都在这里</p></div>
      <button type="button" class="btn" data-act="check-inbox">立即检查新消息</button></header>
    <div class="tabs" role="tablist" aria-label="筛选">${tabs}</div>
    <div class="row">
      <section class="card msg-list grow-2" aria-label="消息列表">${list}</section>
      <section class="card grow-3">${detail}</section>
    </div>`;
}

/* ---------- 人设 ---------- */
const TRAITS = [
  ["energy", "性子", "安安静静", "元气满满"], ["sharp", "说话风格", "软萌温柔", "小嘴犀利"],
  ["talkative", "话多不多", "一两句", "一大段"], ["cute", "撒娇程度", "正经", "超可爱，带颜文字 (｡･ω･｡)ﾉ♡"],
  ["temper", "小脾气", "温顺好脾气", "动不动闹小脾气"], ["volatility", "情绪起伏", "情绪稳定", "喜怒都写在脸上"],
];
const INTEREST_OPTIONS = ["户外旅行", "甜品美食", "穿搭", "萌宠", "数码", "手账文具", "美妆", "音乐", "游戏", "摄影剪辑", "健身", "读书"];

function renderPersona() {
  const p = S.persona;
  const interests = Array.from(new Set([...INTEREST_OPTIONS, ...(p.interests || [])]));
  const chips = interests.map((it) => `<button type="button" class="chip" aria-pressed="${(p.interests || []).includes(it)}" data-act="interest" data-v="${esc(it)}">${esc(it)}</button>`).join("");
  const traits = TRAITS.map(([k, label, lo, hi]) => `
    <div class="field"><label for="t-${k}">${label}</label>
      <input id="t-${k}" type="range" min="0" max="100" value="${Number((p.traits || {})[k] ?? 50)}">
      <div class="range-ends"><span>${lo}</span><span>${hi}</span></div>
      ${k === "cute" ? `<p class="hint" style="margin:2px 0 0">调到 70 以上，她每条回复都会带可爱俏皮的颜文字；50–70 偶尔带一个。</p>` : ""}
      ${k === "temper" ? `<p class="hint" style="margin:2px 0 0">遇到不开心的事，脾气大的会闹点可爱的小脾气，脾气好的会有点委屈。公开评论里只是轻轻嘟囔，不会对任何人发火。</p>` : ""}
      ${k === "volatility" ? `<p class="hint" style="margin:2px 0 0">越高，一件事对她心情的影响越大。心情会写进她说话的语气里，也会让桌宠做不同的动作。</p>` : ""}</div>`).join("");
  const tryLog = S.tryLog.map((x) => x.role === "them"
    ? `<div class="them"><span class="letter" aria-hidden="true">你</span><div class="bubble">${esc(x.text)}</div></div>`
    : `<div class="me">${avatar(36)}<div class="bubble">${esc(x.text)}</div></div>`).join("")
    || `<p class="empty">发一句话试试，比如“你是真人吗？”</p>`;

  $("#main").innerHTML = `
    <header class="page-head">
      <div><h1>人设</h1><p>捏一个专属于你的虚拟女孩：她是什么性格、爱看什么、怎么说话，全部由你来调。保存后马上生效～</p></div>
      <button type="button" class="btn btn-primary" data-act="save-persona">保存</button>
    </header>
    <div class="row">
      <div class="col grow-3">
        <section class="card"><h2>${HEART}头像和个性签名</h2>
          <div class="avatar-box">
            ${avatar(112)}
            <div class="col" style="gap:8px">
              <div class="actions">
                <label class="btn btn-sm" for="avatar-file" style="display:inline-flex;align-items:center">从电脑上传照片</label>
                <input id="avatar-file" type="file" accept="image/png,image/jpeg,image/webp" class="sr-only">
                <button type="button" class="btn btn-sm" data-act="reset-avatar">用默认 Q 版形象</button>
              </div>
              <small style="color:var(--muted)">支持 JPG / PNG / WebP，会自动裁成正方形圆头像</small>
            </div>
          </div>
          <div class="field"><label for="p-signature">个性签名</label>
            <input id="p-signature" class="input" maxlength="80" value="${esc(p.signature)}">
            <small>显示在侧边栏。点“同步到抖音简介”会自动加上 AI 标识：${esc(p.douyin_bio_preview || "")}</small></div>
          <div class="actions"><button type="button" class="btn btn-sm" data-act="sync-sig">同步到抖音简介</button></div>
        </section>
        <section class="card"><h2>${HEART}基本信息</h2>
          <div class="field"><label for="p-name">昵称</label><input id="p-name" class="input" maxlength="20" value="${esc(p.name)}"></div>
          <div class="field"><label for="p-bio">一句话简介</label><input id="p-bio" class="input" value="${esc(p.bio)}"></div>
        </section>
        <section class="card"><h2>${HEART}性格</h2>
          <p class="hint" style="margin:0">拖动滑块，自己调她的性格；兴趣、口头禅和悄悄话也都可以随意改。</p>
          <div class="grid">${traits}</div>
          <div class="field"><span class="label">她感兴趣的内容（刷到这些更愿意停下来看）</span><div class="chips">${chips}</div>
            <div class="actions"><input id="new-interest" class="input" style="max-width:220px" placeholder="自定义兴趣"><button type="button" class="btn btn-sm" data-act="add-interest">+ 添加</button></div></div>
          <div class="grid">
            <div class="field"><label for="p-catch">口头禅（偶尔用）</label><input id="p-catch" class="input" value="${esc(p.catchphrases)}"></div>
            <div class="field"><label for="p-avoid">不喜欢 / 不评论的内容</label><input id="p-avoid" class="input" value="${esc(p.avoid)}"></div>
          </div>
          <div class="field"><label for="p-extra">悄悄话（会直接告诉模型）</label><textarea id="p-extra" class="input" rows="3">${esc(p.extra)}</textarea></div>
        </section>
        <section class="card" style="flex-direction:row;align-items:flex-start;gap:14px">
          <input type="checkbox" checked disabled id="ai-flag" style="margin-top:2px">
          <div><label for="ai-flag" style="font-weight:700">公开 AI 身份（固定开启）</label>
          <p style="margin:6px 0 0;color:var(--muted);line-height:1.7">有人问“你是不是真人”，她会如实回答自己是 AI；同步到抖音的简介会带上 AI 标识。这是抖音规则和国家 AI 内容标识的要求，关不掉哦。</p></div>
        </section>
      </div>
      <section class="card grow-2"><h2>${HEART}和她聊两句试试</h2>
        <p class="hint">输入一条别人的评论或私信，看看她会怎么回（会消耗少量 token，先保存再试效果更准）</p>
        <div class="try-box chat" id="try-box">${tryLog}</div>
        <div class="actions" style="flex-wrap:nowrap">
          <label for="try-input" class="sr-only">测试消息</label>
          <input id="try-input" class="input" placeholder="输入一条测试消息…">
          <button type="button" class="btn btn-primary" data-act="try-send">发送</button>
        </div>
      </section>
    </div>`;
  $("#avatar-file").addEventListener("change", onAvatarFile);
  $("#try-input").addEventListener("keydown", (e) => { if (e.key === "Enter") ACT["try-send"](); });
  const box = $("#try-box"); box.scrollTop = box.scrollHeight;
}

function collectPersona() {
  const p = S.persona;
  return {
    name: $("#p-name").value.trim(), bio: $("#p-bio").value.trim(),
    signature: $("#p-signature").value.trim(), catchphrases: $("#p-catch").value.trim(),
    avoid: $("#p-avoid").value.trim(), extra: $("#p-extra").value.trim(),
    interests: p.interests || [],
    traits: Object.fromEntries(TRAITS.map(([k]) => [k, Number($("#t-" + k).value)])),
  };
}

function onAvatarFile(e) {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  if (f.size > 15 * 1024 * 1024) return toast("图片太大了，请选 15MB 以内的照片", true);
  const reader = new FileReader();
  reader.onload = async () => {
    const url = await call("upload_avatar", reader.result);
    S.st.persona.avatar_url = url;
    S.persona = { ...S.persona, ...collectPersona() };
    renderSide(); renderPersona();
    toast("头像换好啦");
  };
  reader.readAsDataURL(f);
}

/* ---------- 模型 ---------- */
const TASKS = [
  ["vision", "看视频、判断要不要互动", "最好用能识图的模型 · 调用最多，建议用便宜的", true],
  ["write", "写评论、回复 @ 和私信", "决定她“像不像她自己”", false],
  ["safety", "敏感内容检查", "每条要发出的内容都会先过一遍，用便宜的就行", false],
  ["fallback", "备用模型", "主模型连不上时自动切换", false],
];

function modelOptions(provs, splitVision) {
  const keyed = provs.filter((x) => x.has_key && x.models.length);
  if (!keyed.length) return '<option value="">（先在上面填好 API Key 和模型名称，并点“保存这家”）</option>';
  const opt = (x, m) => `<option value="${esc(x.id + "|" + m)}">${esc(x.name.split("（")[0])} · ${esc(m)}</option>`;
  if (!splitVision) return '<option value="">（不设置）</option>' + keyed.flatMap((x) => x.models.map((m) => opt(x, m))).join("");
  const see = keyed.flatMap((x) => x.models.filter((m) => x.vision_models.includes(m)).map((m) => opt(x, m))).join("");
  const blind = keyed.flatMap((x) => x.models.filter((m) => !x.vision_models.includes(m)).map((m) => opt(x, m))).join("");
  return '<option value="">（不设置）</option>'
    + (see ? `<optgroup label="能看画面">${see}</optgroup>` : "")
    + (blind ? `<optgroup label="不能看画面（只看文案和评论判断）">${blind}</optgroup>` : "");
}

function visionChecks(models) {
  if (!models.length) return '<small style="color:var(--muted)">先填写模型名称</small>';
  return models.map((m) => `<label class="chip" style="cursor:pointer"><input type="checkbox" data-vm="${esc(m)}" ${S.visDraft.has(m) ? "checked" : ""} style="width:18px;height:18px">${esc(m)}</label>`).join("");
}

function currentModels() {
  return $("#m-models").value.replace(/，/g, ",").split(",").map((s) => s.trim()).filter(Boolean);
}

function renderModels() {
  const provs = S.providers;
  const p = provs.find((x) => x.id === S.selProv) || provs[0];
  if (!S.visDraft || S.visDraftFor !== p.id) { S.visDraft = new Set(p.vision_models); S.visDraftFor = p.id; }
  const list = provs.map((x) => `
    <label class="prov${x.id === p.id ? " sel" : ""}"><input type="radio" name="prov" value="${x.id}" ${x.id === p.id ? "checked" : ""} data-act="prov-sel">
      <span style="flex:1">${esc(x.name)}</span>
      ${x.has_key ? '<span class="pill ok">已配置</span>' : '<span style="font-size:12px;color:var(--muted)">未配置</span>'}</label>`).join("");
  const anySee = provs.some((x) => x.has_key && x.vision_models.length);
  const tasks = TASKS.map(([k, label, hint, vision]) => `
    <div class="task-row"><div class="what"><label for="task-${k}" style="font-weight:700">${label}</label><small>${hint}</small>
      ${vision && !anySee ? '<small style="color:var(--warn-text)">还没有勾选“能识图”的模型。在上面的模型名称下面勾选能看图的模型并保存；不勾也能用，她会只看文案和评论来判断。</small>' : ""}</div>
      <select id="task-${k}" class="input">${modelOptions(provs, vision)}</select></div>`).join("");

  $("#main").innerHTML = `
    <header class="page-head"><div><h1>模型与 API Key</h1><p>这是她的“小脑袋”～填入任意一家的 API Key 就能用，也可以配好几家，按任务分工</p></div></header>
    <div class="row">
      <fieldset class="card grow-2" style="padding:10px;gap:4px;margin:0"><legend class="sr-only">模型服务商</legend>${list}</fieldset>
      <div class="col grow-3">
        <section class="card">
          <div class="actions" style="justify-content:space-between"><h2>${HEART}${esc(p.name)}</h2>
            ${p.key_url ? `<a href="${esc(p.key_url)}" target="_blank" rel="noopener">去哪里获取 API Key？</a>` : ""}</div>
          <div class="field"><label for="m-key">API Key</label>
            <div class="actions" style="flex-wrap:nowrap"><input id="m-key" type="password" class="input" autocomplete="off" placeholder="${p.has_key ? "已保存：" + esc(p.key_mask) + "（不改就留空）" : "粘贴你的 API Key"}">
            <button type="button" class="btn btn-sm" data-act="toggle-key">显示</button></div>
            <small>只保存在这台电脑上，用 Windows 系统加密存储，不会上传到任何地方</small></div>
          <div class="grid">
            <div class="field"><label for="m-url">接口地址</label><input id="m-url" class="input" value="${esc(p.base_url)}"></div>
            <div class="field"><label for="m-proxy">网络代理（可选）</label><input id="m-proxy" class="input" value="${esc(p.proxy)}" placeholder="例如 http://127.0.0.1:7890"></div>
          </div>
          <div class="field"><label for="m-models">模型名称（多个用逗号隔开）</label><input id="m-models" class="input" value="${esc(p.models.join(", "))}" placeholder="按服务商官网填写模型名"></div>
          <div class="grid">
            <div class="field"><label for="m-pin">输入单价（元 / 百万 token）</label><input id="m-pin" type="number" step="0.01" min="0" class="input" value="${p.price_in || ""}" placeholder="按服务商官网填写"></div>
            <div class="field"><label for="m-pout">输出单价（元 / 百万 token）</label><input id="m-pout" type="number" step="0.01" min="0" class="input" value="${p.price_out || ""}" placeholder="按服务商官网填写"></div>
          </div>
          <div class="field"><span class="label">哪些模型能识图（看视频画面需要，按服务商官网说明勾选）</span>
            <div class="chips" id="m-vision-list">${visionChecks(p.models)}</div></div>
          <details class="field"><summary style="cursor:pointer;color:var(--sub)">高级：额外请求参数（JSON，一般不用改）</summary>
            <textarea id="m-extra" class="input" rows="2" style="margin-top:8px" placeholder='例如 {"thinking": {"type": "disabled"}}'>${esc(p.request_extra)}</textarea>
            <small>会原样加进每次请求里。DeepSeek 默认填了关闭思考模式，否则短回复可能是空的。</small></details>
          <div class="actions">
            <button type="button" class="btn btn-primary" data-act="save-prov">保存这家</button>
            <button type="button" class="btn" data-act="test-prov">测试连接</button>
            ${p.has_key ? `<button type="button" class="btn btn-danger" data-act="clear-key">删除 Key</button>` : ""}
          </div>
          <div id="test-result" style="display:flex;flex-direction:column;gap:6px"></div>
          <small style="color:var(--muted)">填了单价，仪表盘才能算出每天花了多少钱，到上限时她会自动停下。</small>
        </section>
        <section class="card"><h2>${HEART}任务分工</h2>
          <p class="hint">便宜的模型做量大的活，好一点的模型写要发出去的话，能省不少钱。只有已配置 Key 的服务商会出现在这里。</p>
          ${tasks}
          <div class="actions"><button type="button" class="btn btn-primary" data-act="save-tasks">保存分工</button></div>
        </section>
      </div>
    </div>`;
  for (const [k] of TASKS) {
    const t = S.tasks[k] || {};
    const sel = $("#task-" + k);
    if (!t.provider || !t.model) continue;
    const v = t.provider + "|" + t.model;
    if (![...sel.options].some((o) => o.value === v)) {
      // 之前选的模型现在不可用了（删了 Key 或改了模型名），明确告诉用户，而不是显示一片空白
      sel.insertAdjacentHTML("afterbegin", `<option value="${esc(v)}">（已失效：${esc(t.model)}，请重新选择）</option>`);
    }
    sel.value = v;
  }
  $("#m-models").addEventListener("input", () => { $("#m-vision-list").innerHTML = visionChecks(currentModels()); });
  $("#m-vision-list").addEventListener("change", (e) => {
    const m = e.target.dataset && e.target.dataset.vm;
    if (!m) return;
    if (e.target.checked) S.visDraft.add(m); else S.visDraft.delete(m);
  });
}

function collectProvider() {
  const models = currentModels();
  return {
    id: S.selProv, api_key: $("#m-key").value.trim(), base_url: $("#m-url").value.trim(), proxy: $("#m-proxy").value.trim(),
    models, vision_models: models.filter((m) => S.visDraft.has(m)),
    price_in: $("#m-pin").value, price_out: $("#m-pout").value, request_extra: $("#m-extra").value.trim(),
  };
}

/* ---------- 规则 ---------- */
const LIMITS = [["videos", "看视频（个）"], ["likes", "点赞（次）"], ["collects", "收藏视频（个）"], ["comments", "发评论（条）"],
  ["comment_likes", "赞别人的评论（次）"], ["comment_replies", "回复别人的评论（条）"], ["cost", "模型花费（元）"]];
// “回复 @”和“回复私信”的每日上限放在“回复规则”里，和回复速度一起设置

function renderRules() {
  const r = S.rules;
  const limits = LIMITS.map(([k, l]) => `<div class="field"><label for="l-${k}">${l}</label><input id="l-${k}" type="number" min="0" step="${k === "cost" ? "0.5" : "1"}" class="input" value="${r.limits[k]}"></div>`).join("");
  const topics = Object.keys(r.topic_keywords || {}).map((t) => `<button type="button" class="chip" aria-pressed="${r.topics.includes(t)}" data-act="topic" data-v="${esc(t)}" title="${esc((r.topic_keywords[t] || []).join("、"))}">${esc(t)}</button>`).join("");
  $("#main").innerHTML = `
    <header class="page-head"><div><h1>规则与预算</h1><p>和她约法三章：每天做多少事、花多少钱、哪些话题不碰。任何一项到上限，她当天就乖乖停下～</p></div>
      <button type="button" class="btn btn-primary" data-act="save-rules">保存</button></header>
    <div class="row">
      <div class="col grow-1">
        <section class="card"><h2>${HEART}每日上限</h2><div class="grid-sm">${limits}</div></section>
        <section class="card"><h2>${HEART}节奏</h2><p class="hint">慢慢来、带点随机，更像真人的习惯，也更不容易触发平台风控</p>
          <div class="grid">
            <div class="field"><span class="label" id="r-start-lab">活跃开始</span>${timeField("r-start", r.active_start)}</div>
            <div class="field"><span class="label" id="r-end-lab">活跃结束</span>${timeField("r-end", r.active_end)}</div>
            <div class="field"><label for="r-dmin">每个视频最少停留（秒）</label><input id="r-dmin" type="number" min="3" class="input" value="${r.dwell_min}"></div>
            <div class="field"><label for="r-dmax">每个视频最多停留（秒）</label><input id="r-dmax" type="number" min="5" class="input" value="${r.dwell_max}"></div>
            <div class="field"><label for="r-every">连续刷多少个后休息</label><input id="r-every" type="number" min="1" class="input" value="${r.rest_every}"></div>
            <div class="field"><label for="r-rmin">休息最少（分钟）</label><input id="r-rmin" type="number" min="1" class="input" value="${r.rest_min}"></div>
            <div class="field"><label for="r-rmax">休息最多（分钟）</label><input id="r-rmax" type="number" min="1" class="input" value="${r.rest_max}"></div>
            <div class="field"><label for="r-thr">感兴趣到几分才细看（0–10）</label><input id="r-thr" type="number" min="0" max="10" class="input" value="${r.interest_threshold}"></div>
            <div class="field"><label for="r-frames">每个视频截几帧</label><input id="r-frames" type="number" min="1" max="6" class="input" value="${r.frames_per_video}"></div>
          </div></section>
        <section class="card"><h2>${HEART}回复规则</h2>
          <h3 class="sub-h">刷视频时</h3>
          <div class="toggle-row"><label for="r-liketd">点赞倾向<span class="hint" style="display:block;margin:2px 0 0">和她最近看过的视频比，只赞最出挑的那部分；推荐页全是喜欢的内容时也不会见一个赞一个</span></label>
            <select id="r-liketd" class="input" style="max-width:260px">
              ${[["picky", "挑剔：只赞最喜欢的约 15%"], ["normal", "适中：约 25%（推荐）"], ["generous", "大方：约 40%"]].map(([k, v]) => `<option value="${k}" ${(r.like_tendency || "normal") === k ? "selected" : ""}>${v}</option>`).join("")}
            </select></div>
          <label class="toggle-row"><span>刷到特别喜欢、想以后再看的视频时收藏（她自己决定收不收）</span><input id="r-col" type="checkbox" ${r.collect_enabled !== false ? "checked" : ""}></label>
          <label class="toggle-row"><span>刷视频时给喜欢的评论点赞、回复别人的评论</span><input id="r-cm" type="checkbox" ${r.engage_comments !== false ? "checked" : ""}></label>
          <h3 class="sub-h">评论区里 @ 她（让她看视频、分析内容）</h3>
          <label class="toggle-row"><span>有人 @ 她时自动回复</span><input id="r-at" type="checkbox" ${r.auto_reply_at ? "checked" : ""}></label>
          <div class="toggle-row"><label for="r-atint">多久去看一次（分钟）</label><input id="r-atint" type="number" min="1" class="input" style="max-width:120px" value="${r.at_interval ?? 10}"></div>
          <div class="toggle-row"><label for="r-atmax">每天最多回复（条）</label><input id="r-atmax" type="number" min="0" class="input" style="max-width:120px" value="${r.limits.replies_at}"></div>
          <p class="hint">回复 @ 要先看那个视频、截画面给模型看，比较费 token；间隔长一点、上限低一点更省钱。</p>
          <h3 class="sub-h">私信</h3>
          <label class="toggle-row"><span>收到私信时自动回复</span><input id="r-dm" type="checkbox" ${r.auto_reply_dm ? "checked" : ""}></label>
          <div class="toggle-row"><label for="r-dmmode">回复速度</label>
            <select id="r-dmmode" class="input" style="max-width:260px">
              <option value="instant" ${(r.dm_reply_mode || "instant") === "instant" ? "selected" : ""}>秒回（大约 1 分钟内）</option>
              <option value="5min" ${r.dm_reply_mode === "5min" ? "selected" : ""}>每 5 分钟回一次</option>
            </select></div>
          <div class="toggle-row"><label for="r-dmmax">每天最多回复（条）</label><input id="r-dmmax" type="number" min="0" class="input" style="max-width:120px" value="${r.limits.replies_dm}"></div>
          <div class="toggle-row"><label for="r-scope">私信回复谁</label>
            <select id="r-scope" class="input" style="max-width:260px">
              <option value="all" ${r.dm_scope !== "followers" ? "selected" : ""}>回复所有人</option>
              <option value="followers" ${r.dm_scope === "followers" ? "selected" : ""}>只回复关注了她的人</option>
            </select></div>
          <p class="hint">“只回复关注了她的人”：她会去对方主页看关注按钮（显示“回关”或“互相关注”就说明对方关注了她）；没关注的不回，确认不了的放进“待你处理”。</p>
          <div class="toggle-row"><label for="r-watch">别人分享的视频最多完整看（分钟）</label><input id="r-watch" type="number" min="0.5" step="0.5" class="input" style="max-width:120px" value="${r.share_watch_minutes ?? 3}"></div>
          <div class="toggle-row"><label for="r-sframes">看分享视频时截几帧给模型看</label><input id="r-sframes" type="number" min="0" max="12" class="input" style="max-width:120px" value="${r.share_frames ?? 6}"></div>
          <p class="hint">私信里有人分享视频让她分析时，她会先把视频从头看到尾再回复；更长的视频，超过的部分快进着看。截的帧越多越准，也越费 token。</p>
          <h3 class="sub-h">有人回复她的评论（接着聊）</h3>
          <label class="toggle-row"><span>有人回复她的评论时，接着回复</span><input id="r-th" type="checkbox" ${r.auto_reply_thread !== false ? "checked" : ""}></label>
          <div class="toggle-row"><label for="r-thmax">同一条评论下，她最多回复几次</label><input id="r-thmax" type="number" min="1" max="20" class="input" style="max-width:120px" value="${r.thread_max_replies ?? 3}"></div>
          <div class="toggle-row"><label for="r-thday">每天最多接着聊（条）</label><input id="r-thday" type="number" min="0" class="input" style="max-width:120px" value="${r.limits.thread_replies ?? 20}"></div>
          <p class="hint">回到上限时，最后一句她会自然地收个尾（比如“聊得好开心～”），之后这串对话就不再回了。对方说了敏感的话会直接停。</p>
          <div class="toggle-row"><label for="r-per">同一个人每天最多回复</label><input id="r-per" type="number" min="1" class="input" style="max-width:120px" value="${r.per_user_daily}"></div>
        </section>
        ${friendsSection(r)}
      </div>
      <div class="col grow-1">
        <section class="card"><h2>${HEART}敏感话题（碰到就不回）</h2>
          <p class="hint">先用关键词快速过滤，再让模型判断一次意思，两道都过才会发出去。点一下可以开关，鼠标停留能看到关键词。</p>
          <div class="chips">${topics}</div>
          <div class="field"><label for="r-kw">自定义屏蔽词（用逗号分隔）</label><textarea id="r-kw" class="input" rows="3">${esc((r.keywords || []).join(", "))}</textarea></div>
          <fieldset style="margin:0;padding:0;border:0;display:flex;flex-direction:column;gap:8px"><legend class="label" style="font-size:13px;color:var(--sub);margin-bottom:8px">碰到之后</legend>
            <label style="display:flex;gap:10px;align-items:center"><input type="radio" name="hit" value="pending" ${r.on_hit === "pending" ? "checked" : ""}>不回复，放进“待你处理”</label>
            <label style="display:flex;gap:10px;align-items:center"><input type="radio" name="hit" value="log" ${r.on_hit === "log" ? "checked" : ""}>不回复，只记在日志里</label>
          </fieldset>
        </section>
        ${publishSection(r)}
        <section class="card" style="background:var(--tint)">
          <div style="display:flex;gap:16px;align-items:flex-start">${avatar(84)}
          <div class="col" style="gap:10px"><h2>她答应你的（固定开启）</h2>
            <ul style="margin:0;padding-left:18px;line-height:2">
              <li>遇到验证码马上停下，弹窗叫你来帮忙</li><li>连续 3 次操作失败（页面改版、网络问题）自动暂停</li>
              <li>不发链接、不留联系方式、不引导到站外</li><li>被问到是不是真人，如实说自己是 AI</li>
              <li>发作品一定等你确认，并且勾选“内容由AI生成”</li></ul></div></div>
        </section>
      </div>
    </div>`;
}

function collectRules() {
  const r = S.rules;
  const num = (id) => Number($(id).value);
  return {
    limits: {
      ...Object.fromEntries(LIMITS.map(([k]) => [k, Number($("#l-" + k).value) || 0])),
      replies_at: Number($("#r-atmax").value) || 0, replies_dm: Number($("#r-dmmax").value) || 0,
      thread_replies: Number($("#r-thday").value) || 0, at_friends: Number($("#r-atfday").value) || 0,
      publishes: Number($("#r-pubday").value) || 0,
    },
    auto_reply_thread: $("#r-th").checked, thread_max_replies: Math.max(1, num("#r-thmax") || 3),
    at_friends_enabled: $("#r-atf").checked, at_friend_per_day: Math.max(1, num("#r-atfper") || 1),
    publish_enabled: $("#r-pub").checked, publish_start: $("#r-pubs").value || "11:00", publish_end: $("#r-pube").value || "21:00",
    image_gen_provider: $("#r-igp").value, image_gen_model: $("#r-igm").value.trim(), image_gen_size: $("#r-igs").value.trim(),
    image_gen_price: Math.max(0, Number($("#r-igc").value) || 0), image_gen_auto: $("#r-iga").checked,
    active_start: $("#r-start").value || "09:00", active_end: $("#r-end").value || "23:00",
    dwell_min: num("#r-dmin"), dwell_max: Math.max(num("#r-dmax"), num("#r-dmin")),
    rest_every: num("#r-every"), rest_min: num("#r-rmin"), rest_max: Math.max(num("#r-rmax"), num("#r-rmin")),
    at_interval: Math.max(1, num("#r-atint") || 10), dm_reply_mode: $("#r-dmmode").value,
    interest_threshold: num("#r-thr"), frames_per_video: num("#r-frames"),
    auto_reply_at: $("#r-at").checked, auto_reply_dm: $("#r-dm").checked, engage_comments: $("#r-cm").checked, collect_enabled: $("#r-col").checked, like_tendency: $("#r-liketd").value, dm_scope: $("#r-scope").value,
    share_watch_minutes: Math.max(0.5, Number($("#r-watch").value) || 3), share_frames: Math.max(0, Math.min(12, num("#r-sframes"))),
    per_user_daily: num("#r-per"), topics: r.topics,
    keywords: $("#r-kw").value.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean),
    on_hit: (document.querySelector('input[name="hit"]:checked') || {}).value || "pending",
  };
}

/* @ 互关好友：下拉框只显示互关的好友，选中的人她才会 @ */
function friendsSection(r) {
  const fr = (S.friends && S.friends.friends) || [];
  const fetch = (S.friends && S.friends.fetch) || {};
  const chosen = fr.filter((f) => f.enabled);
  const opts = fr.length ? fr.map((f) => `
    <label class="fr-opt"><input type="checkbox" data-uid="${esc(f.uid)}" class="fr-check" ${f.enabled ? "checked" : ""}>
      ${f.avatar ? `<img src="${esc(f.avatar)}" alt="" referrerpolicy="no-referrer">` : `<span class="letter">${esc((f.nickname || "?").slice(0, 1))}</span>`}
      <span>${esc(f.nickname)}${f.douyin_id ? `<small>抖音号 ${esc(f.douyin_id)}</small>` : ""}</span></label>`).join("")
    : `<p class="empty" style="margin:8px">${S.frLoading ? "正在从你的抖音主页读关注列表…" : "还没有互关好友名单，点“刷新互关好友名单”读一下"}</p>`;
  const tags = chosen.map((f) => `
    <div class="fr-tag"><b>${esc(f.nickname)}</b><input class="input fr-tags" data-uid="${esc(f.uid)}" placeholder="TA 喜欢看什么（选填，比如：猫、美食）" value="${esc(f.tags || "")}"></div>`).join("");
  return `
        <section class="card"><h2>${HEART}@ 互关好友看视频</h2>
          <p class="hint">刷到好朋友会喜欢的视频时，她会在评论里 @ 好友、直接点评这个视频。只 @ 下面选中的互关好友。</p>
          <label class="toggle-row"><span>允许她 @ 好友</span><input id="r-atf" type="checkbox" ${r.at_friends_enabled !== false ? "checked" : ""}></label>
          <div class="toggle-row"><label for="r-atfday">每天最多 @ 几次</label><input id="r-atfday" type="number" min="0" class="input" style="max-width:120px" value="${r.limits.at_friends ?? 5}"></div>
          <div class="toggle-row"><label for="r-atfper">同一个好友每天最多被 @ 几次</label><input id="r-atfper" type="number" min="1" class="input" style="max-width:120px" value="${r.at_friend_per_day ?? 1}"></div>
          <div class="fr-select">
            <button type="button" class="btn fr-toggle" data-act="fr-open" aria-expanded="${S.frOpen ? "true" : "false"}">选择可以 @ 的好友（已选 ${chosen.length} 人）▾</button>
            ${S.frOpen ? `<div class="fr-panel" role="listbox" aria-label="互关好友">${opts}</div>` : ""}
          </div>
          ${tags ? `<div class="fr-tags-box">${tags}</div>` : ""}
          <div class="actions">
            <button type="button" class="btn btn-sm" data-act="fr-fetch" ${S.frLoading ? "disabled" : ""}>${S.frLoading ? "正在读取…" : "刷新互关好友名单"}</button>
            <input id="fr-new" class="input" style="max-width:200px" placeholder="手动添加：好友的抖音昵称">
            <button type="button" class="btn btn-sm" data-act="fr-add">+ 添加</button>
          </div>
          <p class="hint">${fetch.at ? `上次读取：${esc(fetch.at)}，互关 ${fetch.count || 0} 人${fetch.error ? `（${esc(fetch.error)}）` : ""}。` : "名单从你抖音主页的“关注”列表里读，只算互相关注的人。"}手动添加的好友请确认是互关的，否则 @ 时可能找不到这个人。</p>
        </section>`;
}

/* 发作品：每天按心情想一想要不要发，写好草稿等你确认；配图可以是文字卡片、AI 生图、你上传的素材 */
function publishSection(r) {
  const provs = (r.image_options || []).map((p) => `<option value="${esc(p.id)}" ${r.image_gen_provider === p.id ? "selected" : ""}>${esc(p.name)}${p.has_key ? "" : "（未填 Key）"}</option>`).join("");
  return `
        <section class="card"><h2>${HEART}发作品</h2>
          <p class="hint">每天在下面的时间段里，她会按心情决定想不想发作品；想发就写好文案和文字卡片，在软件里提示“您的小可爱想发作品”。你确认后才会发布，发布时一定勾选“内容由AI生成”。</p>
          <label class="toggle-row"><span>允许她自己想发作品</span><input id="r-pub" type="checkbox" ${r.publish_enabled !== false ? "checked" : ""}></label>
          <div class="toggle-row"><label for="r-pubday">每天最多发几个作品</label><input id="r-pubday" type="number" min="0" max="10" class="input" style="max-width:120px" value="${r.limits.publishes ?? 1}"></div>
          <div class="grid">
            <div class="field"><span class="label" id="r-pubs-lab">每天想一想的时间段：从</span>${timeField("r-pubs", r.publish_start || "11:00")}</div>
            <div class="field"><span class="label" id="r-pube-lab">到</span>${timeField("r-pube", r.publish_end || "21:00")}</div>
          </div>
          <h3 class="sub-h">AI 生图（可选，用你自己的服务商）</h3>
          <div class="toggle-row"><label for="r-igp">用哪家服务商</label><select id="r-igp" class="input" style="max-width:260px"><option value="">不用 AI 生图</option>${provs}</select></div>
          <div class="toggle-row"><label for="r-igm">生图模型名称</label><input id="r-igm" class="input" style="max-width:220px" placeholder="例如 doubao-seedream-4-0" value="${esc(r.image_gen_model || "")}"></div>
          <div class="toggle-row"><label for="r-igs">图片尺寸</label><input id="r-igs" class="input" style="max-width:160px" value="${esc(r.image_gen_size || "1024x1024")}"></div>
          <div class="toggle-row"><label for="r-igc">单价（元 / 张）</label><input id="r-igc" type="number" min="0" step="0.01" class="input" style="max-width:120px" value="${r.image_gen_price ?? 0}"></div>
          <label class="toggle-row"><span>她写草稿时自动生成一张配图</span><input id="r-iga" type="checkbox" ${r.image_gen_auto !== false ? "checked" : ""}></label>
          <p class="hint">需要服务商提供 OpenAI 兼容的生图接口（/images/generations），模型名按服务商官网填。不配置也没关系：草稿会带她写的文字卡片，你也可以从电脑上传图片或视频。</p>
        </section>`;
}

/* ---------- 作品 ---------- */
const DRAFT_STATUS = {
  wish: ["warn", "她想发 · 等你确认"], publishing: ["brand", "发布中…"], published: ["ok", "已发布"],
  failed: ["bad", "没发出去"], discarded: ["neutral", "不发了"],
};

async function loadDrafts() {
  S.drafts = await call("list_drafts");
  const list = S.drafts.drafts;
  if (S.selDraft && !list.find((d) => d.id === S.selDraft)) S.selDraft = null;
  if (!S.selDraft && list.length) S.selDraft = (list.find((d) => d.status === "wish") || list[0]).id;
  S.draft = S.selDraft ? await call("get_draft", S.selDraft) : null;
}

function renderDrafts() {
  const D = S.drafts || { drafts: [] };
  const list = D.drafts.length ? D.drafts.map((d) => {
    const [cls, label] = DRAFT_STATUS[d.status] || ["neutral", d.status];
    return `<button type="button" class="msg-item draft-item" data-act="draft-sel" data-id="${d.id}" ${S.selDraft === d.id ? 'aria-current="true"' : ""}>
      <span class="d-cover">${d.cover ? `<img src="${d.cover}" alt="">` : "🖼"}</span>
      <span style="display:flex;flex-direction:column;gap:4px;min-width:0;flex:1">
        <span class="who"><b>${esc(d.title || "（还没有标题）")}</b><span>${esc((d.created_at || "").slice(5, 16))}</span></span>
        <span class="txt">${esc(d.reason || "")}</span>
        <span class="pill ${cls}" style="align-self:flex-start">${label}${d.source === "manual" ? " · 你让她写的" : ""}</span>
      </span></button>`;
  }).join("") : `<p class="empty">还没有作品草稿。她心情好的时候会自己想发，也可以点右上角让她现在写一个</p>`;
  const d = S.draft;
  let editor = `<p class="empty">选一个草稿看看</p>`;
  if (d) {
    const [cls, label] = DRAFT_STATUS[d.status] || ["neutral", d.status];
    const locked = d.status === "publishing" || d.status === "published" || d.status === "discarded";
    const media = (d.media_view || []).map((m, i) => `
      <div class="d-media">
        ${m.kind === "video" ? `<div class="d-video">🎬<span>${esc(m.file)}</span></div>` : m.thumb ? `<img src="${m.thumb}" alt="图片 ${i + 1}">` : `<div class="d-video">⚠<span>文件不见了</span></div>`}
        <span class="d-src">${{ card: "文字卡片", ai: "AI 生成", upload: "你上传的" }[m.source] || ""}</span>
        ${locked ? "" : `<div class="d-ops">
          <button type="button" class="btn btn-sm" data-act="d-move" data-name="${esc(m.name)}" data-dir="-1" ${i === 0 ? "disabled" : ""} aria-label="往前挪">‹</button>
          <button type="button" class="btn btn-sm" data-act="d-move" data-name="${esc(m.name)}" data-dir="1" ${i === d.media_view.length - 1 ? "disabled" : ""} aria-label="往后挪">›</button>
          <button type="button" class="btn btn-sm btn-danger" data-act="d-del" data-name="${esc(m.name)}" aria-label="删除">✕</button></div>`}
      </div>`).join("") || `<p class="empty">还没有图片或视频</p>`;
    const cardsText = (d.cards || []).map((c) => [c.title, ...(c.lines || [])].join("\n")).join("\n\n");
    editor = `
      <div class="actions" style="justify-content:space-between"><h2>${HEART}${d.status === "wish" && d.source === "auto" ? "她想发的作品" : "作品草稿"}</h2><span class="pill ${cls}">${label}</span></div>
      ${d.reason ? `<div class="me">${avatar(36)}<div class="bubble">${esc(d.reason)}</div></div>` : ""}
      ${d.error ? `<div class="alert"><span>${esc(d.error)}</span></div>` : ""}
      <div class="field"><label for="d-title">标题（20 字以内最好）</label><input id="d-title" class="input" maxlength="30" value="${esc(d.title)}" ${locked ? "disabled" : ""}></div>
      <div class="field"><label for="d-text">正文</label><textarea id="d-text" class="input" rows="5" ${locked ? "disabled" : ""}>${esc(d.text)}</textarea></div>
      <div class="field"><label for="d-tags">话题（空格分隔，最多 5 个，不用写 #）</label><input id="d-tags" class="input" value="${esc((d.tags || []).join(" "))}" ${locked ? "disabled" : ""}></div>
      <div class="field"><span class="label">图片 / 视频（按顺序发布；视频作品只能放 1 个视频）</span><div class="d-grid">${media}</div></div>
      ${locked ? "" : `<div class="actions">
        <button type="button" class="btn btn-sm" data-act="d-files">从电脑添加图片 / 视频</button>
        <button type="button" class="btn btn-sm" data-act="d-cards">重新生成文字卡片</button>
        ${d.can_ai_image ? `<input id="d-prompt" class="input" style="max-width:240px" placeholder="AI 配图的画面描述" value="${esc(d.image_prompt || "")}"><button type="button" class="btn btn-sm" data-act="d-ai">AI 生成一张配图</button>` : `<span class="hint" style="margin:0">想用 AI 生图？去“规则与预算 → 发作品”配置</span>`}
      </div>
      ${bgSection(d)}
      <details class="d-cards-edit"><summary>编辑文字卡片的内容</summary>
        <p class="hint">每张卡片一段，段与段之间空一行；每段第一行是小标题，后面每行一句（最多 4 句）。改完点“重新生成文字卡片”。</p>
        <textarea id="d-cardtext" class="input" rows="6">${esc(cardsText)}</textarea></details>`}
      <div class="decl"><input type="checkbox" checked disabled id="d-ai-decl"><label for="d-ai-decl">发布时自动勾选“内容由AI生成”声明（固定开启）</label></div>
      <div class="actions">
        ${locked ? (d.status === "publishing" ? `<span class="pill brand">${d.publish_step ? `正在发布：${esc(d.publish_step)}…` : "排好队了：她做完手上这一小步就去发（一般 1 分钟内）"}</span>` : "") : `
        <button type="button" class="btn btn-primary" data-act="d-publish">确认发布</button>
        <button type="button" class="btn" data-act="d-save">保存草稿</button>
        <button type="button" class="btn btn-danger" data-act="d-discard">不发了</button>`}
      </div>`;
  }
  $("#main").innerHTML = `
    <header class="page-head"><div><h1>作品</h1><p>她想发的作品都在这里：你可以改文案、换图、加图片或视频，确认后才会发布。今天已发布 ${D.today || 0} / ${D.limit || 0} 个</p></div>
      <div class="actions"><button type="button" class="btn" data-act="d-new">我自己写一个</button><button type="button" class="btn btn-primary" data-act="d-now">让她现在写一个</button></div></header>
    <div class="row">
      <section class="card msg-list grow-2" aria-label="作品列表">${list}</section>
      <section class="card grow-3 d-editor">${editor}</section>
    </div>`;
}

/* 文字卡片的背景：内置粉色样式 / 背景图库里的图 / 上传新的；文字写在白卡片上还是直接写在图上 */
function bgSection(d) {
  const cur = d.card_bg || d.bg_default || "none";   // 这个草稿实际用的背景
  const panel = d.card_bg ? d.card_panel !== 0 : d.panel_default;
  const isDefault = (cur === "none" ? "" : cur) === (d.bg_default || "");
  const tiles = [`<button type="button" class="bg-tile builtin${cur === "none" ? " on" : ""}" data-act="d-bg" data-name="none" aria-pressed="${cur === "none"}"><span>内置粉色</span></button>`]
    .concat((d.backgrounds || []).map((b) => `
      <div class="bg-wrap"><button type="button" class="bg-tile${cur === b.name ? " on" : ""}" data-act="d-bg" data-name="${esc(b.name)}" aria-pressed="${cur === b.name}" aria-label="用这张背景"><img src="${b.thumb}" alt=""></button>
        <button type="button" class="bg-x" data-act="d-bg-del" data-name="${esc(b.name)}" aria-label="从背景图库删掉">✕</button></div>`)).join("");
  return `
      <div class="field bg-field"><span class="label">文字卡片的背景（选好马上重画卡片）</span>
        <div class="bg-tiles">${tiles}
          <button type="button" class="bg-tile add" data-act="d-bg-up"><span>＋ 上传背景图</span></button></div>
        ${cur !== "none" ? `<label class="toggle-row" style="min-height:40px"><span>文字写在半透明白卡片上（关掉就直接写在背景图上，自动加描边）</span><input type="checkbox" id="d-panel" ${panel ? "checked" : ""}></label>` : ""}
        <div class="actions" style="margin-top:4px">
          ${isDefault ? `<span class="hint" style="margin:0">这是她以后写作品默认用的背景</span>`
                      : `<button type="button" class="btn btn-sm btn-link" data-act="d-bg-default" data-name="${esc(cur)}">以后她写的作品都用这个背景</button>`}
          <span class="hint" style="margin:0">建议竖版 3:4（1080×1440），图会自动铺满、居中裁剪</span>
        </div>
      </div>`;
}

function collectDraft() {
  const out = { title: $("#d-title").value.trim(), text: $("#d-text").value, tags: $("#d-tags").value };
  if ($("#d-cardtext")) out.cards = $("#d-cardtext").value;
  return out;
}

/* 有新的“她想发作品”：弹窗提示（每个只提示一次） */
let shownWish = new Set(JSON.parse(localStorageGet("shownWish") || "[]"));
function localStorageGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } }

async function handleWishes(wishes) {
  const w = (wishes || []).find((x) => !x.seen && !shownWish.has(x.id));
  if (!w || S.page === "drafts") return;
  shownWish.add(w.id);
  localStorageSet("shownWish", JSON.stringify([...shownWish].slice(-50)));
  const go = await dialog({ title: "您的小可爱想发作品 💌", body: `《${w.title || "未命名"}》\n${w.reason || ""}`, ok: "去看看", cancel: "稍后再说" });
  if (go) { S.selDraft = w.id; await showPage("drafts"); }
}

/* ---------- 桌宠 ---------- */
const JOINT_ORDER = ["neck", "shoulder_l", "elbow_l", "hand_l", "shoulder_r", "elbow_r", "hand_r", "hip_l", "hip_r"];

function renderPet() {
  const P = S.pet || {};
  const s = P.settings || {};
  const st = P.status || {};
  const job = P.job;
  const sizes = [[0.7, "小"], [1, "中"], [1.3, "大"], [1.6, "特大"]].map(([v, l]) => `<option value="${v}" ${Math.abs((s.size || 1) - v) < 0.01 ? "selected" : ""}>${l}</option>`).join("");
  const c = S.calib;
  $("#main").innerHTML = `
    <header class="page-head"><div><h1>桌宠</h1><p>她会站在你的任务栏上陪着你：刷视频时捧着手机认真看，点赞冒爱心，回私信时打字，想发作品时举小牌子～</p></div></header>
    <div class="row">
      <div class="col grow-2">
        <section class="card"><h2>${HEART}显示和互动</h2>
          ${st.error ? `<div class="alert"><span>${esc(st.error)}</span></div>` : ""}
          <label class="toggle-row"><span>显示桌宠</span><input id="p-on" type="checkbox" ${s.enabled !== false ? "checked" : ""}></label>
          <div class="toggle-row"><label for="p-size">大小</label><select id="p-size" class="input" style="max-width:140px">${sizes}</select></div>
          <label class="toggle-row"><span>和主人聊天（时不时冒一句话）</span><input id="p-chat" type="checkbox" ${s.chatter !== false ? "checked" : ""}></label>
          <label class="toggle-row"><span>没人理时躲到任务栏后面探头</span><input id="p-hide" type="checkbox" ${s.hide !== false ? "checked" : ""}></label>
          <label class="toggle-row"><span>同步她在抖音上做的事</span><input id="p-sync" type="checkbox" ${s.sync !== false ? "checked" : ""}></label>
          <h3 class="sub-h">对话框颜色</h3>
          <div class="bubble-colors">
            <label>底色<input id="p-bb" type="color" value="${esc(s.bubble_bg || "#FFE8F2")}"></label>
            <label>边框<input id="p-bd" type="color" value="${esc(s.bubble_border || "#FF8CB9")}"></label>
            <label>文字<input id="p-bt" type="color" value="${esc(s.bubble_text || "#DE4684")}"></label>
            <button type="button" class="btn btn-sm" data-act="pet-colors-reset">恢复默认颜色</button>
          </div>
          <div class="bubble-preview" id="bubble-preview">主人好～我来陪你啦！</div>
          <p class="hint" id="bubble-warn"></p>
          <div class="actions"><button type="button" class="btn btn-primary" data-act="pet-save">保存</button></div>
          <p class="hint">摸摸头、挠痒痒、拎起来再松手（会摔下来）、双击打开主界面、右键有更多菜单（跳舞、坐下、睡觉、躲起来、开始 / 暂停刷视频）。摸头、拎来拎去都会影响她的心情哦。</p>
          <div class="actions">
            <button type="button" class="btn btn-sm" data-act="pet-do" data-id="11">跳舞一</button>
            <button type="button" class="btn btn-sm" data-act="pet-do" data-id="12">跳舞二</button>
            <button type="button" class="btn btn-sm" data-act="pet-do" data-id="13">跳舞三</button>
            <button type="button" class="btn btn-sm" data-act="pet-do" data-id="1">摸摸头</button>
            <button type="button" class="btn btn-sm" data-act="pet-do" data-id="2">坐下</button>
            <button type="button" class="btn btn-sm" data-act="pet-do" data-id="4">睡觉</button>
          </div>
        </section>
        <section class="card"><h2>${HEART}形象</h2>
          <div class="rig-pick" role="radiogroup" aria-label="选择形象">
            ${["default", "custom"].filter((k) => k !== "custom" || P.has_custom).map((k) => `
              <button type="button" class="chip" role="radio" aria-checked="${(s.rig || "default") === k}" aria-pressed="${(s.rig || "default") === k}" data-act="pet-use" data-v="${k}">${esc((P.rig_names || {})[k] || k)}</button>`).join("")}
          </div>
          <div class="actions"><button type="button" class="btn btn-sm" data-act="pet-preview">预览动作</button></div>
          <div id="pet-preview-box"></div>
          <h3 class="sub-h">换成你的形象</h3>
          <p class="hint">上传一张卡通形象的<b>正面全身图</b>（站着、手脚不要挡住身体最好），软件会在本机抠图、拆成头 / 身体 / 手臂 / 腿，再让它动起来。不需要配置 AI。</p>
          <div class="actions">
            <label class="btn btn-sm" for="pet-file" style="display:inline-flex;align-items:center">上传形象图</label>
            <input id="pet-file" type="file" accept="image/png,image/jpeg,image/webp" class="sr-only">
            ${P.onnx && !P.model_ready ? `<button type="button" class="btn btn-sm" data-act="pet-model">下载抠图模型（约 176MB）</button>` : ""}
          </div>
          <p class="hint">${P.model_ready ? "本机抠图模型已就绪：复杂背景也能抠。" : P.onnx ? "背景复杂的图需要先下载抠图模型（只下载一次，来自 Hugging Face，国内会自动换镜像）；透明背景的 PNG 或纯色背景的图不用下载。" : "这个版本只能处理透明背景或纯色背景的图。"}</p>
          ${job && job.state === "running" ? `<span class="pill brand" style="align-self:flex-start">${esc(job.message)}</span>` : ""}
          ${job && job.state === "error" ? `<span class="pill bad" style="align-self:flex-start;white-space:normal">${esc(job.error)}</span>` : ""}
          ${P.model_job && P.model_job.state === "running" ? `<span class="pill brand" style="align-self:flex-start">${esc(P.model_job.message)}</span>` : ""}
        </section>
      </div>
      <section class="card grow-3">${c ? calibHTML(c) : `<h2>${HEART}调整关节点</h2><p class="empty">上传形象图后，在这里把关节点拖到准确的位置，再生成预览</p>`}</section>
    </div>`;
  $("#pet-file").addEventListener("change", onPetFile);
  ["#p-bb", "#p-bd", "#p-bt"].forEach((id) => $(id).addEventListener("input", updateBubblePreview));
  updateBubblePreview();
  if (c) bindCalib();
}

/* 对话框颜色预览 + 看不清时提醒（按 WCAG 对比度算） */
function lum(hex) {
  const v = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
}
function updateBubblePreview() {
  const bg = $("#p-bb").value, bd = $("#p-bd").value, tx = $("#p-bt").value;
  const box = $("#bubble-preview");
  box.style.background = bg; box.style.borderColor = bd; box.style.color = tx;
  const [a, b] = [lum(bg), lum(tx)].sort((x, y) => y - x);
  const ratio = (a + 0.05) / (b + 0.05);
  $("#bubble-warn").textContent = ratio < 3 ? `文字和底色太接近了（对比度 ${ratio.toFixed(1)}），在桌面上可能看不清，换个深一点或浅一点的颜色吧` : "";
}

function calibHTML(c) {
  const dots = JOINT_ORDER.map((k) => {
    const [x, y] = c.joints[k];
    return `<button type="button" class="joint j-${k.slice(-1) === "l" ? "l" : k.slice(-1) === "r" ? "r" : "c"}" data-j="${k}" style="left:${(x / c.size[0]) * 100}%;top:${(y / c.size[1]) * 100}%" title="${esc(c.labels[k])}" aria-label="${esc(c.labels[k])}"></button>`;
  }).join("");
  const legend = JOINT_ORDER.map((k) => `<span class="jl"><i class="j-${k.slice(-1) === "l" ? "l" : k.slice(-1) === "r" ? "r" : "c"}"></i>${esc(c.labels[k])}</span>`).join("");
  return `<h2>${HEART}调整关节点</h2>
    <p class="hint">${esc(c.how)}。把每个圆点拖到对应的位置：<b>左 / 右指图片的左边 / 右边</b>。手臂粗细要刚好盖住整条手臂。</p>
    <div class="calib-wrap"><div class="calib" id="calib"><img src="${c.image}" alt="抠好的形象" draggable="false">${dots}${armLines(c)}</div>
      <div class="col" style="gap:10px;min-width:180px">
        <div class="jlegend">${legend}</div>
        <div class="field"><label for="arm-w">手臂粗细：<b id="arm-wv">${Math.round(c.arm_width)}</b> 像素</label><input id="arm-w" type="range" min="6" max="${Math.round(c.size[0] / 3)}" value="${Math.round(c.arm_width)}"></div>
        <button type="button" class="btn btn-sm" data-act="calib-ai">让识图模型帮忙估一下</button>
        <button type="button" class="btn btn-primary" data-act="calib-build">生成预览</button>
        ${c.preview ? `<button type="button" class="btn btn-primary" data-act="calib-apply">用这个形象</button>` : ""}
      </div></div>
    ${c.preview ? `<img class="pet-grid" src="${c.preview}" alt="新形象的动作预览">` : ""}`;
}

function armLines(c) {
  const W = c.size[0], H = c.size[1];
  const seg = (a, b) => `<line x1="${(c.joints[a][0] / W) * 100}" y1="${(c.joints[a][1] / H) * 100}" x2="${(c.joints[b][0] / W) * 100}" y2="${(c.joints[b][1] / H) * 100}"/>`;
  const sw = (c.arm_width / W) * 100;
  return `<svg class="calib-svg" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><g style="stroke-width:${sw}">${seg("shoulder_l", "elbow_l")}${seg("elbow_l", "hand_l")}${seg("shoulder_r", "elbow_r")}${seg("elbow_r", "hand_r")}</g>
    <line class="neckline" x1="0" x2="100" y1="${(c.joints.neck[1] / H) * 100}" y2="${(c.joints.neck[1] / H) * 100}"/>
    <line class="hipline" x1="0" x2="100" y1="${((c.joints.hip_l[1] + c.joints.hip_r[1]) / 2 / H) * 100}" y2="${((c.joints.hip_l[1] + c.joints.hip_r[1]) / 2 / H) * 100}"/></svg>`;
}

function bindCalib() {
  const box = $("#calib");
  let drag = null;
  box.addEventListener("pointerdown", (e) => {
    const j = e.target.closest(".joint");
    if (!j) return;
    drag = j.dataset.j;
    j.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  box.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const r = box.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const y = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
    S.calib.joints[drag] = [x * S.calib.size[0], y * S.calib.size[1]];
    const el = box.querySelector(`[data-j="${drag}"]`);
    el.style.left = `${x * 100}%`; el.style.top = `${y * 100}%`;
    box.querySelector(".calib-svg").outerHTML = armLines(S.calib);
  });
  box.addEventListener("pointerup", () => { drag = null; });
  $("#arm-w").addEventListener("input", (e) => {
    S.calib.arm_width = Number(e.target.value);
    $("#arm-wv").textContent = e.target.value;
    box.querySelector(".calib-svg").outerHTML = armLines(S.calib);
  });
}

function onPetFile(e) {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  if (f.size > 25 * 1024 * 1024) return toast("图片太大了，请选 25MB 以内的", true);
  const reader = new FileReader();
  reader.onload = async () => {
    await call("pet_upload", reader.result);
    S.calib = null;
    toast("开始抠图啦，稍等一下");
    pollPetJob();
  };
  reader.readAsDataURL(f);
}

async function pollPetJob() {
  const job = await call("pet_job", "pet");
  if (S.page !== "pet") return;
  S.pet = { ...(S.pet || {}), job };
  if (job && job.state === "running") { renderPet(); setTimeout(pollPetJob, 800); return; }
  if (job && job.state === "done") {
    S.calib = { ...job.result, preview: "" };
    S.pet.job = null;
    toast("抠好啦，拖一拖关节点就可以生成预览");
  }
  renderPet();
}

async function pollModelJob() {
  const job = await call("pet_job", "model");
  if (S.page !== "pet") return;
  S.pet = { ...(S.pet || {}), model_job: job };
  if (job && job.state === "running") { renderPet(); setTimeout(pollModelJob, 1000); return; }
  if (job && job.state === "error") toast(job.error, true);
  if (job && job.state === "done") { toast("抠图模型下载好了"); S.pet = await call("get_pet"); }
  renderPet();
}

function collectPet() {
  return {
    enabled: $("#p-on").checked, size: Number($("#p-size").value), chatter: $("#p-chat").checked, hide: $("#p-hide").checked, sync: $("#p-sync").checked,
    bubble_bg: $("#p-bb").value, bubble_border: $("#p-bd").value, bubble_text: $("#p-bt").value,
  };
}

/* ---------- 设置 ---------- */
function renderSettings() {
  const theme = S.st.ui.theme;
  const card = (k, name, desc, colors) => `
    <button type="button" class="card" data-act="theme" data-v="${k}" style="cursor:pointer;text-align:left;flex:1 1 260px;${theme === k ? "outline:3px solid var(--brand);outline-offset:2px" : ""}">
      <div style="display:flex;gap:6px">${colors.map((c) => `<span style="width:36px;height:36px;border-radius:50%;background:${c};border:1px solid rgba(0,0,0,.1)"></span>`).join("")}</div>
      <b style="font-size:16px">${name}${theme === k ? "（使用中）" : ""}</b><span style="color:var(--muted)">${desc}</span></button>`;
  $("#main").innerHTML = `
    <header class="page-head"><div><h1>设置</h1><p>外观主题、数据文件和调试工具</p></div></header>
    <section class="card"><h2>${HEART}外观主题</h2>
      <div class="row">
        ${card("pink", "粉色 Q 版卡通", "默认主题：甜系配色、圆润卡片、Q 版小助手", ["#CF2E68", "#FFE4EE", "#F06292", "#FFF5F8"])}
        ${card("blue", "简约蓝白", "第一版主题：深色侧边栏、蓝色主色、清爽简洁", ["#2F5BEA", "#15171C", "#EEF2FF", "#F4F5F7"])}
      </div></section>
    <section class="card"><h2>${HEART}数据</h2>
      <p style="margin:0">人设、看过的视频、消息记录都保存在安装目录的 data 文件夹（SQLite 数据库 app.db），整个文件夹复制走就是完整备份。卸载软件时不会删除它。</p>
      <div class="actions"><button type="button" class="btn" data-act="open-data">打开数据文件夹</button></div></section>
    <section class="card"><h2>${HEART}调试工具</h2>
      <p style="margin:0">如果某个动作在抖音上失效（比如找不到私信入口），先在弹出的浏览器里打开对应页面，再点下面的按钮，把 data\\debug 里新生成的截图和 HTML 发给开发者校准。页面定位写在 data\\selectors.json 里，也可以直接修改。</p>
      <div class="actions"><button type="button" class="btn" data-act="dump">保存当前页面结构</button></div></section>
    <section class="card"><h2>${HEART}浏览器窗口</h2>
      <p style="margin:0">她用的浏览器平时藏在屏幕外面、在后台运行，不会弹出来挡住你用电脑，也不会抢走你的键盘焦点。只有扫码登录、抖音要求手动验证时，才会自动拿到前面，完成后自动藏回去。请不要在任务栏里把它关掉。</p>
      <div class="actions">
        <button type="button" class="btn" data-act="browser-show">看看她的浏览器</button>
        <button type="button" class="btn" data-act="browser-hide">藏回后台</button>
      </div></section>
    <section class="card"><h2>${HEART}关于</h2>
      <p style="margin:0">${esc(S.st.app.name)}（${esc(S.st.app.name_en || "aiWannaTikTok")}）v${esc(S.st.app.version)} · 公开 AI 身份的抖音虚拟人，所有数据只保存在这台电脑上。</p>
      <p style="margin:0">作者：<b>liaozhijin</b></p>
      <h3 class="oss-title">用到的开源组件</h3>
      <p style="margin:0;color:var(--muted)">感谢这些开源项目的作者和贡献者。</p>
      <table class="oss-table"><thead><tr><th>名称</th><th>在软件里做什么</th></tr></thead><tbody>
        ${OPEN_SOURCE.map(([n, d]) => `<tr><td>${esc(n)}</td><td>${esc(d)}</td></tr>`).join("")}
      </tbody></table></section>`;
}

/* “关于”里列出的开源组件（只列软件运行时用到的；开发、打包、测试工具和参考项目见 README） */
const OPEN_SOURCE = [
  ["pywebview", "主界面窗口：把网页界面做成桌面窗口，并和程序互相调用"],
  ["Playwright", "控制电脑上的 Edge / Chrome 打开抖音网页版：刷视频、点赞、收藏、评论、回私信、发作品"],
  ["Requests", "请求大模型接口、AI 生图接口，下载抠图模型"],
  ["Pillow", "画作品文字卡片、头像和背景图，处理桌宠图像和视频截图"],
  ["NumPy", "桌宠手脚连接、身体轮廓补齐和抠图时的图像计算"],
  ["ONNX Runtime", "上传自定义桌宠形象时，在本机运行抠图模型"],
  ["anime-segmentation", "动漫人物抠图模型（第一次用到时才下载）"],
  ["DIS / IS-Net", "抠图模型所基于的网络结构"],
  ["ZCOOL KuaiLe（站酷快乐体）", "界面和作品卡片使用的字体"],
];

/* ---------- 页面切换和刷新 ---------- */
async function showPage(page) {
  S.page = page;
  renderSide();
  if (page === "dashboard") renderDashboard();
  else if (page === "inbox") { await loadMessages(); renderInbox(); }
  else if (page === "persona") { S.persona = await call("get_persona"); renderPersona(); }
  else if (page === "models") { const d = await call("get_providers"); S.providers = d.providers; S.tasks = d.tasks; renderModels(); }
  else if (page === "rules") { S.rules = await call("get_rules"); S.friends = await call("get_friends"); renderRules(); }
  else if (page === "drafts") { await loadDrafts(); renderDrafts(); }
  else if (page === "pet") { S.pet = await call("get_pet"); renderPet(); }
  else if (page === "settings") renderSettings();
  else if (page === "history") {
    if (!S.hist.data) S.hist.day = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    await loadHistory(); renderHistory();
  }
  $("#main").scrollTop = 0;
}

function applyTheme() {
  document.documentElement.dataset.theme = (S.st && S.st.ui.theme) || "pink";
}

async function refresh() {
  try {
    S.st = await call("get_state", S.evPage || 1);
  } catch (e) { return; }
  applyTheme();
  handleAttention(S.st.agent && S.st.agent.attention);
  const req = S.st.ui_request || {};
  if (S.uiSeq === null) S.uiSeq = req.seq;
  else if (req.seq !== S.uiSeq) { S.uiSeq = req.seq; if (req.page) { if (req.page === "drafts") S.selDraft = null; await showPage(req.page); } }
  handleWishes(S.st.wishes);
  renderSide();
  const typing = document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
  if (S.page === "dashboard") renderDashboard();
  else if (S.page === "inbox" && !typing) { await loadMessages(); renderInbox(); }
  else if (S.page === "drafts" && !typing && S.draft && S.draft.status === "publishing") { await loadDrafts(); renderDrafts(); }
}

/* 好友下拉框：勾选状态和“喜欢看什么” */
function friendChoices() {
  const fr = (S.friends && S.friends.friends) || [];
  const checks = Object.fromEntries($$(".fr-check").map((c) => [c.dataset.uid, c.checked]));
  const tags = Object.fromEntries($$(".fr-tags").map((c) => [c.dataset.uid, c.value.trim()]));
  return fr.map((f) => ({ uid: f.uid, enabled: f.uid in checks ? checks[f.uid] : !!f.enabled, tags: f.uid in tags ? tags[f.uid] : (f.tags || "") }));
}
function keepFriendInputs() {
  if (!S.friends) return;
  const ch = friendChoices();
  S.friends.friends = S.friends.friends.map((f) => { const c = ch.find((x) => x.uid === f.uid); return c ? { ...f, enabled: c.enabled ? 1 : 0, tags: c.tags } : f; });
}
document.addEventListener("change", (e) => {
  if (e.target.classList && e.target.classList.contains("fr-check") && S.page === "rules") {
    S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs(); renderRules();
  }
  if (e.target.id === "d-panel" && S.page === "drafts" && S.draft) {  // 文字写在白卡片上 / 直接写在图上：马上重画
    const cur = S.draft.card_bg || S.draft.bg_default || "none";
    (async () => {
      await call("save_draft", S.selDraft, collectDraft());
      toast("正在重画卡片…");
      await call("draft_set_bg", S.selDraft, cur, e.target.checked);
      await loadDrafts(); renderDrafts();
    })();
  }
});

/* ---------- 按钮动作 ---------- */
const ACT = {
  nav: (el) => showPage(el.dataset.page),
  login: async () => {
    if (S.st.agent.logged_in) {
      await dialog({ title: "已经登录啦", body: "抖音已经登录着，不用再扫码。登录状态会一直保存在这台电脑上，下次点“开始刷视频”会直接开始。想换账号的话，先点“退出登录”。", cancel: "" });
      return;
    }
    await call("douyin_login");
    toast("浏览器窗口打开后，用抖音 App 扫码登录");
  },
  logout: async () => {
    const ok = await dialog({
      title: "退出抖音登录？",
      body: "退出后，她会停止刷视频，这台电脑上保存的抖音登录状态会被清除。下次需要重新扫码登录（抖音可能会要求验证手机）。",
      ok: "确定退出", cancel: "先不退出",
    });
    if (!ok) return;
    await call("douyin_logout");
    toast("已退出抖音登录");
    setTimeout(refresh, 1500);
  },
  start: async () => {
    const go = await dialog({
      title: "开始刷视频前",
      body: "登录抖音的浏览器不能关闭，避免影响观看视频，浏览器可以后台运行，你可以去做别的事情，不影响。",
      ok: "知道了，开始刷视频",
    });
    if (!go) return;
    await call("start_agent");
    toast("她开始刷视频啦");
    setTimeout(refresh, 800);
  },
  pause: async (el) => {
    if (el) { el.disabled = true; el.textContent = "正在停下…"; }
    await call("pause_agent");
    toast("好的，她做完手上这一小步就休息");
    refresh();
  },
  activity: (el) => showActivity(el.dataset.k),
  "event-detail": (el) => showEventDetail(el.dataset.ref, el.dataset.title),
  "tp-open": (el) => openTimePop(el),
  "tp-pick": (el) => pickTime(el),
  "ev-page": async (el) => { S.evPage = Number(el.dataset.p) || 1; S.st = await call("get_state", S.evPage); renderDashboard(); },
  "h-page": async (el) => { S.hist.page = Number(el.dataset.p) || 1; await loadHistory(); renderHistory(); $("#main").scrollTop = 0; },
  "h-day": async (el) => { S.hist.day = el.dataset.day || ""; S.hist.page = 1; await loadHistory(); renderHistory(); },
  "h-shift": async (el) => {
    if (!S.hist.day) return;
    S.hist.day = shiftDay(S.hist.day, Number(el.dataset.n)); S.hist.page = 1; await loadHistory(); renderHistory();
  },
  "open-video": async (el) => { await call("open_video", el.dataset.url); },
  "check-inbox": async () => { await call("check_inbox_now"); toast("马上去看看有没有人找她"); },
  dismiss: async () => { await call("dismiss_alert"); refresh(); },
  "goto-msg": async (el) => { S.msgFilter = "pending"; S.selMsg = Number(el.dataset.id); await showPage("inbox"); },

  "msg-filter": async (el) => { S.msgFilter = el.dataset.f; S.selMsg = null; await loadMessages(); renderInbox(); },
  "msg-sel": (el) => { S.selMsg = Number(el.dataset.id); renderInbox(); },
  "msg-do": async (el) => {
    const act = el.dataset.do;
    const text = act === "manual" ? $("#manual-text").value.trim() : "";
    if (act === "manual" && !text) return toast("先写好回复内容", true);
    if (act === "block" && !confirm("以后都不自动回复这个人了，确定吗？")) return;
    await call("message_action", S.selMsg, act, text);
    toast({ manual: "已交给她发送", open: "已在浏览器里打开", done: "已标记", block: "以后不回复此人" }[act]);
    await loadMessages(); renderInbox();
  },

  interest: (el) => {
    const v = el.dataset.v; const list = S.persona.interests || [];
    S.persona.interests = list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
    el.setAttribute("aria-pressed", String(S.persona.interests.includes(v)));
  },
  "add-interest": () => {
    const v = $("#new-interest").value.trim();
    if (!v) return;
    S.persona = { ...S.persona, ...collectPersona() };
    if (!S.persona.interests.includes(v)) S.persona.interests.push(v);
    renderPersona();
  },
  "save-persona": async () => { await call("save_persona", collectPersona()); S.persona = await call("get_persona"); await refresh(); renderPersona(); toast("人设已保存"); },
  "reset-avatar": async () => { await call("reset_avatar"); S.st.persona.avatar_url = ""; S.persona = { ...S.persona, ...collectPersona() }; renderSide(); renderPersona(); },
  "sync-sig": async () => {
    await call("save_persona", collectPersona());
    const bio = await call("sync_signature");
    toast("正在同步到抖音简介：" + bio);
  },
  "try-send": async () => {
    const input = $("#try-input"); const text = input.value.trim();
    if (!text || S.busy) return;
    S.busy = true; S.persona = { ...S.persona, ...collectPersona() };
    S.tryLog.push({ role: "them", text }); renderPersona();
    try {
      const d = await call("try_chat", text);
      S.tryLog.push({ role: "me", text: d.ok ? d.text : `（这条不会发出去：${d.reason}）${d.text}` });
    } catch (e) { S.tryLog.push({ role: "me", text: "（没能回复：" + e.message + "）" }); }
    S.busy = false; renderPersona();
  },

  "prov-sel": (el) => { S.selProv = el.value; renderModels(); },
  "toggle-key": (el) => { const k = $("#m-key"); k.type = k.type === "password" ? "text" : "password"; el.textContent = k.type === "password" ? "显示" : "隐藏"; },
  "save-prov": async () => { await call("save_provider", collectProvider()); S.visDraftFor = null; await showPage("models"); toast("已保存"); },
  "clear-key": async () => { if (!confirm("确定删除这家的 API Key 吗？")) return; await call("save_provider", { ...collectProvider(), api_key: "", clear_key: true }); S.visDraftFor = null; await showPage("models"); },
  "test-prov": async () => {
    const out = $("#test-result"); out.innerHTML = '<span class="pill neutral" style="align-self:flex-start">逐个测试模型中，请稍等…</span>';
    await call("save_provider", collectProvider());
    try {
      const d = await call("test_provider", S.selProv, "");
      out.innerHTML = d.results.map((r) => {
        if (!r.ok) return `<span class="pill bad" style="align-self:flex-start;white-space:normal">✕ ${esc(r.model)}：${esc(r.error)}</span>`;
        const v = r.vision_ok === true ? " · 看图正常" : r.vision_ok === false ? ` · ${esc(r.error)}` : " · 未勾选识图";
        return `<span class="pill ${r.vision_ok === false ? "warn" : "ok"}" style="align-self:flex-start;white-space:normal">● ${esc(r.model)}：连接成功 · ${r.seconds} 秒${v}</span>`;
      }).join("");
      const prov = await call("get_providers"); S.providers = prov.providers;
    } catch (e) { out.innerHTML = `<span class="pill bad" style="align-self:flex-start">测试失败：${esc(e.message)}</span>`; }
  },
  "save-tasks": async () => {
    const t = {};
    for (const [k] of TASKS) { const [provider, ...m] = ($("#task-" + k).value || "").split("|"); t[k] = { provider: provider || "", model: m.join("|") }; }
    await call("save_tasks", t); S.tasks = t; toast("分工已保存"); refresh();
  },

  topic: (el) => {
    const v = el.dataset.v; const r = S.rules;
    r.topics = r.topics.includes(v) ? r.topics.filter((x) => x !== v) : [...r.topics, v];
    el.setAttribute("aria-pressed", String(r.topics.includes(v)));
  },
  "save-rules": async () => {
    await call("save_rules", collectRules());
    if (S.friends) await call("save_friends", friendChoices());
    S.rules = await call("get_rules"); S.friends = await call("get_friends"); renderRules(); refresh(); toast("规则已保存");
  },

  "goto-draft": async (el) => { S.selDraft = Number(el.dataset.id); await showPage("drafts"); },
  "fr-open": () => { S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs(); S.frOpen = !S.frOpen; renderRules(); },
  "fr-fetch": async () => {
    if (S.frLoading) return;
    const before = (S.friends && S.friends.fetch && S.friends.fetch.at) || "";
    S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs();
    if (S.friends) await call("save_friends", friendChoices());  // 先存好你已经勾选的
    await call("fetch_friends");
    S.frLoading = true; renderRules();
    toast("她去你的抖音主页读互关好友了，读完会自动刷新");
    const t0 = Date.now();
    const poll = async () => {
      let f = null;
      try { f = await call("get_friends"); } catch (e) { /* 已经提示 */ }
      const done = f && ((f.fetch && f.fetch.at) || "") !== before;
      if (!done && Date.now() - t0 < 180000) { setTimeout(poll, 2000); return; }
      S.frLoading = false;
      if (f) S.friends = f;
      if (S.page !== "rules") return;
      S.rules = { ...S.rules, ...collectRules() };
      S.frOpen = true; renderRules();
      if (!done) toast("还没读完，可能浏览器在忙，稍后再点一次试试", true);
      else if (f.fetch.error) toast(f.fetch.error, true);
      else toast(`读好啦：互关好友 ${f.fetch.count || 0} 人`);
    };
    setTimeout(poll, 2000);
  },
  "fr-add": async () => {
    const v = $("#fr-new").value.trim();
    if (!v) return toast("先输入好友的抖音昵称", true);
    S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs();
    await call("save_friends", friendChoices()); await call("add_friend", v);
    S.friends = await call("get_friends"); S.frOpen = true; renderRules(); toast("已添加");
  },

  "draft-sel": async (el) => { S.selDraft = Number(el.dataset.id); S.draft = await call("get_draft", S.selDraft); renderDrafts(); },
  "d-new": async () => { S.selDraft = await call("new_draft"); await loadDrafts(); renderDrafts(); },
  "d-now": async () => { await call("draft_now"); toast("她在构思了，写好会提醒你～"); setTimeout(async () => { if (S.page === "drafts") { await loadDrafts(); renderDrafts(); } }, 15000); },
  "d-save": async () => { await call("save_draft", S.selDraft, collectDraft()); await loadDrafts(); renderDrafts(); toast("草稿已保存"); },
  "d-cards": async (el) => {
    el.disabled = true; el.textContent = "正在画卡片…";
    await call("save_draft", S.selDraft, collectDraft()); await call("draft_cards", S.selDraft);
    await loadDrafts(); renderDrafts(); toast("文字卡片重新画好啦");
  },
  "d-ai": async (el) => {
    const prompt = $("#d-prompt").value.trim();
    await call("save_draft", S.selDraft, collectDraft());
    el.disabled = true; el.textContent = "正在生成，可能要几十秒…";
    try { await call("draft_ai_image", S.selDraft, prompt); toast("配图生成好啦"); } catch (e) { /* 已经提示 */ }
    await loadDrafts(); renderDrafts();
  },
  "d-bg": async (el) => {
    await call("save_draft", S.selDraft, collectDraft());
    toast("正在按新背景重画卡片…");
    await call("draft_set_bg", S.selDraft, el.dataset.name, $("#d-panel") ? $("#d-panel").checked : null);
    await loadDrafts(); renderDrafts();
  },
  "d-bg-up": async () => {
    await call("save_draft", S.selDraft, collectDraft());
    const names = await call("card_bg_upload", S.selDraft);
    if (names && names.length) toast(`加了 ${names.length} 张背景，已经换上`);
    await loadDrafts(); renderDrafts();
  },
  "d-bg-del": async (el) => {
    const ok = await dialog({ title: "从背景图库删掉这张图？", body: "已经画好的卡片不受影响。", ok: "删掉", cancel: "算了" });
    if (!ok) return;
    await call("card_bg_delete", el.dataset.name); await loadDrafts(); renderDrafts();
  },
  "d-bg-default": async (el) => {
    await call("card_bg_set_default", el.dataset.name, $("#d-panel") ? $("#d-panel").checked : true);
    toast("好的，以后她写的作品都用这个背景"); await loadDrafts(); renderDrafts();
  },
  "d-files": async () => {
    await call("save_draft", S.selDraft, collectDraft());
    const n = await call("draft_add_files", S.selDraft);
    await loadDrafts(); renderDrafts(); if (n) toast(`加了 ${n} 个文件`);
  },
  "d-del": async (el) => { await call("save_draft", S.selDraft, collectDraft()); await call("draft_remove_media", S.selDraft, el.dataset.name); await loadDrafts(); renderDrafts(); },
  "d-move": async (el) => {
    const names = S.draft.media.map((m) => m.name);
    const i = names.indexOf(el.dataset.name); const j = i + Number(el.dataset.dir);
    if (i < 0 || j < 0 || j >= names.length) return;
    [names[i], names[j]] = [names[j], names[i]];
    await call("save_draft", S.selDraft, { ...collectDraft(), order: names }); await loadDrafts(); renderDrafts();
  },
  "d-publish": async () => {
    await call("save_draft", S.selDraft, collectDraft());
    const ok = await dialog({ title: "确认发布这个作品？", body: "会用她的抖音账号发布，并自动勾选“内容由AI生成”声明。发布过程中浏览器在后台操作，不会打扰你。", ok: "确认发布", cancel: "再看看" });
    if (!ok) return;
    await call("publish_draft", S.selDraft); toast("开始发布啦"); await loadDrafts(); renderDrafts();
  },
  "d-discard": async () => {
    const ok = await dialog({ title: "不发这个作品了吗？", body: "她可能会有一点点失落哦。草稿会保留在列表里，标记为“不发了”。", ok: "不发了", cancel: "再想想" });
    if (!ok) return;
    await call("discard_draft", S.selDraft); await loadDrafts(); renderDrafts();
  },

  "pet-save": async () => { await call("save_pet", collectPet()); S.pet = await call("get_pet"); renderPet(); toast("桌宠设置已保存"); },
  "pet-colors-reset": () => { $("#p-bb").value = "#FFE8F2"; $("#p-bd").value = "#FF8CB9"; $("#p-bt").value = "#DE4684"; updateBubblePreview(); },
  "pet-do": async (el) => { await call("pet_action", Number(el.dataset.id)); },
  "pet-preview": async (el) => { el.disabled = true; const url = await call("pet_preview", "current"); $("#pet-preview-box").innerHTML = `<img class="pet-grid" src="${url}" alt="动作预览">`; el.disabled = false; },
  "pet-use": async (el) => {
    await call("save_pet", collectPet());
    await call("pet_apply", el.dataset.v); S.pet = await call("get_pet"); renderPet(); toast("形象换好啦，看看桌面～");
  },
  "pet-model": async () => { await call("pet_download_model"); pollModelJob(); },
  "calib-ai": async (el) => {
    el.disabled = true; el.textContent = "识图模型在看…";
    try { const j = await call("pet_guess_ai", S.calib.joints); S.calib.joints = { ...S.calib.joints, ...j }; toast("估好啦，再微调一下"); } catch (e) { /* 已经提示 */ }
    renderPet();
  },
  "calib-build": async (el) => {
    el.disabled = true; el.textContent = "正在拆部件…";
    try { S.calib.preview = await call("pet_build", S.calib.joints, S.calib.arm_width); } catch (e) { /* 已经提示 */ }
    renderPet();
  },
  "calib-apply": async () => { await call("pet_apply", "new"); S.calib = null; S.pet = await call("get_pet"); renderPet(); toast("新形象用上啦，看看桌面～"); },

  theme: async (el) => { await call("set_theme", el.dataset.v); S.st.ui.theme = el.dataset.v; applyTheme(); renderSettings(); renderSide(); },
  "open-data": () => call("open_data_dir"),
  dump: async () => { await call("dump_page"); toast("已保存到 data\\debug"); },
  "browser-show": async () => { await call("browser_window", true); toast("浏览器窗口拿到前面了，看完点“藏回后台”"); },
  "browser-hide": async () => { await call("browser_window", false); toast("浏览器已经藏回后台"); },
};

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const fn = ACT[el.dataset.act];
  if (!fn) return;
  if (el.tagName === "BUTTON") e.preventDefault();
  Promise.resolve(fn(el, e)).catch(() => {});
});

async function init() {
  await refresh();
  await showPage("dashboard");
  setInterval(refresh, 3000);
}

if (window.pywebview && window.pywebview.api) init();
else window.addEventListener("pywebviewready", init);
