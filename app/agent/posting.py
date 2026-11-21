"""她怎么写作品：先想、再挑、再写、写完自己读一遍。

做法参考了这几份资料（只借鉴思路）：
- 提示词里的要求越多，模型写出来的东西越套路、越没创造力（CS4，arXiv:2410.04197；
  “What Prompts Don't Say”，arXiv:2505.13360）。所以写作品的提示词里不再列“写什么、怎么写、几张卡片”这些规矩，
  只给她今天的经历、心情、最近发过的东西和大家的反应，其余全由她自己想；
  链接、联系方式、敏感内容这些底线交给程序检查（写完先查一遍，有问题让她自己换个说法；发布前还会再查）。
- 对齐过的大模型容易“模式坍缩”：同一个问题总给出最典型的那个答案（Verbalized Sampling，arXiv:2510.01171）。
  所以先让她把脑子里冒出来的几个想法都列出来、各标一个“我真的会发它的可能性”，程序再挑——
  多半挑那些不那么典型的（可能性低的），这样每次发的东西不一样，不会天天“今日份小确幸”。
- 中文读者觉得“有 AI 味”的地方：三段式、“不是……而是……”、排比、硬凹的比喻、结尾总结升华、
  抽象的漂亮话、心灵鸡汤腔（虎嗅 2025 年对小红书 142 篇“一眼 AI”笔记的整理等）。不在提示词里一条条禁止，
  而是写完以后程序看一眼：这种痕迹多了，就让她像真人那样“又读了一遍”，按自己平时打字的样子改一改。
- 写作品时想起今天的哪些事：参考 Generative Agents（Park et al. 2023）按重要程度、新近、情绪来挑，见 memory.highlights。
"""
from __future__ import annotations

import random
import re

TAIL_P = 0.2          # 可能性不超过这个的算“不那么典型的想法”
TAIL_CHANCE = 0.6     # 有这种想法时，多大概率从里面挑
IDEA_SAME = 0.6       # 想法和最近发过的作品重合到这个程度：算写过了
POST_SAME = 0.4       # 写出来的和最近发过的像到这个程度：让她换一个
TELLS_TO_REREAD = 2   # 有几种 AI 味痕迹就让她再读一遍

_ENDING = re.compile(r"^(愿你|愿我们|愿所有|或许这就是|也许这就是|这就是生活|这大概就是|总之|总而言之|说到底|"
                     r"生活就是|人生就是|所以啊?，?生活|原来[^。！？]{0,12}才是)")
_NOT_BUT = re.compile(r"不是[^。！？!?\n]{1,24}?而是|不只是[^。！？!?\n]{1,24}?(?:更是|还是)|与其说[^。！？!?\n]{1,24}?不如说")
_FANCY = ("时光", "岁月", "治愈", "温柔", "美好", "灵魂", "星辰", "浪漫", "意义", "救赎", "宇宙", "光芒", "诗意", "静好",
          "奔赴", "烟火气", "热爱", "生命力", "仪式感")
_SOUP = re.compile(r"接住|被看见|和自己和解|与自己和解|允许自己|你并不孤单|你值得|一切都会好起来|好好爱自己")


def grams(text: str) -> set[str]:
    t = re.sub(r"[\s\W_]+", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


def similar(a: str, b: str) -> float:
    """两段话有多像（相邻两个字的重合度，0～1）。"""
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def covered(idea: str, post: str) -> float:
    """一个想法有多少已经在这条作品里写过了（0～1）。"""
    gi, gp = grams(idea), grams(post)
    return len(gi & gp) / len(gi) if gi and gp else 0.0


def ai_tells(text: str) -> list[str]:
    """一段话里有哪几种“AI 味”的痕迹（没有就是空列表）。只看很明显的几种，宁可漏看，不冤枉随手写的话。"""
    t = (text or "").strip()
    if len(t) < 20:
        return []
    out = []
    if _NOT_BUT.search(t):
        out.append("“不是……而是……”")
    if sum(w in t for w in ("首先", "其次", "最后", "第一，", "第二，", "第三，")) >= 2:
        out.append("一二三地分条讲")
    sentences = [x.strip() for x in re.split(r"[。！？!?\n]+", t) if x.strip()]
    if len(sentences) >= 3 and _ENDING.search(sentences[-1]):
        out.append("结尾在总结升华")
    clauses = [x.strip() for x in re.split(r"[，。！？；,;!?\n]+", t) if len(x.strip()) >= 3]
    heads: dict[str, int] = {}
    for c in clauses:
        heads[c[:2]] = heads.get(c[:2], 0) + 1
    if clauses and max(heads.values()) >= 3 and len(clauses) >= 4:
        out.append("排比")
    if t.count("——") >= 2:
        out.append("破折号很多")
    if sum(t.count(w) for w in _FANCY) >= 3:
        out.append("抽象的漂亮话很多")
    if _SOUP.search(t):
        out.append("心灵鸡汤的腔调")
    return out


def pick_idea(ideas, avoid: list[str], rnd: random.Random) -> dict | None:
    """从她列出的几个想法里挑一个：最近写过的不挑；多半从不那么典型的里面挑（见文件开头）。"""
    cands = []
    for it in ideas if isinstance(ideas, list) else []:
        if isinstance(it, str):
            it = {"idea": it}
        if not isinstance(it, dict):
            continue
        text = str(it.get("idea") or it.get("text") or "").strip()
        if not text:
            continue
        try:
            p = min(1.0, max(0.0, float(it.get("p", it.get("probability", 0.2)))))
        except (TypeError, ValueError):
            p = 0.2
        if any(covered(text, a) >= IDEA_SAME for a in avoid if a):
            continue
        cands.append({"idea": text[:200], "p": p})
    if not cands:
        return None
    tails = [c for c in cands if c["p"] <= TAIL_P]
    if tails and rnd.random() < TAIL_CHANCE:
        return rnd.choice(tails)
    total = sum(max(c["p"], 0.02) for c in cands)
    x = rnd.uniform(0, total)
    for c in cands:
        x -= max(c["p"], 0.02)
        if x <= 0:
            return c
    return cands[-1]


def _n(v) -> str:
    return "?" if v is None else str(int(v))


def posts_block(posts: list[dict], unposted: list[dict] | None = None) -> str:
    """她最近发过的作品和大家的反应、写了没发的草稿：写成几行给她看（不是规矩，只是让她知道）。"""
    lines = []
    for p in posts or []:
        head = f"- {str(p.get('day') or '')[5:]}《{(p.get('title') or '无标题')[:20]}》“{(p.get('text') or '')[:40]}”"
        if p.get("likes") is None:
            head += " —— 还没看到数据"
        else:
            head += f" —— {_n(p.get('likes'))} 个赞、{_n(p.get('comments'))} 条评论、{_n(p.get('collects'))} 个收藏"
        said = [f"{s.get('user') or '有人'}说“{(s.get('text') or '')[:30]}”" + (f"（你回“{s['reply'][:20]}”）" if s.get("reply") else "")
                for s in (p.get("said") or [])[:3] if s.get("text")]
        if said:
            head += "；" + "；".join(said)
        lines.append(head)
    out = ("你最近发的作品（新的在前）和大家的反应：\n" + "\n".join(lines)) if lines else "你最近还没发过作品。"
    left = [f"- {str(d.get('created_at') or '')[5:10]}《{(d.get('title') or '无标题')[:20]}》“{(d.get('text') or '')[:30]}”"
            for d in unposted or []]
    if left:
        out += "\n你最近写了、但没发出去的：\n" + "\n".join(left)
    return out
