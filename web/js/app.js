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
  mem: { tab: "events", q: "", person: "", data: null },   // 记忆页
  about: null,
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
  memory: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3V4z"/><path d="M5 17a3 3 0 0 1 3-3h11"/><path d="M9 8h6"/></svg>',
  check: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>',
};
const PAGES = [
  ["dashboard", "仪表盘"], ["history", "历史记录"], ["inbox", "消息中心"], ["memory", "记忆"], ["drafts", "作品"], ["persona", "人设"], ["pet", "桌宠"],
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
  at_friend: ["brand", "@好友"], sticker: ["brand", "表情包"], owner_chat: ["ok", "找主人聊天"], owner_share: ["ok", "分享给主人"], publish_wish: ["warn", "想发作品"], published: ["ok", "发布作品"],
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
    ? ` · 私信${rb.dm_reply_mode === "5min" ? "每 5 分钟回一次" : "秒回"} · ${a.next_at_s > 0 ? `${Math.ceil(a.next_at_s / 60)} 分钟后去看 @ 和评论回复` : "马上去看 @ 和评论回复"}`
      + (a.notice_checked && a.notice_checked.at
        ? `（上次 ${esc(String(a.notice_checked.at).slice(11, 16))} 看过：新 @ ${a.notice_checked.new_at} 条、新回复 ${a.notice_checked.new_reply} 条）`
        : "")
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
  S.keepDays = d.comment_keep_days || 5;
  if (S.selMsg && !S.msgs.find((m) => m.id === S.selMsg)) S.selMsg = null;
  if (!S.selMsg && S.msgs.length) S.selMsg = S.msgs[0].id;
}

const KIND_TEXT = { at: "评论里 @ 了她", reply: "回复了她的评论", dm: "私信" };

/* 同一个人的消息合成一组，不管是私信还是评论区：左边每人只有一个框（显示最新的一条），
   右边按时间显示和 TA 的来回，每一条标着是私信、评论里 @ 她，还是回复她的评论 */
function msgGroups() {
  const groups = new Map();
  for (const m of S.msgs) {          // S.msgs 是新的在前
    const key = m.user_name || "";
    if (!groups.has(key)) groups.set(key, { key, dm: false, cmt: false, owner: null, who: null, items: [] });
    const g = groups.get(key);
    g.items.push(m);
    if (m.kind === "dm") g.dm = true; else g.cmt = true;
    if (m.owner !== null && m.owner !== undefined && g.owner === null) g.owner = m.owner;
    if (!g.who && m.user) g.who = m;   // 读到过 TA 的头像、昵称的那一条
  }
  return Array.from(groups.values());
}
const groupWhere = (g) => (g.dm && g.cmt ? "私信 + 评论区" : g.dm ? "私信" : "评论区");

