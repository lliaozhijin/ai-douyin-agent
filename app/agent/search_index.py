"""记忆的全文索引：记忆越来越多，也能马上找到“和现在聊的事有关的那几条”。

以前想起一件事，是把最近的 400 条记忆读出来一条条比；记忆多了，更早的事就“想不起来”了，也越来越慢。
现在用 SQLite 自带的 FTS5 全文索引（倒排索引 + BM25 打分），在全部记忆里找：
- FTS5 自带的分词器不会切中文（整句当成一个词），所以先在 Python 里把中文切成“相邻两个字一组”
  （“团子是橘猫” → 团子 子是 是橘 橘猫），英文和数字按单词，再交给 FTS5 建索引——中文搜索的常用做法。
- 索引表是“无内容”的（content=''，只存索引、不再存一份原文），占的空间小；
  SQLite 太旧、不支持删除无内容索引里的行时，退回普通 FTS5 表；连 FTS5 都没有时，退回原来的逐条比较。
- 查的时候把问题也切成两字一组，用 OR 连起来，按 BM25 排出最相关的一批，再按“强度 × 新近度”细排（memory.recall）。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_WORD = re.compile(r"[A-Za-z0-9]+")
MAX_QUERY_TOKENS = 40


def tokens(text: str) -> list[str]:
    """切词：中文相邻两个字一组（只有一个字的就单独一个），英文 / 数字按单词（小写）。"""
    out: list[str] = []
    t = text or ""
    for m in _CJK.finditer(t):
        run = m.group(0)
        if len(run) == 1:
            out.append(run)
        else:
            out += [run[i:i + 2] for i in range(len(run) - 1)]
    out += [w.lower() for w in _WORD.findall(t) if len(w) > 1 or w.isdigit()]
    return out


def doc(text: str) -> str:
    return " ".join(tokens(text))


def match_query(text: str) -> str:
    """问题 → FTS5 查询：两字一组用 OR 连起来（每个都加引号，不会被当成语法）。"""
    seen: list[str] = []
    for tk in tokens(text):
        if tk not in seen:
            seen.append(tk)
        if len(seen) >= MAX_QUERY_TOKENS:
            break
    return " OR ".join('"' + tk.replace('"', "") + '"' for tk in seen)


class TextIndex:
    """一个全文索引（一张 FTS5 表），行号就是被索引的那条记录的 id。"""

    def __init__(self, db, name: str):
        self.db = db
        self.name = name
        self.ok = False
        self.contentless = False
        self._create()

    def _create(self) -> None:
        with self.db._lock:
            for ddl, contentless in (
                    (f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.name} USING fts5(tok, content='', contentless_delete=1)",
                     True),
                    (f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.name} USING fts5(tok)", False)):
                try:
                    self.db.conn.execute(ddl)
                    self.db.conn.commit()
                    self.ok, self.contentless = True, contentless
                    # 已经建过的表：看看是哪一种
                    sql = self.db.conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (self.name,)).fetchone()
                    self.contentless = bool(sql and "content=''" in (sql[0] or ""))
                    return
                except Exception as e:  # noqa: BLE001
                    log.info("全文索引 %s 用不了这种建法（%s），换一种", self.name, e)
        log.warning("这台电脑的 SQLite 没有 FTS5 全文索引：记忆检索退回逐条比较")

    def put(self, rid: int, text: str) -> None:
        if not self.ok or not rid:
            return
        try:
            with self.db._lock:
                self.db.conn.execute(f"DELETE FROM {self.name} WHERE rowid=?", (rid,))
                self.db.conn.execute(f"INSERT INTO {self.name}(rowid, tok) VALUES(?, ?)", (rid, doc(text)))
                self.db.conn.commit()
        except Exception:  # noqa: BLE001 —— 索引坏了不影响记忆本身，下次重建
            log.warning("更新全文索引 %s 失败", self.name, exc_info=True)

    def drop(self, rid: int) -> None:
        if not self.ok:
            return
        try:
            self.db.execute(f"DELETE FROM {self.name} WHERE rowid=?", (rid,))
        except Exception:  # noqa: BLE001
            log.warning("删全文索引 %s 的一行失败", self.name, exc_info=True)

    def search(self, text: str, limit: int = 60) -> list[tuple[int, float]]:
        """最相关的几条：[(id, BM25 分数，越小越相关)]。"""
        if not self.ok:
            return []
        q = match_query(text)
        if not q:
            return []
        try:
            rows = self.db.query(f"SELECT rowid AS id, bm25({self.name}) AS s FROM {self.name} WHERE {self.name} MATCH ? "
                                 f"ORDER BY s LIMIT ?", (q, limit))
        except Exception:  # noqa: BLE001
            log.warning("全文检索 %s 失败", self.name, exc_info=True)
            return []
        return [(int(r["id"]), float(r["s"])) for r in rows]

    def count(self) -> int:
        if not self.ok:
            return 0
        try:
            return int(self.db.query(f"SELECT COUNT(*) AS n FROM {self.name}")[0]["n"])
        except Exception:  # noqa: BLE001
            return -1

    def rebuild(self, rows: list[tuple[int, str]]) -> None:
        """整个重建（升级后第一次用、或者条数对不上时）。"""
        if not self.ok:
            return
        with self.db._lock:
            self.db.conn.execute(f"DELETE FROM {self.name}")
            self.db.conn.executemany(f"INSERT INTO {self.name}(rowid, tok) VALUES(?, ?)",
                                     [(rid, doc(t)) for rid, t in rows])
            self.db.conn.commit()

    def sync(self, sql: str, table: str = "") -> bool:
        """条数和源表对不上（比如老版本留下的数据）就重建。sql 选出 (id, t 文字)；table 给了先只数条数。
        返回有没有重建。"""
        if not self.ok:
            return False
        if table:
            n = int(self.db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
            if n == self.count():
                return False
        src = self.db.query(sql)
        if self.count() == len(src):
            return False
        self.rebuild([(int(r["id"]), r["t"] or "") for r in src])
        log.info("重建全文索引 %s：%d 条", self.name, len(src))
        return True
