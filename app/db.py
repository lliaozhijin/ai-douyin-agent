"""SQLite 数据层。数据库文件在 <安装目录>/data/app.db。"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Any

from . import defaults
from .paths import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS providers (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL DEFAULT '',
    api_key_enc TEXT NOT NULL DEFAULT '',
    proxy       TEXT NOT NULL DEFAULT '',
    models      TEXT NOT NULL DEFAULT '[]',
    vision      INTEGER NOT NULL DEFAULT 0,
    price_in    REAL NOT NULL DEFAULT 0,
    price_out   REAL NOT NULL DEFAULT 0,
    extra       TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS videos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   TEXT,
    url        TEXT,
    author     TEXT,
    title      TEXT,
    interest   INTEGER,
    decision   TEXT,
    liked      INTEGER NOT NULL DEFAULT 0,
    collected  INTEGER NOT NULL DEFAULT 0,
    comment    TEXT,
    reason     TEXT,
    cost       REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_vid ON videos(video_id);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- at / dm / reply
    ext_id      TEXT UNIQUE,              -- 去重用
    user_name   TEXT,
    content     TEXT,
    video_title TEXT,
    video_url   TEXT,
    reply       TEXT,
    status      TEXT NOT NULL,            -- replied / pending / skipped_sensitive / skipped_limit / skipped_link / skipped_gone / skipped_old / uncertain / failed / manual / blocked
    reason      TEXT,
    cost        REAL NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    handled_at  TEXT
);
CREATE TABLE IF NOT EXISTS dm_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_name TEXT NOT NULL,
    role      TEXT NOT NULL,              -- them / me
    content   TEXT NOT NULL,
    ts        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    kind   TEXT NOT NULL,                 -- like / comment / reply_at / reply_dm / skip / pause / info / error
    title  TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ref    TEXT NOT NULL DEFAULT ''       -- 详情指向：video:<id> / cmt:<评论id> / msg:<id>
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS daily_usage (
    day        TEXT PRIMARY KEY,
    videos     INTEGER NOT NULL DEFAULT 0,
    likes      INTEGER NOT NULL DEFAULT 0,
    comments   INTEGER NOT NULL DEFAULT 0,
    replies_at INTEGER NOT NULL DEFAULT 0,
    replies_dm INTEGER NOT NULL DEFAULT 0,
    comment_likes   INTEGER NOT NULL DEFAULT 0,
    comment_replies INTEGER NOT NULL DEFAULT 0,
    thread_replies  INTEGER NOT NULL DEFAULT 0,
    at_friends      INTEGER NOT NULL DEFAULT 0,
    publishes       INTEGER NOT NULL DEFAULT 0,
    tokens     INTEGER NOT NULL DEFAULT 0,
    cost       REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comment_actions (
    cid        TEXT PRIMARY KEY,          -- 评论 id（读不到 id 时用作者+内容算一个）
    video_id   TEXT NOT NULL DEFAULT '',
    user_name  TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    liked      INTEGER NOT NULL DEFAULT 0,
    reply      TEXT NOT NULL DEFAULT '',
    video_title  TEXT NOT NULL DEFAULT '',
    video_author TEXT NOT NULL DEFAULT '',
    video_url    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dm_users (
    name       TEXT PRIMARY KEY,          -- 私信列表里显示的名字
    nickname   TEXT NOT NULL DEFAULT '',
    douyin_id  TEXT NOT NULL DEFAULT '',  -- 抖音号
    avatar     TEXT NOT NULL DEFAULT '',  -- 头像图片地址
    url        TEXT NOT NULL DEFAULT '',  -- 主页地址
    follows_me INTEGER NOT NULL DEFAULT -1, -- 1 关注了她 / 0 没关注 / -1 不知道
    checked_at TEXT NOT NULL DEFAULT ''   -- 上次去主页看资料的时间
);
CREATE TABLE IF NOT EXISTS threads (
    id         TEXT PRIMARY KEY,          -- 一串评论对话（她的某条评论下，和某个人的来回）
    video_url  TEXT NOT NULL DEFAULT '',
    root_text  TEXT NOT NULL DEFAULT '',  -- 她最开始的那条评论
    partner    TEXT NOT NULL DEFAULT '',  -- 和谁在聊
    count      INTEGER NOT NULL DEFAULT 0, -- 她在这串里已经回了几次
    closed     INTEGER NOT NULL DEFAULT 0, -- 1 不再继续回
    last_at    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS thread_lines (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    role      TEXT NOT NULL,              -- them / me
    content   TEXT NOT NULL,
    ts        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS friends (
    uid        TEXT PRIMARY KEY,          -- 主页 id（sec_uid），读不到时用昵称
    nickname   TEXT NOT NULL DEFAULT '',
    douyin_id  TEXT NOT NULL DEFAULT '',
    avatar     TEXT NOT NULL DEFAULT '',
    mutual     INTEGER NOT NULL DEFAULT 0, -- 1 互相关注
    enabled    INTEGER NOT NULL DEFAULT 0, -- 1 你选了允许她 @ 这个人
    tags       TEXT NOT NULL DEFAULT '',  -- TA 喜欢看什么（逗号分隔）
    last_at_day TEXT NOT NULL DEFAULT '', -- 最近一次被她 @ 的日期
    day_count  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    status       TEXT NOT NULL DEFAULT 'wish',  -- wish 她想发、等你确认 / publishing 发布中 / published 已发布 / failed 发布失败 / discarded 不发了
    kind         TEXT NOT NULL DEFAULT 'image', -- image 图文 / video 视频
    title        TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL DEFAULT '',
    tags         TEXT NOT NULL DEFAULT '[]',
    media        TEXT NOT NULL DEFAULT '[]',    -- [{name, kind: image/video, source: card/ai/upload, path?}]
    cards        TEXT NOT NULL DEFAULT '[]',    -- 她写的文字卡片内容
    image_prompt TEXT NOT NULL DEFAULT '',
    mood         TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',      -- 她为什么想发
    error        TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'auto',  -- auto 她自己想发 / manual 你让她写的
    seen         INTEGER NOT NULL DEFAULT 0,    -- 你看过提醒了
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    card_bg      TEXT NOT NULL DEFAULT '',      -- 文字卡片背景：空 = 用默认设置；none = 内置粉色样式；其他 = 背景图文件名
    card_panel   INTEGER NOT NULL DEFAULT 1     -- 1 文字写在半透明白卡片上；0 直接写在背景图上
);
CREATE TABLE IF NOT EXISTS blocked_users (
    user_name  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""

USAGE_FIELDS = ("videos", "likes", "collects", "comments", "replies_at", "replies_dm", "comment_likes", "comment_replies",
                "thread_replies", "at_friends", "publishes", "tokens", "cost")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return date.today().isoformat()


def _video_detail(r: dict) -> dict:
    parts = []
    if r.get("comment"):
        parts.append({"who": "她的评论", "me": True, "text": r["comment"]})
    if r.get("liked"):
        parts.append({"who": "她点了赞", "me": True, "text": ""})
    if r.get("reason"):
        parts.append({"who": "她的想法", "me": True, "text": r["reason"], "note": True})
    return {"kind": "video", "time": r.get("created_at", ""),
            "video": {"title": r.get("title") or "", "author": r.get("author") or "", "url": r.get("url") or ""},
            "parts": parts}


def _comment_detail(r: dict) -> dict:
    parts = [{"who": f"{r.get('user_name') or '一位用户'} 的评论", "me": False, "text": r.get("text") or ""}]
    if r.get("reply"):
        parts.append({"who": "她的回复", "me": True, "text": r["reply"]})
    elif r.get("liked"):
        parts.append({"who": "她给这条评论点了赞", "me": True, "text": ""})
    return {"kind": "comment", "time": r.get("created_at", ""),
            "video": {"title": r.get("video_title") or "", "author": r.get("video_author") or "",
                      "url": r.get("video_url") or ""},
            "parts": parts}


def _message_detail(r: dict) -> dict:
    where = {"at": "在评论区 @ 了她", "reply": "回复了她的评论"}.get(r.get("kind"), "发来的私信")
    parts = [{"who": f"{r.get('user_name') or '对方'} {where}", "me": False, "text": r.get("content") or ""}]
    if r.get("reply"):
        parts.append({"who": "她的回复", "me": True, "text": r["reply"]})
    return {"kind": r.get("kind") or "dm", "time": r.get("handled_at") or r.get("created_at") or "",
            "video": {"title": r.get("video_title") or "", "author": "", "url": r.get("video_url") or ""}
            if (r.get("video_title") or r.get("video_url")) else None,
            "parts": parts}


def _draft_detail(r: dict) -> dict:
    tags = " ".join("#" + t for t in json.loads(r.get("tags") or "[]"))
    parts = [{"who": "她发布的作品", "me": True, "text": f"{r.get('title') or ''}\n{r.get('text') or ''}\n{tags}".strip()}]
    if r.get("reason"):
        parts.append({"who": "她为什么想发", "me": True, "text": r["reason"], "note": True})
    return {"kind": "draft", "time": r.get("published_at") or r.get("created_at") or "", "video": None, "parts": parts}


def _draft_row(r: dict) -> dict:
    out = dict(r)
    for k in ("tags", "media", "cards"):
        try:
            out[k] = json.loads(r.get(k) or "[]")
        except json.JSONDecodeError:
            out[k] = []
    return out


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Database:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self._seed()

    _NEW_COLUMNS = {
        "daily_usage": {"comment_likes": "INTEGER NOT NULL DEFAULT 0", "comment_replies": "INTEGER NOT NULL DEFAULT 0",
                        "thread_replies": "INTEGER NOT NULL DEFAULT 0", "at_friends": "INTEGER NOT NULL DEFAULT 0",
                        "publishes": "INTEGER NOT NULL DEFAULT 0", "collects": "INTEGER NOT NULL DEFAULT 0"},
        "videos": {"collected": "INTEGER NOT NULL DEFAULT 0", "topic": "TEXT NOT NULL DEFAULT ''"},
        "drafts": {"card_bg": "TEXT NOT NULL DEFAULT ''", "card_panel": "INTEGER NOT NULL DEFAULT 1"},
        "events": {"ref": "TEXT NOT NULL DEFAULT ''"},
        "comment_actions": {"video_title": "TEXT NOT NULL DEFAULT ''", "video_author": "TEXT NOT NULL DEFAULT ''",
                            "video_url": "TEXT NOT NULL DEFAULT ''"},
    }

    def _migrate(self) -> None:
        """老版本建的表缺新列时补上（数据不动）。"""
        for table, cols in self._NEW_COLUMNS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        # 0.1.0 早期版本读私信对象资料时可能读成了别人（后面视频作者）的：清掉，下次重新准确读取
        # 旧版本读“互动消息”时把整个页面当成了一条 @（内容里带着面板底部的“暂时没有更多了”）：删掉这些错误记录
        if not self.conn.execute("SELECT 1 FROM kv WHERE key='bad_at_cleanup_v1'").fetchone():
            self.conn.execute("DELETE FROM messages WHERE kind='at' AND content LIKE '%暂时没有更多了%'")
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('bad_at_cleanup_v1', 'true')")
        done = self.conn.execute("SELECT 1 FROM kv WHERE key='dm_users_reset_v2'").fetchone()
        if not done:
            self.conn.execute("DELETE FROM dm_users")
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('dm_users_reset_v2', 'true')")

    # ---------- 基础 ----------
    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---------- 键值配置 ----------
    def get_json(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return copy.deepcopy(default)
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return copy.deepcopy(default)

    def set_json(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def _seed(self) -> None:
        for key, value in (
            ("persona", defaults.DEFAULT_PERSONA),
            ("rules", defaults.DEFAULT_RULES),
            ("ui", defaults.DEFAULT_UI),
            ("tasks", defaults.DEFAULT_TASKS),
        ):
            if self.get_json(key) is None:
                self.set_json(key, value)
        for p in defaults.PROVIDER_PRESETS:
            preset_extra = {k: p[k] for k in ("vision_models", "request_extra") if k in p}
            rows = self.query("SELECT extra FROM providers WHERE id=?", (p["id"],))
            if not rows:
                self.execute(
                    "INSERT INTO providers(id, kind, name, base_url, models, vision, extra, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (p["id"], p["kind"], p["name"], p["base_url"], json.dumps(p["models"]), int(p["vision"]),
                     json.dumps(preset_extra, ensure_ascii=False), now_str()),
                )
                continue
            # 老版本建的行：补上新版本才有的默认请求参数（例如关闭 DeepSeek 思考模式），不动用户自己的设置
            extra = json.loads(rows[0]["extra"] or "{}")
            if "request_extra" in p and "request_extra" not in extra:
                extra["request_extra"] = p["request_extra"]
                self.execute("UPDATE providers SET extra=? WHERE id=?",
                             (json.dumps(extra, ensure_ascii=False), p["id"]))

    def persona(self) -> dict:
        return _deep_merge(defaults.DEFAULT_PERSONA, self.get_json("persona", {}))

    def rules(self) -> dict:
        return _deep_merge(defaults.DEFAULT_RULES, self.get_json("rules", {}))

    def ui(self) -> dict:
        return _deep_merge(defaults.DEFAULT_UI, self.get_json("ui", {}))

    def tasks(self) -> dict:
        return _deep_merge(defaults.DEFAULT_TASKS, self.get_json("tasks", {}))

    # ---------- 模型服务商 ----------
    def providers(self) -> list[dict]:
        rows = self.query("SELECT * FROM providers ORDER BY rowid")
        for r in rows:
            r["models"] = json.loads(r["models"] or "[]")
            r["extra"] = json.loads(r["extra"] or "{}")
            r["vision"] = bool(r["vision"])
        return rows

    def provider(self, pid: str) -> dict | None:
        for p in self.providers():
            if p["id"] == pid:
                return p
        return None

    def save_provider(self, p: dict) -> None:
        self.execute(
            """UPDATE providers SET base_url=?, proxy=?, models=?, vision=?, price_in=?, price_out=?, extra=?, updated_at=?
               WHERE id=?""",
            (p.get("base_url", ""), p.get("proxy", ""), json.dumps(p.get("models", []), ensure_ascii=False),
             int(bool(p.get("vision"))), float(p.get("price_in") or 0), float(p.get("price_out") or 0),
             json.dumps(p.get("extra", {}), ensure_ascii=False), now_str(), p["id"]),
        )

    def set_provider_key(self, pid: str, api_key_enc: str) -> None:
        self.execute("UPDATE providers SET api_key_enc=?, updated_at=? WHERE id=?", (api_key_enc, now_str(), pid))

    # ---------- 每日用量 ----------
    def usage(self, day: str | None = None) -> dict:
        day = day or today_str()
        rows = self.query("SELECT * FROM daily_usage WHERE day=?", (day,))
        if rows:
            return rows[0]
        return {"day": day, **{f: 0 for f in USAGE_FIELDS}}

    def add_usage(self, field: str, amount: float = 1, day: str | None = None) -> None:
        if field not in USAGE_FIELDS:
            raise ValueError(field)
        day = day or today_str()
        self.execute("INSERT OR IGNORE INTO daily_usage(day) VALUES(?)", (day,))
        self.execute(f"UPDATE daily_usage SET {field} = {field} + ? WHERE day=?", (amount, day))

    # ---------- 动态 ----------
    def add_event(self, kind: str, title: str, detail: str = "", ref: str = "") -> None:
        self.execute("INSERT INTO events(ts, kind, title, detail, ref) VALUES(?,?,?,?,?)",
                     (now_str(), kind, title, detail, ref))

    def events(self, limit: int = 30) -> list[dict]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def events_page(self, page: int = 1, size: int = 10, day: str = "", kinds: list[str] | None = None,
                    max_pages: int = 0) -> dict:
        """分页读她的操作记录（最新的在前）。day 只看某一天（YYYY-MM-DD）；kinds 只看这几类；
        max_pages > 0 时最多只翻这么多页（仪表盘只展示最新的 10 页）。"""
        size = max(1, min(100, int(size or 10)))
        where, args = [], []
        if day:
            where.append("substr(ts,1,10)=?")
            args.append(day)
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += list(kinds)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = int(self.query(f"SELECT COUNT(*) AS n FROM events {w}", tuple(args))[0]["n"])
        shown = min(total, max_pages * size) if max_pages else total
        pages = max(1, -(-shown // size))
        page = max(1, min(int(page or 1), pages))
        rows = self.query(f"SELECT * FROM events {w} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                          (*args, size, (page - 1) * size))
        return {"items": rows, "page": page, "pages": pages, "size": size, "total": total, "shown": shown}

    def event_days(self, limit: int = 366) -> list[dict]:
        """有记录的日期和每天的条数（最新的在前）。"""
        return self.query("SELECT substr(ts,1,10) AS day, COUNT(*) AS n FROM events GROUP BY day "
                          "ORDER BY day DESC LIMIT ?", (limit,))

    # ---------- 视频 ----------
    def seen_video(self, video_id: str) -> bool:
        return bool(video_id) and bool(self.query("SELECT 1 FROM videos WHERE video_id=? LIMIT 1", (video_id,)))

    def add_video(self, **kw) -> int:
        kw.setdefault("created_at", now_str())
        cols = ", ".join(kw.keys())
        marks = ", ".join("?" for _ in kw)
        return self.execute(f"INSERT INTO videos({cols}) VALUES({marks})", tuple(kw.values())).lastrowid

    # ---------- 消息 ----------
    def message_exists(self, ext_id: str) -> bool:
        return bool(self.query("SELECT 1 FROM messages WHERE ext_id=? LIMIT 1", (ext_id,)))

    def add_message(self, **kw) -> int:
        kw.setdefault("created_at", now_str())
        cols = ", ".join(kw.keys())
        marks = ", ".join("?" for _ in kw)
        return self.execute(f"INSERT OR IGNORE INTO messages({cols}) VALUES({marks})", tuple(kw.values())).lastrowid

    def update_message(self, mid: int, **kw) -> None:
        if not kw:
            return
        sets = ", ".join(f"{k}=?" for k in kw)
        self.execute(f"UPDATE messages SET {sets} WHERE id=?", (*kw.values(), mid))

    def messages(self, flt: str = "all", limit: int = 200) -> list[dict]:
        where = {
            "all": "1=1",
            "at": "kind IN ('at','reply')",
            "dm": "kind='dm'",
            "pending": "status='pending'",
            "skipped": "status LIKE 'skipped%'",
        }.get(flt, "1=1")
        return self.query(f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?", (limit,))

    def message_counts(self) -> dict:
        rows = self.query(
            """SELECT
                 COUNT(*) AS all_count,
                 SUM(kind IN ('at','reply')) AS at_count,
                 SUM(kind='dm') AS dm_count,
                 SUM(status='pending') AS pending_count,
                 SUM(status LIKE 'skipped%') AS skipped_count
               FROM messages"""
        )[0]
        return {k: int(v or 0) for k, v in rows.items()}

    def replies_to_user_today(self, user_name: str) -> int:
        rows = self.query(
            "SELECT COUNT(*) AS n FROM messages WHERE user_name=? AND status='replied' AND substr(handled_at,1,10)=?",
            (user_name, today_str()),
        )
        return int(rows[0]["n"])

    # ---------- 私信上下文 ----------
    def add_dm(self, user_name: str, role: str, content: str) -> None:
        self.execute("INSERT INTO dm_history(user_name, role, content, ts) VALUES(?,?,?,?)",
                     (user_name, role, content, now_str()))

    def dm_history(self, user_name: str, limit: int = 10) -> list[dict]:
        rows = self.query("SELECT role, content FROM dm_history WHERE user_name=? ORDER BY id DESC LIMIT ?",
                          (user_name, limit))
        return list(reversed(rows))

    # ---------- 不回复名单 ----------
    # ---------- 别人的评论 ----------
    def comment_done(self, cid: str) -> bool:
        return bool(cid) and bool(self.query("SELECT 1 FROM comment_actions WHERE cid=?", (cid,)))

    def add_comment_action(self, cid: str, video_id: str, user_name: str, text: str,
                           liked: bool = False, reply: str = "", video: dict | None = None) -> None:
        v = video or {}
        self.execute(
            "INSERT INTO comment_actions(cid, video_id, user_name, text, liked, reply, video_title, video_author, "
            "video_url, created_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cid) DO UPDATE SET "
            "liked = MAX(liked, excluded.liked), reply = CASE WHEN excluded.reply != '' THEN excluded.reply ELSE reply END",
            (cid, video_id, user_name, text[:300], int(liked), reply, (v.get("desc") or "")[:200],
             v.get("author") or "", v.get("url") or "", now_str()))

    # ---------- 详情（仪表盘点开看） ----------
    def detail(self, ref: str) -> dict | None:
        """按动态里的 ref 取出一条详情：她评论了什么 / 别人说了什么、她回了什么。"""
        kind, _, key = (ref or "").partition(":")
        if kind == "video":
            rows = self.query("SELECT * FROM videos WHERE id=?", (key,))
            return _video_detail(rows[0]) if rows else None
        if kind == "cmt":
            rows = self.query("SELECT * FROM comment_actions WHERE cid=?", (key,))
            return _comment_detail(rows[0]) if rows else None
        if kind == "msg":
            rows = self.query("SELECT * FROM messages WHERE id=?", (key,))
            return _message_detail(rows[0]) if rows else None
        return None

    def activity(self, kind: str, day: str | None = None, limit: int = 100) -> list[dict]:
        """某一天某类互动的详情列表（仪表盘点用量格子时显示）。"""
        like = (day or today_str()) + "%"
        if kind == "comments":
            rows = self.query("SELECT * FROM videos WHERE comment != '' AND comment IS NOT NULL AND created_at LIKE ? "
                              "ORDER BY id DESC LIMIT ?", (like, limit))
            return [_video_detail(r) for r in rows]
        if kind == "comment_replies":
            rows = self.query("SELECT * FROM comment_actions WHERE reply != '' AND created_at LIKE ? "
                              "ORDER BY rowid DESC LIMIT ?", (like, limit))
            return [_comment_detail(r) for r in rows]
        if kind == "comment_likes":
            rows = self.query("SELECT * FROM comment_actions WHERE liked = 1 AND created_at LIKE ? "
                              "ORDER BY rowid DESC LIMIT ?", (like, limit))
            return [_comment_detail(r) for r in rows]
        if kind in ("replies_at", "replies_dm", "thread_replies"):
            mk = {"replies_at": "at", "replies_dm": "dm", "thread_replies": "reply"}[kind]
            rows = self.query("SELECT * FROM messages WHERE kind=? AND status IN ('replied','manual') "
                              "AND COALESCE(handled_at, created_at) LIKE ? ORDER BY id DESC LIMIT ?",
                              (mk, like, limit))
            return [_message_detail(r) for r in rows]
        if kind == "at_friends":
            rows = self.query("SELECT * FROM videos WHERE comment LIKE '@%' AND created_at LIKE ? "
                              "ORDER BY id DESC LIMIT ?", (like, limit))
            return [_video_detail(r) for r in rows]
        if kind == "publishes":
            rows = self.query("SELECT * FROM drafts WHERE status='published' AND published_at LIKE ? "
                              "ORDER BY id DESC LIMIT ?", (like, limit))
            return [_draft_detail(r) for r in rows]
        if kind == "likes":
            rows = self.query("SELECT * FROM videos WHERE liked = 1 AND created_at LIKE ? ORDER BY id DESC LIMIT ?",
                              (like, limit))
            return [_video_detail(r) for r in rows]
        if kind == "collects":
            rows = self.query("SELECT * FROM videos WHERE collected = 1 AND created_at LIKE ? ORDER BY id DESC LIMIT ?",
                              (like, limit))
            return [_video_detail(r) for r in rows]
        return []

    # ---------- 私信对象的资料 ----------
    def dm_user(self, name: str) -> dict | None:
        rows = self.query("SELECT * FROM dm_users WHERE name=?", (name,))
        return rows[0] if rows else None

    def save_dm_user(self, name: str, profile: dict, checked: bool = False) -> None:
        """保存私信对象的资料。只更新读到的项；checked=True 表示刚去主页看过。"""
        if not name:
            return
        self.execute("INSERT OR IGNORE INTO dm_users(name) VALUES(?)", (name,))
        sets, vals = [], []
        for k in ("nickname", "douyin_id", "avatar", "url"):
            v = profile.get(k)
            if v:
                sets.append(f"{k}=?")
                vals.append(str(v)[:500])
        if profile.get("follows_me") is not None:
            sets.append("follows_me=?")
            vals.append(1 if profile["follows_me"] else 0)
        if checked:
            sets.append("checked_at=?")
            vals.append(now_str())
        if sets:
            self.execute(f"UPDATE dm_users SET {', '.join(sets)} WHERE name=?", (*vals, name))

    def dm_users_map(self, names: list[str]) -> dict:
        names = [n for n in set(names) if n]
        if not names:
            return {}
        marks = ",".join("?" for _ in names)
        return {r["name"]: r for r in self.query(f"SELECT * FROM dm_users WHERE name IN ({marks})", names)}

    # ---------- 评论对话串（有人回复她的评论，她接着回） ----------
    def thread(self, tid: str) -> dict | None:
        rows = self.query("SELECT * FROM threads WHERE id=?", (tid,))
        return rows[0] if rows else None

    def ensure_thread(self, tid: str, video_url: str = "", root_text: str = "", partner: str = "") -> dict:
        self.execute("INSERT OR IGNORE INTO threads(id, video_url, root_text, partner, last_at) VALUES(?,?,?,?,?)",
                     (tid, video_url, root_text[:300], partner, now_str()))
        return self.thread(tid)

    def thread_add(self, tid: str, role: str, content: str) -> None:
        self.execute("INSERT INTO thread_lines(thread_id, role, content, ts) VALUES(?,?,?,?)",
                     (tid, role, content[:500], now_str()))
        if role == "me":
            self.execute("UPDATE threads SET count = count + 1, last_at=? WHERE id=?", (now_str(), tid))

    def thread_lines(self, tid: str, limit: int = 12) -> list[dict]:
        rows = self.query("SELECT role, content FROM thread_lines WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                          (tid, limit))
        return list(reversed(rows))

    def close_thread(self, tid: str) -> None:
        self.execute("UPDATE threads SET closed=1 WHERE id=?", (tid,))

    def my_comment_texts(self, limit: int = 300) -> list[str]:
        """她最近发过的评论和回复（用来认出别人回复的是她哪条评论）。"""
        rows = self.query(
            "SELECT comment AS t FROM videos WHERE comment != '' AND comment IS NOT NULL "
            "UNION ALL SELECT reply AS t FROM comment_actions WHERE reply != '' "
            "UNION ALL SELECT reply AS t FROM messages WHERE kind IN ('at','reply') AND status IN ('replied','manual') "
            "AND reply IS NOT NULL AND reply != '' LIMIT ?", (limit,))
        return [r["t"] for r in rows if r["t"]]

    # ---------- 互关好友 ----------
    def friends(self, mutual_only: bool = True) -> list[dict]:
        where = "WHERE mutual=1" if mutual_only else ""
        return self.query(f"SELECT * FROM friends {where} ORDER BY enabled DESC, nickname")

    def save_friend(self, uid: str, info: dict) -> None:
        if not uid:
            return
        self.execute("INSERT OR IGNORE INTO friends(uid) VALUES(?)", (uid,))
        sets, vals = ["updated_at=?"], [now_str()]
        for k in ("nickname", "douyin_id", "avatar"):
            if info.get(k):
                sets.append(f"{k}=?")
                vals.append(str(info[k])[:500])
        if info.get("mutual") is not None:
            sets.append("mutual=?")
            vals.append(1 if info["mutual"] else 0)
        self.execute(f"UPDATE friends SET {', '.join(sets)} WHERE uid=?", (*vals, uid))

    def set_friend_choice(self, uid: str, enabled: bool, tags: str = "") -> None:
        self.execute("UPDATE friends SET enabled=?, tags=? WHERE uid=?", (1 if enabled else 0, tags[:200], uid))

    def friends_for_at(self, per_day: int = 1) -> list[dict]:
        """今天还可以被 @ 的好友：你选了的、互关的、今天被 @ 次数没到上限的。"""
        today = today_str()
        rows = self.query("SELECT * FROM friends WHERE enabled=1 AND mutual=1 ORDER BY RANDOM()")
        return [r for r in rows if r["last_at_day"] != today or r["day_count"] < per_day]

    def mark_friend_at(self, uid: str) -> None:
        today = today_str()
        self.execute("UPDATE friends SET day_count = CASE WHEN last_at_day=? THEN day_count + 1 ELSE 1 END, "
                     "last_at_day=? WHERE uid=?", (today, today, uid))

    # ---------- 作品草稿 ----------
    def add_draft(self, **kw) -> int:
        for k in ("tags", "media", "cards"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k], ensure_ascii=False)
        kw.setdefault("created_at", now_str())
        kw.setdefault("updated_at", kw["created_at"])
        cols = ", ".join(kw.keys())
        marks = ", ".join("?" for _ in kw)
        return self.execute(f"INSERT INTO drafts({cols}) VALUES({marks})", tuple(kw.values())).lastrowid

    def draft(self, did: int) -> dict | None:
        rows = self.query("SELECT * FROM drafts WHERE id=?", (int(did),))
        return _draft_row(rows[0]) if rows else None

    def drafts(self, include_done: bool = True, limit: int = 60) -> list[dict]:
        where = "" if include_done else "WHERE status NOT IN ('published','discarded')"
        return [_draft_row(r) for r in self.query(f"SELECT * FROM drafts {where} ORDER BY id DESC LIMIT ?", (limit,))]

    def update_draft(self, did: int, **kw) -> None:
        if not kw:
            return
        for k in ("tags", "media", "cards"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k], ensure_ascii=False)
        kw["updated_at"] = now_str()
        sets = ", ".join(f"{k}=?" for k in kw)
        self.execute(f"UPDATE drafts SET {sets} WHERE id=?", (*kw.values(), int(did)))

    def drafts_today(self) -> int:
        """今天她想发的（还没被你拒绝的）+ 已经发的作品数。"""
        rows = self.query("SELECT COUNT(*) AS n FROM drafts WHERE source='auto' AND substr(created_at,1,10)=? "
                          "AND status != 'discarded'", (today_str(),))
        return int(rows[0]["n"])

    def block_user(self, user_name: str) -> None:
        self.execute("INSERT OR IGNORE INTO blocked_users(user_name, created_at) VALUES(?,?)", (user_name, now_str()))

    def is_blocked(self, user_name: str) -> bool:
        return bool(self.query("SELECT 1 FROM blocked_users WHERE user_name=?", (user_name,)))
