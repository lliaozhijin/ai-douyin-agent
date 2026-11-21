"""导出视频文案时的“用大模型改写”：把视频里说的话改写成适合在短视频平台发布的原创文案。

改写规则（RULES，也会原样显示在“其他功能”页上）是按自媒体运营的做法定的，核心是四件事：
1. 内容一点不少：原文的每个知识点、数据、步骤、结论、例子都保留，不加原文没有的事实，不改原意；
2. 字数差不多、只是更精简：只去掉口头禅、重复、空洞的过渡和求关注之类的话，篇幅是原文的 70%～95%；
3. 不侵权：换成自己的说法和句式，不照搬原句，不带原作者的账号名、口号、固定开场白；
4. 适合短视频：开头 3 秒抓人、短句口语、一句一个信息点、结尾引导评论；同时守平台规则（不用极限词、不标题党、不导流）。

大模型改完后程序再检查一遍（check）：字数是不是太短 / 太长、有没有连着十几个字和原文一模一样，
不合格就把问题告诉大模型重改一次；还不合格也照样导出，但在文本里写明检查结果。
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

MIN_RATIO = 0.70          # 改写后至少是原文的 70%（太短就是删内容了）
MAX_RATIO = 1.05          # 最多和原文差不多长（要的是“更精简”）
COPY_RUN = 13             # 连着这么多个字和原文一样，算“照搬原句”
CHUNK = 5000              # 原文太长时分段改写

RULES = """【改写规则】你是一名资深的短视频自媒体运营和文案编辑，要把一段视频的口播文字改写成可以直接发布的原创文案。

一、内容完全一致（最重要）
1. 原文里的每一个知识点、观点、数据、步骤、结论、例子都要保留，一个都不能少；数字、专有名词、步骤的先后顺序保持准确。
2. 不新增原文没有的事实、数据、案例、承诺或个人经历；不改变原意，不加自己的评价。
3. 原文没说清楚的地方保持原样的模糊，不要替原作者“补充”。

二、字数和原文差不多，只是更精简
1. 只删这些：口头禅（“那个”“然后呢”“对吧”“就是说”）、重复啰嗦的话、空洞的过渡句、自我介绍、“关注我”“点赞收藏”之类的引导、和主题无关的闲聊。
2. 改写后的正文是原文字数的 70%～95%；宁可长一点，也不能为了精简删掉知识点。

三、原创表达，不侵权
1. 用自己的话重新组织：换说法、换句式、合并或拆分句子；不要连着十几个字照抄原句。
2. 在不影响逻辑的前提下可以调整讲述顺序（比如结论前置），但步骤类内容的先后不能乱。
3. 去掉原作者的个人信息：账号名、昵称、口号、系列名、固定的开场白和结束语、原作者自己的梗；不提“原视频”“某博主说”。

四、适合短视频平台发布（爆款写法）
1. 开头一句话就抓人（黄金 3 秒）：从原文内容里提炼痛点、反常识的点、或者先说结果、或者抛一个问题；不能夸大，不能编造。
2. 正文用短句、口语化，一句只讲一个信息点，适合配音和字幕；多用“你”拉近距离；有步骤或要点时用“第一、第二……”或分行列出来。
3. 节奏紧凑、层层递进，让人想看完；关键信息可以用一句话再强调一次（不算新增内容）。
4. 结尾用一句话总结，再加一句自然的互动引导（比如提一个问题请大家在评论区说说），不要求关注、不诱导点赞。

五、守平台规则
1. 不用绝对化和夸张用语：最、第一、唯一、100%、绝对、根治、包治、必火、稳赚等。
2. 不标题党、不制造焦虑，不用“震惊”“速看”“不看后悔”“全网首发”这类词。
3. 不出现微信号、手机号、网址、二维码等站外导流，不涉及医疗效果、投资收益的保证。

六、另外给出
1. 3 个备选标题：每个不超过 20 个字，带上核心关键词，有吸引力但不标题党。
2. 5 个话题标签（#开头，和内容相关的热门说法）。"""

FORMAT = """只输出一个 JSON 对象，不要任何别的文字：
{"titles": ["标题1", "标题2", "标题3"], "body": "改写后的正文（分段用 \\n）", "tags": ["#话题1", "#话题2", "#话题3", "#话题4", "#话题5"]}"""

_PUNCT = re.compile(r"[\s　-〿＀-￯，。！？、；：“”‘’（）《》【】…—·,.!?;:'\"()\[\]<>\-_/\\#@~`*+=|^&%$]+")


def plain(text: str) -> str:
    """去掉空白和标点：用来数字数、比相同的句子。"""
    return _PUNCT.sub("", text or "")


def count(text: str) -> int:
    return len(plain(text))


def copied_runs(original: str, rewritten: str, n: int = COPY_RUN, limit: int = 5) -> list[str]:
    """改写稿里和原文连着 n 个字以上一模一样的片段（不算标点）。"""
    a, b = plain(original), plain(rewritten)
    if len(a) < n or len(b) < n:
        return []
    grams = {a[i:i + n] for i in range(len(a) - n + 1)}
    runs, i = [], 0
    while i <= len(b) - n:
        if b[i:i + n] in grams:
            j = i + n
            while j < len(b) and b[i:j + 1] in a:
                j += 1
            runs.append(b[i:j])
            if len(runs) >= limit:
                break
            i = j
        else:
            i += 1
    return runs