function renderInbox() {
  const c = S.counts || {};
  const tabs = [["all", "全部", c.all_count], ["at", "评论区", c.at_count], ["dm", "私信", c.dm_count],
    ["pending", "待你处理", c.pending_count], ["skipped", "已跳过", c.skipped_count]]
    .map(([k, l, n]) => `<button type="button" role="tab" aria-selected="${S.msgFilter === k}" data-act="msg-filter" data-f="${k}">${l} · ${n || 0}</button>`).join("");
  const groups = msgGroups();
  const m = S.msgs.find((x) => x.id === S.selMsg);
  const cur = m ? groups.find((g) => g.items.includes(m)) : null;
  const list = groups.length ? groups.map((g) => {
    const top = g.items[0];
    const pend = g.items.filter((x) => x.status === "pending").length;
    const [cls, label] = MSG_STATUS[top.status] || ["neutral", top.status];
    return `<button type="button" class="msg-item" data-act="msg-sel" data-id="${top.id}" ${cur === g ? 'aria-current="true"' : ""}>
      ${userAvatar(g.who || top)}
      <span style="display:flex;flex-direction:column;gap:4px;min-width:0;flex:1">
        <span class="who"><b>@${esc(displayName(g.who || top))}${g.owner !== null ? " · 主人" : ""} · ${groupWhere(g)}</b><span>${esc(hhmm(top.created_at))}</span></span>
        <span class="txt">${top.kind === "dm" ? "" : "［评论区］"}“${esc((top.content || "").slice(0, 60))}”</span>
        <span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <span class="pill ${cls}">${label}</span>
          ${g.items.length > 1 ? `<span class="pill neutral">共 ${g.items.length} 条</span>` : ""}
          ${pend ? `<span class="pill warn">${pend} 条待处理</span>` : ""}
        </span>
      </span></button>`;
  }).join("") : `<p class="empty">还没有消息</p>`;

  let detail = `<p class="empty">选一个人看看</p>`;
  if (m && cur) {
    const [cls, label] = MSG_STATUS[m.status] || ["neutral", m.status];
    // 最近的来回，旧的在上、新的在下；点一条可以对那一条做操作
    const SHOWN = 100;
    const who = cur.who || m;
    const talk = cur.items.slice(0, SHOWN).reverse().map((x) => {
      const [c2, l2] = MSG_STATUS[x.status] || ["neutral", x.status];
      return `
        <div class="talk-turn ${x.id === m.id ? "sel" : ""}">
          <button type="button" class="talk-meta" data-act="msg-sel" data-id="${x.id}">${esc(x.created_at.slice(5, 16))} · ${KIND_TEXT[x.kind] || "私信"}${x.video_title ? ` · 视频：${esc(x.video_title.slice(0, 24))}` : ""} <span class="pill ${c2}">${l2}</span></button>
          <div class="them">${userAvatar(x.user ? x : who, 36)}<div class="bubble">${esc(x.content)}</div></div>
          ${x.reply ? `<div class="me">${avatar(36)}<div class="bubble">${esc(x.reply)}</div></div>` : ""}
        </div>`;
    }).join("");
    detail = `
      <div class="actions" style="justify-content:space-between"><h2>${HEART}和 @${esc(displayName(who))} 的${cur.dm && cur.cmt ? "私信和评论区对话" : cur.dm ? "私信" : "评论区对话"}</h2><span class="pill neutral">${cur.items.length > SHOWN ? `最近 ${SHOWN} 条（共 ${cur.items.length} 条）` : `共 ${cur.items.length} 条`}</span></div>
      <div class="u-card">
        ${userAvatar(who, 56)}
        <div class="u-info">
          <b>${esc(displayName(who))}${cur.owner !== null ? `（主人${cur.owner ? "，她叫 TA “" + esc(cur.owner) + "”" : ""}）` : ""}</b>
          ${cur.dm ? `<span>抖音号：${esc((who.user && who.user.douyin_id) || "还没读到")}</span>
          <span>私信列表里的名字：${esc(m.user_name)} · ${followText(who.user)}</span>` : `<span>只在评论区聊过，还没有私信</span>`}
        </div>
        ${who.user && who.user.url ? `<button type="button" class="btn btn-sm" data-act="open-video" data-url="${esc(who.user.url)}">打开主页</button>` : ""}
        <button type="button" class="btn btn-sm" data-act="mem-person" data-name="${esc(m.user_name)}">她对 TA 的记忆</button>
      </div>
      <div class="chat talk" id="talk-box">${talk}</div>
      <p class="hint" style="margin:0">下面的操作针对选中的这一条（${esc(m.created_at.slice(5, 16))}，<span class="pill ${cls}">${label}</span>）；点上面某一条的时间可以换一条。</p>
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
    <header class="page-head"><div><h1>消息中心</h1><p>谁 @ 了她、谁给她发了私信，她是怎么回的，都在这里。同一个人的私信和评论区消息合在一个框里。私信记录一直保留；评论区的记录，和这个人 ${S.keepDays || 5} 天没有新一轮对话就自动删除</p></div>
      <button type="button" class="btn" data-act="check-inbox">立即检查新消息</button></header>
    <div class="tabs" role="tablist" aria-label="筛选">${tabs}</div>
    <div class="row">
      <section class="card msg-list grow-2" aria-label="消息列表（每人一条）">${list}</section>
      <section class="card grow-3">${detail}</section>
    </div>`;
  const box = $("#talk-box"); if (box) box.scrollTop = box.scrollHeight;
}

/* ---------- 记忆 ---------- */
const MEM_KIND = { video: "看过的视频", dm: "私信", thread: "评论区", at: "被 @", post: "发作品", note: "你告诉她的" };
const IMPORTANCE = [[1, "1 · 小事"], [2, "2"], [3, "3 · 一般"], [4, "4"], [5, "5 · 重要（永远不忘）"]];

async function loadMemory() {
  S.mem.data = await call("get_memory", S.mem.tab, S.mem.q, S.mem.person);
}

function renderMemory() {
  const d = S.mem.data || {};
  const tabs = [["events", "具体的事"], ["people", "私信好友"], ["diary", "日记"]]
    .map(([k, l]) => `<button type="button" role="tab" aria-selected="${S.mem.tab === k}" data-act="mem-tab" data-t="${k}">${l}</button>`).join("");
  let body = "";
  if (S.mem.tab === "events") {
    const rows = (d.events || []).map((r) => `
      <tr>
        <td style="white-space:nowrap;color:var(--muted)">${esc(r.day.slice(5))}<br>${esc(MEM_KIND[r.kind] || r.kind)}</td>
        <td>${esc(r.summary)}${r.person ? `<br><small style="color:var(--muted)">和 ${esc(r.person)} 有关</small>` : ""}</td>
        <td style="white-space:nowrap">${r.kept ? `<span class="pill ok">不会忘</span>` : `<span class="pill ${r.now < 0.5 ? "warn" : "neutral"}">强度 ${r.now}</span>`}<br><small style="color:var(--muted)">想起 ${r.recalls} 次 · 半衰期 ${Math.round(r.half_life)} 天</small></td>
        <td><label class="sr-only" for="imp-${r.id}">重要程度</label><select id="imp-${r.id}" class="input mem-imp" data-id="${r.id}" style="min-width:150px">${IMPORTANCE.map(([v, l]) => `<option value="${v}" ${r.importance === v ? "selected" : ""}>${l}</option>`).join("")}</select></td>
        <td><button type="button" class="btn btn-sm btn-danger" data-act="mem-del" data-id="${r.id}">删除</button></td>
      </tr>`).join("");
    body = `
      <section class="card"><h2>${HEART}她记得的事${S.mem.person ? `（和 ${esc(S.mem.person)} 有关的）` : ""}</h2>
        <p class="hint" style="margin:0">一共 ${d.total || 0} 条。每条记忆的强度每 ${d.half_life || 30} 天减半；被想起一次，强度回升、半衰期变长。强度低于 ${d.forget_below || 0.25} 就会忘掉（细节删除，只留在当天的日记里）。重要程度设成 5 的、和主人有关的永远不忘。</p>
        <div class="actions">
          <label class="sr-only" for="mem-q">搜索记忆</label><input id="mem-q" class="input" style="max-width:260px" placeholder="搜一搜，比如 橘猫" value="${esc(S.mem.q)}">
          <button type="button" class="btn btn-sm" data-act="mem-search">搜索</button>
          ${S.mem.person || S.mem.q ? `<button type="button" class="btn btn-sm" data-act="mem-clear">看全部</button>` : ""}
        </div>
        <div class="actions"><label class="sr-only" for="mem-note">告诉她一件要记住的事</label><input id="mem-note" class="input" placeholder="告诉她一件要一直记住的事，比如：我喜欢橘猫，不吃香菜">
          <button type="button" class="btn btn-sm" data-act="mem-note">让她记住</button></div>
        ${rows ? `<div class="table-wrap"><table class="oss-table mem-table"><thead><tr><th>哪天</th><th>内容</th><th>记得多牢</th><th>重要程度</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="empty">还没有记忆。她刷视频、回私信之后，这里会慢慢多起来</p>`}
      </section>`;
  } else if (S.mem.tab === "people") {
    const cards = (d.people || []).map((p) => `
      <section class="card">
        <div class="actions" style="justify-content:space-between"><h2>${HEART}${esc(p.name)}${p.owner ? ` <span class="pill ok">主人</span>` : ""}</h2><span style="color:var(--muted)">聊过 ${p.chats} 次 · ${p.n} 条记忆 · 上次 ${esc((p.last_at || "").slice(5, 16))}</span></div>
        ${p.card ? `<p style="margin:0">${esc(p.card)}</p>` : `<p style="margin:0;color:var(--muted)">还没整理出印象（再聊几次会自动整理，也可以点“现在整理”）</p>`}
        <dl class="meta">
          <div><dt>说话风格</dt><dd>${esc(p.style || "—")}</dd></div>
          <div><dt>常聊的话题</dt><dd>${esc(p.topics || "—")}</dd></div>
          <div><dt>要记住的事</dt><dd>${esc(p.facts || "—")}</dd></div>
        </dl>
        <div class="actions">
          <button type="button" class="btn btn-sm" data-act="mem-person" data-name="${esc(p.name)}">看和 TA 有关的记忆</button>
          <button type="button" class="btn btn-sm" data-act="mem-card" data-name="${esc(p.name)}">现在整理印象</button>
          <button type="button" class="btn btn-sm btn-danger" data-act="mem-forget" data-name="${esc(p.name)}">忘掉这个人</button>
        </div>
      </section>`).join("");
    body = `<p class="hint" style="margin:0">只有和她私信聊过的人才有印象卡。评论区里的人不建卡：她只记那一串评论和那个视频的上下文，过几天就淡了。</p>
      ${cards || `<section class="card"><p class="empty">还没有私信好友</p></section>`}`;
  } else {
    const LV = { day: "日记", week: "周记", month: "月记" };
    const items = (d.diary || []).map((x) => `
      <section class="card">
        <div class="actions" style="justify-content:space-between"><h2>${HEART}${esc(x.period)} · ${LV[x.level] || x.level}${x.mood ? ` · ${esc(x.mood)}` : ""}</h2>
          <button type="button" class="btn btn-sm btn-danger" data-act="mem-diary-del" data-id="${x.id}">删除</button></div>
        <p style="margin:0;line-height:1.9;white-space:pre-wrap">${esc(x.content)}</p>
      </section>`).join("");
    body = `<p class="hint" style="margin:0">她每天写一篇大约 500 字的日记（同一件事只写一次）。过完的一周合并成周记，过完的一个月合并成月记；一个多月以前的日记只留周记，半年以前的只留月记。</p>
      ${items || `<section class="card"><p class="empty">还没有日记。过完一天（或者当天活跃时段结束后）她会自己写</p></section>`}`;
  }
  $("#main").innerHTML = `
    <header class="page-head"><div><h1>记忆</h1><p>她记得什么、对谁有什么印象、每天写了什么日记。你可以删掉某条记忆、调整它有多重要，或者让她忘掉某个人</p></div></header>
    <div class="tabs" role="tablist" aria-label="记忆分类">${tabs}</div>
    ${body}`;
  const q = $("#mem-q"); if (q) q.addEventListener("keydown", (e) => { if (e.key === "Enter") ACT["mem-search"](); });
}

/* ---------- 人设 ---------- */
const TRAITS = [
  ["energy", "性子", "安安静静", "元气满满"], ["sharp", "说话风格", "软萌温柔", "小嘴犀利"],
  ["talkative", "话多不多", "一两句", "一大段"], ["cute", "撒娇程度", "正经", "超可爱，带颜文字 (｡･ω･｡)ﾉ♡"],
  ["temper", "小脾气", "温顺好脾气", "动不动闹小脾气"], ["volatility", "情绪起伏", "情绪稳定", "喜怒都写在脸上"],
  ["humor", "幽默感", "一本正经", "爱玩梗抖机灵"], ["curious", "好奇心", "随缘不多问", "什么都想问"],
  ["confident", "自信", "害羞内向", "自信大方"], ["empathy", "共情", "理性冷静", "感性体贴"],
  ["clingy", "黏人", "独立", "黏人、想被关注"],
];
const CATCH_FREQ = [["often", "经常"], ["sometimes", "偶尔"], ["rare", "少"]];
const EMOTES = [["auto", "跟着撒娇程度走"], ["kaomoji", "颜文字 (≧▽≦)"], ["emoji", "emoji 表情"], ["none", "不用表情"]];
const MBTI = ["", "INFP", "ENFP", "INFJ", "ENFJ", "INTP", "ENTP", "INTJ", "ENTJ", "ISFP", "ESFP", "ISFJ", "ESFJ", "ISTP", "ESTP", "ISTJ", "ESTJ"];
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
            <small>显示在侧边栏。点“同步到抖音简介”会自动加上 AI 标识（只在主页简介里，她说话时不带这几个字）：${esc(p.douyin_bio_preview || "")}</small></div>
          <div class="actions"><button type="button" class="btn btn-sm" data-act="sync-sig">同步到抖音简介</button></div>
        </section>
        <section class="card"><h2>${HEART}基本信息</h2>
          <div class="field"><label for="p-name">昵称</label><input id="p-name" class="input" maxlength="20" value="${esc(p.name)}"></div>
          <div class="field"><label for="p-bio">一句话简介</label><input id="p-bio" class="input" value="${esc(p.bio)}"></div>
        </section>
        <section class="card"><h2>${HEART}性格</h2>
          <p class="hint" style="margin:0">先选一个性格底色，再拖滑块细调；兴趣、口头禅和自定义身份提示词也都可以随意改。只要这些设定还在，不管什么时候、在哪里和她说话，她都是同一个她。</p>
          <div class="field"><span class="label">性格底色（单选，再点一下取消）</span>
            <div class="chips">${(p.archetypes || []).map((a) => `<button type="button" class="chip" aria-pressed="${p.archetype === a.id}" data-act="archetype" data-v="${esc(a.id)}" title="${esc(a.desc)}">${esc(a.name)}</button>`).join("")}</div></div>
          <div class="grid">
            <div class="field"><label for="p-self">怎么称呼自己</label>
              <input id="p-self" class="input" maxlength="6" list="self-names" value="${esc(p.self_name || "我")}"><datalist id="self-names">${(p.self_names || []).map((n) => `<option value="${esc(n)}">`).join("")}</datalist></div>
            <div class="field"><label for="p-emote">表情习惯</label>
              <select id="p-emote" class="input">${EMOTES.map(([k, l]) => `<option value="${k}" ${(p.emote || "auto") === k ? "selected" : ""}>${l}</option>`).join("")}</select></div>
            <div class="field"><label for="p-mbti">MBTI（可以不选）</label>
              <select id="p-mbti" class="input">${MBTI.map((m) => `<option value="${m}" ${(p.mbti || "") === m ? "selected" : ""}>${m || "不选"}</option>`).join("")}</select></div>
          </div>
          <div class="grid">${traits}</div>
          <div class="field"><span class="label">她感兴趣的内容（刷到这些更愿意停下来看）</span><div class="chips">${chips}</div>
            <div class="actions"><input id="new-interest" class="input" style="max-width:220px" placeholder="自定义兴趣"><button type="button" class="btn btn-sm" data-act="add-interest">+ 添加</button></div></div>
          <div class="grid">
            <div class="field"><label for="p-catch">口头禅</label><input id="p-catch" class="input" value="${esc(p.catchphrases)}"></div>
            <div class="field"><label for="p-catchf">口头禅用得多不多</label>
              <select id="p-catchf" class="input">${CATCH_FREQ.map(([k, l]) => `<option value="${k}" ${(p.catch_freq || "sometimes") === k ? "selected" : ""}>${l}</option>`).join("")}</select></div>
            <div class="field"><label for="p-avoid">不喜欢 / 不评论的内容</label><input id="p-avoid" class="input" value="${esc(p.avoid)}"></div>
          </div>
          <div class="field"><label for="p-extra">自定义身份提示词</label><textarea id="p-extra" class="input" rows="5" maxlength="1500" placeholder="给她再加一些更鲜明的特点，比如：说话爱用叠词；看到猫就走不动道；讨厌香菜；喜欢先夸一个具体的小细节再提问……">${esc(p.extra)}</textarea>
            <small>这段话会原样写进她的身份里，每次说话都带着。写得越具体，她的个性越鲜明。</small></div>
        </section>
        <section class="card" style="flex-direction:row;align-items:flex-start;gap:14px">
          <input type="checkbox" checked disabled id="ai-flag" style="margin-top:2px">
          <div><label for="ai-flag" style="font-weight:700">公开 AI 身份（固定开启）</label>
          <p style="margin:6px 0 0;color:var(--muted);line-height:1.7">她平时说话不会把“AI虚拟人”挂在嘴边；但有人认真问“你是不是真人”，她会如实回答自己是 AI；同步到抖音的简介会带上 AI 标识。这是抖音规则和国家 AI 内容标识的要求，关不掉哦。</p></div>
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
    archetype: p.archetype || "", self_name: $("#p-self").value.trim() || "我", emote: $("#p-emote").value,
    mbti: $("#p-mbti").value, catch_freq: $("#p-catchf").value,
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
          <p class="hint" style="margin:0">点不点赞、评不评论、收不收藏，由她按自己的性格、兴趣和记忆判断（人设页里调）；这里的每日上限会按活跃时段匀着用。同一个视频她只评论一次。</p>
          <label class="toggle-row"><span>听视频里的声音（把说的话、唱的词转成文字一起判断；浏览器是静音播放的，不会吵到你）<span class="hint" style="display:block;margin:2px 0 0">${listenHint(r)}</span></span><input id="r-listen" type="checkbox" ${r.listen_enabled !== false ? "checked" : ""}></label>
          <div class="toggle-row"><label for="r-lmodel">语音模型</label>
            <select id="r-lmodel" class="input" style="max-width:260px">${[["tiny", "tiny：最快（约 75MB）"], ["base", "base：推荐（约 145MB）"], ["small", "small：最准、最慢（约 480MB）"]].map(([k, v]) => `<option value="${k}" ${(r.listen_model || "base") === k ? "selected" : ""}>${v}</option>`).join("")}</select></div>
          <label class="toggle-row"><span>刷到广告直接划走（不看、不点赞、不评论，也不花模型的钱）</span><input id="r-ads" type="checkbox" ${r.skip_ads !== false ? "checked" : ""}></label>
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
          <p class="hint">回到上限时，最后一句她会自然地收个尾（比如“聊得好开心～”），之后这串对话就不再回了。评论区里她不记人，只记这一串评论和这个视频的上下文。</p>
          <div class="toggle-row"><label for="r-per">同一个人每天最多回复</label><input id="r-per" type="number" min="1" class="input" style="max-width:120px" value="${r.per_user_daily}"></div>
        </section>
        ${friendsSection(r)}
        ${ownersSection(r)}
      </div>
      <div class="col grow-1">
        <section class="card"><h2>${HEART}敏感话题</h2>
          <label class="toggle-row"><span>聊到敏感话题时，直接告诉对方为什么不聊<span class="hint" style="display:block;margin:2px 0 0">开：对方的话里出现下面选中话题的关键词，她直接回一句“这个话题涉及××，我不方便聊”，不走模型。关：照常让她回复（发出去之前的安全检查还在）。对主人不做这一步。</span></span><input id="r-sg" type="checkbox" ${r.sensitive_guard !== false ? "checked" : ""}></label>
          <div class="chips">${topics}</div>
          <p class="hint" style="margin:0">点一下可以开关某个话题，鼠标停留能看到关键词。</p>
          <div class="field"><label for="r-kw">自定义屏蔽词（用逗号分隔；命中就不回复）</label><textarea id="r-kw" class="input" rows="2">${esc((r.keywords || []).join(", "))}</textarea></div>
          <fieldset style="margin:0;padding:0;border:0;display:flex;flex-direction:column;gap:8px"><legend class="label" style="font-size:13px;color:var(--sub);margin-bottom:8px">命中屏蔽词、或者她写的回复没通过安全检查时</legend>
            <label style="display:flex;gap:10px;align-items:center"><input type="radio" name="hit" value="pending" ${r.on_hit === "pending" ? "checked" : ""}>不回复，放进“待你处理”</label>
            <label style="display:flex;gap:10px;align-items:center"><input type="radio" name="hit" value="log" ${r.on_hit === "log" ? "checked" : ""}>不回复，只记在日志里</label>
          </fieldset>
        </section>
        ${autoReplySection(r)}
        ${emoteSection(r)}
        ${publishSection(r)}
        ${voiceSection(r)}
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
    auto_reply_at: $("#r-at").checked, auto_reply_dm: $("#r-dm").checked, engage_comments: $("#r-cm").checked, collect_enabled: $("#r-col").checked, skip_ads: $("#r-ads").checked, listen_enabled: $("#r-listen").checked, listen_model: $("#r-lmodel").value, dm_scope: $("#r-scope").value,
    share_watch_minutes: Math.max(0.5, Number($("#r-watch").value) || 3), share_frames: Math.max(0, Math.min(12, num("#r-sframes"))),
    per_user_daily: num("#r-per"), topics: r.topics,
    keywords: $("#r-kw").value.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean),
    on_hit: (document.querySelector('input[name="hit"]:checked') || {}).value || "pending",
    sensitive_guard: $("#r-sg").checked,
    auto_replies: $$(".ar-row").map((row) => ({ keys: row.querySelector(".ar-keys").value.trim(), reply: row.querySelector(".ar-reply").value.trim() })),
    owner_enabled: $("#r-own").checked, owner_share_per_day: Math.max(0, num("#r-ownshare") || 0),
    douyin_emoji: $("#r-dyemoji").checked, sticker_dm: $("#r-stdm").checked, sticker_comment: $("#r-stcm").checked,
    sticker_per_day: Math.max(0, num("#r-stday") || 0),
    ...collectVoice(),
    owners: $$(".own-row").map((row) => ({
      name: row.dataset.name || "", call: row.querySelector(".own-call").value.trim(),
      relation: row.querySelector(".own-rel").value.trim(), enabled: row.querySelector(".own-on").checked,
      chat: row.querySelector(".own-chat").checked, share: row.querySelector(".own-share").checked,
      closeness: Number(row.dataset.close || 0),
    })),
  };
}

