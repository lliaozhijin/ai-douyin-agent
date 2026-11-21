"""记忆仓库整理：她用得越久，记下的东西越多。这里让数据库长期运行也不变得臃肿，查起来还快。

思路（都有出处，见 README“存储优化”一节）：
1. **冷热分层**：最近的记录（热数据）照常放在表里，查起来最快；过了“热数据期”的旧记录（冷数据）
   按天 / 按月打包成一块，压缩后放进 archive 表，原来的行删掉。历史记录页、详情查看照样能读到（读的时候解压那一块）。
2. **分块 + 预置字典压缩**：一条条短记录单独压缩几乎压不小（压缩算法没有“前文”可学）；
   把同一天的几百条打成一块压缩，再给每张表训练一份“预置字典”（zlib 的 zdict，最多 32KB，
   常见的字段名、固定说法都在里面），小块也能压得很小。块大的时候再试一下 xz（LZMA），哪个小用哪个。
   这和 zstd 的字典训练、RocksDB 的“预置字典”、sqlite-zstd 的“按块训练字典的行级压缩”是同一个道理，
   只用 Python 自带的 zlib / lzma，不用额外装组件。
3. **只留去重用的“指纹”**：旧视频、旧评论归档以后，只在很小的 seen_videos / seen_comments 表里
   留一个 id（WITHOUT ROWID），“这个视频看过没有、评论过没有”照样秒查。
4. **回收空间**：数据库改成增量自动回收（auto_vacuum=INCREMENTAL，第一次整理时 VACUUM 一次），
   以后每次整理用 incremental_vacuum 把空出来的页还给磁盘；WAL 日志超过 4MB 就截断；PRAGMA optimize 维护查询计划。
5. **磁盘上的其他大头**：浏览器缓存（只删缓存，不碰登录状态）、调试用的页面快照（只留最近的）。

不做的：私信的消息记录（消息中心里一直能看到）不归档；被忘掉的记忆另有 memory_archive（见 memory.py），
压缩后留着，被提起时还能想起来。
"""
from __future__ import annotations

import json
import logging
import lzma
import os
import shutil
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# 各表的热数据期（天）：超过的按 天 / 月 打包归档
HOT_DAYS = {"events": 45, "videos": 45, "comment_actions": 60, "thread_lines": 30, "owner_shares": 180}
TIME_COL = {"events": "ts", "videos": "created_at", "comment_actions": "created_at", "thread_lines": "ts",
            "owner_shares": "created_at", "dm_history": "ts"}
BY_DAY = ("events", "videos")                 # 按天打包（历史记录页按天看）；其余按月
DM_KEEP_PER_USER = 300                         # 每个人的私信上下文留最近这么多条在表里（回复只用最近 24 条）
BATCH_ROWS = 20000                             # 一批最多归档这么多行
MAINTAIN_SECONDS = 60                          # 一次整理最多花这么久（剩下的下次接着做）
ZDICT_MAX = 32 * 1024                          # zlib 预置字典最多 32KB（窗口大小）
XZ_FROM = 16 * 1024                            # 原文超过这么大时也试一下 xz
DICT_REFRESH_DAYS = 180

# 浏览器缓存（Chromium 的缓存目录，删了会自动重建；Cookies、Local Storage 不在里面，不影响登录）
CACHE_DIRS = ("Default/Cache", "Default/Code Cache", "Default/GPUCache", "Default/DawnCache",
              "Default/DawnGraphiteCache", "Default/DawnWebGPUCache", "Default/Service Worker/CacheStorage",
              "Default/Service Worker/ScriptCache", "ShaderCache", "GrShaderCache", "GraphiteDawnCache")
