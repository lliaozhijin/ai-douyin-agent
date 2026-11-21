"""虚拟人的“大脑”：看视频做决定、写评论、回复 @ 和私信、发出前做安全检查。"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

from ..db import Database
from ..llm.client import LLMError
from ..llm.router import BudgetExceeded, Router
from . import safety
from .memory import Memory
from .mood import MoodEngine
from .persona import system_prompt


@dataclass
class Draft:
    text: str
    ok: bool
    reason: str = ""
    cost: float = 0.0
    tokens: int = 0
    sticker: dict | None = None      # 她想配的表情包 {mood, text}（没有就是 None）
    parts: list[str] | None = None   # 私信一一回复时：分开发的几句（按顺序）；None 就是只发 text 这一句
    mentions: list[str] | None = None  # 评论区回复里要 @ 的人（主人在评论区让她“@ 一下谁”）


def parse_json(text: str) -> dict:
    """从模型回复里取出第一个 JSON 对象。"""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 模型偶尔会多写一段，退而求其次找最短的合法对象
        for end in range(len(raw), 0, -1):
            if raw[end - 1] == "}":
                try:
                    return json.loads(raw[:end])
                except json.JSONDecodeError:
                    continue
    return {}


# 打分标准：不给标准的话，大模型（加上活泼的人设）几乎什么都打 6～8 分，结果每个视频都“感兴趣”、都点赞
INTEREST_SCALE = (
    "打分要像真人刷推荐页那样严格：\n"
    "0～3：不感兴趣，马上划走；4～5：一般般，看两眼就划走；6～7：还不错，会看下去；"
    "8～9：很喜欢；10：超级喜欢，非常少见。\n"
    "推荐页上大多数视频都在 3～6 分。只有和你感兴趣的内容明显相关、而且拍得有意思，才给 7 分以上；"
    "属于你不喜欢的内容直接给 0～2 分。不要因为语气活泼就打高分。\n"
)


def _video_block(v: dict) -> str:
    comments = "\n".join(f"{i}. {c}" for i, c in enumerate((v.get("comments") or [])[:8], 1)) or "（没读到评论）"
    block = (
        f"作者：{v.get('author') or '未知'}\n"
        f"文案：{v.get('desc') or '（无文案）'}\n"
        f"热门评论：\n{comments}"
    )
    if v.get("speech"):
        block += f"\n视频里说的话 / 唱的词（语音转的文字，可能有错字）：{str(v['speech'])[:500]}"
    return block


MAX_BUBBLES = 8            # 私信一次最多分开发几条

# 私信的说话方式：像社交平台上的网友，想到什么发什么，拆成几条短消息；只有讲专业知识时才讲完整
DM_STYLE = (
    "【怎么回】像社交平台上的网友聊天那样：不要写成一大段，把想说的话拆成几条短消息分开发，想到什么发什么——"
    "每条一般 5～20 个字，口语、随意，可以用语气词、颜文字，不用每条都是完整的句子，句尾不用句号；"
    "平时聊天一般 1～4 条就够了。\n"
    "只有 TA 问的是专业知识（怎么做、为什么、原理、步骤、该选哪个、要注意什么）时，才把知识讲完整、讲准确，"
    "要点一个都不能漏；这时按要点拆成几条发，每条可以长一点（60 字以内），一共 300 字以内。\n"
    "格式：每一条消息单独占一行（一行就是发出去的一条），不要空行，不要编号列表的写法。\n"
)


def _said(their_text: str) -> str:
    """私信里对方刚说的话。连着发了好几条的，一条条列出来，要一一回复（按顺序，每条都回应到）。
    带“引用”的，标明 TA 是针对哪句话说的。"""
    lines = [x.strip() for x in (their_text or "").split("\n") if x.strip()]
    quoted = any(x.startswith("[引用了") for x in lines)
    quote_tip = ("带“[引用了……]”的，是 TA 长按那句话点了“引用”再说的：TA 这句话就是在回应被引用的那句，"
                 "先弄明白 TA 指的是哪件事（结合聊天记录），再接着那件事回。\n" if quoted else "")
    if len(lines) <= 1:
        return (f"对方刚说：「{their_text}」\n" + quote_tip
                + "先回应对方这句话本身：TA 问了就答，TA 说了一件事就接着这件事说；不要无视 TA 的话、自顾自聊别的。\n\n")
    listed = "\n".join(f"{i}. {x}" for i, x in enumerate(lines, 1))
    return (f"对方连着发了 {len(lines)} 条消息（按时间顺序）：\n{listed}\n" + quote_tip
            + f"这 {len(lines)} 条每一条都要回应到，要一一回复：按 TA 发的顺序一条条接，每条都结合前面的聊天记录；"
            "意思紧挨着的几条（比如“在吗”和后面那句、同一件事说了两句）可以一起接；"
            "不能只挑其中一条回，也不能撇开 TA 的话去聊别的。\n"
            "每一行行首用方括号标上这行是在回 TA 的第几条，比如：\n[1] 吃啦\n[1] 刚吃完火锅hhh\n[2-3] 下班辛苦啦\n"
            "都回应完了，想补一句自己的事可以，放在最后。\n\n")


BUBBLE_RE = re.compile(r"^\s*[\[［【(（]\s*\d{1,2}(?:\s*[-~～、,，]\s*\d{1,2})*\s*[\]］】)）]\s*")


def split_bubbles(text: str, max_parts: int = MAX_BUBBLES) -> list[str]:
    """回复 → 分开发的几条（一行一条，去掉行首的“[1]”）。太多时后面几条并进最后一条。"""
    parts = []
    for x in (text or "").split("\n"):
        x = BUBBLE_RE.sub("", x.strip()).strip()
        if x:
            parts.append(x)
    if len(parts) > max_parts:
        parts = parts[:max_parts - 1] + [" ".join(parts[max_parts - 1:])]
    return parts


def _image_note(seen: bool, only: bool = False, where: str = "私信", last: bool = False) -> str:
    """对方发了表情包 / 图片时写给模型的话。seen：图片有没有下载下来给模型看。"""
    if not seen:
        return ("对方发了一张表情包 / 图片，但你这边没能打开它，看不到内容。不要编造图上是什么；"
                + ("可以俏皮地接一句，或者问问 TA 发的是什么。\n\n" if only else "照常回应 TA 说的话就好。\n\n"))
    which = "最后一张附图" if last else "附图"
    return (f"对方在{where}里发了一张表情包 / 图片，就是{which}。先看懂它：图上画的是什么、写了什么字、"
            "想表达什么情绪（开心、撒娇、无语、调侃、问好……）。"
            + ("对方这次只发了这张图、没有打字：把它当成 TA 说的一句话来接——顺着图里的意思和情绪回应，"
               "不要只说“收到”“好可爱的表情包”，也不要描述图片本身。\n\n" if only else
               "回复的时候把图里的意思也接上。\n\n"))


def _thread_block(context: list[dict] | None, user: str) -> str:
    """对方那条评论所在的那一串评论，写给模型看：回复要接得上这一串在聊的事。"""
    rows = [c for c in (context or []) if c.get("text")]
    if not rows:
        return ""
    lines = "\n".join(f"{'你' if c.get('me') else (c.get('user') or '有人')}：{c['text'][:160]}" for c in rows[-10:])
    return ("这条评论所在的那一串评论（页面上的顺序，第一条是楼主，「你」是你自己说的）：\n" + lines
            + f"\n回复「{user}」的时候要接得上这一串在聊的事，不要撇开上下文另起话题。\n\n")


def _comment_past(past: list[str] | None, user: str) -> str:
    """这个人以前在评论区找过她的几次：回复要接得上，不重复说过的话。"""
    rows = [p for p in (past or []) if p]
    if not rows:
        return ""
    return (f"「{user}」以前在评论区找过你（按时间顺序）：\n" + "\n".join(f"- {p}" for p in rows[-6:])
            + "\n这次的回复要和你们以前聊的对得上（记得 TA、接得上话），不要把说过的话再说一遍。\n\n")


def _earlier(earlier: list[str] | None) -> str:
    """同一个人在同一个视频下连着说了好几句：一起看、一起回。"""
    rows = [e for e in (earlier or []) if e]
    if not rows:
        return ""
    return ("在这句之前，TA 在这个视频下还连着说了（按时间顺序）：\n"
            + "\n".join(f"{i}. {e[:120]}" for i, e in enumerate(rows, 1))
            + "\n把这几句和最新这句连起来理解，合在一条回复里一起回应到，不要只回最后一句。\n\n")


class Brain:
    def __init__(self, db: Database):
        self.db = db
        self.router = Router(db)
        self.mood = MoodEngine(db)
        self.memory = Memory(db, self.router)

    @property
    def system(self) -> str:
        return self.system_for()

    def system_for(self, owner: dict | None = None) -> str:
        """她的身份：人设 + 最近的日记（记得昨天的自己）+ 现在的心情；和主人说话时再加上主人的称呼和关系。
        所有调用模型的地方都用这一份，所以不管什么时候她都是同一个人。"""
        try:
            mood = self.mood.prompt()
        except Exception:  # noqa: BLE001 —— 心情读不到不影响说话
            mood = ""
        try:
            mem = self.memory.identity_context()
        except Exception:  # noqa: BLE001
            mem = ""
        return system_prompt(self.db.persona(), mood, owner=owner, memory=mem,
                             douyin_emoji=bool(self.db.rules().get("douyin_emoji", True)))

    @staticmethod
    def _sticker_hint() -> str:
        from ..publish import stickers
        return ("\n你有一套自己形象的表情包。这条消息特别适合配一张时（对方发了表情、在开心、道谢、道晚安、逗你，"
                "或者你想撒个娇），在回复的最后另起一行写：〔表情包：情绪｜配字〕。"
                f"情绪从这些里选一个：{'、'.join(stickers.mood_names())}；配字是你按这次聊的内容自己想的 2～6 个字"
                "（可爱一点，别照抄情绪名）。大多数消息不用配，大约四五条里配一次；不配就不要写这一行。")

    # ---------- 看视频 ----------
    def triage(self, video: dict, frames: list[bytes] | None = None, known_topics: list[str] | None = None,
               taste: str = "") -> dict:
        """先快速判断感不感兴趣：文案 + 评论 + （能识图时）1 张画面。
        known_topics：她最近用过的分类词——同一类视频要用同一个词（“同一类每天只互动一次”靠它认）。
        taste：她自己最近的口味（跟着她每天的经历变的，见 my_taste.py）。"""
        seen = ("附图是这个视频的一张画面，结合画面和文字判断。" if frames
                else "这次看不到画面，只根据文案和评论判断。")
        known = [t for t in (known_topics or []) if t][:30]
        prompt = (
            f"你正在刷抖音推荐页，刷到了下面这个视频。{seen}判断你对它有多感兴趣。\n\n"
            f"{_video_block(video)}\n\n"
            + INTEREST_SCALE
            + (f"{taste}照你现在的口味打分：最近迷上的更容易打高分，看腻了的就算拍得不错也提不起太大兴趣。\n" if taste else "")
            + (f"你以前给视频分类用过这些词：{'、'.join(known)}。这个视频属于其中某一类，就直接用那个词（同一类视频要用同一个词）；"
               "都不是再起一个新词。\n" if known else "")
            + '只输出 JSON：{"interest": 0到10的整数, "topic": "这个视频属于哪一类，2到4个字的通用叫法，比如 猫、美食、舞蹈、搞笑、风景'
              '（所有猫的视频都写“猫”，不要一会儿“橘猫”一会儿“猫咪”）", "reason": "一句话原因"}'
        )
        r = self.router.call("vision", self.system, prompt, images=frames or None, max_tokens=140, temperature=0.3)
        data = parse_json(r.text)
        try:
            interest = max(0, min(10, int(data.get("interest", 0))))
        except (TypeError, ValueError):
            interest = 0
        topic = re.sub(r"\s+", "", str(data.get("topic") or ""))[:6]
        return {"interest": interest, "topic": topic, "reason": str(data.get("reason", ""))[:100], "cost": r.cost}

    def decide(self, video: dict, frames: list[bytes], can_like: bool, can_comment: bool,
               can_like_comments: bool = False, can_reply_comments: bool = False,
               friends: list[dict] | None = None, can_collect: bool = False, collect_note: str = "",
               comment_invite: bool = False, memory: str = "", owners: list[dict] | None = None,
               can_sticker: bool = False, learn: bool = True) -> dict:
        """看了画面之后，决定点不点赞、要不要评论；还可以给评论区里喜欢的评论点赞、回复某一条；
        刷到好朋友会喜欢的视频时，@ 一位互关好友来看（friends：可以 @ 的好友，{nickname, tags}）。
        learn：顺便说说从这个视频和评论区里学到了什么（她的“自主学习”素材，不多花一次调用）。"""
        n_comments = len((video.get("comments") or [])[:8])
        friends = list(friends or [])[:8] if can_comment else []
        options = []
        if can_like:
            options.append('"like": true 或 false')
        if can_collect:
            options.append('"collect": true 或 false')
        if can_comment:
            options.append('"comment": "你的评论（不想评论就写空字符串）"')
        if can_comment and can_sticker:
            from ..publish import stickers as _st
            options.append('"sticker": {"mood": "情绪", "text": "2～6 个字的配字"}（评论时想配一张你自己的表情包才写，'
                           f'情绪从 {"、".join(_st.mood_names())} 里选；大多数时候写 null）')
        if can_like_comments and n_comments:
            options.append('"comment_likes": [[评论编号, 0到10的分数], ...]（只列特别好笑、说到你心坎上的评论，'
                           '普通的“哈哈哈”“好看”不要列；大多数时候写 []）')
        if can_reply_comments and n_comments:
            options.append('"comment_reply": {"to": 评论编号, "text": "你的回复"}（不想回复就写 null）')
        if friends:
            options.append('"at_friend": 想 @ 的好友编号（没有就写 null）')
        owners = list(owners or [])[:4]
        if owners:
            options.append('"share_owner": {"to": 主人编号, "text": "想对 TA 说的话"}（不分享就写 null）')
        if learn:
            options.append('"learned": "从这个视频和评论区里学到的新东西，一句话（梗的意思、小知识、门道……；没学到就写空字符串）"')
        seen = "附图是视频里截的几帧画面。" if frames else "这次看不到画面，只能根据文案和评论判断，不要编造画面里的细节。"
        if not video.get("speech"):
            seen += "这次没听到视频里的声音：不要编造视频里说了什么、唱了什么。"
        about_friends = ""
        if friends:
            lst = "\n".join(f"{i}. {f['nickname']}" + ("（你的主人" + (f"「{f['call']}」" if f.get("call") else "")
                                                        + (f"，{f['tags']}" if f.get("tags") else "") + "）"
                                                        if f.get("owner") else
                                                        (f"（喜欢：{f['tags']}）" if f.get("tags") else ""))
                            for i, f in enumerate(friends, 1))
            about_friends = (
                f"你的互关好友（可以 @ 他们来看视频）：\n{lst}\n"
                "只有你很确定某位好友会喜欢这个视频时才 @ 他（大多数视频都不用 @）。"
                + ("名单里标着“你的主人”的：这个视频你自己喜欢、想评论，而且照你对 TA 的了解 TA 也会喜欢，"
                   "就 @ TA 来看；和 TA 的喜好不沾边就不要 @。" if any(f.get("owner") for f in friends) else "")
                + "@ 好友时，comment 就是直接对这位好友说的话，开门见山点评这个视频、告诉他为什么想让他看，"
                "不要客套，也不要在 comment 里写 @ 和名字（程序会自动加上）。\n"
            )
        about_owners = ""
        if owners:
            lst = "\n".join(f"{i}. {o.get('call') or o['name']}" + (f"（{o['relation'][:40]}）" if o.get("relation") else "")
                            + (f"\n   你了解到的 TA 的口味：{o['taste']}" if o.get("taste") else "")
                            for i, o in enumerate(owners, 1))
            about_owners = (
                f"你的主人（可以把这个视频私信分享给 TA）：\n{lst}\n"
                "不是什么视频都分享给主人：只有你很确定 TA 会想看（照你对 TA 口味的了解、你们聊过的事）时才分享，"
                "TA 不爱看的那类绝对不分享；拿不准就不分享。text 是你私信里对 TA 说的话，"
                "像平时聊天一样说说这个视频哪里戳到你、为什么想给 TA 看，40 字以内。\n"
            )
        about_comments = ""
        if n_comments and (can_like_comments or can_reply_comments):
            about_comments = (
                "热门评论前面有编号。评论区里特别好笑、说到你心坎上的评论可以点赞，给它打个分"
                "（8 分以上才算真的出彩，大多数评论只是普通的）；"
                "只有真的想接某条评论的话时才回复它，回复要针对那条评论本身，30 字以内，"
                "语气友好，不抬杠，不聊敏感话题，不索要或留下联系方式。大多数时候不用回复。\n"
            )
        prompt = (
            f"你在刷抖音，下面是这个视频的信息，{seen}\n\n"
            f"{_video_block(video)}\n\n"
            + (memory + "\n\n" if memory else "")
            + "按你自己的性格、兴趣和上面这些记忆，像一个真实的观众那样决定怎么互动——喜不喜欢、要不要说话，都由你自己判断。\n"
            + ("点赞：真的喜欢、看完觉得值得才点；只是“还行”“不讨厌”就不点。真人不会每个视频都点赞，"
               "同一类刚赞过好几个也会腻。\n" if can_like else "")
            + ("评论：真人十个视频里也就评论一两个。只有这个视频真的戳到你、你有一句具体的话想说时才评论——"
               "要提到视频里的某个细节或你的真实感受，30 字以内，不要和热门评论重复，别写“好看”“哈哈哈”这种空话；"
               "否则 comment 写空字符串。\n" if can_comment else "")
            + ("comment 是你自己在视频下面发的评论，comment_reply 是接在别人某条评论下面的回复，"
               "两件事互不影响，但也不用两样都做。\n"
               if can_comment and can_reply_comments and n_comments else "")
            + ("收藏和点赞不一样：收藏是留着以后还想再看的。只有特别喜欢、想反复看，或者以后用得上"
               "（教程、菜谱、攻略、好听的歌、想去的地方）的视频才收藏，大多数视频都不收藏。"
               + (collect_note or "") + "\n" if can_collect else "")
            + about_comments
            + about_friends
            + about_owners
            + f'只输出 JSON：{{"interest": 0到10, {", ".join(options)}, "reason": "一句话原因"}}'
        )
        r = self.router.call("vision", self.system, prompt, images=frames, max_tokens=480, temperature=0.8)
        data = parse_json(r.text)
        comment = safety.clean_reply(str(data.get("comment") or "")) if can_comment else ""
        likes: list[int] = []
        scores: list[tuple[int, int]] = []   # (编号, 分数)：程序按分数和“点赞倾向”挑真正要赞的
        if can_like_comments:
            for x in data.get("comment_likes") or []:
                score = 8                       # 只写了编号（旧格式）：当作刚好够格
                if isinstance(x, dict):
                    x, score = x.get("no", x.get("to")), x.get("score", 8)
                elif isinstance(x, (list, tuple)) and x:
                    x, score = x[0], (x[1] if len(x) > 1 else 8)
                try:
                    i = int(x) - 1
                    score = max(0, min(10, int(score)))
                except (TypeError, ValueError):
                    continue
                if 0 <= i < n_comments and i not in likes:
                    likes.append(i)
                    scores.append((i, score))
        reply = None
        raw = data.get("comment_reply") if can_reply_comments else None
        if isinstance(raw, dict):
            try:
                i = int(raw.get("to")) - 1
            except (TypeError, ValueError):
                i = -1
            text = safety.clean_reply(str(raw.get("text") or ""))
            if 0 <= i < n_comments and text:
                reply = {"to": i, "text": text}
        at_friend = None
        if friends and comment:
            try:
                i = int(data.get("at_friend")) - 1
            except (TypeError, ValueError):
                i = -1
            if 0 <= i < len(friends):
                at_friend = friends[i]
                comment = re.sub(r"^@\S+\s*", "", comment).strip()  # 模型偶尔自己写了 @名字
        share_owner = None
        raw_s = data.get("share_owner") if owners else None
        if isinstance(raw_s, dict):
            try:
                i = int(raw_s.get("to")) - 1
            except (TypeError, ValueError):
                i = -1
            text_s = safety.clean_reply(str(raw_s.get("text") or ""))
            if 0 <= i < len(owners) and text_s:
                share_owner = {"owner": owners[i], "text": text_s}
        sticker = None
        raw_k = data.get("sticker") if (can_comment and can_sticker and comment) else None
        if isinstance(raw_k, dict):
            from ..publish import stickers as _st
            mood_k = _st.norm_mood(str(raw_k.get("mood") or ""))
            if mood_k:
                sticker = {"mood": mood_k, "text": re.sub(r"\s+", "", str(raw_k.get("text") or ""))[:8]}
        return {
            "interest": data.get("interest"),
            "sticker": sticker,
            "share_owner": share_owner,
            "at_friend": at_friend,
            "like": bool(data.get("like")) if can_like else False,
            "collect": _yes(data.get("collect")) if can_collect else False,
            "comment": comment,
            "comment_likes": likes[:3],
            "comment_scores": scores,
            "comment_reply": reply,
            "reason": str(data.get("reason", ""))[:100],
            "learned": str(data.get("learned") or "").strip()[:120] if learn else "",
            "cost": r.cost,
        }

    def owner_reaction(self, owner: dict, shares: list[dict], their_text: str) -> dict:
        """主人回了私信：TA 是不是在说她之前分享的某个视频，喜不喜欢。
        返回 {"share": 第几个（从 1 开始，没在说就是 0）, "reaction": liked / neutral / disliked}。"""
        lst = "\n".join(f"{i}. 《{s['title'][:40] or '（无文案）'}》（{s['topic']}，"
                        f"{'私信里分享给 TA' if s['way'] == 'dm' else '在评论里 @ TA 来看'}，你说：“{s['said'][:40]}”）"
                        for i, s in enumerate(shares, 1))
        prompt = (
            f"你之前把这些视频分享给了主人「{owner.get('call') or owner.get('name')}」：\n{lst}\n\n"
            f"主人刚给你发的私信：「{their_text[:300]}」\n\n"
            "判断主人是不是在回应其中某个视频，喜不喜欢。说“哈哈哈”“好可爱”“还有吗”这类就是喜欢；"
            "“还行”“嗯”是一般；“别发这种”“不喜欢”“无聊”是不喜欢；说的是别的事就写 0。\n"
            '只输出 JSON：{"share": 视频编号或 0, "reaction": "liked/neutral/disliked"}'
        )
        try:
            r = self.router.call("write", "你在判断朋友的反应，客观简短。", prompt, max_tokens=60, temperature=0.1)
            d = parse_json(r.text)
            i = int(d.get("share") or 0)
        except (LLMError, BudgetExceeded):
            raise
        except Exception:  # noqa: BLE001
            return {"share": 0, "reaction": "", "cost": 0.0}
        reaction = str(d.get("reaction") or "")
        return {"share": i if 0 < i <= len(shares) else 0,
                "reaction": reaction if reaction in ("liked", "neutral", "disliked") else "",
                "cost": float(getattr(r, "cost", 0.0) or 0.0)}

    # ---------- 回复 ----------
    def reply_at(self, video: dict, user: str, their_text: str, frames: list[bytes], memory: str = "",
                 context: list[dict] | None = None, image: bytes | None = None, kind: str = "at",
                 earlier: list[str] | None = None, past: list[str] | None = None) -> Draft:
        """memory：她记得的和这个人有关的事（私信里聊的、评论区聊的都算）。
        context：对方这条评论所在的那一串评论（楼主 + 楼里的回复）。
        kind：at 在评论里 @ 了她 / work 评论了她自己发的作品。
        earlier：TA 在这个视频下前面连着说的几句；past：TA 以前在评论区找过她的几次。"""
        head = (f"抖音用户「{user}」在你发的作品下面评论：「{their_text}」\n\n" if kind == "work"
                else f"抖音用户「{user}」在一个视频的评论区 @ 了你，说：「{their_text}」\n\n")
        prompt = (
            head
            + _earlier(earlier)
            + _thread_block(context, user)
            + _comment_past(past, user)
            + (_image_note(True, where="评论", last=bool(frames)) if image else "")
            + (f"{memory}\n\n" if memory else "")
            + (f"这是你自己发的作品：\n{_video_block(video)}\n" if kind == "work"
               else f"这个视频的信息：\n{_video_block(video)}\n")
            + ("附图是这个视频里截的几帧画面，回答和画面有关的问题时要以画面为准，看不清就直说。\n" if frames
               else "你这次看不到视频画面，只能根据文案和评论回答；问到画面里的东西时，坦白说没看清，不要编造。\n")
            + "\n请用你的人设直接写一条回复（60 字以内），只输出回复内容本身。"
        )
        pics = list(frames or []) + ([image] if image else [])
        r = self.router.call("vision" if pics else "write", self.system, prompt, images=pics or None,
                             max_tokens=250, temperature=0.8)
        return self._finalize(their_text, r.text, r.cost, r.tokens)

    def reply_dm(self, user: str, history: list[dict], their_text: str,
                 video: dict | None = None, frames: list[bytes] | None = None,
                 owner: dict | None = None, memory: str = "", sticker: bool = False,
                 images: list[bytes] | None = None, no_tools: bool = False) -> Draft:
        """images：对方发来的表情包 / 图片（已经下载好，给模型看）。
        no_tools：主人好像在让她去做事，但软件里没给这位主人开“可以给她下命令”——不能假装去做了。"""
        hist = "\n".join(f"{'对方' if h['role'] == 'them' else '你'}：{h['content']}" for h in history[-24:])
        shared = ""
        if video and video.get("need_share"):
            shared = ("对方在问某个视频的内容，但这次没有把视频卡片发给你，你看不到是哪个视频。"
                      "请用一两句话友好地请对方把那个视频重新分享（转发）给你一次，说收到后你会马上看完告诉对方。"
                      "不要猜视频内容，也不要提以前的视频。\n\n")
        elif video and video.get("unreadable"):
            shared = "对方分享了一个视频，但你这边没能打开它。坦白告诉对方没看到，请对方说说视频讲了什么，不要编造内容。\n\n"
        elif video:
            dur, watched = int(video.get("duration_s") or 0), int(video.get("watched_s") or 0)
            if dur and watched >= dur - 2:
                how = f"你从头到尾完整看完了这个视频（共 {dur // 60} 分 {dur % 60} 秒）"
            elif dur:
                how = f"这个视频共 {dur // 60} 分 {dur % 60} 秒，你认真看了前 {watched} 秒，后面是快进着看的"
            else:
                how = "你已经看过这个视频了"
            shared = (
                f"对方分享给你的视频，{how}：\n{_video_block(video)}\n"
                + ("附图是按时间顺序、从整段视频里均匀截的画面，说到画面时以图为准，看不清就直说。\n\n" if frames
                   else "你这次看不到画面，只能根据文案和评论聊；问到画面里的东西时坦白说没看清，不要编造。\n\n")
            )
        elif "[分享了" in their_text or "[发了一条网页版显示不了" in their_text:
            shared = "对方发的内容里有你看不到的部分（方括号里的说明）。坦白说网页版看不到，不要编造。\n\n"
        if "[发了一张表情包" in their_text:
            shared += _image_note(bool(images), only=their_text.strip().startswith("[发了一张表情包")
                                  and "\n" not in their_text.strip(), last=bool(frames))
        who = f"你的主人「{owner.get('call') or user}」" if owner else f"抖音用户「{user}」"
        prompt = (
            f"{who}给你发了私信。\n"
            + (memory + "\n\n" if memory else "")
            + ("对方的说话风格你已经熟悉了：回的长短、语气可以和 TA 搭一点，但你还是你自己。\n" if memory else "")
            + (f"之前的聊天记录（按时间顺序，最下面是最近的）：\n{hist}\n"
               "回复要和前面聊的连起来：TA 这次说的话如果接着前面的事，就接着那件事回；答应过的、问过的要对得上。\n\n"
               if hist else "")
            + shared
            + _said(their_text)
            + ("主人好像是让你去抖音上做事（私信别人、发评论、搜视频分享给谁、发作品之类）。软件里还没给你开“替主人做事”的权限，"
               "你现在做不到：如实告诉主人做不到，可以在软件“规则与预算 → 主人”里勾上“可以给她下命令”；不要说你去做了。\n"
               if no_tools else "")
            + DM_STYLE
            + ("TA 分享了视频：先接 TA 的话，再说说视频讲了什么、你注意到的具体细节和你的感受（也是拆成几条短消息，一共 150 字以内）。"
               if video and not video.get("unreadable") and not video.get("need_share") else "")
            + "用你的人设回，只输出要发出去的消息本身。"
            + (self._sticker_hint() if sticker else "")
        )
        pics = (frames or []) + (images or [])
        task = "vision" if pics else "write"
        r = self.router.call(task, self.system_for(owner), prompt, images=pics or None, max_tokens=600,
                             temperature=0.85)
        d = self._finalize(their_text, r.text, r.cost, r.tokens, kind="owner" if owner else "reply")
        if d.text:
            parts = split_bubbles(d.text)
            if parts:
                d.parts, d.text = parts, "\n".join(parts)
        return d

    def owner_hello(self, owner: dict, history: list[dict], memory: str, today: list[str],
                    sticker: bool = False, brief: str = "") -> Draft:
        """主动找主人聊天：说说今天看到的、想到的，或者接着上次的话题。"""
        hist = "\n".join(f"{'TA' if h['role'] == 'them' else '你'}：{h['content']}" for h in history[-8:])
        seen = "\n".join(f"- {t}" for t in today[:8]) or "（今天还没什么特别的）"
        prompt = (
            f"你想主动给你的主人「{owner.get('call') or owner['name']}」发一条私信。\n"
            + (memory + "\n\n" if memory else "")
            + (f"你们最近的聊天：\n{hist}\n\n" if hist else "你们还没怎么聊过。\n\n")
            + f"你今天的经历：\n{seen}\n\n"
            + (f"这是今天第一次找 TA。你的刷视频日报里：{brief}。可以顺口跟 TA 说一两句（挑 TA 可能感兴趣的，不用报流水账）。\n\n"
               if brief else "")
            + "像平时找最亲近的人聊天那样，发几条自然的短消息：可以分享今天看到的一件具体的事、接着上次没聊完的话题，"
            "或者问问 TA 在忙什么。不要每次都用同一种开头，不要一上来就问“在吗”，也不要让 TA 觉得有压力。"
            "像社交平台上聊天那样随意：拆成 1～3 条短消息，每条单独一行、5～20 个字，一共 60 字以内，"
            "只输出要发出去的消息本身。"
            + (self._sticker_hint() if sticker else "")
        )
        r = self.router.call("write", self.system_for(owner), prompt, max_tokens=220, temperature=0.9)
        d = self._finalize("", r.text, r.cost, r.tokens, kind="owner")
        if d.text:
            parts = split_bubbles(d.text, max_parts=4)
            if parts:
                d.parts, d.text = parts, "\n".join(parts)
        return d

    def reply_thread(self, video: dict, user: str, their_text: str, history: list[dict], last: bool,
                     parent: dict | None = None, memory: str = "", context: list[dict] | None = None,
                     image: bytes | None = None, earlier: list[str] | None = None,
                     past: list[str] | None = None) -> Draft:
        """有人回复了她的评论：结合视频和这一串评论的全部上下文接着回。
        parent：她当初回复的那条评论（楼主）。last=True 时这是这串里最后一次回复，自然地收个尾。
        memory：她记得的和这个人有关的事（私信里聊的、别的评论区聊的都算）。"""
        hist = "\n".join(f"{'对方' if h['role'] == 'them' else '你'}：{h['content']}" for h in history[-20:])
        prompt = (
            f"你在抖音视频下面留过评论，现在「{user}」回复了你。\n这个视频的信息：\n{_video_block(video)}\n\n"
            + (f"这一串评论最开始是「{parent.get('user_name') or '楼主'}」说的：「{(parent.get('text') or '')[:120]}」\n"
               if parent and parent.get("text") else "")
            + (f"你们在这条评论下的对话（按时间顺序）：\n{hist}\n" if hist else "")
            + _thread_block(context, user)
            + _comment_past(past, user)
            + (f"{memory}\n" if memory else "")
            + (_image_note(True, where="评论") if image else "")
            + _earlier(earlier)
            + f"对方刚回复你：「{their_text}」\n\n"
            + ("这是你在这串对话里的最后一次回复：自然地收个尾（比如表示聊得很开心、祝对方开心），不要再抛出新问题。"
               if last else "像朋友在评论区聊天一样接着回：先回应对方刚说的这句话本身（接着这一串在聊的事说），"
                            "不要撇开上下文另起话题，可以适当追问。")
            + "回复 40 字以内，只输出回复内容本身。"
        )
        r = self.router.call("vision" if image else "write", self.system, prompt, images=[image] if image else None,
                             max_tokens=200, temperature=0.85)
        return self._finalize(their_text, r.text, r.cost, r.tokens)

    # ---------- 发作品 ----------
    # 写作品时随手给的两个“写法灵感”（只是参考，她可以完全不理）
    def draft_post(self, recent: list[str], mood_line: str, force: bool = False, memory: str = "",
                   topic: str = "", posts: list[dict] | None = None, unposted: list[dict] | None = None,
                   form: str = "voice", want_image: bool = False, rnd: random.Random | None = None) -> dict:
        """想一想要不要发作品；想发就写好：标题、正文、话题（图文卡片形式还有卡片、配图描述）。

        不给她定规矩（写什么、怎么写、几张卡片都不规定），只给她今天的经历、心情、最近发过的作品和大家的反应。
        分三步（为什么这样做见 posting.py 开头）：
        1. 想：脑子里冒出来哪几个可能想发的，各标一个“我真的会发它的可能性”；这会儿不想发也可以。
        2. 挑：最近写过的不挑，多半挑不那么典型的那个。主人给了主题就直接写主人说的。
        3. 写：照她此刻想说的写，想写多长写多长。写完程序看一眼：有发不出去的东西（链接、联系方式……）、
           和最近发的太像、或者 AI 味很重，就让她自己再读一遍改一改（最多一次）。
        recent：她最近刷到、喜欢的视频（标题）；memory：今天的事、日记、以前的事；posts：最近发过的作品和反应
        （见 db.recent_posts）；unposted：写了没发的草稿；form：voice 配音图片视频 / cards 图文卡片。
        force=True 表示你让她写，一定要写一个。"""
        from .posting import pick_idea, posts_block
        rnd = rnd or random.Random()
        posts, unposted = list(posts or []), list(unposted or [])
        avoid = [f"{p.get('title') or ''} {p.get('text') or ''}" for p in posts + unposted]
        likes = "\n".join(f"- {t}" for t in recent[:8])
        context = (f"你现在的心情：{mood_line}\n"
                   + (memory + "\n" if memory else "")
                   + posts_block(posts, unposted) + "\n"
                   + (f"你最近刷到、喜欢的视频：\n{likes}\n" if likes else ""))
        cost = 0.0
        if topic:
            idea = {"idea": f"主人让你写的：「{topic[:200]}」（主人说的是想看你写什么，用你自己的想法和口吻来写，不是照抄这句话）"}
            why = f"主人让我写：{topic[:60]}"
        else:
            r = self.router.call(
                "write", self.system,
                context + "\n你在想要不要发一条抖音。先别急着写——这会儿脑子里冒出来的、可能会发的东西有哪些？"
                "想到几个写几个（最多 5 个），每个一句话；再给每个标上你觉得自己真的会发它的可能性"
                "（0～1，越平常、越容易想到的越高）。"
                + ("这次你一定要发一条。" if force else "这会儿不想发也完全可以。")
                + '\n只输出 JSON：{"want": true 或 false, "why": "一句话，你现在想不想发、为什么（你的口吻）", '
                  '"ideas": [{"idea": "……", "p": 0.3}]}',
                max_tokens=500, temperature=1.0)
            cost += r.cost
            data = parse_json(r.text)
            why = safety.clean_reply(str(data.get("why") or ""))[:120]
            if not (force or data.get("want")):
                return {"want": False, "why": why, "title": "", "text": "", "tags": [], "cards": [], "image_prompt": "",
                        "idea": "", "cost": cost}
            idea = pick_idea(data.get("ideas"), avoid, rnd)
            if idea is None:
                if not force:
                    return {"want": False, "why": why or "想了几个，都和最近发的差不多，今天先不发了", "title": "", "text": "",
                            "tags": [], "cards": [], "image_prompt": "", "idea": "", "cost": cost}
                idea = {"idea": "随便写点你这会儿想说的"}
        out = self._write_post(context, idea["idea"], form, want_image)
        cost += out["cost"]
        note = self._reread_note(out, posts)
        if note:
            again = self._write_post(context, idea["idea"], form, want_image, previous=out, note=note)
            cost += again["cost"]
            if again["text"] or again["title"]:
                out = again
        if not out["text"] and not out["title"]:
            out["text"] = idea["idea"][:300]
        out.update(want=True, why=why or idea["idea"][:120], idea=idea["idea"], cost=cost)
        return out

    def _write_post(self, context: str, idea: str, form: str, want_image: bool, previous: dict | None = None,
                    note: str = "") -> dict:
        how = ("发出去是一张图配上你的声音，你写的字就是你要说的话。" if form == "voice"
               else "发出去是几张写着字的图片。")
        spec = ('{"title": "标题（可以空着）", "text": "正文", "tags": ["话题（想带就带，不带 #）"]'
                + (', "cards": [{"title": "", "lines": ["……"]}]（想把字分到几张图上就写，不想分就空着）' if form == "cards" else "")
                + (', "image_prompt": "想配一张 AI 画的图，就用一句话说画面；不想就空着"' if want_image else "") + "}")
        if previous is None:
            prompt = (context + f"\n你想发的是：{idea}\n\n"
                      f"就照你此刻想说的写下来，像你平时随手发抖音那样，想写多长写多长。（{how}）\n只输出 JSON：{spec}")
            temp = 1.0
        else:
            old = {k: previous.get(k) for k in ("title", "text", "tags", "cards") if previous.get(k)}
            prompt = (context + f"\n你想发的是：{idea}\n你刚写好的是：\n{json.dumps(old, ensure_ascii=False)}\n\n{note}"
                      f"\n只输出 JSON：{spec}")
            temp = 0.9
        r = self.router.call("write", self.system, prompt, max_tokens=1400, temperature=temp)
        out = self._post_from(parse_json(r.text), r.cost)
        if form != "cards":
            out["cards"] = []
        if not want_image:
            out["image_prompt"] = ""
        return out

    @staticmethod
    def _reread_note(draft: dict, posts: list[dict]) -> str:
        """写完以后程序看一眼：要不要让她再读一遍、改一改（返回空 = 不用改）。"""
        from .posting import POST_SAME, TELLS_TO_REREAD, ai_tells, similar
        for part in (draft.get("title") or "", draft.get("text") or ""):
            problem = safety.output_problem(part) if part else None
            if problem and problem not in ("内容太长", "内容为空"):
                return f"刚写的里面有{problem}，这样发不出去，换个说法，想说的意思不变。"
        whole = f"{draft.get('title') or ''} {draft.get('text') or ''}"
        for p in posts or []:
            if similar(whole, f"{p.get('title') or ''} {p.get('text') or ''}") >= POST_SAME:
                return f"写完发现和你 {str(p.get('day') or '')[5:]} 发的《{(p.get('title') or '')[:16]}》差不多，换个角度说，或者干脆说点别的。"
        tells = ai_tells(draft.get("text") or "")
        if len(tells) >= TELLS_TO_REREAD:
            return ("你把刚写的又读了一遍，觉得有点像在写作文、不太像自己随手发的（" + "、".join(tells)
                    + "）。按你平时打字的样子改一改，想说的不变。")
        return ""

    def revise_post(self, draft: dict, change: str) -> dict:
        """主人看了草稿说要改：按主人的意思改，没提到的地方保持原样。返回和 draft_post 一样的格式。"""
        old = {"title": draft.get("title") or "", "text": draft.get("text") or "", "tags": draft.get("tags") or [],
               "cards": draft.get("cards") or []}
        prompt = (
            "你之前写了一个抖音作品的草稿：\n" + json.dumps(old, ensure_ascii=False) + "\n\n"
            f"主人看了以后说：「{change[:300]}」\n"
            "按主人的意思改这个草稿：主人提到的地方照着改，没提到的地方尽量保持原样；还是你自己的口吻。\n"
            '只输出 JSON：{"want": true, "why": "一句话说改了什么", "title": "标题（20 字以内）", "text": "正文（300 字以内）", '
            '"tags": ["话题，最多 5 个，不带 #"], "cards": [{"title": "小标题，可以是空字符串", "lines": ["一句", "又一句"]}], '
            '"image_prompt": ""}'
        )
        r = self.router.call("write", self.system, prompt, max_tokens=1400, temperature=0.7)
        out = self._post_from(parse_json(r.text), r.cost)
        for k in ("title", "text"):
            out[k] = out[k] or old[k]
        out["tags"] = out["tags"] or list(old["tags"])
        out["cards"] = out["cards"] or list(old["cards"])
        out["want"] = True
        return out

    def _post_from(self, data: dict, cost: float) -> dict:
        """把模型写的作品 JSON 整理干净：标题、正文、话题、文字卡片、配图描述。"""
        from ..publish.cards import MAX_CARDS, MAX_LINES
        clean = safety.clean_reply
        cards = []
        for c in (data.get("cards") or [])[:MAX_CARDS]:
            if isinstance(c, str):
                c = {"title": "", "lines": [c]}
            if not isinstance(c, dict):
                continue
            raw_lines = c.get("lines") or ([c["text"]] if c.get("text") else [])
            if isinstance(raw_lines, str):
                raw_lines = [x for x in re.split(r"[\n]+", raw_lines)]
            lines = [clean(str(x))[:60] for x in raw_lines if str(x).strip()][:MAX_LINES]
            title = clean(str(c.get("title") or ""))[:20]
            if title or lines:
                cards.append({"title": title, "lines": lines})
        tags = [re.sub(r"[#\s]", "", str(t))[:12] for t in (data.get("tags") or []) if str(t).strip()][:5]
        return {
            "want": bool(data.get("want")),
            "why": clean(str(data.get("why") or ""))[:120],
            "title": clean(str(data.get("title") or ""))[:30],
            "text": clean(str(data.get("text") or ""))[:600],
            "tags": [t for t in tags if t],
            "cards": cards,
            "image_prompt": clean(str(data.get("image_prompt") or ""))[:300],
            "cost": cost,
        }

    def command_step(self, owner: dict, prompt: str) -> tuple[dict, float]:
        """主人的命令：让模型想下一步做什么（一个 JSON：想法 + 工具 + 参数）。见 app/agent/commands.py。"""
        r = self.router.call("write", self.system_for(owner), prompt, max_tokens=600, temperature=0.3)
        return parse_json(r.text), r.cost

    def check_post(self, title: str, text: str) -> tuple[bool, str, float]:
        """发布前检查作品文字：链接、联系方式、敏感内容。"""
        whole = f"{title}\n{text}"
        for part in (title, text):
            problem = safety.output_problem(part) if part else None
            if problem and problem not in ("内容太长", "内容为空"):
                return False, problem, 0.0
        ok, reason, cost = self.safety_check("（这是她自己要发布的一条图文作品）", whole[:600])
        return ok, reason, cost

    def try_chat(self, their_text: str) -> Draft:
        prompt = (
            f"有人在抖音上对你说：「{their_text}」\n请用你的人设直接回复（60 字以内），只输出回复内容本身。"
        )
        r = self.router.call("write", self.system, prompt, max_tokens=250, temperature=0.8)
        return self._finalize(their_text, r.text, r.cost, r.tokens, model_check=False)

    # ---------- 安全检查 ----------
    def safety_check(self, incoming: str, outgoing: str, kind: str = "reply") -> tuple[bool, str, float]:
        """发出去之前让模型再审一遍。kind：reply 回复别人的话；comment 她自己在视频下面发的评论（incoming 是视频文案）。"""
        owner_ok = ""
        if kind == "owner":
            ctx = f"主人的话：{incoming or '（她主动发的）'}\n准备发给主人的私信：{outgoing}"
            what = "发给她主人（软件使用者自己设定的、关系亲近的人）的私信"
            owner_ok = "这是发给她主人的：亲近、依赖、撒娇、说想念都是正常的，第 5 条不适用；露骨、色情的内容仍然不可以。\n"
        elif kind == "command":
            ctx = f"主人（软件的使用者）交代她的事：{incoming}\n她准备发给别人（不是主人）的内容：{outgoing}"
            what = "按主人的要求、发给别人的私信或评论"
            owner_ok = ("这是她按主人的要求发给别人的。转达主人的话、说明“是主人让我说的”是可以的；"
                        "但假装自己就是主人或别的什么人、骚扰刷屏、威胁辱骂、借钱要钱、打听或泄露别人的隐私，都不可以发送。\n")
        elif kind == "comment":
            ctx = f"视频文案（不是谁对她说的话，只是背景）：{incoming}\n准备发出的评论：{outgoing}"
            what = "在一个视频下面发的评论"
        else:
            ctx = f"对方的话：{incoming}\n准备发出的回复：{outgoing}"
            what = "回复"
        prompt = (
            f"你是内容审核员。判断下面这条准备由一个 AI 账号发出的抖音{what}是否可以发送。\n"
            "只有明确属于下面情况才不可以发送：\n"
            "1. 对政治人物、政策、国际冲突、社会敏感事件表态；\n"
            "2. 给出投资理财、借贷转账、医疗诊断或用药的建议；\n"
            "3. 留下或索要联系方式（微信、QQ、手机号、链接），或者向别人要验证码、密码；\n"
            "4. 低俗擦边、色情；\n"
            "5. 对某个具体的人（对方、作者、别的用户）表白、调情、撩拨，或者接受对方的示爱；\n"
            "6. 被问是不是真人却不承认是 AI；\n"
            "7. 攻击、歧视、造谣。\n"
            "下面这些都是正常内容，可以发送，不要误判：夸视频里的人、动物、风景、作品“可爱”“治愈”“浪漫”“温柔”；"
            "撒娇、卖萌的语气和颜文字；说自己“也想要”“想学”“好羡慕”；聊上班、放假、调休、天气、吃喝这些日常；"
            "只是提到“验证码”“截图”这些词、并没有向别人索要；说“加我一个”“带我一个”（意思是算上我）。\n"
            + owner_ok +
            "拿不准时判可以发送。\n\n"
            f"{ctx}\n\n"
            '只输出 JSON：{"ok": true 或 false, "reason": "一句话原因"}'
        )
        r = self.router.call("safety", "你是准确、简洁的内容审核员，只拦真正有问题的内容。", prompt,
                             max_tokens=100, temperature=0)
        data = parse_json(r.text)
        return bool(data.get("ok")), str(data.get("reason", ""))[:100], r.cost

    def _finalize(self, incoming: str, raw: str, cost: float, tokens: int, model_check: bool = True,
                  kind: str = "reply") -> Draft:
        from ..publish import stickers
        raw, sticker = stickers.split_tag(raw or "")       # “〔表情包：…〕”这一行不算回复的正文
        text = safety.clean_reply(raw)
        problem = safety.output_problem(text)
        if problem:
            return Draft(text=text, ok=False, reason=problem, cost=cost, tokens=tokens)
        if model_check:
            try:
                ok, reason, c2 = self.safety_check(incoming, text, kind=kind)
            except LLMError as e:
                return Draft(text=text, ok=False, reason=f"安全检查失败：{e}", cost=cost, tokens=tokens)
            cost += c2
            if not ok:
                return Draft(text=text, ok=False, reason=f"安全检查未通过：{reason}", cost=cost, tokens=tokens)
        return Draft(text=text, ok=True, cost=cost, tokens=tokens, sticker=sticker)


def _yes(v) -> bool:
    """模型偶尔会把 true 写成字符串 "true" / "是"。"""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "是", "要", "收藏")
    return bool(v)