function listenHint(r) {
  const st = r.listen_status || {};
  if (st.broken) return "现在用不了：" + esc(st.broken);
  if (st.installed === false) return "还没装听声音的组件（faster-whisper），装好之前她只按画面和文案判断";
  if (!(st.models || []).length) return "第一次用时会下载语音模型，之后离线可用";
  const own = st.bundled || [];
  return "可以直接用的语音模型：" + (st.models || []).map((m) => esc(m) + (own.includes(m) ? "（软件自带）" : "（已下载）")).join("、")
    + "。换成别的模型时，第一次用会先下载";
}

/* 表情：抖音自带的小表情 + 她自己的表情包（用桌宠形象画的） */
function emoteSection(r) {
  return `<section class="card"><h2>${HEART}表情和表情包</h2>
    <label class="toggle-row"><span>说话时偶尔带抖音自带的小表情<span class="hint" style="display:block;margin:2px 0 0">比如 [比心]、[捂脸]，发出去会显示成表情。人设里“表情习惯”选了“不用表情”时不会带。</span></span><input id="r-dyemoji" type="checkbox" ${r.douyin_emoji !== false ? "checked" : ""}></label>
    <label class="toggle-row"><span>私信里发她自己的表情包<span class="hint" style="display:block;margin:2px 0 0">用桌宠现在的形象画的会动的小图，配字是她按聊天内容自己想的。回完文字后再发，大约四五条里配一次。</span></span><input id="r-stdm" type="checkbox" ${r.sticker_dm !== false ? "checked" : ""}></label>
    <label class="toggle-row"><span>评论时也可以配一张表情包<span class="hint" style="display:block;margin:2px 0 0">默认关。评论里的图是不会动的。</span></span><input id="r-stcm" type="checkbox" ${r.sticker_comment ? "checked" : ""}></label>
    <div class="toggle-row"><label for="r-stday">每天最多发几张表情包</label><input id="r-stday" type="number" min="0" max="100" class="input" style="max-width:120px" value="${r.sticker_per_day ?? 10}"></div>
    <div class="actions"><button type="button" class="btn btn-sm" data-act="sticker-sheet">看看她的表情包</button>
      <label class="sr-only" for="st-mood">情绪</label><select id="st-mood" class="input" style="max-width:120px">${(r.sticker_moods || []).map((m) => `<option>${esc(m)}</option>`).join("")}</select>
      <label class="sr-only" for="st-text">配字</label><input id="st-text" class="input" style="max-width:140px" maxlength="8" placeholder="配字，比如 冲鸭">
      <button type="button" class="btn btn-sm" data-act="sticker-one">画一张试试</button></div>
    <div id="sticker-box"></div></section>`;
}