CACHE_LIMIT = 300 * 1024 * 1024                # 浏览器缓存超过 300MB 才清
DEBUG_KEEP_DAYS = 14
DEBUG_LIMIT = 150 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS archive (       -- 归档的旧记录：同一张表、同一天 / 同一月的记录打成一块压缩
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl        TEXT NOT NULL,
    period     TEXT NOT NULL,              -- 2026-10-01（按天）/ 2026-10（按月）
    first_rid  INTEGER NOT NULL DEFAULT 0, -- 这一块里原来的行号范围（按 id 找详情用）
    last_rid   INTEGER NOT NULL DEFAULT 0,
    n          INTEGER NOT NULL DEFAULT 0,
    raw_bytes  INTEGER NOT NULL DEFAULT 0,
    codec      TEXT NOT NULL,              -- zd 预置字典的 zlib / z 普通 zlib / xz LZMA
    dict_id    INTEGER NOT NULL DEFAULT 0,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_period ON archive(tbl, period);
CREATE INDEX IF NOT EXISTS idx_archive_rid ON archive(tbl, first_rid, last_rid);
CREATE TABLE IF NOT EXISTS zdicts (        -- 每张表训练出来的压缩字典（旧字典留着，解压旧块要用）
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl        TEXT NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_videos (   -- 归档了的视频只留 id：看过没有、评论过没有
    video_id  TEXT PRIMARY KEY,
    commented INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seen_comments ( -- 归档了的“别人的评论”只留 id：赞过 / 回过的不再重复
    cid TEXT PRIMARY KEY
) WITHOUT ROWID;
"""


# ---------------------------------------------------------------- 压缩
def train_zdict(samples: list[bytes], size: int = ZDICT_MAX) -> bytes:
    """用样本做 zlib 的预置字典：出现得多的片段放在后面（zlib 离得近的匹配更省）。
    做法很简单：样本去重，按出现次数从少到多排，拼起来留最后 size 个字节。"""
    counts: dict[bytes, int] = {}
    for s in samples:
        counts[s] = counts.get(s, 0) + 1
    ordered = sorted(counts, key=lambda s: counts[s])
    return b"\n".join(ordered)[-size:]


def pack(raw: bytes, zdict: bytes | None = None) -> tuple[str, bytes]:
    """压缩一块数据：有字典用带字典的 zlib；块够大再试 xz；哪个小用哪个。"""
    if zdict:
        c = zlib.compressobj(9, zlib.DEFLATED, -15, 9, zlib.Z_DEFAULT_STRATEGY, zdict)
        best = ("zd", c.compress(raw) + c.flush())
    else:
        best = ("z", zlib.compress(raw, 9))
    if len(raw) >= XZ_FROM:
        x = lzma.compress(raw, preset=6)
        if len(x) < len(best[1]):
            best = ("xz", x)
    return best


def unpack(codec: str, data: bytes, zdict: bytes | None = None) -> bytes:
    if codec == "zd":
        d = zlib.decompressobj(-15, zdict or b"")
        return d.decompress(data) + d.flush()
    if codec == "z":
        return zlib.decompress(data)
    if codec == "xz":
        return lzma.decompress(data)
    if codec == "raw":
        return data
    raise ValueError(f"不认识的压缩格式 {codec}")


def _jl(rows: list[dict]) -> bytes:
    return "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"), default=str) for r in rows).encode("utf-8")


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


class Keeper:
    """数据库的整理员：归档旧记录、读归档、回收空间、统计占用。"""

    def __init__(self, db):
        self.db = db
        self._dicts: dict[int, bytes] = {}
        with db._lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()

    # ---------- 字典 ----------
    def _dict_for(self, tbl: str, samples: list[bytes]) -> tuple[int, bytes]:
        """这张表现在用的字典；没有（或者太旧了）就用样本训练一份新的。"""
        rows = self.db.query("SELECT id, data, created_at FROM zdicts WHERE tbl=? ORDER BY id DESC LIMIT 1", (tbl,))
        if rows:
            try:
                age = datetime.now() - datetime.strptime(rows[0]["created_at"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                age = timedelta(0)
            if age < timedelta(days=DICT_REFRESH_DAYS) or len(samples) < 50:
                self._dicts[rows[0]["id"]] = rows[0]["data"]
                return rows[0]["id"], rows[0]["data"]
        if len(samples) < 20:
            return 0, b""
        data = train_zdict(samples)
        did = self.db.execute("INSERT INTO zdicts(tbl, data, created_at) VALUES(?,?,?)",
                              (tbl, data, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
        self._dicts[did] = data
        return did, data

    def _dict(self, did: int) -> bytes:
        if not did:
            return b""
        if did not in self._dicts:
            rows = self.db.query("SELECT data FROM zdicts WHERE id=?", (did,))
            self._dicts[did] = rows[0]["data"] if rows else b""
        return self._dicts[did]

    def compress_text(self, tbl: str, raw: bytes, samples: list[bytes] | None = None) -> tuple[str, int, bytes]:
        """压缩单条小记录（被忘掉的记忆用）：用这张表的字典。"""
        did, zd = self._dict_for(tbl, samples or [])
        codec, data = pack(raw, zd or None)
        return codec, did, data

    def decompress(self, codec: str, did: int, data: bytes) -> bytes:
        return unpack(codec, data, self._dict(did))

    # ---------- 归档 ----------
    def _period(self, tbl: str, ts: str) -> str:
        return (ts or "")[:10] if tbl in BY_DAY else (ts or "")[:7]

    def archive_table(self, tbl: str, cutoff: str, extra_where: str = "", args: tuple = (),
                      limit: int = BATCH_ROWS) -> int:
        """把 tbl 里时间早于 cutoff 的记录打包归档。返回归档了几行。"""
        col = TIME_COL[tbl]
        rows = self.db.query(f"SELECT rowid AS _rid, * FROM {tbl} WHERE {col} < ? AND {col} != '' {extra_where} "
                             f"ORDER BY rowid LIMIT ?", (cutoff, *args, limit))
        if not rows:
            return 0
        samples = [_jl([r]) for r in self.db.query(f"SELECT rowid AS _rid, * FROM {tbl} ORDER BY rowid DESC LIMIT 400")]
        did, zd = self._dict_for(tbl, samples)
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(self._period(tbl, r[col]), []).append(r)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tally = None
        if tbl == "events":     # 归档掉的各种操作的次数留个总数（成长经验等统计要用，不然归档后会“变少”）
            tally = self.db.get_json("archived_kinds:events", {}) or {}
            for r in rows:
                tally[r.get("kind") or ""] = int(tally.get(r.get("kind") or "", 0)) + 1
        with self.db._lock:
            cur = self.db.conn.cursor()
            try:
                cur.execute("BEGIN")
                if tally is not None:
                    cur.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                                "value=excluded.value", ("archived_kinds:events", json.dumps(tally, ensure_ascii=False)))
                for period, grp in groups.items():
                    raw = _jl(grp)
                    codec, data = pack(raw, zd or None)
                    cur.execute("INSERT INTO archive(tbl, period, first_rid, last_rid, n, raw_bytes, codec, dict_id, "
                                "data, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (tbl, period, grp[0]["_rid"], grp[-1]["_rid"], len(grp), len(raw), codec,
                                 did if codec == "zd" else 0, data, now))
                    if tbl == "videos":
                        cur.executemany("INSERT INTO seen_videos(video_id, commented) VALUES(?,?) ON CONFLICT(video_id) "
                                        "DO UPDATE SET commented = MAX(commented, excluded.commented)",
                                        [(r["video_id"], int(bool(r.get("comment"))))
                                         for r in grp if r.get("video_id")])
                    if tbl == "comment_actions":
                        cur.executemany("INSERT OR IGNORE INTO seen_comments(cid) VALUES(?)",
                                        [(r["cid"],) for r in grp if r.get("cid")])
                    ids = [r["_rid"] for r in grp]
                    for i in range(0, len(ids), 500):
                        part = ids[i:i + 500]
                        cur.execute(f"DELETE FROM {tbl} WHERE rowid IN ({','.join('?' * len(part))})", part)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return len(rows)

    def archived_count(self, tbl: str, kinds: tuple | list) -> int:
        """归档掉的某几种记录一共多少条（目前只统计 events 的 kind）。"""
        tally = self.db.get_json(f"archived_kinds:{tbl}", {}) or {}
        return sum(int(tally.get(k, 0)) for k in kinds)

    def _blocks(self, tbl: str, period: str) -> list[dict]:
        return self.db.query("SELECT * FROM archive WHERE tbl=? AND period=? ORDER BY id", (tbl, period))

    def _rows_of(self, block: dict) -> list[dict]:
        raw = self.decompress(block["codec"], block["dict_id"], block["data"])
        return [json.loads(line) for line in raw.decode("utf-8").split("\n") if line]

    def read(self, tbl: str, day: str) -> list[dict]:
        """归档里某一天的记录（按月打包的表，读那个月再挑出这一天）。"""
        period = day if tbl in BY_DAY else day[:7]
        col = TIME_COL[tbl]
        out = []
        for b in self._blocks(tbl, period):
            out += [r for r in self._rows_of(b) if str(r.get(col) or "").startswith(day)]
        return out

    def find(self, tbl: str, rid: int) -> dict | None:
        """按原来的行号找一条归档的记录。"""
        for b in self.db.query("SELECT * FROM archive WHERE tbl=? AND first_rid<=? AND last_rid>=? ORDER BY id",
                               (tbl, rid, rid)):
            for r in self._rows_of(b):
                if int(r.get("_rid") or 0) == int(rid):
                    return r
        return None

    def find_by(self, tbl: str, key: str, value) -> dict | None:
        """按某一列找一条归档的记录（比如评论 id）：从新到旧一块块找，找到就停。"""
        for b in self.db.query("SELECT * FROM archive WHERE tbl=? ORDER BY id DESC", (tbl,)):
            for r in self._rows_of(b):
                if r.get(key) == value:
                    return r
        return None

    def days(self, tbl: str = "events") -> dict[str, int]:
        """归档里每一天有几条（按天打包的表）。"""
        out: dict[str, int] = {}
        for r in self.db.query("SELECT period, SUM(n) AS n FROM archive WHERE tbl=? GROUP BY period", (tbl,)):
            out[r["period"]] = int(r["n"] or 0)
        return out

    # ---------- 整理 ----------
    def maintain(self, now: datetime | None = None, vacuum: bool = True) -> dict:
        """整理一次：旧记录归档、私信上下文只留最近的、回收空间。返回做了什么。"""
        now = now or datetime.now()
        t0 = time.time()
        done: dict = {"archived": {}, "dm_trimmed": 0}
        for tbl, days in HOT_DAYS.items():
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            extra, args = "", ()
            if tbl == "owner_shares":
                extra = "AND status != 'sent'"
            n = 0
            try:
                while True:     # 攒了很多旧记录（比如刚升级）：一批批归档，最多花 MAINTAIN_SECONDS 秒，剩下的下次再说
                    k = self.archive_table(tbl, cutoff, extra, args)
                    n += k
                    if k < BATCH_ROWS or time.time() - t0 > MAINTAIN_SECONDS:
                        break
            except Exception:  # noqa: BLE001 —— 一张表出错不影响别的
                log.warning("归档 %s 出错", tbl, exc_info=True)
            if n:
                done["archived"][tbl] = n
        done["dm_trimmed"] = self.trim_dm_history()
        if vacuum:
            done.update(self.reclaim())
        done["seconds"] = round(time.time() - t0, 2)
        if done["archived"] or done["dm_trimmed"]:
            log.info("整理记忆仓库：%s", done)
        self.db.set_json("storage_last", {"at": now.strftime("%Y-%m-%d %H:%M:%S"), **{k: v for k, v in done.items()}})
        return done

    def trim_dm_history(self) -> int:
        """私信上下文：每个人只在表里留最近 DM_KEEP_PER_USER 条，更早的按月归档（回复只用最近 24 条）。"""
        users = self.db.query("SELECT user_name, COUNT(*) AS n FROM dm_history GROUP BY user_name HAVING n > ?",
                              (DM_KEEP_PER_USER,))
        total = 0
        for u in users:
            keep_from = self.db.query("SELECT id FROM dm_history WHERE user_name=? ORDER BY id DESC LIMIT 1 OFFSET ?",
                                      (u["user_name"], DM_KEEP_PER_USER - 1))
            if not keep_from:
                continue
            total += self.archive_table("dm_history", "9999", "AND user_name=? AND id < ?",
                                        (u["user_name"], keep_from[0]["id"]))
        return total

    def reclaim(self) -> dict:
        """把空出来的空间还给磁盘。第一次会把数据库改成“增量自动回收”（要 VACUUM 一次）。"""
        out = {}
        db = self.db
        with db._lock:
            try:
                mode = int(db.conn.execute("PRAGMA auto_vacuum").fetchone()[0])
                free_before = int(db.conn.execute("PRAGMA freelist_count").fetchone()[0])
                if mode != 2:
                    db.conn.commit()
                    db.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                    db.conn.execute("VACUUM")
                    out["vacuumed"] = True
                elif free_before:
                    db.conn.execute(f"PRAGMA incremental_vacuum({free_before})").fetchall()   # 要一步步读完才会真的回收
                    db.conn.commit()
                out["freed_pages"] = free_before
                db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.conn.execute("PRAGMA optimize")
            except Exception as e:  # noqa: BLE001 —— 回收不了不影响用
                log.info("回收数据库空间没成功：%s", e)
        return out

    # ---------- 磁盘 ----------
    @staticmethod
    def clean_browser_cache(profile: Path, limit: int = CACHE_LIMIT, force: bool = False) -> int:
        """浏览器没在运行时，缓存超过 limit 就清掉（只删缓存目录，登录状态不受影响）。返回清掉的字节数。"""
        dirs = [profile / d for d in CACHE_DIRS if (profile / d).exists()]
        size = sum(_dir_size(d) for d in dirs)
        if not dirs or (size < limit and not force):
            return 0
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
        log.info("清掉了浏览器缓存 %.1f MB（登录状态不受影响）", size / 1048576)
        return size

    @staticmethod
    def clean_debug(debug: Path, keep_days: int = DEBUG_KEEP_DAYS, limit: int = DEBUG_LIMIT) -> int:
        """调试用的页面快照：超过 keep_days 天的删掉；总共还超过 limit 就从最旧的删。返回删了多少字节。"""
        if not debug.exists():
            return 0
        files = sorted((f for f in debug.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
        cutoff = time.time() - keep_days * 86400
        total = sum(f.stat().st_size for f in files)
        freed = 0
        for f in files:
            st = f.stat()
            if st.st_mtime < cutoff or total - freed > limit:
                try:
                    f.unlink()
                    freed += st.st_size
                except OSError:
                    pass
        return freed

    def usage(self, paths: dict | None = None) -> dict:
        """各部分占了多少空间（字节）。"""
        db = self.db
        out: dict = {}
        p = Path(db.path)
        out["db"] = p.stat().st_size if p.exists() else 0
        wal = Path(str(p) + "-wal")
        out["wal"] = wal.stat().st_size if wal.exists() else 0
        with db._lock:
            page = int(db.conn.execute("PRAGMA page_size").fetchone()[0])
            out["free"] = page * int(db.conn.execute("PRAGMA freelist_count").fetchone()[0])
        tables = {}
        for t in ("events", "videos", "messages", "dm_history", "memories", "memory_archive", "knowledge",
                  "comment_actions", "content_marks", "archive", "diary"):
            try:
                tables[t] = int(db.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"])
            except Exception:  # noqa: BLE001
                continue
        out["rows"] = tables
        a = db.query("SELECT COALESCE(SUM(raw_bytes),0) AS raw, COALESCE(SUM(length(data)),0) AS stored, "
                     "COALESCE(SUM(n),0) AS n, COUNT(*) AS blocks FROM archive")[0]
        out["archive"] = {"raw": int(a["raw"]), "stored": int(a["stored"]), "rows": int(a["n"]),
                          "blocks": int(a["blocks"]),
                          "ratio": round(a["raw"] / a["stored"], 1) if a["stored"] else 0}
        for name, path in (paths or {}).items():
            out[name] = _dir_size(Path(path))
        out["last"] = db.get_json("storage_last", {}) or {}
        return out
