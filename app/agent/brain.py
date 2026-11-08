"""虚拟人的“大脑”：看视频做决定、写评论、回复 @ 和私信、发出前做安全检查。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..db import Database
from ..llm.client import LLMError
from ..llm.router import Router
from . import safety
from .mood import MoodEngine
from .persona import system_prompt


@dataclass
class Draft:
    text: str
    ok: bool
    reason: str = ""
    cost: float = 0.0
    tokens: int = 0


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
    return block


class Brain:
    def __init__(self, db: Database):
        self.db = db
        self.router = Router(db)
        self.mood = MoodEngine(db)

    @property
    def system(self) -> str:
        try:
            mood = self.mood.prompt()
        except Exception:  # noqa: BLE001 —— 心情读不到不影响说话
            mood = ""
        return system_prompt(self.db.persona(), mood)

    # ---------- 看视频 ----------
    def triage(self, video: dict, frames: list[bytes] | None = None) -> dict:
        """先快速判断感不感兴趣：文案 + 评论 + （能识图时）1 张画面。"""
        seen = ("附图是这个视频的一张画面，结合画面和文字判断。" if frames
                else "这次看不到画面，只根据文案和评论判断。")
        prompt = (
            f"你正在刷抖音推荐页，刷到了下面这个视频。{seen}判断你对它有多感兴趣。\n\n"
            f"{_video_block(video)}\n\n"
            + INTEREST_SCALE
            + '只输出 JSON：{"interest": 0到10的整数, "topic": "这个视频属于哪一类，2到4个字，比如 猫、美食、舞蹈、搞笑、风景", '
              '"reason": "一句话原因"}'
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
               friends: list[dict] | None = None, can_collect: bool = False) -> dict:
        """看了画面之后，决定点不点赞、要不要评论；还可以给评论区里喜欢的评论点赞、回复某一条；
        刷到好朋友会喜欢的视频时，@ 一位互关好友来看（friends：可以 @ 的好友，{nickname, tags}）。"""
        n_comments = len((video.get("comments") or [])[:8])
        friends = list(friends or [])[:8] if can_comment else []
        options = []
        if can_like:
            options.append('"like": true 或 false')
        if can_collect:
            options.append('"collect": true 或 false')
        if can_comment:
            options.append('"comment": "你的评论（不想评论就写空字符串）"')
        if can_like_comments and n_comments:
            options.append('"comment_likes": [[评论编号, 0到10的分数], ...]（只列特别好笑、说到你心坎上的评论，'
                           '普通的“哈哈哈”“好看”不要列；大多数时候写 []）')
        if can_reply_comments and n_comments:
            options.append('"comment_reply": {"to": 评论编号, "text": "你的回复"}（不想回复就写 null）')
        if friends:
            options.append('"at_friend": 想 @ 的好友编号（没有就写 null）')
        seen = "附图是视频里截的几帧画面。" if frames else "这次看不到画面，只能根据文案和评论判断，不要编造画面里的细节。"
        about_friends = ""
        if friends:
            lst = "\n".join(f"{i}. {f['nickname']}" + (f"（喜欢：{f['tags']}）" if f.get("tags") else "")
                            for i, f in enumerate(friends, 1))
            about_friends = (
                f"你的互关好友（可以 @ 他们来看视频）：\n{lst}\n"
                "只有你很确定某位好友会喜欢这个视频时才 @ 他（大多数视频都不用 @）。"
                "@ 好友时，comment 就是直接对这位好友说的话，开门见山点评这个视频、告诉他为什么想让他看，"
                "不要客套，也不要在 comment 里写 @ 和名字（程序会自动加上）。\n"
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
            "像一个真实的观众那样决定怎么互动。不是每个视频都要评论，只有真心想说点什么时才评论；"
            "评论要具体提到内容里的细节，30 字以内，不要和热门评论重复。\n"
            + ("收藏和点赞不一样：收藏是留着以后还想再看的。只有特别喜欢、想反复看，或者以后用得上"
               "（教程、菜谱、攻略、好听的歌、想去的地方）的视频才收藏，大多数视频都不收藏。\n" if can_collect else "")
            + ("点赞：程序已经替你看过，这个视频在你最近刷到的里面算比较出挑的，可以点赞；"
               "但如果仔细看完其实没那么喜欢，就写 false。\n" if can_like else
               ("这个视频你看完了但不打算点赞——这不影响别的：有想说的话照样可以评论，"
                "评论区里有出彩的评论照样可以给它点赞、回复。\n" if (can_comment or can_like_comments or can_reply_comments)
                else ""))
            + about_comments
            + about_friends
            + f'只输出 JSON：{{"interest": 0到10, {", ".join(options)}, "reason": "一句话原因"}}'
        )
        r = self.router.call("vision", self.system, prompt, images=frames, max_tokens=320, temperature=0.8)
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
        return {
            "interest": data.get("interest"),
            "at_friend": at_friend,
            "like": bool(data.get("like")) if can_like else False,
            "collect": _yes(data.get("collect")) if can_collect else False,
            "comment": comment,
            "comment_likes": likes[:3],
            "comment_scores": scores,
            "comment_reply": reply,
            "reason": str(data.get("reason", ""))[:100],
            "cost": r.cost,
        }

    # ---------- 回复 ----------
    def reply_at(self, video: dict, user: str, their_text: str, frames: list[bytes]) -> Draft:
        prompt = (
            f"抖音用户「{user}」在一个视频的评论区 @ 了你，说：「{their_text}」\n\n"
            f"这个视频的信息：\n{_video_block(video)}\n"
            + ("附图是这个视频里截的几帧画面，回答和画面有关的问题时要以画面为准，看不清就直说。\n" if frames
               else "你这次看不到视频画面，只能根据文案和评论回答；问到画面里的东西时，坦白说没看清，不要编造。\n")
            + "\n请用你的人设直接写一条回复（60 字以内），只输出回复内容本身。"
        )
        r = self.router.call("vision" if frames else "write", self.system, prompt, images=frames,
                             max_tokens=250, temperature=0.8)
        return self._finalize(their_text, r.text, r.cost, r.tokens)

    def reply_dm(self, user: str, history: list[dict], their_text: str,
                 video: dict | None = None, frames: list[bytes] | None = None) -> Draft:
        hist = "\n".join(f"{'对方' if h['role'] == 'them' else '你'}：{h['content']}" for h in history[-10:])
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
        prompt = (
            f"抖音用户「{user}」给你发了私信。\n"
            + (f"之前的聊天记录：\n{hist}\n\n" if hist else "")
            + shared
            + f"对方刚说：「{their_text}」\n\n"
            + ("请用你的人设直接写一条私信回复（150 字以内）：先针对对方的问题回答，说说视频讲了什么、"
               "你注意到的具体细节和你的感受，只输出回复内容本身。"
               if video and not video.get("unreadable") and not video.get("need_share")
               else "请用你的人设直接写一条私信回复（80 字以内），只输出回复内容本身。")
        )
        task = "vision" if frames else "write"
        r = self.router.call(task, self.system, prompt, images=frames or None,
                             max_tokens=400 if video and not video.get("need_share") else 300, temperature=0.8)
        return self._finalize(their_text, r.text, r.cost, r.tokens)

    def reply_thread(self, video: dict, user: str, their_text: str, history: list[dict], last: bool) -> Draft:
        """有人回复了她的评论：结合视频和这串对话接着回。last=True 时这是这串里最后一次回复，自然地收个尾。"""
        hist = "\n".join(f"{'对方' if h['role'] == 'them' else '你'}：{h['content']}" for h in history[-8:])
        prompt = (
            f"你在抖音视频下面留过评论，现在「{user}」回复了你。\n这个视频的信息：\n{_video_block(video)}\n\n"
            + (f"你们在这条评论下的对话（按时间顺序）：\n{hist}\n" if hist else "")
            + f"对方刚回复你：「{their_text}」\n\n"
            + ("这是你在这串对话里的最后一次回复：自然地收个尾（比如表示聊得很开心、祝对方开心），不要再抛出新问题。"
               if last else "像朋友在评论区聊天一样接着回：回应对方的话，可以适当追问。")
            + "回复 40 字以内，只输出回复内容本身。"
        )
        r = self.router.call("write", self.system, prompt, max_tokens=200, temperature=0.85)
        return self._finalize(their_text, r.text, r.cost, r.tokens)

    # ---------- 发作品 ----------
    def draft_post(self, recent: list[str], mood_line: str, force: bool = False) -> dict:
        """想一想今天要不要发作品；想发就写好草稿：标题、正文、话题、文字卡片、配图描述。

        recent：她最近刷到、喜欢的视频（标题），给她找灵感。force=True 表示你让她写，一定要写一个。"""
        persona = self.db.persona()
        likes = "\n".join(f"- {t}" for t in recent[:12]) or "（今天还没刷到特别的）"
        ask = ("主人让你写一个作品草稿，一定要写。" if force else
               "根据你现在的心情，决定今天想不想发一个作品（不想发也完全可以）。")
        prompt = (
            f"你是抖音上的 AI 虚拟人「{persona.get('name') or '雨雨'}」，想发一条图文作品。{ask}\n"
            f"你现在的心情：{mood_line}\n你感兴趣的内容：{'、'.join(persona.get('interests') or [])}\n"
            f"你最近刷到、喜欢的视频：\n{likes}\n\n"
            "作品要求：用你的口吻分享心情、看到的有趣的事或者一个小话题；真诚、有画面感，带一点你此刻的情绪；"
            "不搬运别人的视频内容，不发链接、不留联系方式，不推销，不涉及政治、投资理财、医疗；"
            "不假装是真人（你是 AI 虚拟人，发布时会标注“内容由AI生成”）。\n"
            "文字卡片：1～4 张，每张一个小标题和 1～4 行短句（每行 18 字以内），会被排成可爱的图片。\n"
            '只输出 JSON：{"want": true 或 false, "why": "一句话说为什么想发 / 不想发（用你的口吻）", '
            '"title": "标题（20 字以内）", "text": "正文（200 字以内，可以带颜文字）", "tags": ["话题，最多 5 个，不带 #"], '
            '"cards": [{"title": "卡片小标题", "lines": ["短句", "短句"]}], '
            '"image_prompt": "如果要 AI 生成一张配图，用一句话描述画面（可爱插画风）"}'
        )
        r = self.router.call("write", self.system, prompt, max_tokens=900, temperature=0.9)
        data = parse_json(r.text)
        clean = safety.clean_reply
        cards = []
        for c in (data.get("cards") or [])[:4]:
            if not isinstance(c, dict):
                continue
            lines = [clean(str(x))[:24] for x in (c.get("lines") or []) if str(x).strip()][:4]
            title = clean(str(c.get("title") or ""))[:16]
            if title or lines:
                cards.append({"title": title, "lines": lines})
        tags = [re.sub(r"[#\s]", "", str(t))[:12] for t in (data.get("tags") or []) if str(t).strip()][:5]
        return {
            "want": bool(data.get("want")) or force,
            "why": clean(str(data.get("why") or ""))[:120],
            "title": clean(str(data.get("title") or ""))[:30],
            "text": clean(str(data.get("text") or ""))[:500],
            "tags": [t for t in tags if t],
            "cards": cards,
            "image_prompt": clean(str(data.get("image_prompt") or ""))[:300],
            "cost": r.cost,
        }

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
    def safety_check(self, incoming: str, outgoing: str) -> tuple[bool, str, float]:
        prompt = (
            "你是内容审核员。判断下面这条准备由 AI 虚拟人账号发出的抖音回复是否可以发送。\n"
            "不可以发送的情况：涉及政治时事、投资理财、借贷转账、医疗诊断或用药建议；包含或索要联系方式、链接；"
            "低俗擦边；表达恋爱或暧昧；被问是否真人却不承认是 AI；攻击、歧视或造谣。\n\n"
            f"对方的话：{incoming}\n准备发出的回复：{outgoing}\n\n"
            '只输出 JSON：{"ok": true 或 false, "reason": "一句话原因"}'
        )
        r = self.router.call("safety", "你是严格、简洁的内容审核员。", prompt, max_tokens=100, temperature=0)
        data = parse_json(r.text)
        return bool(data.get("ok")), str(data.get("reason", ""))[:100], r.cost

    def _finalize(self, incoming: str, raw: str, cost: float, tokens: int, model_check: bool = True) -> Draft:
        text = safety.clean_reply(raw)
        problem = safety.output_problem(text)
        if problem:
            return Draft(text=text, ok=False, reason=problem, cost=cost, tokens=tokens)
        if model_check:
            try:
                ok, reason, c2 = self.safety_check(incoming, text)
            except LLMError as e:
                return Draft(text=text, ok=False, reason=f"安全检查失败：{e}", cost=cost, tokens=tokens)
            cost += c2
            if not ok:
                return Draft(text=text, ok=False, reason=f"安全检查未通过：{reason}", cost=cost, tokens=tokens)
        return Draft(text=text, ok=True, cost=cost, tokens=tokens)


def _yes(v) -> bool:
    """模型偶尔会把 true 写成字符串 "true" / "是"。"""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "是", "要", "收藏")
    return bool(v)