/* 她的声音：BreezeBlue 语音合成（发作品时读文案）：填 Key、填音色 ID、试听 */
function voiceSection(r) {
  return `<section class="card"><h2>${HEART}她的声音</h2>
    <p class="hint">用 BreezeBlue（Breeze TTS 2）把作品的文案读出来，和文字卡片合成一个视频发布（在“作品”页点“用她的声音配音”，可以先试听）。到 breezeblue.ai 注册、在控制台里拿 API Key 和音色 ID（voc_ 开头）：可以用官方音色，也可以在那边用文字描述设计一个只属于她的音色。合成的声音是 AI 生成的，发布时照样声明“内容由 AI 生成”；不要克隆别人的声音。${r.voice_ready ? "" : `<b style="color:var(--warn-text)"> 现在还没设置好：要填 Key 和音色 ID。</b>`}</p>
    <div class="field"><label for="v-key">BreezeBlue API Key ${r.voice_has_key ? `<span class="pill ok">已保存</span>` : `<span class="pill warn">还没填</span>`}</label>
      <div class="actions"><input id="v-key" type="password" class="input" autocomplete="off" placeholder="${r.voice_has_key ? "已经保存了；要换就填新的" : "粘贴 Key，然后点保存 Key"}">
        <button type="button" class="btn btn-sm" data-act="voice-key">保存 Key</button>
        ${r.voice_has_key ? `<button type="button" class="btn btn-sm btn-danger" data-act="voice-key-clear">清掉</button>` : ""}</div>
      <span class="hint">Key 用 Windows 加密后存在这台电脑上，不会显示出来，也不会发到别处。</span></div>
    <div class="field"><label for="v-name">音色 ID</label>
      <div class="actions"><input id="v-name" class="input" value="${esc(r.voice_name || "")}" placeholder="voc_xxxxxxxxxxxx">
        <button type="button" class="btn btn-sm" data-act="voice-list" data-kind="personal">读我账号里的音色</button>
        <button type="button" class="btn btn-sm" data-act="voice-list" data-kind="default">看官方中文女声</button></div>
      <div id="voice-list-box"></div></div>
    <div class="field"><label for="v-ins">表演指令（可不填）：用一句话告诉她怎么读</label><input id="v-ins" class="input" maxlength="300" value="${esc(r.voice_instruction || "")}" placeholder="比如：用轻快、带一点撒娇的语气读，像在跟朋友分享"></div>
    <div class="grid">
      <div class="field"><label for="v-rate">语速（0.5～2，1 是正常）</label><input id="v-rate" type="number" step="0.05" min="0.5" max="2" class="input" value="${r.voice_rate ?? 1}"></div>
      <div class="field"><label for="v-vol">音量（1～200，100 是正常）</label><input id="v-vol" type="number" min="1" max="200" class="input" value="${r.voice_volume ?? 100}"></div>
      <div class="field"><label for="v-guide">多听指令的话（1～10，写了表演指令才有用）</label><input id="v-guide" type="number" step="0.5" min="1" max="10" class="input" value="${r.voice_guidance ?? 3}"></div>
      <div class="field"><label for="v-price">价格（元 / 万字，估算花费用）</label><input id="v-price" type="number" step="0.5" min="0" class="input" value="${r.voice_price ?? 5}"></div>
    </div>
    <details class="d-cards-edit"><summary>连不上或要指定模型时再看这里</summary>
      <div class="field"><label for="v-proxy">代理（可不填；连不上 BreezeBlue 时填，比如 http://127.0.0.1:7890）</label><input id="v-proxy" class="input" value="${esc(r.voice_proxy || "")}"></div>
      <div class="field"><label for="v-model">模型（留空自动选；中文用 breeze-tts-2）</label><input id="v-model" class="input" value="${esc(r.voice_model || "")}" placeholder="留空"></div>
      <div class="field"><label for="v-base">接口地址（留空 = https://api.breeze.blue）</label><input id="v-base" class="input" value="${esc(r.voice_base || "")}"></div></details>
    <div class="toggle-row"><label for="v-form">她自己写作品时用哪种形式<span class="hint" style="display:block;margin:2px 0 0">配音图片视频：文案写在一张带桌宠的图上，写完自动配好音。图文卡片：封面 + 文字卡片。每个作品里还可以再换。</span></label>
      <select id="v-form" class="input" style="max-width:200px"><option value="voice" ${(r.post_form || "voice") !== "cards" ? "selected" : ""}>配音图片视频（默认）</option><option value="cards" ${r.post_form === "cards" ? "selected" : ""}>图文卡片</option></select></div>
    <label class="toggle-row"><span>图文卡片形式的作品也自动配音<span class="hint" style="display:block;margin:2px 0 0">关着也可以在“作品”页手动点“用她的声音配音”。配好的视频要你确认后才会发布。</span></span><input id="v-auto" type="checkbox" ${r.voice_auto ? "checked" : ""}></label>
    <div class="actions"><label class="sr-only" for="v-say">试听的话</label><input id="v-say" class="input" placeholder="输入一句话试听（不填就读一句自我介绍）">
      <button type="button" class="btn btn-sm" data-act="voice-test">试听</button></div>
    <div id="voice-box"></div>
  </section>`;
}