def check(original: str, body: str) -> dict:
    """改写稿合不合格：字数比例、照搬的片段。"""
    n0, n1 = count(original), count(body)
    ratio = n1 / n0 if n0 else 1.0
    runs = copied_runs(original, body)
    problems = []
    if ratio < MIN_RATIO:
        problems.append(f"太短了：原文 {n0} 字，改写只有 {n1} 字（{ratio:.0%}），至少要 {int(n0 * MIN_RATIO) + 1} 字。"
                        "把漏掉的知识点、数据、步骤、例子补回来，不要删内容")
    elif ratio > MAX_RATIO:
        problems.append(f"太长了：原文 {n0} 字，改写有 {n1} 字（{ratio:.0%}）。要更精简，"
                        f"控制在 {int(n0 * 0.95)} 字以内，只删口头禅和重复，不删知识点")
    if runs:
        problems.append("这些地方和原文一字不差，要换成自己的说法：" + "；".join(f"“{r}”" for r in runs[:5]))
    return {"original_chars": n0, "chars": n1, "ratio": round(ratio, 2), "copied": runs, "problems": problems,
            "ok": not problems}


def parse(text: str) -> dict:
    """大模型的回答 → {titles, body, tags}。不是 JSON 就把整段当正文。"""
    t = (text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            body = str(d.get("body") or "").strip()
            if body:
                titles = [str(x).strip() for x in d.get("titles") or [] if str(x).strip()][:3]
                tags = []
                for x in d.get("tags") or []:
                    x = str(x).strip().replace(" ", "")
                    if x:
                        tags.append(x if x.startswith("#") else "#" + x)
                return {"titles": titles, "body": body.replace("\\n", "\n"), "tags": tags[:5]}
        except (ValueError, AttributeError):
            pass
    t = re.sub(r"^```\w*|```$", "", t).strip()
    return {"titles": [], "body": t, "tags": []}


def _chunks(text: str) -> list[str]:
    paras = [p for p in (text or "").split("\n") if p.strip()]
    out, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > CHUNK:
            out.append(cur)
            cur = ""
        cur = (cur + "\n" + p) if cur else p
    if cur:
        out.append(cur)
    return out or [text]


def rewrite(original: str, agent, desc: str = "", extra: str = "", progress=None) -> dict:
    """改写。返回 {titles, body, tags, check}。原文太长时分段改写（只有第一段要开头钩子、最后一段要结尾）。"""
    if agent is None:
        raise RuntimeError("没有可用的大模型")
    router = agent.brain.router
    parts = _chunks(original)
    titles, tags, bodies = [], [], []
    for i, part in enumerate(parts):
        where = ""
        if len(parts) > 1:
            where = (f"\n\n（原文很长，分成了 {len(parts)} 段，这是第 {i + 1} 段。"
                     + ("这一段要有开头钩子，不要写结尾。" if i == 0 else
                        "这一段要写结尾，不要再写开头钩子。" if i == len(parts) - 1 else
                        "这是中间部分：不要开头钩子，也不要结尾，接着前面往下讲。") + "）")
        user = (RULES + ("\n\n【这次的补充要求】" + extra.strip() if extra and extra.strip() else "")
                + (f"\n\n【视频简介（帮助理解主题，不要照抄）】\n{desc.strip()[:500]}" if desc and desc.strip() and i == 0 else "")
                + f"\n\n【原文（{count(part)} 字）】\n{part}{where}\n\n" + FORMAT)
        res = parse(router.call("write", "你是资深的短视频自媒体运营和文案编辑，严格遵守给你的改写规则。", user,
                                max_tokens=min(6000, len(part) * 3 + 600), temperature=0.7).text)
        chk = check(part, res["body"])
        if not chk["ok"]:
            # 不合格：把问题告诉它，重改一次
            log.info("改写第 %d 段不合格，重改：%s", i + 1, chk["problems"])
            fix = (user + "\n\n【你上一次的改写】\n" + res["body"] + "\n\n【上一次的问题，这次必须改正】\n"
                   + "\n".join(f"- {p}" for p in chk["problems"]))
            again = parse(router.call("write", "你是资深的短视频自媒体运营和文案编辑，严格遵守给你的改写规则。", fix,
                                      max_tokens=min(6000, len(part) * 3 + 600), temperature=0.6).text)
            if again["body"] and len(check(part, again["body"])["problems"]) <= len(chk["problems"]):
                res = {**res, **{k: v for k, v in again.items() if v}}
        bodies.append(res["body"])
        titles = titles or res["titles"]
        tags = tags or res["tags"]
        if progress:
            progress((i + 1) / len(parts))
    body = "\n\n".join(b.strip() for b in bodies if b.strip())
    return {"titles": titles, "body": body, "tags": tags, "check": check(original, body)}


def report(chk: dict) -> str:
    """导出的文本里写明检查结果。"""
    line = f"原文 {chk['original_chars']} 字 → 改写 {chk['chars']} 字（{chk['ratio']:.0%}）"
    line += "；和原文连着 13 个字以上一样的地方：" + (f"{len(chk['copied'])} 处（" + "；".join(chk["copied"][:3]) + "）"
                                                   if chk["copied"] else "没有")
    if chk["ratio"] < MIN_RATIO:
        line += "；⚠ 偏短，可能漏了内容，发布前请对照原文检查"
    return line
