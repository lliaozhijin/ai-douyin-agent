"""SQLite 数据层。数据库文件在 <安装目录>/data/app.db。"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
from datetime import datetime, date, timedelta
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
    kind        TEXT NOT NULL,            -- at 评论里 @ 她 / reply 回复她的评论 / work 评论她的作品 / dm 私信
    ext_id      TEXT UNIQUE,              -- 去重用
    user_name   TEXT,
    content     TEXT,
    video_title TEXT,
    video_url   TEXT,
    reply       TEXT,
    status      TEXT NOT NULL,            -- replied / pending / merged 和同一个人的另一条一起回了 / skipped_sensitive / skipped_limit / skipped_link / skipped_gone / skipped_old / uncertain / failed / manual / blocked
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
CREATE TABLE IF NOT EXISTS memories (     -- 她记得的一件件具体的事
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,             -- video 看过的视频 / dm 私信 / thread 评论区的一串对话 / at 被 @ / post 发作品 / note 你手动加的
    scope      TEXT NOT NULL DEFAULT '',  -- dm:<名字> 和某个私信好友有关 / thread:<对话号> 只属于评论区的这一串 / 空 = 她自己的事
    person     TEXT NOT NULL DEFAULT '',  -- 私信好友的名字（只有私信的记忆才关联到人）
    summary    TEXT NOT NULL,             -- 一句话
    detail     TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',  -- 用 | 分开：话题、作者……
    importance INTEGER NOT NULL DEFAULT 3, -- 1～5；5 = 重要，永远不忘
    strength   REAL NOT NULL DEFAULT 1,   -- 上次被想起时的强度
    half_life  REAL NOT NULL DEFAULT 30,  -- 半衰期（天）；每被想起一次变长
    recalls    INTEGER NOT NULL DEFAULT 0,
    day        TEXT NOT NULL,
    created_at TEXT NOT NULL,
    touched_at TEXT NOT NULL,             -- 上次被想起（或写下）的时间
    dedupe     TEXT UNIQUE                -- 同一件事只记一条
);
CREATE INDEX IF NOT EXISTS idx_mem_person ON memories(person);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_mem_day ON memories(day);
CREATE TABLE IF NOT EXISTS memory_archive ( -- 淡忘了的记忆：不再主动想起，压缩后留着；被人提起时还能想起来
    id          INTEGER PRIMARY KEY,           -- 和原来在 memories 里的 id 一样
    kind        TEXT NOT NULL DEFAULT '',
    person      TEXT NOT NULL DEFAULT '',
    day         TEXT NOT NULL DEFAULT '',
    codec       TEXT NOT NULL,
    dict_id     INTEGER NOT NULL DEFAULT 0,
    data        BLOB NOT NULL,                 -- 压缩过的整条记忆（JSON）
    archived_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marc_person ON memory_archive(person);
CREATE TABLE IF NOT EXISTS people (       -- 私信好友的人物印象卡（评论区的人不建卡）
    name       TEXT PRIMARY KEY,
    card       TEXT NOT NULL DEFAULT '',  -- 一段话的印象
    style      TEXT NOT NULL DEFAULT '',  -- 对方的说话风格
    topics     TEXT NOT NULL DEFAULT '',  -- 常聊的话题
    facts      TEXT NOT NULL DEFAULT '',  -- 对方告诉过她的事、她答应过对方的事
    chats      INTEGER NOT NULL DEFAULT 0, -- 聊过几次
    pending    INTEGER NOT NULL DEFAULT 0, -- 上次整理印象之后又聊了几次
    last_at    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS diary (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL,             -- day 日记 / week 周记 / month 月记
    period     TEXT NOT NULL,             -- 2026-11-10 / 2026-W46 / 2026-11
    content    TEXT NOT NULL,
    mood       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(level, period)
);
CREATE TABLE IF NOT EXISTS owners (       -- 主人：她会主动找 TA 聊天、分享视频，和 TA 的记忆不会忘
    name       TEXT PRIMARY KEY,          -- 私信列表里显示的名字
    call       TEXT NOT NULL DEFAULT '',  -- 她怎么称呼 TA
    relation   TEXT NOT NULL DEFAULT '',  -- 她和 TA 是什么关系（你自己写）
    enabled    INTEGER NOT NULL DEFAULT 1,
    chat       INTEGER NOT NULL DEFAULT 1, -- 主动找 TA 聊天
    share      INTEGER NOT NULL DEFAULT 1, -- 主动分享视频
    at         INTEGER NOT NULL DEFAULT 1, -- 刷到 TA 会喜欢的视频时，评论里 @ TA 来看
    closeness  REAL NOT NULL DEFAULT 0,   -- 亲密度 0～100，聊得越多越高
    last_proactive TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
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
    updated_at TEXT NOT NULL DEFAULT '',
    manual     INTEGER NOT NULL DEFAULT 0  -- 1 你手动添加的（没从抖音核实过是不是互关：可以 @，不能设为主人）
);
CREATE TABLE IF NOT EXISTS gone_messages ( -- 评论区的消息记录过期删除后留下的“回执”：只为了不把同一条通知当成新的再回一遍
    ext_id  TEXT PRIMARY KEY,
    kind    TEXT NOT NULL DEFAULT '',
    reply   TEXT NOT NULL DEFAULT '',     -- 她当时回的话（别人以后回复这句时，还能认出是她哪条评论）
    gone_at TEXT NOT NULL DEFAULT ''
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
CREATE TABLE IF NOT EXISTS content_marks ( -- 她互动过的内容：同样的内容只互动一次，同一类视频每天只互动一次
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   TEXT NOT NULL DEFAULT '',
    text_hash  TEXT NOT NULL DEFAULT '',  -- 文案的 SimHash（十六进制）；文案太短为空
    img_hash   TEXT NOT NULL DEFAULT '',  -- 第一眼画面的 dHash；画面太单调为空
    txt        TEXT NOT NULL DEFAULT '',  -- 清理后的文案（比较短文案用）
    topic      TEXT NOT NULL DEFAULT '',  -- 这个视频属于哪一类（她看的时候自己判断的）
    title      TEXT NOT NULL DEFAULT '',
    liked      INTEGER NOT NULL DEFAULT 0,
    commented  INTEGER NOT NULL DEFAULT 0,
    collected  INTEGER NOT NULL DEFAULT 0,
    shared     INTEGER NOT NULL DEFAULT 0, -- 分享给主人 / 在评论里 @ 人来看
    day        TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marks_day ON content_marks(day, topic);
CREATE TABLE IF NOT EXISTS owner_taste (  -- 她了解到的主人口味：每一类视频主人喜不喜欢（Beta 分布）
    owner      TEXT NOT NULL,
    topic      TEXT NOT NULL,
    alpha      REAL NOT NULL DEFAULT 1,   -- 主人喜欢的证据（含先验）
    beta       REAL NOT NULL DEFAULT 2,   -- 主人不喜欢 / 没兴趣的证据（含先验）
    said       TEXT NOT NULL DEFAULT '',  -- 主人自己说过：喜欢 / 不喜欢
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(owner, topic)
);
CREATE TABLE IF NOT EXISTS owner_shares ( -- 她分享给主人 / @ 主人来看的视频，和主人的反应
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,
    way         TEXT NOT NULL,             -- dm 私信里分享 / at 评论里 @
    video_id    TEXT NOT NULL DEFAULT '',
    video_url   TEXT NOT NULL DEFAULT '',
    topic       TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    said        TEXT NOT NULL DEFAULT '',  -- 她分享时说的话
    status      TEXT NOT NULL DEFAULT 'sent', -- sent 等反应 / liked / neutral / disliked / ignored
    feedback    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS knowledge (    -- 她自己学到的东西（刷到的视频、评论区、私信里）
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL DEFAULT '',
    fact       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',  -- video / comment / dm
    ref        TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.6,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    used_at    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'stable', -- candidate 刚学到、还要再验证 / stable 确认过的
    hits       INTEGER NOT NULL DEFAULT 1,      -- 又在别处见到同样的说法几次
    days       TEXT NOT NULL DEFAULT ''         -- 在哪几天见到过（逗号分开，最多留 5 天）
);
CREATE TABLE IF NOT EXISTS learn_notes (  -- 还没整理的学习素材
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    ref        TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tool_jobs (    -- “其他功能”的任务：下载视频、导出文案
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,             -- video 下载视频 / text 导出文案
    url         TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    files       TEXT NOT NULL DEFAULT '[]',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS blocked_users (
    user_name  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_ids (   -- 名字 → 主页 id：私信列表、评论区、通知里同一个人的名字写法可能不一样，靠主页 id 认成一个人
    name       TEXT PRIMARY KEY,
    uid        TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_person_ids_uid ON person_ids(uid);
CREATE TABLE IF NOT EXISTS my_taste (     -- 她自己的口味：每一类视频她现在有多喜欢（跟着她每天的经历自己变，不用设置）
    topic      TEXT PRIMARY KEY,
    score      REAL NOT NULL DEFAULT 0,   -- 越大越喜欢；每天自然回落一点（会淡、会腻）
    seen       INTEGER NOT NULL DEFAULT 0, -- 刷到过几个
    loved      INTEGER NOT NULL DEFAULT 0, -- 其中 8 分以上的
    acted      INTEGER NOT NULL DEFAULT 0, -- 点赞 / 收藏 / 评论过几个
    talked     INTEGER NOT NULL DEFAULT 0, -- 私信、评论区里聊到过几次
    first_at   TEXT NOT NULL DEFAULT '',
    last_at    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS my_works (     -- 她发出去的作品现在怎么样了（从自己主页读的）
    aweme_id   TEXT PRIMARY KEY,
    draft_id   INTEGER NOT NULL DEFAULT 0,  -- 对应哪个草稿（认不出是 0）
    desc       TEXT NOT NULL DEFAULT '',
    posted_at  TEXT NOT NULL DEFAULT '',
    likes      INTEGER NOT NULL DEFAULT 0,
    comments   INTEGER NOT NULL DEFAULT 0,
    collects   INTEGER NOT NULL DEFAULT 0,
    shares     INTEGER NOT NULL DEFAULT 0,
    plays      INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL DEFAULT ''
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
    where = {"at": "在评论区 @ 了她", "reply": "回复了她的评论", "work": "评论了她的作品"}.get(r.get("kind"), "发来的私信")
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
        # WAL 日志用完截断到 4MB 以内（不然长期运行会一直变大）
        self.conn.execute("PRAGMA journal_size_limit=4194304")
        self._migrate()
        self.conn.commit()
        self._seed()
        from .storage import Keeper
        self.keeper = Keeper(self)       # 旧记录的归档、压缩、回收空间（见 storage.py）

    _NEW_COLUMNS = {
        "daily_usage": {"comment_likes": "INTEGER NOT NULL DEFAULT 0", "comment_replies": "INTEGER NOT NULL DEFAULT 0",
                        "thread_replies": "INTEGER NOT NULL DEFAULT 0", "at_friends": "INTEGER NOT NULL DEFAULT 0",
                        "publishes": "INTEGER NOT NULL DEFAULT 0", "collects": "INTEGER NOT NULL DEFAULT 0"},
        "videos": {"collected": "INTEGER NOT NULL DEFAULT 0", "topic": "TEXT NOT NULL DEFAULT ''"},
        "drafts": {"card_bg": "TEXT NOT NULL DEFAULT ''", "card_panel": "INTEGER NOT NULL DEFAULT 1",
                   # 作品的形式：voice 配音图片视频 / cards 图文卡片 / upload 你上传的图片视频；空 = 以前的草稿，按图文卡片
                   "form": "TEXT NOT NULL DEFAULT ''"},
        "events": {"ref": "TEXT NOT NULL DEFAULT ''"},
        "friends": {"manual": "INTEGER NOT NULL DEFAULT 0"},
        # cmd：这位主人可以在私信里给她下命令（让她去私信别人、@、搜视频分享、发作品……）；默认不开，你在“主人”里勾上
        "owners": {"at": "INTEGER NOT NULL DEFAULT 1", "cmd": "INTEGER NOT NULL DEFAULT 0"},
        # 好感度（0～100）：TA 说话友善会慢慢升、说难听的话会降、很久不联系会慢慢回落到一般（参考 dsh_soul 的好感度）
        "people": {"affection": "REAL NOT NULL DEFAULT 30"},
        # 内容指纹按 16 位一段切成 4 段（SimHash 分段索引）：差 3 位以内的两个指纹至少有一段完全相同
        "content_marks": {**{f"b{i}": "INTEGER" for i in range(4)}, **{f"i{i}": "INTEGER" for i in range(4)}},
        "knowledge": {"status": "TEXT NOT NULL DEFAULT 'stable'", "hits": "INTEGER NOT NULL DEFAULT 1",
                      "days": "TEXT NOT NULL DEFAULT ''"},
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
        for i in range(4):
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_marks_b{i} ON content_marks(b{i})")
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_marks_i{i} ON content_marks(i{i})")
        if not self.conn.execute("SELECT 1 FROM kv WHERE key='content_marks_v1'").fetchone():
            self._backfill_content_marks()
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('content_marks_v1', 'true')")
        if self.conn.execute("SELECT 1 FROM content_marks WHERE b0 IS NULL AND (text_hash != '' OR img_hash != '') "
                             "LIMIT 1").fetchone():
            from .fingerprint import bands, from_hex
            rows = self.conn.execute("SELECT id, text_hash, img_hash FROM content_marks WHERE b0 IS NULL").fetchall()
            self.conn.executemany(
                "UPDATE content_marks SET b0=?, b1=?, b2=?, b3=?, i0=?, i1=?, i2=?, i3=? WHERE id=?",
                [(*bands(from_hex(r["text_hash"])), *bands(from_hex(r["img_hash"])), r["id"]) for r in rows])
        done = self.conn.execute("SELECT 1 FROM kv WHERE key='dm_users_reset_v2'").fetchone()
        if not done:
            self.conn.execute("DELETE FROM dm_users")
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('dm_users_reset_v2', 'true')")

    def _backfill_content_marks(self) -> None:
        """升级时：以前点过赞、评论过、收藏过的视频也记进“互动过的内容”，同样的内容以后不再重复互动。"""
        from .fingerprint import fingerprint, to_hex

        rows = self.conn.execute(
            "SELECT video_id, title, topic, liked, collected, comment, created_at FROM videos "
            "WHERE liked=1 OR collected=1 OR (comment IS NOT NULL AND comment != '') ORDER BY id DESC LIMIT 3000"
        ).fetchall()
        for r in rows:
            fp = fingerprint(r["title"] or "")
            self.conn.execute(
                "INSERT INTO content_marks(video_id, text_hash, txt, topic, title, liked, commented, collected, shared, "
                "day, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (r["video_id"] or "", to_hex(fp["th"]), fp["txt"], r["topic"] or "", (r["title"] or "")[:60],
                 int(r["liked"] or 0), int(bool(r["comment"])), int(r["collected"] or 0),
                 int(str(r["comment"] or "").startswith("@")), (r["created_at"] or "")[:10], r["created_at"] or ""))

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
        if day and day in self.keeper.days("events"):
            # 这一天的记录已经归档压缩了：解压出来，和表里剩下的合在一起分页
            rows = self.keeper.read("events", day) + self.query(f"SELECT * FROM events {w}", tuple(args))
            if kinds:
                rows = [r for r in rows if r.get("kind") in kinds]
            for r in rows:
                r.pop("_rid", None)
            rows.sort(key=lambda r: (r.get("ts") or "", int(r.get("id") or 0)), reverse=True)
            total = shown = len(rows)
            pages = max(1, -(-shown // size))
            page = max(1, min(int(page or 1), pages))
            return {"items": rows[(page - 1) * size: page * size], "page": page, "pages": pages, "size": size,
                    "total": total, "shown": shown, "archived": True}
        total = int(self.query(f"SELECT COUNT(*) AS n FROM events {w}", tuple(args))[0]["n"])
        shown = min(total, max_pages * size) if max_pages else total
        pages = max(1, -(-shown // size))
        page = max(1, min(int(page or 1), pages))
        rows = self.query(f"SELECT * FROM events {w} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                          (*args, size, (page - 1) * size))
        return {"items": rows, "page": page, "pages": pages, "size": size, "total": total, "shown": shown}

    def event_days(self, limit: int = 366) -> list[dict]:
        """有记录的日期和每天的条数（最新的在前）。"""
        days = {r["day"]: int(r["n"]) for r in self.query(
            "SELECT substr(ts,1,10) AS day, COUNT(*) AS n FROM events GROUP BY day")}
        for d, n in self.keeper.days("events").items():      # 归档了的日子也列出来
            days[d] = days.get(d, 0) + n
        return [{"day": d, "n": days[d]} for d in sorted(days, reverse=True)[:limit]]

    # ---------- 视频 ----------
    def seen_video(self, video_id: str) -> bool:
        return bool(video_id) and bool(self.query("SELECT 1 FROM videos WHERE video_id=? UNION ALL "
                                                  "SELECT 1 FROM seen_videos WHERE video_id=? LIMIT 1",
                                                  (video_id, video_id)))

    def add_video(self, **kw) -> int:
        kw.setdefault("created_at", now_str())
        cols = ", ".join(kw.keys())
        marks = ", ".join("?" for _ in kw)
        return self.execute(f"INSERT INTO videos({cols}) VALUES({marks})", tuple(kw.values())).lastrowid

    # ---------- 消息 ----------
    def message_exists(self, ext_id: str) -> bool:
        """这条通知 / 私信处理过没有。评论区的记录过期删掉以后，回执（gone_messages）还在：不会再回一遍。"""
        return bool(self.query("SELECT 1 FROM messages WHERE ext_id=? UNION ALL "
                               "SELECT 1 FROM gone_messages WHERE ext_id=? LIMIT 1", (ext_id, ext_id)))

    # 以前的版本会把这些消息丢下不回（启动前收到的、额度用完的、只回关注她的人……），或者回到一半被打断了。
    # 现在只要有人找她，她都会回：再看到这些消息时，重新拿出来回复
    REVIVE_STATUSES = ("skipped_old", "skipped_limit", "skipped_scope")
    REVIVE_REASONS = ("处理中", "你让她休息了", "遇到验证", "今天对这个人的回复已到上限", "没能确认对方有没有关注她")

    @classmethod
    def revivable(cls, status: str, reason: str) -> bool:
        return status in cls.REVIVE_STATUSES or (
            status == "pending" and any((reason or "").startswith(r) for r in cls.REVIVE_REASONS))

    def message_handled(self, ext_id: str) -> bool:
        """这条通知 / 私信已经有了结果（回过了、放进待你处理了、跳过了……），不用再回。
        以前被丢下、或者回到一半被打断的不算：还要回。"""
        if self.query("SELECT 1 FROM gone_messages WHERE ext_id=? LIMIT 1", (ext_id,)):
            return True
        rows = self.query("SELECT status, reason FROM messages WHERE ext_id=?", (ext_id,))
        return bool(rows) and not self.revivable(rows[0]["status"], rows[0]["reason"] or "")

    def claim_message(self, **kw) -> int | None:
        """开始处理一条消息：新的就记下来；以前丢下 / 被打断的就重新拿出来。已经有结果的返回 None。"""
        ext = kw.get("ext_id")
        with self._lock:
            if ext and self.message_handled(ext):
                return None
            rows = self.query("SELECT id FROM messages WHERE ext_id=?", (ext,)) if ext else []
            if rows:
                mid = int(rows[0]["id"])
                self.update_message(mid, status="pending", reason="处理中", handled_at=None,
                                    content=kw.get("content", ""), video_url=kw.get("video_url", "") or "")
                return mid
            kw.setdefault("status", "pending")
            kw.setdefault("reason", "处理中")
            return self.add_message(**kw) or None

    def settle_dm_leftovers(self, user: str, keep_mid: int) -> int:
        """回完 user 的私信以后：TA 以前没回到的那几批（被打断的、以前丢下的）这次已经一起看过、一起回了，
        不再挂在“待你处理”里。"""
        rows = self.query("SELECT id, status, reason FROM messages WHERE kind='dm' AND user_name=? AND id != ?",
                          (user, keep_mid))
        ids = [r["id"] for r in rows if self.revivable(r["status"], r["reason"] or "")]
        for i in ids:
            self.update_message(i, status="merged", reason="后来和 TA 的新消息一起看、一起回了", handled_at=now_str())
        return len(ids)

    def comment_history(self, user: str, video_url: str = "", exclude: int = 0, limit: int = 6) -> list[dict]:
        """评论区里，这个人以前找过她的几次（同一个视频下的排在前面），每条：对方说的、她回的、在哪个视频。"""
        rows = self.query(
            "SELECT id, kind, content, reply, video_title, video_url, created_at FROM messages "
            "WHERE user_name=? AND kind IN ('at','reply','work') AND id != ? AND status IN ('replied','manual','merged') "
            "ORDER BY id DESC LIMIT 30", (user, exclude))
        same = [r for r in rows if video_url and r["video_url"] == video_url]
        other = [r for r in rows if r not in same]
        return list(reversed((same + other)[:limit]))

    COMMENT_KEEP_DAYS = 5      # 评论区的消息记录：和这个人没有新一轮对话以后，再留几天
    GONE_KEEP_DAYS = 180       # 回执留多久（抖音“互动消息”里的旧通知早就翻不到了）

    def purge_comment_messages(self, days: int | None = None, now: datetime | None = None) -> int:
        """删掉评论区里已经聊完的消息记录（@ 她的、回复她评论的）：同一个人在评论区最后一次有动静
        （对方新发的，或者她 / 你回复的）过去 days 天以后，这个人在评论区的记录整组删除。
        期间只要有新一轮对话，整组都留着、重新计时。私信记录不删。返回删了几条。"""
        days = self.COMMENT_KEEP_DAYS if days is None else days
        now = now or datetime.now()
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            users = [r["user_name"] for r in self.query(
                "SELECT user_name, MAX(MAX(created_at), MAX(COALESCE(handled_at, ''))) AS last FROM messages "
                "WHERE kind IN ('at','reply','work') GROUP BY user_name HAVING last < ?", (cutoff,))]
            n = 0
            for u in users:
                where = "kind IN ('at','reply','work') AND " + ("user_name IS NULL" if u is None else "user_name=?")
                args = () if u is None else (u,)
                self.execute(
                    "INSERT OR IGNORE INTO gone_messages(ext_id, kind, reply, gone_at) "
                    f"SELECT ext_id, kind, CASE WHEN status IN ('replied','manual') THEN COALESCE(reply, '') ELSE '' END, ? "
                    f"FROM messages WHERE {where} AND ext_id IS NOT NULL", (now.strftime("%Y-%m-%d %H:%M:%S"), *args))
                n += int(self.execute(f"DELETE FROM messages WHERE {where}", args).rowcount or 0)
            old = (now - timedelta(days=self.GONE_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            self.execute("DELETE FROM gone_messages WHERE gone_at < ?", (old,))
        return n

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
            "at": "kind IN ('at','reply','work')",
            "dm": "kind='dm'",
            "pending": "status='pending'",
            "skipped": "status LIKE 'skipped%'",
        }.get(flt, "1=1")
        return self.query(f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?", (limit,))

    def message_counts(self) -> dict:
        rows = self.query(
            """SELECT
                 COUNT(*) AS all_count,
                 SUM(kind IN ('at','reply','work')) AS at_count,
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

    def thread_replies_today(self, user_name: str) -> int:
        """今天她在评论区和这个人“接着聊”回了几次（回复她评论的那种；发出去了、或者可能发出去了都算）。"""
        rows = self.query(
            "SELECT COUNT(*) AS n FROM messages WHERE kind='reply' AND user_name=? AND status IN ('replied', 'uncertain') "
            "AND substr(handled_at,1,10)=?", (user_name, today_str()))
        return int(rows[0]["n"])

    # ---------- 私信上下文 ----------
    def add_dm(self, user_name: str, role: str, content: str) -> None:
        self.execute("INSERT INTO dm_history(user_name, role, content, ts) VALUES(?,?,?,?)",
                     (user_name, role, content, now_str()))

    def dm_history(self, user_name: str, limit: int = 10) -> list[dict]:
        rows = self.query("SELECT role, content FROM dm_history WHERE user_name=? ORDER BY id DESC LIMIT ?",
                          (user_name, limit))
        return list(reversed(rows))

    def dm_lines(self, names: list[str], since: str = "", limit: int = 10) -> list[dict]:
        """这几个名字（同一个人）最近的私信：[{user_name, role, content, ts}]，按时间顺序。"""
        names = [n for n in names if n]
        if not names:
            return []
        rows = self.query(f"SELECT user_name, role, content, ts FROM dm_history WHERE user_name IN "
                          f"({','.join('?' * len(names))}) AND ts >= ? ORDER BY id DESC LIMIT ?",
                          (*names, since or "", limit))
        return list(reversed(rows))

    def comment_lines(self, names: list[str], since: str = "", limit: int = 6) -> list[dict]:
        """这几个名字（同一个人）最近在评论区和她的来回（找她的、回复她评论的、评论她作品的，
        还有她刷视频时回复过 TA 的评论）：[{ts, where, them, me}]，按时间顺序。"""
        names = [n for n in names if n]
        if not names:
            return []
        marks = ",".join("?" * len(names))
        out = [{"ts": r["created_at"], "where": r["video_title"] or "", "them": r["content"] or "", "me": r["reply"] or "",
                "kind": r["kind"]}
               for r in self.query(
                   f"SELECT kind, content, reply, video_title, created_at FROM messages WHERE user_name IN ({marks}) "
                   "AND kind IN ('at','reply','work') AND status IN ('replied','manual','merged','uncertain') "
                   "AND created_at >= ? ORDER BY id DESC LIMIT ?", (*names, since or "", limit))]
        out += [{"ts": r["created_at"], "where": r["video_title"] or "", "them": r["text"] or "", "me": r["reply"],
                 "kind": "my_reply"}
                for r in self.query(
                    f"SELECT text, reply, video_title, created_at FROM comment_actions WHERE user_name IN ({marks}) "
                    "AND reply != '' AND created_at >= ? ORDER BY created_at DESC LIMIT ?", (*names, since or "", limit))]
        out.sort(key=lambda x: x["ts"])
        return out[-limit:]

    # ---------- 她发过的作品 ----------
    def save_my_works(self, works: list[dict]) -> int:
        """存从自己主页读到的作品数据，顺便认出是哪个草稿发的（文案对得上，或者发布时间差不多）。返回认出了几个。"""
        from datetime import datetime as _dt
        sq = lambda t: re.sub(r"\s+|#\S+", "", t or "")  # noqa: E731
        posted = self.query("SELECT id, title, text, published_at FROM drafts WHERE status='published' AND published_at != '' "
                            "ORDER BY published_at DESC LIMIT 60")
        matched = 0
        for w in works:
            when = _dt.fromtimestamp(w["create_time"]).strftime("%Y-%m-%d %H:%M:%S") if w.get("create_time") else ""
            desc = sq(w.get("desc"))
            did = 0
            for d in posted:
                t, x = sq(d["title"])[:8], sq(d["text"])[:10]
                if (x and x in desc) or (t and len(t) >= 4 and t in desc):
                    did = d["id"]
                    break
            if not did and when:
                near = [d for d in posted if abs((_dt.strptime(d["published_at"][:19], "%Y-%m-%d %H:%M:%S")
                                                  - _dt.strptime(when, "%Y-%m-%d %H:%M:%S")).total_seconds()) <= 2 * 3600]
                did = near[0]["id"] if len(near) == 1 else 0
            matched += bool(did)
            self.execute(
                "INSERT INTO my_works(aweme_id, draft_id, desc, posted_at, likes, comments, collects, shares, plays, checked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(aweme_id) DO UPDATE SET draft_id=CASE WHEN excluded.draft_id>0 "
                "THEN excluded.draft_id ELSE my_works.draft_id END, desc=excluded.desc, likes=excluded.likes, "
                "comments=excluded.comments, collects=excluded.collects, shares=excluded.shares, plays=excluded.plays, "
                "checked_at=excluded.checked_at",
                (w["aweme_id"], did, w.get("desc") or "", when, int(w.get("likes") or 0), int(w.get("comments") or 0),
                 int(w.get("collects") or 0), int(w.get("shares") or 0), int(w.get("plays") or 0), now_str()))
        return matched

    def recent_posts(self, days: int = 30, limit: int = 6) -> list[dict]:
        """她最近发出去的作品，新的在前：[{id, title, text, day, likes, comments, collects, shares, said: [{user, text, reply}]}]
        （点赞这些从自己主页读的，没读到是 None；said 是别人在作品下的评论和她的回复）。"""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for d in self.query("SELECT id, title, text, published_at FROM drafts WHERE status='published' AND published_at >= ? "
                            "ORDER BY published_at DESC LIMIT ?", (since, limit)):
            w = self.query("SELECT * FROM my_works WHERE draft_id=? ORDER BY checked_at DESC LIMIT 1", (d["id"],))
            w = w[0] if w else None
            said = []
            if w:
                said = [{"user": r["user_name"] or "", "text": r["content"] or "", "reply": r["reply"] or ""}
                        for r in self.query("SELECT user_name, content, reply FROM messages WHERE kind='work' AND video_url LIKE ? "
                                            "ORDER BY id DESC LIMIT 4", (f"%{w['aweme_id']}%",))]
            out.append({"id": d["id"], "title": d["title"], "text": d["text"], "day": d["published_at"][:10],
                        "likes": w["likes"] if w else None, "comments": w["comments"] if w else None,
                        "collects": w["collects"] if w else None, "shares": w["shares"] if w else None, "said": said})
        return out

    def unposted_drafts(self, days: int = 7, limit: int = 4) -> list[dict]:
        """最近写了、但没发出去的草稿（你没让发的、发失败的）。"""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        return self.query("SELECT id, title, text, status, created_at FROM drafts WHERE status IN ('discarded','failed','wish') "
                          "AND created_at >= ? ORDER BY id DESC LIMIT ?", (since, limit))

    # ---------- 同一个人的几个名字 ----------
    def link_person(self, name: str, uid: str) -> None:
        """记下“这个名字是这个主页 id 的人”（私信、评论区里读到主页 id 时）。"""
        name, uid = (name or "").strip(), (uid or "").strip()
        if not name or not uid or uid.startswith("name:"):
            return
        self.execute("INSERT INTO person_ids(name, uid, updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                     "uid=excluded.uid, updated_at=excluded.updated_at", (name, uid, now_str()))

    def person_aliases(self, name: str) -> list[str]:
        """和 name 是同一个人的所有名字（name 自己排第一）：主页 id 一样的（私信列表、评论区、互关好友里的），
        以及去掉 emoji、标点以后一字不差的（“吃白饭的鲸🐳”和“吃白饭的鲸”）。"""
        from .names import name_key, uid_of
        name = (name or "").strip()
        if not name:
            return []
        out = [name]
        uids = {r["uid"] for r in self.query("SELECT uid FROM person_ids WHERE name=?", (name,)) if r["uid"]}
        uids |= {r["uid"] for r in self.query("SELECT uid FROM friends WHERE nickname=? AND uid NOT LIKE 'name:%'",
                                               (name,)) if r["uid"]}
        for r in self.query("SELECT url FROM dm_users WHERE name=? AND url != ''", (name,)):
            if uid_of(r["url"]):
                uids.add(uid_of(r["url"]))
        for uid in uids:
            out += [r["name"] for r in self.query("SELECT name FROM person_ids WHERE uid=?", (uid,))]
            out += [r["nickname"] for r in self.query("SELECT nickname FROM friends WHERE uid=?", (uid,))]
            out += [r["name"] for r in self.query("SELECT name FROM dm_users WHERE url LIKE ?", (f"%/user/{uid}%",))]
        key = name_key(name)
        if len(key) >= 2:
            pool = {r["n"] for r in self.query("SELECT DISTINCT user_name AS n FROM dm_history")}
            pool |= {r["n"] for r in self.query("SELECT name AS n FROM people")}
            pool |= {r["n"] for r in self.query("SELECT DISTINCT user_name AS n FROM messages WHERE user_name != '' "
                                                 "ORDER BY id DESC LIMIT 2000")}
            out += [n for n in pool if n and name_key(n) == key]
        return list(dict.fromkeys(n for n in out if n))

    # ---------- 不回复名单 ----------
    # ---------- 别人的评论 ----------
    def comment_done(self, cid: str) -> bool:
        return bool(cid) and bool(self.query("SELECT 1 FROM comment_actions WHERE cid=? UNION ALL "
                                             "SELECT 1 FROM seen_comments WHERE cid=? LIMIT 1", (cid, cid)))

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
            if not rows and key.isdigit():
                old = self.keeper.find("videos", int(key))       # 归档了的旧视频
                rows = [old] if old else []
            return _video_detail(rows[0]) if rows else None
        if kind == "cmt":
            rows = self.query("SELECT * FROM comment_actions WHERE cid=?", (key,))
            if not rows:
                old = self.keeper.find_by("comment_actions", "cid", key)
                rows = [old] if old else []
            return _comment_detail(rows[0]) if rows else None
        if kind == "msg":
            rows = self.query("SELECT * FROM messages WHERE id=?", (key,))
            return _message_detail(rows[0]) if rows else None
        return None

    def activity(self, kind: str, day: str | None = None, limit: int = 100) -> list[dict]:
        """某一天某类互动的详情列表（仪表盘点用量格子时显示）。那一天的记录归档了的，解压出来看。"""
        like = (day or today_str()) + "%"
        if day and kind in self._ARCHIVED_ACTIVITY:
            tbl, keep, fmt = self._ARCHIVED_ACTIVITY[kind]
            old = [r for r in self.keeper.read(tbl, day) if keep(r)]
            if old:
                return [fmt(r) for r in sorted(old, key=lambda r: r.get("_rid") or 0, reverse=True)[:limit]]
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
            mk = {"replies_at": ("at", "work"), "replies_dm": ("dm",), "thread_replies": ("reply",)}[kind]
            rows = self.query("SELECT * FROM messages WHERE kind IN (%s) AND status IN ('replied','manual') "
                              "AND COALESCE(handled_at, created_at) LIKE ? ORDER BY id DESC LIMIT ?"
                              % ",".join("?" * len(mk)), (*mk, like, limit))
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

    _ARCHIVED_ACTIVITY = {
        "comments": ("videos", lambda r: bool(r.get("comment")), _video_detail),
        "likes": ("videos", lambda r: bool(r.get("liked")), _video_detail),
        "collects": ("videos", lambda r: bool(r.get("collected")), _video_detail),
        "at_friends": ("videos", lambda r: str(r.get("comment") or "").startswith("@"), _video_detail),
        "comment_replies": ("comment_actions", lambda r: bool(r.get("reply")), _comment_detail),
        "comment_likes": ("comment_actions", lambda r: bool(r.get("liked")), _comment_detail),
    }

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

    def thread_lines(self, tid: str, limit: int = 30) -> list[dict]:
        rows = self.query("SELECT role, content FROM thread_lines WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                          (tid, limit))
        return list(reversed(rows))

    def close_thread(self, tid: str) -> None:
        self.execute("UPDATE threads SET closed=1 WHERE id=?", (tid,))

    # ---------- 同一个视频只评论一次（永久） ----------
    def commented_video(self, video_id: str) -> bool:
        """这个视频她以前评论过没有（不管过了多久）。评论过就不再发新评论，但别人回复她的还可以接着聊。"""
        if not video_id:
            return False
        return bool(self.query("SELECT 1 FROM videos WHERE video_id=? AND comment IS NOT NULL AND comment != '' "
                               "UNION ALL SELECT 1 FROM seen_videos WHERE video_id=? AND commented=1 LIMIT 1",
                               (video_id, video_id)))

    # ---------- 主人 ----------
    def owners(self, enabled_only: bool = False) -> list[dict]:
        return self.query("SELECT * FROM owners" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY created_at")

    def owner(self, name: str) -> dict | None:
        rows = self.query("SELECT * FROM owners WHERE name=? AND enabled=1", ((name or "").strip(),))
        return rows[0] if rows else None

    def save_owners(self, items: list[dict]) -> list[str]:
        """整份保存主人名单（界面上改完一起存）。亲密度、上次主动联系的时间保留。

        新添加的主人必须是互关好友（从抖音“关注”列表里核实过的）；不是的不保存，名字放在返回值里。
        以前已经设好的主人不受影响（照旧保留）。"""
        keep = {o["name"]: o for o in self.owners()}
        mutual = set(self.mutual_names())
        names, refused = [], []
        for it in items or []:
            name = str(it.get("name") or "").strip()[:40]
            if not name or name in names:
                continue
            if name not in keep and name not in mutual:
                refused.append(name)
                continue
            names.append(name)
            old = keep.get(name) or {}
            self.execute(
                "INSERT INTO owners(name, call, relation, enabled, chat, share, at, cmd, closeness, last_proactive, "
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET call=excluded.call, "
                "relation=excluded.relation, enabled=excluded.enabled, chat=excluded.chat, share=excluded.share, "
                "at=excluded.at, cmd=excluded.cmd",
                (name, str(it.get("call") or "")[:20], str(it.get("relation") or "")[:200],
                 int(bool(it.get("enabled", True))), int(bool(it.get("chat", True))), int(bool(it.get("share", True))),
                 int(bool(it.get("at", True))), int(bool(it.get("cmd", old.get("cmd") or False))),
                 float(old.get("closeness") or 0), old.get("last_proactive") or "", old.get("created_at") or now_str()))
        for name in keep:
            if name not in names:
                self.execute("DELETE FROM owners WHERE name=?", (name,))
        return refused

    def owner_uid(self, name: str) -> str:
        """主人在互关好友名单里的主页 id（sec_uid）：用来认准私信里的是不是主人本人（昵称可以重名，主页 id 不会）。
        名单里没有、只有名字没有 id、或者有两个同名的互关好友，都返回空。"""
        rows = self.query("SELECT uid FROM friends WHERE nickname=? AND mutual=1 AND uid NOT LIKE 'name:%'",
                          ((name or "").strip(),))
        return rows[0]["uid"] if len(rows) == 1 else ""

    def friend_uid(self, name: str) -> str:
        """互关好友的主页 id（同名的有两个以上时分不清，返回空）。"""
        return self.owner_uid(name)

    def owner_unanswered(self, name: str) -> tuple[int, str]:
        """她最近连着给这位主人发了几句、对方还没回，以及最后一句是什么时候发的
        （主动找主人聊天前看一眼：连着几句都没回就先不打扰）。表情包不算一句。"""
        n, last = 0, ""
        for r in self.query("SELECT role, content, ts FROM dm_history WHERE user_name=? ORDER BY id DESC LIMIT 12",
                            (name,)):
            if r["role"] != "me":
                break
            last = last or r["ts"]
            if not str(r["content"] or "").startswith("[发了一张表情包"):
                n += 1
        return n, last

    def owner_touch(self, name: str, closer: float = 0.0, proactive: bool = False) -> None:
        if closer:
            self.execute("UPDATE owners SET closeness = MIN(100, closeness + ?) WHERE name=?", (closer, name))
        if proactive:
            self.execute("UPDATE owners SET last_proactive=? WHERE name=?", (now_str(), name))

    def reply_parent(self, mine: str) -> dict | None:
        """她这条回复当初是回在谁的评论下面的（楼主那条评论的 id、作者、内容）；不是回复别人的返回 None。"""
        if not mine:
            return None
        rows = self.query("SELECT cid, user_name, text FROM comment_actions WHERE reply=? "
                          "ORDER BY created_at DESC LIMIT 1", (mine,))
        return rows[0] if rows else None

    def my_comment_texts(self, limit: int = 300) -> list[str]:
        """她最近发过的评论和回复（用来认出别人回复的是她哪条评论）。"""
        rows = self.query(
            "SELECT comment AS t FROM videos WHERE comment != '' AND comment IS NOT NULL "
            "UNION ALL SELECT reply AS t FROM comment_actions WHERE reply != '' "
            "UNION ALL SELECT reply AS t FROM messages WHERE kind IN ('at','reply','work') AND status IN ('replied','manual') "
            "AND reply IS NOT NULL AND reply != '' "
            "UNION ALL SELECT reply AS t FROM gone_messages WHERE reply != '' LIMIT ?", (limit,))
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
        if info.get("manual") is not None:
            sets.append("manual=?")
            vals.append(1 if info["manual"] else 0)
        self.execute(f"UPDATE friends SET {', '.join(sets)} WHERE uid=?", (*vals, uid))

    def mutual_names(self) -> list[str]:
        """从抖音“关注”列表里核实过互相关注的好友的名字（手动添加的不算）：只有他们可以设为主人。"""
        return [r["nickname"] for r in self.query(
            "SELECT DISTINCT nickname FROM friends WHERE mutual=1 AND manual=0 AND nickname != '' ORDER BY nickname")]

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

    def delete_draft(self, did: int) -> None:
        self.execute("DELETE FROM drafts WHERE id=?", (int(did),))

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