function collectVoice() {
  const g = (id) => $(id);
  return {
    voice_name: g("#v-name").value.trim(), voice_instruction: g("#v-ins").value.trim(),
    voice_rate: Math.min(2, Math.max(0.5, Number(g("#v-rate").value) || 1)),
    voice_volume: Math.min(200, Math.max(1, Number(g("#v-vol").value) || 100)),
    voice_guidance: Math.min(10, Math.max(1, Number(g("#v-guide").value) || 3)),
    voice_price: Math.max(0, Number(g("#v-price").value) || 0),
    voice_proxy: g("#v-proxy").value.trim(), voice_model: g("#v-model").value.trim(), voice_base: g("#v-base").value.trim(),
    voice_auto: g("#v-auto").checked, post_form: g("#v-form").value,
  };
}

/* 关键字自动回复：对方的话里出现关键字，直接发你写好的话，不走模型 */
function autoReplySection(r) {
  const rows = (r.auto_replies || []).map((a, i) => `
    <div class="ar-row list-row">
      <div class="field"><label for="ar-k-${i}">关键字（多个用逗号分开）</label><input id="ar-k-${i}" class="input ar-keys" value="${esc(a.keys || "")}" placeholder="比如：价格，多少钱"></div>
      <div class="field"><label for="ar-r-${i}">自动回复的话</label><textarea id="ar-r-${i}" class="input ar-reply" rows="2" maxlength="300">${esc(a.reply || "")}</textarea></div>
      <button type="button" class="btn btn-sm btn-danger" data-act="ar-del" data-i="${i}">删掉这条</button>
    </div>`).join("");
  return `<section class="card"><h2>${HEART}关键字自动回复</h2>
    <p class="hint" style="margin:0">别人在私信、@ 她或回复她的评论里说到你设的关键字，她直接发你写好的话，不经过模型（不花钱，也最快）。可以设很多条，从上往下第一条命中的生效。</p>
    ${rows || `<p class="empty">还没有设置</p>`}
    <div class="actions"><button type="button" class="btn btn-sm" data-act="ar-add">+ 加一条</button></div></section>`;
}

/* 主人：她会主动找主人聊天、分享视频；和主人聊天不设上限，记忆不会忘 */
/* 主人：只能从互关好友里选（从抖音“关注”列表里核实过的；手动添加的好友不算） */
function ownerChoices(r) {
  const fr = (S.friends && S.friends.friends) || null;
  return fr ? fr.filter((f) => !f.manual && f.nickname) : (r.owner_choices || []).map((n) => ({ nickname: n }));
}
function ownersSection(r) {
  const close = (v) => (v >= 80 ? "非常亲密" : v >= 50 ? "很亲近" : v >= 20 ? "熟悉了" : "刚认识");
  const all = ownerChoices(r);
  const byName = Object.fromEntries(all.map((f) => [f.nickname, f]));
  const fetch = (S.friends && S.friends.fetch) || {};
  const taken = new Set((r.owners || []).map((o) => o.name));
  const free = all.filter((f) => !taken.has(f.nickname));
  const rows = (r.owners || []).map((o, i) => {
    const f = byName[o.name];
    return `
    <div class="own-row list-row" data-close="${Number(o.closeness || 0)}" data-name="${esc(o.name || "")}">
      <div class="own-head">
        ${f && f.avatar ? `<img class="own-avatar" src="${esc(f.avatar)}" alt="" referrerpolicy="no-referrer">` : `<span class="own-avatar letter">${esc((o.name || "?").slice(0, 1))}</span>`}
        <span class="own-who"><b>${esc(o.name || "")}</b>${f && f.douyin_id ? `<small>抖音号 ${esc(f.douyin_id)}</small>` : ""}</span>
        ${f ? `<span class="pill ok">互关好友</span>` : `<span class="pill warn">互关名单里没有 TA</span>`}
        <span class="pill neutral">亲密度 ${Math.round(Number(o.closeness || 0))} · ${close(Number(o.closeness || 0))}</span>
      </div>
      ${f ? "" : `<p class="hint" style="margin:0">互关好友名单里没找到这个名字：可能还没刷新名单、TA 改了昵称，或者已经不是互关了。点下面的“刷新好友列表”再看看；确认不是互关的话请移除。</p>`}
      <div class="grid">
        <div class="field"><label for="own-c-${i}">她怎么称呼 TA</label><input id="own-c-${i}" class="input own-call" maxlength="20" value="${esc(o.call || "")}" placeholder="比如：主人、哥哥、小廖"></div>
        <div class="field"><label for="own-r-${i}">她和 TA 是什么关系</label><textarea id="own-r-${i}" class="input own-rel" rows="2" maxlength="200" placeholder="比如：TA 是把我带到这个世界的人，我最信任 TA，什么都想第一个告诉 TA">${esc(o.relation || "")}</textarea></div>
      </div>
      <div class="actions">
        <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" class="own-on" ${o.enabled !== 0 && o.enabled !== false ? "checked" : ""}>启用</label>
        <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" class="own-chat" ${o.chat !== 0 && o.chat !== false ? "checked" : ""}>她可以主动找 TA 聊天</label>
        <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" class="own-share" ${o.share !== 0 && o.share !== false ? "checked" : ""}>主动分享视频</label>
        <button type="button" class="btn btn-sm btn-danger" data-act="own-del" data-i="${i}">移除</button>
      </div>
    </div>`;
  }).join("");
  const picker = all.length
    ? (free.length
      ? `<select id="own-pick" class="input" style="max-width:260px" aria-label="从互关好友里选一位">${free.map((f) => `<option value="${esc(f.nickname)}">${esc(f.nickname)}${f.douyin_id ? `（抖音号 ${esc(f.douyin_id)}）` : ""}</option>`).join("")}</select>
         <button type="button" class="btn btn-sm btn-primary" data-act="own-add">设为主人</button>`
      : `<span class="hint" style="margin:0">互关好友都已经是主人了</span>`)
    : `<span class="hint" style="margin:0">${S.frLoading ? "正在从你的抖音主页读关注列表…" : "还没有互关好友名单，先点“刷新好友列表”"}</span>`;
  return `<section class="card"><h2>${HEART}主人</h2>
    <label class="toggle-row"><span>开启主人功能<span class="hint" style="display:block;margin:2px 0 0">只有和她互相关注的好友才能设为主人。主人的私信和别人不一样：随时都回（今天的私信额度用完了、不在活跃时段也回），想聊多久聊多久；她还会按自己的心情主动找主人聊天——想撒娇、开心、无聊的时候找得勤，犯困、闹小脾气的时候找得少，不用设次数。和主人有关的记忆不会忘，聊得越多越亲近。请只添加你自己的账号，或者同意被她主动联系的朋友。</span></span><input id="r-own" type="checkbox" ${r.owner_enabled !== false ? "checked" : ""}></label>
    <div class="toggle-row"><label for="r-ownshare">每天最多主动分享视频（个）</label><input id="r-ownshare" type="number" min="0" class="input" style="max-width:120px" value="${r.owner_share_per_day ?? 2}"></div>
    ${rows || `<p class="empty">还没有设置主人</p>`}
    <div class="actions">
      ${picker}
      <button type="button" class="btn btn-sm" data-act="fr-fetch" ${S.frLoading ? "disabled" : ""}>${S.frLoading ? "正在读取…" : "刷新好友列表"}</button>
    </div>
    <p class="hint" style="margin:0">${fetch.at ? `好友列表上次读取：${esc(fetch.at)}，互关 ${fetch.count || 0} 人${fetch.error ? `（${esc(fetch.error)}）` : ""}。` : "好友列表从她抖音主页的“关注”里读，只列出互相关注的人。"}主人要先给她发过一条私信（她才能在私信列表里找到 TA）。她连着找了主人几次都没回音，会先不打扰、隔上大半天再问候。被问到是不是真人时，对主人也会如实说自己是 AI。</p></section>`;
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
        ${!m.exists ? `<div class="d-video">⚠<span>文件不见了</span></div>`
          : m.kind === "video" ? `<button type="button" class="d-open d-video" data-act="d-view" data-name="${esc(m.name)}" aria-label="播放视频 ${esc(m.file)}"><span class="d-play" aria-hidden="true">▶</span><span>${esc(m.file)}</span><span>点一下播放</span></button>`
          : m.thumb ? `<button type="button" class="d-open" data-act="d-view" data-name="${esc(m.name)}" aria-label="放大看图片 ${i + 1}"><img src="${m.thumb}" alt="图片 ${i + 1}"><span class="d-zoom" aria-hidden="true">🔍 点开放大</span></button>`
          : `<div class="d-video">⚠<span>图片打不开</span></div>`}
        <span class="d-src">${{ card: "文字卡片", ai: "AI 生成", upload: "你上传的", voice: "配音视频", voicecard: "配音图片" }[m.source] || ""}</span>
        ${locked ? "" : `<div class="d-ops">
          <button type="button" class="btn btn-sm" data-act="d-move" data-name="${esc(m.name)}" data-dir="-1" ${i === 0 ? "disabled" : ""} aria-label="往前挪">‹</button>
          <button type="button" class="btn btn-sm" data-act="d-move" data-name="${esc(m.name)}" data-dir="1" ${i === d.media_view.length - 1 ? "disabled" : ""} aria-label="往后挪">›</button>
          <button type="button" class="btn btn-sm btn-danger" data-act="d-del" data-name="${esc(m.name)}" aria-label="删除">✕</button></div>`}
      </div>`).join("") || `<p class="empty">还没有图片或视频</p>`;
    // 一张卡片一段；只有一句话的卡片就写一行；有好几句但没有小标题的，第一行写“-”
    const cardsText = (d.cards || []).map((c) => {
      const lines = c.lines || [];
      if (!c.title && lines.length === 1) return lines[0];
      return [c.title || "-", ...lines].join("\n");
    }).join("\n\n");
    const form = d.form || "cards";
    editor = `
      <div class="actions" style="justify-content:space-between"><h2>${HEART}${d.status === "wish" && d.source === "auto" ? "她想发的作品" : "作品草稿"}</h2><span class="pill ${cls}">${label}</span></div>
      ${d.reason ? `<div class="me">${avatar(36)}<div class="bubble">${esc(d.reason)}</div></div>` : ""}
      ${d.error ? `<div class="alert"><span>${esc(d.error)}</span></div>` : ""}
      <div class="field"><label for="d-title">标题（20 字以内最好）</label><input id="d-title" class="input" maxlength="30" value="${esc(d.title)}" ${locked ? "disabled" : ""}></div>
      <div class="field"><label for="d-text">正文</label><textarea id="d-text" class="input" rows="5" ${locked ? "disabled" : ""}>${esc(d.text)}</textarea></div>
      <div class="field"><label for="d-tags">话题（空格分隔，最多 5 个，不用写 #）</label><input id="d-tags" class="input" value="${esc((d.tags || []).join(" "))}" ${locked ? "disabled" : ""}></div>
      <div class="field"><span class="label">图片 / 视频（按顺序发布；视频作品只能放 1 个视频）。点图片可以放大看，点视频可以播放</span><div class="d-grid">${media}</div></div>
      ${(d.media_view || []).some((m) => m.exists) ? `<div class="actions">
        <button type="button" class="btn btn-sm" data-act="d-view-all">▶ 从头预览全部</button>
        ${(d.media_view || []).some((m) => m.audio_url) ? `<button type="button" class="btn btn-sm" data-act="d-voice-play">🔊 听她的配音</button>` : ""}
      </div>` : ""}
      ${locked ? `<p class="hint" style="margin:0">作品形式：${FORM_NAME[form]}</p>` : `
      <div class="field"><span class="label">作品形式（换形式时，上一种形式自动生成的图和配音会去掉，你上传的不动）</span>
        <div class="seg" role="radiogroup" aria-label="作品形式">
          ${["voice", "cards", "upload"].map((f) => `<button type="button" class="chip" role="radio" aria-checked="${form === f}" aria-pressed="${form === f}" data-act="d-form" data-form="${f}">${FORM_NAME[f]}</button>`).join("")}
        </div>
        <p class="hint" style="margin:6px 0 0">${FORM_HINT[form]}</p></div>
      ${form === "voice" ? `<div class="actions">
        <button type="button" class="btn btn-sm btn-primary" data-act="d-voice" ${d.can_voice ? "" : "disabled"} title="${d.can_voice ? "" : "先到“规则与预算 → 她的声音”里填好 Key 和音色 ID"}">${d.has_voice ? "重新生成图片并配音" : "生成图片并配音"}</button>
        ${d.can_voice ? "" : `<span class="hint" style="margin:0">还没设置她的声音，现在只有图片：到“规则与预算 → 她的声音”里填好 Key 和音色 ID 就能配音。</span>`}
      </div>
      <p class="hint" style="margin:0">${d.has_voice ? "已经配好音了：发布时发的是“配音视频”（一张图 + 她读文案的声音，画面不翻页）。" : "发布前点上面的按钮配音；不配音的话发出去的是这一张图。"}改了标题或正文，图会马上按新文案重画，配音需要重新生成；每次重新生成，桌宠会换一个动作。</p>
      ${bgSection(d)}` : form === "cards" ? `<div class="actions">
        <button type="button" class="btn btn-sm" data-act="d-cards">重新生成文字卡片</button>
        <button type="button" class="btn btn-sm" data-act="d-voice" ${d.can_voice ? "" : "disabled"} title="${d.can_voice ? "" : "先到“规则与预算 → 她的声音”里填好 Key 和音色 ID"}">${d.has_voice ? "重新配音" : "用她的声音配音"}</button>
        <button type="button" class="btn btn-sm" data-act="d-files">从电脑添加图片 / 视频</button>
        ${d.can_ai_image ? `<input id="d-prompt" class="input" style="max-width:240px" placeholder="AI 配图的画面描述" value="${esc(d.image_prompt || "")}"><button type="button" class="btn btn-sm" data-act="d-ai">AI 生成一张配图</button>` : `<span class="hint" style="margin:0">想用 AI 生图？去“规则与预算 → 发作品”配置</span>`}
      </div>
      <p class="hint" style="margin:0">“重新生成文字卡片”会按正文输入框里现在的全部内容重新分卡片（正文里每一句都会在卡片上）。${d.has_voice ? "这个草稿已经配好音了：发布时只发“配音视频”（文字卡片已经在视频里）；不想发视频就把配音视频删掉。改了标题或正文后配音会自动去掉，需要重新配。" : ""}</p>
      ${bgSection(d)}
      <details class="d-cards-edit"><summary>想自己安排每张卡片写什么（可选）</summary>
        <p class="hint">每张卡片一段，段与段之间空一行，最多 9 张，想写几张写几张。一段只有一行，就是一句话的卡片；一段有好几行时，第一行是小标题、后面每行一句（最多 8 句），不想要小标题就在第一行写一个“-”。字多了会自动缩小。改完点“重新生成文字卡片”。</p>
        <textarea id="d-cardtext" class="input" rows="6">${esc(cardsText)}</textarea>
        <div class="actions" style="margin-top:8px"><button type="button" class="btn btn-sm" data-act="d-cards-custom">按上面写的重画卡片</button></div></details>` : `<div class="actions">
        <button type="button" class="btn btn-sm btn-primary" data-act="d-files">从电脑添加图片 / 视频</button>
        ${d.can_ai_image ? `<input id="d-prompt" class="input" style="max-width:240px" placeholder="AI 配图的画面描述" value="${esc(d.image_prompt || "")}"><button type="button" class="btn btn-sm" data-act="d-ai">AI 生成一张配图</button>` : `<span class="hint" style="margin:0">想用 AI 生图？去“规则与预算 → 发作品”配置</span>`}
      </div>`}`}
      <div class="decl"><input type="checkbox" checked disabled id="d-ai-decl"><label for="d-ai-decl">发布时自动勾选“内容由AI生成”声明（固定开启）</label></div>
      <div class="actions">
        ${locked ? (d.status === "publishing" ? `<span class="pill brand">${d.publish_step ? `正在发布：${esc(d.publish_step)}…` : "排好队了：她做完手上这一小步就去发（一般 1 分钟内）"}</span>`
          : d.status === "discarded" ? `<span class="hint" style="margin:0">这个作品已经不发了，留着只是个记录。</span><button type="button" class="btn btn-danger" data-act="d-remove">删除这条记录</button>` : "") : `
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

/* 预览：图片放大看、视频和配音直接播放。items: [{ type: "image"|"video"|"audio", url, label, name }]
   ← → 换上一个 / 下一个，Esc 或点空白处关闭。文件由软件在本机开的小服务提供（只有这台电脑能访问）。 */
function previewItems(d) {
  const SRC = { card: "文字卡片", ai: "AI 生成的配图", upload: "你上传的", voice: "配音视频", voicecard: "配音图片" };
  const out = [];
  (d.media_view || []).forEach((m, i) => {
    if (!m.exists) return;
    out.push({ type: m.kind === "video" ? "video" : "image", url: m.url, thumb: m.thumb, name: m.name, i,
               label: `${SRC[m.source] || (m.kind === "video" ? "视频" : "图片")} · 第 ${i + 1} 个` });
    if (m.audio_url) out.push({ type: "audio", url: m.audio_url, name: m.audio, i, label: "她的配音（只听声音）" });
  });
  return out;
}
function lightbox(items, at = 0) {
  if (!items.length) return;
  let idx = Math.max(0, Math.min(at, items.length - 1));
  const wrap = document.createElement("div");
  wrap.className = "lightbox";
  wrap.setAttribute("role", "dialog");
  wrap.setAttribute("aria-modal", "true");
  wrap.setAttribute("aria-label", "预览");
  const stop = () => wrap.querySelectorAll("video, audio").forEach((x) => { try { x.pause(); } catch (e) { /* 已经停了 */ } });
  const close = () => { stop(); document.removeEventListener("keydown", onKey); wrap.remove(); };
  const draw = () => {
    stop();
    const it = items[idx];
    const sys = `<button type="button" class="btn btn-sm" data-lb="sys">用电脑上的播放器打开</button>`;
    let body;
    if (!it.url && it.type !== "image") body = `<div class="lb-msg">软件里暂时播放不了这个文件。${sys}</div>`;
    else if (it.type === "video") body = `<video controls autoplay playsinline src="${esc(it.url)}"></video>`;
    else if (it.type === "audio") body = `<div class="lb-audio"><div class="lb-note" aria-hidden="true">🔊</div><audio controls autoplay src="${esc(it.url)}"></audio></div>`;
    else body = `<img src="${esc(it.url || it.thumb)}" alt="${esc(it.label)}">`;
    wrap.innerHTML = `
      <div class="lb-top"><span>${esc(it.label)}${items.length > 1 ? `（${idx + 1} / ${items.length}）` : ""}</span>
        <span class="actions">${it.type === "image" ? "" : sys}<button type="button" class="btn btn-sm" data-lb="close" aria-label="关闭预览">关闭 ✕</button></span></div>
      <div class="lb-stage">
        ${items.length > 1 ? `<button type="button" class="lb-nav" data-lb="prev" aria-label="上一个" ${idx === 0 ? "disabled" : ""}>‹</button>` : ""}
        <div class="lb-body">${body}</div>
        ${items.length > 1 ? `<button type="button" class="lb-nav" data-lb="next" aria-label="下一个" ${idx === items.length - 1 ? "disabled" : ""}>›</button>` : ""}
      </div>`;
    const media = wrap.querySelector("video, audio");
    if (media) media.addEventListener("error", () => {
      wrap.querySelector(".lb-body").innerHTML = `<div class="lb-msg">这个文件的格式软件里播放不了。${sys}</div>`;
    });
    const img = wrap.querySelector(".lb-body img");
    if (img && it.url && it.thumb) img.addEventListener("error", () => { img.src = it.thumb; }, { once: true });
    (wrap.querySelector('[data-lb="close"]') || wrap).focus();
  };
  const go = (step) => { const n = idx + step; if (n >= 0 && n < items.length) { idx = n; draw(); } };
  const onKey = (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "ArrowRight") go(1);
  };
  wrap.addEventListener("click", (e) => {
    const b = e.target.closest("[data-lb]");
    if (b) {
      const what = b.dataset.lb;
      if (what === "close") close();
      else if (what === "prev") go(-1);
      else if (what === "next") go(1);
      else if (what === "sys") call("open_draft_media", S.selDraft, items[idx].name).catch(() => {});
    } else if (e.target === wrap || e.target.classList.contains("lb-stage") || e.target.classList.contains("lb-body")) close();
  });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(wrap);
  draw();
}

/* 文字卡片的背景：内置粉色样式 / 背景图库里的图 / 上传新的；文字写在白卡片上还是直接写在图上 */
function bgSection(d) {
  const cur = d.card_bg || d.bg_default || "none";   // 这个草稿实际用的背景
  const panel = d.card_bg ? d.card_panel !== 0 : d.panel_default;
  const isDefault = (cur === "none" ? "" : cur) === (d.bg_default || "");
  const tiles = [`<button type="button" class="bg-tile builtin${cur === "none" ? " on" : ""}" data-act="d-bg" data-name="none" aria-pressed="${cur === "none"}"><span>内置粉色</span></button>`]
    .concat((d.backgrounds || []).map((b) => `
      <div class="bg-wrap"><button type="button" class="bg-tile${cur === b.name ? " on" : ""}" data-act="d-bg" data-name="${esc(b.name)}" aria-pressed="${cur === b.name}" aria-label="用这张背景：${esc(b.label || "")}" title="${esc(b.label || "")}"><img src="${b.thumb}" alt=""></button>
        <button type="button" class="bg-x" data-act="d-bg-del" data-name="${esc(b.name)}" aria-label="从背景图库删掉">✕</button></div>`)).join("");
  return `
      <div class="field bg-field"><span class="label">图片的背景（选好马上重画）</span>
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

/* 作品的三种形式 */
const FORM_NAME = { voice: "配音图片视频", cards: "图文卡片", upload: "我上传的图片 / 视频" };
const FORM_HINT = {
  voice: "标题和正文全部写在一张图上，图上站着桌宠（每次随机一个动作和表情），她用自己的声音把文案读出来，做成一个画面不动的视频。",
  cards: "一张封面 + 几张文字卡片，当图文发；也可以点“用她的声音配音”做成一张张翻的视频。",
  upload: "只发你从电脑上添加的图片或视频（视频作品只能放 1 个视频），标题、正文、话题照常填。",
};

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
      <div class="table-wrap"><table class="oss-table"><thead><tr><th>名称</th><th>在软件里做什么</th><th>许可证</th></tr></thead><tbody>
        ${((S.about && S.about.oss) || []).map((o) => `<tr><td>${esc(o.name)}<br><small style="color:var(--muted);word-break:break-all">${esc(o.url)}</small></td><td>${esc(o.use)}</td><td>${esc(o.license)}</td></tr>`).join("")}
      </tbody></table></div></section>`;
}

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
  else if (page === "settings") { if (!S.about) S.about = await call("about"); renderSettings(); }
  else if (page === "memory") { await loadMemory(); renderMemory(); }
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
document.addEventListener("change", async (e) => {
  if (e.target.classList && e.target.classList.contains("mem-imp")) {
    await call("memory_action", "importance", Number(e.target.dataset.id), Number(e.target.value));
    await loadMemory(); renderMemory(); toast(Number(e.target.value) >= 5 ? "设成重要了，她不会忘" : "重要程度改好了");
    return;
  }
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
  archetype: (el) => {
    S.persona = { ...S.persona, ...collectPersona() };
    S.persona.archetype = S.persona.archetype === el.dataset.v ? "" : el.dataset.v;
    renderPersona();
  },
  "ar-add": () => { S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs(); S.rules.auto_replies.push({ keys: "", reply: "" }); renderRules(); },
  "ar-del": (el) => { S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs(); S.rules.auto_replies.splice(Number(el.dataset.i), 1); renderRules(); },
  "own-add": () => {
    const pick = $("#own-pick");
    if (!pick || !pick.value) { toast("先点“刷新好友列表”，再从互关好友里选一位", true); return; }
    const name = pick.value;
    S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs();
    S.rules.owners.push({ name, call: "", relation: "", enabled: 1, chat: 1, share: 1, closeness: 0 });
    renderRules(); toast(`把 ${name} 设为主人了，填好称呼后记得点右上角“保存”`);
  },
  "own-del": (el) => { S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs(); S.rules.owners.splice(Number(el.dataset.i), 1); renderRules(); },
  "mem-tab": async (el) => { S.mem.tab = el.dataset.t; S.mem.person = ""; S.mem.q = ""; await loadMemory(); renderMemory(); },
  "mem-search": async () => { S.mem.q = $("#mem-q").value.trim(); await loadMemory(); renderMemory(); },
  "mem-clear": async () => { S.mem.q = ""; S.mem.person = ""; await loadMemory(); renderMemory(); },
  "mem-person": async (el) => { S.mem = { tab: "events", q: "", person: el.dataset.name, data: null }; await showPage("memory"); },
  "mem-del": async (el) => { await call("memory_action", "delete", Number(el.dataset.id)); await loadMemory(); renderMemory(); toast("这条记忆删掉了"); },
  "mem-diary-del": async (el) => {
    if (!(await dialog({ title: "删掉这篇日记？", body: "删掉以后她就不记得这一天写过什么了。", ok: "删除", cancel: "先留着" }))) return;
    await call("memory_action", "delete_diary", Number(el.dataset.id)); await loadMemory(); renderMemory();
  },
  "mem-card": async (el) => { toast("正在整理印象…"); await call("memory_action", "refresh_card", el.dataset.name); await loadMemory(); renderMemory(); },
  "mem-forget": async (el) => {
    const name = el.dataset.name;
    if (!(await dialog({ title: `忘掉 ${name}？`, body: "她对 TA 的印象卡、和 TA 的私信聊天记录、和 TA 有关的记忆都会删掉，恢复不了。以后 TA 再来私信，她会当成第一次认识。", ok: "忘掉", cancel: "先留着" }))) return;
    await call("memory_action", "forget_person", name); await loadMemory(); renderMemory(); toast("已经忘掉 " + name);
  },
  "mem-note": async () => {
    const v = $("#mem-note").value.trim();
    if (!v) return toast("先写点内容", true);
    await call("memory_action", "add_note", "", v); await loadMemory(); renderMemory(); toast("她记住了");
  },
  "save-rules": async () => {
    const res = await call("save_rules", collectRules());
    if (S.friends) await call("save_friends", friendChoices());
    S.rules = await call("get_rules"); S.friends = await call("get_friends"); renderRules(); refresh();
    const no = (res && res.owners_refused) || [];
    if (no.length) toast(`规则已保存；${no.join("、")} 不是互关好友，没有设为主人`, true); else toast("规则已保存");
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
    // 按正文输入框里现在的全部内容重新分卡片
    const data = collectDraft(); delete data.cards;
    el.disabled = true; el.textContent = "正在按正文画卡片…";
    try { await call("save_draft", S.selDraft, data); await call("draft_cards", S.selDraft, true); toast("按正文重新做好文字卡片啦"); } catch (e) { /* 已经提示 */ }
    await loadDrafts(); renderDrafts();
  },
  "d-cards-custom": async (el) => {
    el.disabled = true; el.textContent = "正在画卡片…";
    try { await call("save_draft", S.selDraft, collectDraft()); await call("draft_cards", S.selDraft, false); toast("按你写的重画好啦"); } catch (e) { /* 已经提示 */ }
    await loadDrafts(); renderDrafts();
  },
  "d-voice": async (el) => {
    await call("save_draft", S.selDraft, collectDraft());
    el.disabled = true; el.textContent = "正在配音、合成视频，可能要半分钟…";
    try { await call("draft_voice", S.selDraft); toast("配好音啦，点视频可以播放"); } catch (e) { /* 已经提示 */ }
    await loadDrafts(); renderDrafts();
  },
  "d-form": async (el) => {
    const form = el.dataset.form;
    if (form === (S.draft || {}).form) return;
    const made = ((S.draft || {}).media || []).some((m) => ["card", "voice", "voicecard"].includes(m.source));
    if (made && !(await dialog({ title: `换成“${FORM_NAME[form]}”？`, body: "现在这种形式自动生成的图片和配音会去掉（你从电脑上添加的不动），再按新的形式重新做。", ok: "换", cancel: "不换" }))) return;
    await call("save_draft", S.selDraft, collectDraft());
    toast(form === "voice" ? "正在画图、配音，可能要半分钟…" : "正在换作品形式…");
    try {
      const r = await call("draft_set_form", S.selDraft, form);
      toast(form === "voice" ? (r.voiced ? "图片和配音都做好啦" : "图片做好了（还没配音）") : form === "cards" ? "文字卡片做好啦" : "已换成只用你上传的图片 / 视频");
    } catch (e) { /* 已经提示 */ }
    await loadDrafts(); renderDrafts();
  },
  "d-voice-play": () => {
    const items = previewItems(S.draft || {});
    const at = items.findIndex((x) => x.type === "audio");
    if (at < 0) return toast("这个作品还没有配音", true);
    lightbox(items, at);
  },
  "d-view": (el) => {
    const items = previewItems(S.draft || {});
    lightbox(items, Math.max(0, items.findIndex((x) => x.name === el.dataset.name && x.type !== "audio")));
  },
  "d-view-all": () => lightbox(previewItems(S.draft || {}), 0),
  "d-remove": async () => {
    const ok = await dialog({ title: "删除这条作品记录？", body: "它的文案、文字卡片、AI 配图和配音都会一起删掉，恢复不了。你从电脑上添加的图片 / 视频原文件不会动。", ok: "删除", cancel: "先留着" });
    if (!ok) return;
    await call("delete_draft", S.selDraft); S.selDraft = null; await loadDrafts(); renderDrafts(); toast("已删除");
  },
  "sticker-sheet": async (el) => {
    el.disabled = true;
    try { const r = await call("sticker_preview"); $("#sticker-box").innerHTML = `<img src="${r.image}" alt="她的表情包预览" style="max-width:100%;border-radius:12px">`; } finally { el.disabled = false; }
  },
  "sticker-one": async (el) => {
    el.disabled = true;
    try { const r = await call("sticker_preview", $("#st-mood").value, $("#st-text").value.trim()); $("#sticker-box").innerHTML = `<img src="${r.image}" alt="表情包" style="width:240px;border-radius:12px">`; } finally { el.disabled = false; }
  },
  "voice-test": async (el) => {
    el.disabled = true; el.textContent = "正在合成…";
    try { const r = await call("voice_test", $("#v-say").value.trim(), collectVoice()); $("#voice-box").innerHTML = `<audio controls autoplay src="${r.audio}" style="width:100%"></audio>`; } catch (e) { /* 已经提示 */ }
    el.disabled = false; el.textContent = "试听";
  },
  "voice-key": async () => {
    const key = $("#v-key").value.trim();
    if (!key) { toast("先把 Key 粘贴进来", true); return; }
    S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs();
    const r = await call("voice_save_key", key);
    S.rules.voice_has_key = r.has_key; S.rules.voice_ready = r.has_key && !!S.rules.voice_name;
    renderRules(); toast("Key 保存好了（已加密）");
  },
  "voice-key-clear": async () => {
    S.rules = { ...S.rules, ...collectRules() }; keepFriendInputs();
    await call("voice_save_key", ""); S.rules.voice_has_key = false; S.rules.voice_ready = false;
    renderRules(); toast("Key 清掉了");
  },
  "voice-list": async (el) => {
    el.disabled = true;
    try {
      const r = await call("voice_list", el.dataset.kind, collectVoice());
      $("#voice-list-box").innerHTML = r.voices.length
        ? `<div class="voice-list">${r.voices.map((v) => `<button type="button" class="btn btn-sm" data-act="voice-pick" data-id="${esc(v.id)}" title="${esc(v.desc || "")}">${esc(v.name)}<span class="hint" style="margin:0 0 0 6px">${esc(v.id)}</span></button>`).join("")}</div><span class="hint">点一个就填到上面；填好后可以试听，再点右上角“保存”。</span>`
        : `<span class="hint">${el.dataset.kind === "personal" ? "你的账号里还没有自己的音色：到 BreezeBlue 控制台里设计一个，或者先用官方音色。" : "没读到官方音色。"}</span>`;
    } catch (e) { /* 已经提示 */ }
    el.disabled = false;
  },
  "voice-pick": (el) => { $("#v-name").value = el.dataset.id; toast("填好了，可以点“试听”听听看"); },
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
    const ok = await dialog({ title: "不发这个作品了吗？", body: "她可能会有一点点失落哦。草稿会保留在列表里，标记为“不发了”；之后想清掉，可以点“删除这条记录”。", ok: "不发了", cancel: "再想想" });
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
