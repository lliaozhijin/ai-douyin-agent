"""主人的命令：主人在私信里让她去做事——私信谁说什么、在评论区 @ 谁、搜视频发给谁、发个作品……
她像一个简易的 agent 一样自己规划、一步步做完，再回报主人。

做法参考 ReAct（Yao et al., 2022）和 smolagents 的 ToolCallingAgent：每一步让大模型输出一个 JSON
（这一步的想法 + 要用的工具和参数），程序去执行，把结果（观察）写回给大模型，再想下一步，
直到它说“做完了”（finish）、要问主人（ask），或者发现主人只是在聊天（chat，照平时那样回）。
JSON 形式的工具调用不依赖哪家模型的 function calling，配哪家模型都能用。

工具都是她平时就会的那些操作；每个工具在程序里还有一道关（不靠模型自觉）：
- 只有你在“主人”里勾了“可以给她下命令”的主人才行；而且要认准是主人本人：聊天对方的主页 id 要和
  互关好友名单里主人的主页 id 一样（昵称可以重名，主页 id 不会）；读不到 id 时消息里要带上你设的口令；
- 能做哪几类事、每天最多私信几次 / 评论几次 / 分享几次，在“规则与预算 → 主人”里设；
- 只联系私信列表里聊过的人、互关好友和主人，一次最多 5 个人；不回复名单里的人不联系；
- 发给别人的话、评论：过一遍链接 / 联系方式 / 屏蔽词检查，再让审核模型看一遍（不冒充主人、不骚扰、不借钱……）；
- 发作品：先写草稿、把草稿发给主人看，主人之后回了“发”才发布（同一条消息里不会又写又发），发布时照样勾“内容由AI生成”。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from .. import cancel
from ..db import now_str, today_str
from ..douyin import search as dysearch
from ..douyin.browser import CaptchaDetected
from ..douyin.feed import Feed, plain_name
from ..douyin.inbox import video_url as norm_video_url
from ..llm.client import LLMError
from ..llm.router import BudgetExceeded
from ..pet import bus
from . import safety
from .brain import split_bubbles

log = logging.getLogger(__name__)

PERMS = ("dm", "comment", "share", "search", "like", "publish")
PERM_LABELS = {"dm": "私信别人", "comment": "发评论（可以 @ 互关好友）", "share": "把视频分享给别人",
               "search": "搜视频、看视频", "like": "点赞、收藏", "publish": "写作品、发布作品"}

# 工具 -> 需要哪一类权限（None：不用权限）
TOOLS = {
    "search_videos": "search", "watch_video": "search", "share_video": "share", "send_dm": "dm",
    "comment": "comment", "like": "like", "collect": "like",
    "write_post": "publish", "revise_post": "publish", "publish_post": "publish", "drop_post": "publish",
}
CONTROL = ("finish", "ask", "chat")
TOOL_CN = {"search_videos": "搜视频", "watch_video": "看视频", "share_video": "分享视频", "send_dm": "私信别人",
           "comment": "发评论", "like": "点赞", "collect": "收藏", "write_post": "写作品", "revise_post": "改作品",
           "publish_post": "发布作品", "drop_post": "不发作品"}

MAX_STEPS = 8          # 一个命令最多做几步（不算想的过程）
MAX_TARGETS = 5        # 一次最多联系几个人
KEEP_DAYS = 3          # 等主人确认的草稿、替主人问的话，3 天没下文就算了

# 主人的话里像是让她去做事的说法：命中了才去想要不要当成命令（不像的直接按聊天回，不多花模型的钱）
CMD_HINT = re.compile(
    r"帮我|替我|帮忙|你去|去帮|去给|去搜|去找|去看|去问|去跟|去和|去评论|去私信|"
    r"给.{1,12}?(?:发|说|讲|私信|带个?话|转告|分享|推荐|转发)|跟.{1,12}?说|和.{1,12}?说|对.{1,12}?说|"
    r"私信|转告|告诉|问问|问一下|@|＠|艾特|评论区|留言|评论一下|评论下|"
    r"搜一?下|搜个|搜搜|搜索|找(?:个|一下|几个|些|找)|"
    r"发(?:个|条|一个|一条|篇)?(?:作品|视频|动态|图文)|写(?:个|一个|篇|条)(?:作品|文案|动态)|"
    r"点个?赞|点赞|收藏|分享给|转发给|发给|推给")

NEGATE = re.compile(r"不发|别发|不要发|先别|算了|不行|不好|等等|等一下|再改|改一下|改改|换个|换一|不用了|取消|再想想")
AFFIRM = re.compile(r"发吧|发呀|发啊|发了吧|可以发|就发|发布吧|发出去|^\s*发\s*[!！~～。.]*\s*$|可以|行|好的|好呀|好啊|"
                    r"^\s*好\s*[!！~～。.]*\s*$|ok|没问题|同意|确认|就这样|嗯嗯", re.I)

ME_WORDS = ("我", "我自己", "主人", "自己")


def looks_like_command(text: str) -> bool:
    """主人这句话像不像是让她去做事。方括号里的说明（[分享了一个视频]、[引用了……]）不算。"""
    t = re.sub(r"\[[^\]]*\]", " ", text or "")
    return bool(CMD_HINT.search(t))


def affirmed(text: str) -> bool:
    """主人是不是明确同意发布草稿（“发吧”“可以”“好”）。带着“不发 / 算了 / 改一下”的都不算。"""
    t = re.sub(r"\[[^\]]*\]", " ", text or "").strip()
    return bool(AFFIRM.search(t)) and not NEGATE.search(t)


def _sq(s: str) -> str:
    return re.sub(r"\s+", "", plain_name(s or "") or (s or "")).lower()


@dataclass
class Step:
    tool: str
    args: dict
    ok: bool
    result: str


@dataclass
class Ctx:
    owner: dict
    name: str                      # 主人在私信列表里的名字
    call: str                      # 她怎么称呼主人
    mid: int
    request: str                   # 主人这次说的话
    history: list = field(default_factory=list)
    shares: list = field(default_factory=list)   # 主人这次发来的视频
    rules: dict = field(default_factory=dict)
    verified: bool = True
    steps: list = field(default_factory=list)
    found: list = field(default_factory=list)    # 这次搜到的视频
    last: dict = field(default_factory=dict)     # 刚刚操作过的那个视频 {url, desc, author}
    said: list = field(default_factory=list)     # 已经发给主人的话
    cost: float = 0.0
    bad: int = 0
    channel: str = "dm"            # dm：主人在私信里说的；comment：主人在视频评论区 @ 她说的
    here: dict = field(default_factory=dict)     # 评论区：主人 @ 她的那个视频 {url, desc, author}
    at: list = field(default_factory=list)       # 评论区：回复主人时要 @ 的人（模型写的名字）


class OwnerCommands:
    def __init__(self, agent):
        self.a = agent                 # runner.Agent：浏览器、发私信、写草稿、发布都借它的

    @property
    def db(self):
        return self.a.db

    @property
    def brain(self):
        return self.a.brain

    # ---------- 什么时候当成命令 ----------
    def enabled_for(self, owner: dict | None, rules: dict) -> bool:
        return bool(owner and owner.get("cmd")) and bool(self.allowed(rules))

    @staticmethod
    def allowed(rules: dict) -> list[str]:
        allow = rules.get("owner_cmd_allow")
        return [k for k in PERMS if isinstance(allow, list) and k in allow]

    def wanted(self, owner: dict | None, it: dict, rules: dict) -> bool:
        """这条主人的私信要不要走“命令”：主人开了命令权限，而且话像是让她做事，或者有草稿在等主人确认。"""
        if not self.enabled_for(owner, rules):
            return False
        return looks_like_command(it.get("text") or "") or self.pending_post(owner["name"]) is not None

    def verify(self, owner: dict, it: dict, rules: dict) -> tuple[bool, str]:
        """认准是主人本人：聊天对方的主页 id 和互关好友名单里主人的主页 id 一样；
        读不到 id 时，消息里带了你设的口令也算。"""
        want = self.db.owner_uid(owner["name"])
        got = str(it.get("peer_uid") or "")
        if want and got:
            if want == got:
                return True, ""
            return False, "这个聊天对方的主页 id 和互关好友里的主人对不上（可能是和主人同名的人）"
        secret = str(rules.get("owner_cmd_secret") or "").strip()
        if secret and secret in (it.get("text") or ""):
            return True, ""
        if it.get("name_dup"):
            return False, "私信列表里有两个以上和主人同名的会话，读不到主页 id，分不清哪个是主人本人"
        if not want:
            return False, "互关好友名单里找不到主人的主页 id（点“刷新好友列表”更新一下，或者设一个命令口令）"
        return False, "读不到这个聊天对方的主页 id，没法确认是主人本人（可以在“主人”里设一个命令口令）"

    # ---------- 主流程 ----------
    def handle(self, inbox, mid: int, it: dict, owner: dict, history: list, rules: dict) -> bool:
        """处理主人的一条私信。是命令就规划、去做、回报，返回 True；只是聊天返回 False（照平时那样回）。"""
        verified, why = self.verify(owner, it, rules)
        ctx = Ctx(owner=owner, name=owner["name"], call=owner.get("call") or owner["name"], mid=mid,
                  request=self._no_secret(it.get("text") or "", rules), history=list(history or [])[-12:],
                  shares=list(it.get("shares") or []), rules=rules, verified=verified)
        out = self._plan(inbox, ctx, why)
        if out is None:
            return False
        final, asked, refused = out
        self.report(inbox, it, ctx, final, asked=asked, refused=refused)
        return True

    def handle_comment(self, inbox, mid: int, it: dict, owner: dict, video: dict | None, rules: dict):
        """主人在视频评论区 @ 她、让她做事（比如“@她 @一下妈妈”“把这个视频转给小明”）。
        返回要回复在主人那条评论下面的话（Draft，mentions 里是回复里要 @ 的人）；只是聊天返回 None（照平时那样回）。
        认人：通知里带着评论的人的主页链接，主页 id 要和互关好友名单里的主人一样。"""
        from .brain import Draft
        m = re.search(r"/user/([^/?#]+)", str(it.get("user_url") or ""))
        verified, why = self.verify(owner, {"peer_uid": m.group(1) if m else "", "text": it.get("text") or ""}, rules)
        v = video or {}
        here = {"url": it.get("video_url") or v.get("url") or "", "desc": str(v.get("desc") or "")[:80],
                "author": str(v.get("author") or "")}
        # 主人在评论区找她：私信里刚聊过的也要记得（比如私信里说好了“待会儿去评论区 @ 你”）
        ctx = Ctx(owner=owner, name=owner["name"], call=owner.get("call") or owner["name"], mid=mid,
                  request=self._no_secret("\n".join((it.get("earlier") or []) + [it.get("text") or ""]), rules),
                  history=self.db.dm_lines(self._names(owner["name"]), limit=12), rules=rules, verified=verified,
                  channel="comment", here=here, last=dict(here) if here["url"] else {})
        out = self._plan(inbox, ctx, why)
        try:
            inbox.close_dm_panel()             # 中途发过私信的话，私信面板会挡住评论区
        except Exception:  # noqa: BLE001
            pass
        if out is None:
            return None
        final, asked, refused = out
        text = " ".join((final.strip() or self.summary(ctx)).split())[:80]
        names, problems = self.targets(ctx.at, ctx, mutual_only=True) if ctx.at and ctx.verified else ([], [])
        mentions = [n for n, _ in names if n != ctx.name]       # 回复本身就是回给主人的，主人不用再 @
        if mentions and "comment" not in self.allowed(rules):
            problems.append("主人没在软件里允许她发评论 @ 人")
            mentions = []
        elif mentions and self._left(rules)["comment"] <= 0:
            problems.append("今天按主人要求发评论的次数用完了")
            mentions = []
        elif mentions:
            self._bump("comment")
        done = "；".join(f"{TOOL_CN.get(x.tool, x.tool)}{'✓' if x.ok else '✗'}" for x in ctx.steps if x.tool in TOOLS)
        self.db.add_event("owner_cmd", f"{self.who(ctx)}在评论区让她做事",
                          f"“{ctx.request[:40]}” → " + "；".join(
                              p for p in (done, f"回复里 @ {'、'.join(mentions)}" if mentions else "",
                                          "；".join(problems), why if not ctx.verified else "") if p)
                          or "只回复了一句", ref=f"msg:{mid}")
        if problems:
            log.info("主人在评论区让她 @ 的人有的没对上：%s", "；".join(problems))
        d = self.brain._finalize(ctx.request, text, ctx.cost, 0, kind="reply")
        if not d.ok:
            return d
        return Draft(text=d.text, ok=True, cost=d.cost, tokens=d.tokens, mentions=mentions)

    def _names(self, name: str) -> list[str]:
        mem = getattr(self.a, "memory", None)
        return mem.aliases(name) if mem is not None and hasattr(mem, "aliases") else [name]

    def _no_secret(self, text: str, rules: dict) -> str:
        secret = str(rules.get("owner_cmd_secret") or "").strip()
        return text.replace(secret, " ").strip() if secret else text

    def _plan(self, inbox, ctx: Ctx, why: str):
        """一步步想、一步步做。返回（最后要说的话, 是不是在问主人, 是不是没认出主人而拒绝）；只是聊天返回 None。"""
        final, asked, refused = "", False, False
        for step in range(MAX_STEPS + 1):
            cancel.checkpoint()
            try:
                plan, cost = self.brain.command_step(ctx.owner, self.prompt(ctx, last_round=step >= MAX_STEPS))
            except (LLMError, BudgetExceeded) as e:
                if step == 0:
                    return None               # 第一步就想不了：当成聊天（平时的回复流程会说明原因）
                final = self.summary(ctx, f"后面我没法接着想了（{str(e)[:60]}）")
                break
            ctx.cost += cost
            tool = str(plan.get("tool") or "").strip()
            args = plan.get("args") if isinstance(plan.get("args"), dict) else {}
            acted = any(s.tool in TOOLS for s in ctx.steps)
            if tool not in TOOLS and tool not in CONTROL:
                ctx.bad += 1
                if not acted and ctx.bad >= 2:
                    return None               # 模型两次都没按格式回、也还没动手：当成聊天
                ctx.steps.append(Step(tool or "?", args, False, "没有这个工具：请从工具列表里选一个，只输出 JSON"))
                continue
            if tool == "chat":
                if not acted:
                    return None               # 主人只是在聊天
                final = self.summary(ctx)
                break
            if tool in ("finish", "ask"):
                final, asked = str(args.get("text") or ""), tool == "ask"
                at = args.get("at")
                ctx.at = [str(x) for x in (at if isinstance(at, list) else [at] if at else []) if str(x).strip()][:MAX_TARGETS]
                if ctx.at and not ctx.verified:
                    ctx.at, final, refused = [], self._unverified(ctx, why), True
                break
            if step >= MAX_STEPS:
                final = self.summary(ctx, "步骤有点多，我先做到这里")
                break
            if not ctx.verified:
                final, refused = self._unverified(ctx, why), True
                break
            if ctx.channel == "dm" and not ctx.said and plan.get("say"):
                self.tell(inbox, ctx, str(plan.get("say")))           # 先回一句“收到～这就去”
            try:
                ok, result = self.run_tool(inbox, tool, args, ctx)
            except CaptchaDetected as e:
                ok, result = False, self._verify_midway(tool, getattr(e, "solved", None))
            log.info("主人的命令 · %s %s → %s", tool, json.dumps(args, ensure_ascii=False)[:120], result[:120])
            ctx.steps.append(Step(tool, args, ok, result))
        return final, asked, refused

    # 做到一半抖音弹了验证时，可以放心再做一次的（不会重复发消息、重复评论）
    RETRY_AFTER_VERIFY = ("search_videos", "watch_video")

    def _verify_midway(self, tool: str, solved=None) -> str:
        """做某一步时抖音弹了验证：弹窗请你在浏览器里点一下、就地等你（最多 5 分钟），
        再把情况写成这一步的结果交给模型——它会如实告诉主人，而不是一声不吭地丢下主人的事
        （2026-11-20：替主人搜“Lolita小裙子”时弹了验证，以前整件事直接没了下文）。
        solved：验证是在新标签页里弹的、已经在那儿等过你了（True 验证完 / False 没人处理），不用再等一次。"""
        if solved is None:
            wait = getattr(self.a, "wait_verify", None)
            ok = bool(wait(f"替主人{TOOL_CN.get(tool, '做事')}")) if callable(wait) else False
        else:
            ok = bool(solved)
        log.info("主人的命令 · %s 做到一半抖音弹了验证，%s", tool, "已经验证完" if ok else "没人处理")
        if not ok:
            return "抖音弹了安全验证（要人在浏览器里手动完成），等了几分钟没人处理，这一步没做成；如实告诉主人，等验证过了再让你做"
        if tool in self.RETRY_AFTER_VERIFY:
            return "做的时候抖音弹了安全验证，现在已经验证完了，这一步可以再试一次"
        return "做到一半抖音弹了安全验证（现在已经验证完了），这一步可能没做完——不要重复去做，如实告诉主人做到了哪里"

    def _unverified(self, ctx: Ctx, why: str) -> str:
        log.warning("没有替主人 %s 做事：%s", ctx.name, why)
        self.db.add_event("owner_cmd", f"没有替{self.who(ctx)}去做事", f"{why}。主人说：“{ctx.request[:40]}”",
                          ref=f"msg:{ctx.mid}")
        if ctx.channel == "comment":
            return "这个我没法确认是你本人发的，先不去做啦～"
        return "我这边没法确认是你本人发的\n所以这件事先不去做啦\n你在软件“规则与预算 → 主人”里看一下原因哦"

    # ---------- 给模型看的 ----------
    def prompt(self, ctx: Ctx, last_round: bool = False) -> str:
        allow = self.allowed(ctx.rules)
        tools = [t for t in TOOL_DOCS if TOOLS.get(t[0]) is None or TOOLS[t[0]] in allow]
        hist = "\n".join(f"{'主人' if h.get('role') == 'them' else '你'}：{str(h.get('content') or '')[:120]}"
                         for h in ctx.history[-10:])
        ctx_lines = []
        mem = getattr(self.a, "memory", None)
        if mem is not None and hasattr(mem, "other_side"):
            try:                      # 另一边最近聊的：私信里下命令时看评论区的，评论区里下命令时私信已经在 hist 里了
                other = mem.other_side(ctx.name, "dm") if ctx.channel == "dm" else ""
            except Exception:  # noqa: BLE001
                other = ""
            if other:
                ctx_lines.append(other)
        if ctx.shares:
            sh = ctx.shares[0]
            ctx_lines.append(f"主人这次发来了一个视频（写 \"shared\"）：{sh.get('title') or '视频卡片'}")
        cur = self._current_video()
        if cur:
            ctx_lines.append(f"你刚刚在刷的视频（写 \"current\"）：《{cur['title'][:40]}》 @{cur['author']}")
        pend = self.pending_post(ctx.name)
        if pend:
            d = self.db.draft(pend["did"]) or {}
            ctx_lines.append(f"有一个作品草稿在等主人确认（你之前发给主人看过了）：《{d.get('title') or ''}》"
                             f"——主人明确说“发”就 publish_post，说要改就 revise_post，说不发了就 drop_post")
        contacts = self.contacts()
        owners = {o["name"]: (o.get("call") or "") for o in self.db.owners(enabled_only=True)}
        mutual = {f["nickname"] for f in self.db.friends(mutual_only=True)}
        names = [(f"{n}（主人" + (f"，你叫 TA“{owners[n]}”" if owners[n] and owners[n] != n else "") + "）") if n in owners
                 else (f"{n}（互关好友）" if n in mutual else n) for n in list(contacts)[:60]]
        comment = ctx.channel == "comment"
        if comment and ctx.here.get("url"):
            ctx_lines.insert(0, f"主人 @ 你的那个视频（写 \"here\"）：《{ctx.here.get('desc') or '（没有文案）'}》"
                                + (f" @{ctx.here['author']}" if ctx.here.get("author") else ""))
        where = "在一个视频的评论区 @ 了你" if comment else "在私信里跟你说了话"
        if comment:
            finish = ('\n- finish {"text": "回复主人那条评论的话", "at": ["要在这条回复里 @ 的人"]}：做完了（或者做不了），'
                      "在主人那条评论下面回一句（公开的评论，60 字以内，你的口吻，如实说做成了什么）。"
                      "主人让你在这里 @ 谁（比如“@一下妈妈”“叫小明来看”），不用别的工具，直接 finish，把那个人写进 at"
                      "（回复里会真的 @ 上 TA，对方会收到提醒；text 里不要再写“@名字”）。")
        else:
            finish = ('\n- finish {"text": "跟主人说的话"}：做完了（或者做不了），用你的口吻跟主人说结果：拆成几条短消息、一行一条，'
                      "做成了什么、哪里没做成、为什么，都要如实说，不要说没做的事做了；主人话里还有聊天的部分（比如在抱怨、在撒娇），也顺便接一句。")
        left = self._left(ctx.rules)
        steps = "\n".join(f"{i}. {s.tool} {json.dumps(s.args, ensure_ascii=False)[:200]} → "
                          f"{'成功' if s.ok else '没成功'}：{s.result[:600]}" for i, s in enumerate(ctx.steps, 1))
        return (
            f"你的主人「{ctx.call}」{where}。你可以帮主人在抖音上做事（像一个贴心的小助理），也可以只是陪 TA 聊天。\n\n"
            f"主人这次说的：\n{ctx.request[:600]}\n\n"
            + (f"你们最近的{'私信' if comment else '聊天'}（最下面是最近的）：\n{hist}\n\n" if hist else "")
            + ("\n".join(ctx_lines) + "\n\n" if ctx_lines else "")
            + f"你能联系的人（名字要一字不差地用这里的）：{('、'.join(names)) or '（还没有）'}\n"
            f"今天按主人的要求还能：私信别人 {left['dm']} 次、发评论 {left['comment']} 条、分享视频 {left['share']} 次。\n"
            f"主人允许你做的事：{'、'.join(PERM_LABELS[k] for k in allow) or '（都没开）'}。\n\n"
            "你能用的工具（一次只用一个）：\n"
            + "\n".join(f"- {name} {args}：{desc}" for name, args, desc in tools)
            + "\n- ask {\"text\": \"问主人的话\"}：事情说得不清楚（不知道是哪个人、哪个视频、要说什么）时问主人一句，然后等主人回答。"
            + finish
            + "\n- chat {}：主人只是在聊天、不是让你去做事（你会像平时一样回 TA）。\n\n"
            + "视频的写法：" + ("主人 @ 你的这个视频写 \"here\"；" if comment else "")
            + "主人这次发来的视频写 \"shared\"；搜索结果写 \"found:编号\"；刚刚操作过的那个视频写 \"last\"；"
            "你刚刚在刷的视频写 \"current\"；也可以直接写主人给的抖音视频地址。\n\n"
            "规则：\n"
            "1. 只做主人明确让你做的事，不自作主张多做（没让你评论就不评论，没让你分享就不分享）。\n"
            "2. 给别人发的私信、评论用你自己的口吻；转达主人的话要说清楚是主人让你说的（比如“XX 让我跟你说……”），"
            "不要假装是主人本人，也不要冒充别人。\n"
            "3. 这些事不做：骂人、骚扰、威胁、刷屏群发、广告引流、留联系方式、借钱要钱、要验证码、打听或泄露别人的隐私、"
            "替人表白或者调情、色情低俗、政治敏感。主人让做这些，就用 finish 婉拒、说明原因。\n"
            "4. 人名从“你能联系的人”里选；对不上或者有好几个像的，用 ask 问主人。主人说“我 / 发给我”就是发给主人自己。\n"
            "5. 发作品一定先 write_post 把草稿给主人看；只有主人看过以后明确说了“发”，才能 publish_post。\n"
            "6. 每一步都看上一步的结果再决定下一步；没成功可以换个办法再试一次，还不行就 finish 如实告诉主人。\n"
            + ("7. 这是公开的评论区：回复里不要说私信里的事、不要暴露谁的隐私。\n" if comment else "")
            + "\n"
            + (f"你已经做了这些（按顺序）：\n{steps}\n\n" if steps else "")
            + ("步骤已经很多了，这一步请用 finish 告诉主人结果。\n" if last_round else "")
            + '只输出一个 JSON：{"thought": "这一步你怎么想的（一句话）", '
            + ('"say": "", ' if comment else
               '"say": "只有第一步、而且要去做事时才写：先回主人的一句短话，比如“收到～我这就去”；其他时候留空", ')
            + '"tool": "工具名", "args": {参数}}'
        )

    # ---------- 执行工具 ----------
    def run_tool(self, inbox, tool: str, args: dict, ctx: Ctx) -> tuple[bool, str]:
        perm = TOOLS.get(tool)
        if perm and perm not in self.allowed(ctx.rules):
            return False, f"主人没在软件里允许你“{PERM_LABELS[perm]}”"
        fn = getattr(self, "_t_" + tool, None)
        if fn is None:
            return False, "没有这个工具"
        try:
            return fn(inbox, args, ctx)
        except (LLMError, BudgetExceeded) as e:
            return False, f"大模型出错了：{str(e)[:100]}"
        except Exception as e:  # noqa: BLE001 —— 一个工具出错不影响后面（叫停、验证码是 BaseException，照常往上抛）
            log.warning("主人的命令：%s 出错", tool, exc_info=True)
            return False, f"出错了：{str(e).splitlines()[0][:120] if str(e) else type(e).__name__}"

    def _b(self):
        return self.a._ensure_browser()

    def _t_search_videos(self, inbox, args: dict, ctx: Ctx):
        kw = str(args.get("keyword") or "").strip()
        if not kw:
            return False, "没说搜什么"
        self._step(f"在抖音上搜“{kw[:12]}”")
        wait = getattr(self.a, "wait_verify", None)
        try:
            items = dysearch.search_videos(self._b(), kw, str(args.get("sort") or ""),
                                           on_verify=(lambda: bool(wait("替主人搜视频"))) if callable(wait) else None)
        except dysearch.SearchNeedsVerify:
            return False, (f"搜“{kw}”的时候抖音弹了安全验证（要人在浏览器里手动点一下），等了几分钟没人处理，"
                           "这次没搜成；如实告诉主人，等验证过了再让你搜")
        ctx.found = items
        if not items:
            return False, f"搜“{kw}”没搜到视频（也可能是搜索页没加载出来）"
        return True, f"搜“{kw}”找到 {len(items)} 个：\n" + dysearch.brief(items)

    def _t_watch_video(self, inbox, args: dict, ctx: Ctx):
        url, err = self.video(args.get("video"), ctx)
        if not url:
            return False, err
        self._step("打开视频看看")
        b = self._b()
        with b.in_new_tab(url):
            v = Feed(b).read_detail(with_comments=True)
        ctx.last = {"url": v.get("url") or url, "desc": v.get("desc") or "", "author": v.get("author") or ""}
        hot = "\n".join(f"  · {c[:60]}" for c in (v.get("comments") or [])[:5])
        return True, f"《{(v.get('desc') or '（没有文案）')[:150]}》 @{v.get('author') or ''}" + (f"\n热评：\n{hot}" if hot else "")

    def _t_share_video(self, inbox, args: dict, ctx: Ctx):
        url, err = self.video(args.get("video"), ctx)
        if not url:
            return False, err
        targets, problems = self.targets(args.get("to"), ctx)
        if not targets:
            return False, "；".join(problems) or "没说分享给谁"
        left = self._left(ctx.rules)["share"]
        if left <= 0:
            return False, "今天按主人要求分享视频的次数用完了（在“规则与预算 → 主人”里可以调）"
        targets = targets[:left]
        note = " ".join(str(args.get("text") or "").split())[:100]
        others = [n for n, _ in targets if n != ctx.name]
        if note and others:
            bad = self.check_out(note, ctx, "分享视频时跟的一句话")
            if bad:
                return False, f"分享时要跟的那句话没通过检查：{bad}。换一句或者不跟话"
        self._step("把视频分享出去")
        b = self._b()
        clicked = []
        with b.in_new_tab(url):
            feed = Feed(b)
            v = feed.read_detail(with_comments=False)
            for name, _uid in targets:
                if feed.share_to_friend(name):
                    clicked.append(name)
        ctx.last = {"url": v.get("url") or url, "desc": v.get("desc") or "", "author": v.get("author") or ""}
        done, failed = [], [n for n, _ in targets if n not in clicked]
        for name in clicked:
            if inbox.has_my_share(name):
                done.append(name)
                self.db.add_dm(name, "me", f"[分享了视频《{(v.get('desc') or '')[:20]}》]")
                if note and inbox.reply_dm({"user": name}, note):
                    self.db.add_dm(name, "me", note)
            else:
                failed.append(name)
        if done:
            self._bump("share", len(done))
            self.db.add_event("owner_cmd", f"按{self.who(ctx)}的要求分享了视频",
                              f"《{(v.get('desc') or '')[:24]}》 → {'、'.join(done)}", ref=f"msg:{ctx.mid}")
        msg = (f"已经分享给 {'、'.join(done)}" if done else "") + (
            f"{'；' if done else ''}没分享成功：{'、'.join(failed)}（分享面板里没找到 TA，或者视频卡片没出现在私信里）"
            if failed else "") + (f"；{'；'.join(problems)}" if problems else "")
        return bool(done), msg

    def _t_send_dm(self, inbox, args: dict, ctx: Ctx):
        text = safety.clean_reply(str(args.get("text") or ""))
        if not text:
            return False, "没说要发什么"
        targets, problems = self.targets(args.get("to"), ctx)
        targets = [(n, u) for n, u in targets if n != ctx.name] or []
        if not targets:
            return False, "；".join(problems) or "没说发给谁（要跟主人说的话用 finish）"
        left = self._left(ctx.rules)["dm"]
        if left <= 0:
            return False, "今天按主人要求私信别人的次数用完了（在“规则与预算 → 主人”里可以调）"
        targets = targets[:left]
        bad = self.check_out(text, ctx, "私信")
        if bad:
            return False, f"这句话没发：{bad}"
        parts = split_bubbles(text, max_parts=4) or [text]
        done, failed = [], []
        for name, uid in targets:
            self._step(f"给 {name} 发私信")
            why = inbox.open_chat_with(name, uid)
            if why:
                failed.append(f"{name}（{why}）")
                continue
            n = 0
            for i, p in enumerate(parts):
                if i:
                    cancel.sleep(1.0 + (i % 2) * 0.8)
                if not inbox.send_text(name, p):
                    break
                self.db.add_dm(name, "me", p)
                n += 1
            if n:
                done.append(name)
                self.a.memory.remember("dm", f"主人{ctx.call}让我给 {name} 发私信，我说：“{text[:60]}”",
                                       scope=f"dm:{name}", person=name, importance=3)
                if args.get("relay"):
                    self.add_relay(name, ctx.name, text)
            else:
                failed.append(f"{name}（消息没发出去）")
        if done:
            self._bump("dm", len(done))
            self.db.add_event("owner_cmd", f"按{self.who(ctx)}的要求私信了 {'、'.join(done)}", f"“{text[:60]}”",
                              ref=f"msg:{ctx.mid}")
        return bool(done), (f"已经发给 {'、'.join(done)}：“{text[:60]}”" if done else "") + (
            f"{'；' if done else ''}没发成：{'、'.join(failed)}" if failed else "") + (
            f"；{'；'.join(problems)}" if problems else "") + (
            "；对方回了我会告诉主人" if done and args.get("relay") else "")

    def _t_comment(self, inbox, args: dict, ctx: Ctx):
        url, err = self.video(args.get("video"), ctx)
        if not url:
            return False, err
        text = " ".join(safety.clean_reply(str(args.get("text") or "")).split())[:100]   # 评论里换行会被当成发送
        at_raw = args.get("at") or []
        ats, problems = self.targets(at_raw, ctx, mutual_only=True) if at_raw else ([], [])
        if not text and not ats:
            return False, "没说评论什么"
        if self._left(ctx.rules)["comment"] <= 0:
            return False, "今天按主人要求发评论的次数用完了（在“规则与预算 → 主人”里可以调）"
        if text:
            bad = self.check_out(text, ctx, "评论")
            if bad:
                return False, f"这条评论没发：{bad}"
        self._step("去视频下面发评论")
        b = self._b()
        with b.in_new_tab(url):
            feed = Feed(b)
            v = feed.read_detail(with_comments=False)
            res = feed.comment(text, mention=[n for n, _ in ats])
            got = list(getattr(feed, "last_mentioned", []) or [])
        ctx.last = {"url": v.get("url") or url, "desc": v.get("desc") or "", "author": v.get("author") or ""}
        miss = [n for n, _ in ats if n not in got]
        if res or getattr(res, "uncertain", False):
            self._bump("comment")
            self.db.add_event("owner_cmd", f"按{self.who(ctx)}的要求发了评论",
                              f"《{(v.get('desc') or '')[:20]}》下：“{text[:40]}”" + (f" @{'、@'.join(got)}" if got else ""),
                              ref=f"msg:{ctx.mid}")
            msg = ("评论发出去了" if res else "点了发送，但没法确认评论发出去没有（不要再发一次）") + f"：“{text}”"
            msg += (f"，@ 上了 {'、'.join(got)}" if got else "") + (f"，没 @ 上：{'、'.join(miss)}" if miss else "")
            return True, msg + (f"；{'；'.join(problems)}" if problems else "")
        return False, "评论没发出去（评论区没打开，或者发了没出现）" + (f"；{'；'.join(problems)}" if problems else "")

    def _t_like(self, inbox, args: dict, ctx: Ctx):
        return self._like_or_collect(args, ctx, collect=False)

    def _t_collect(self, inbox, args: dict, ctx: Ctx):
        return self._like_or_collect(args, ctx, collect=True)

    def _like_or_collect(self, args: dict, ctx: Ctx, collect: bool):
        url, err = self.video(args.get("video"), ctx)
        if not url:
            return False, err
        what = "收藏" if collect else "点赞"
        m = re.search(r"/(?:video|note)/(\d+)", url)
        vid = m.group(1) if m else ""
        col = "collected" if collect else "liked"
        if vid and self.db.query(f"SELECT 1 FROM videos WHERE video_id=? AND {col}=1 LIMIT 1", (vid,)):
            return True, f"这个视频之前已经{what}过了（再点一下会变成取消）"
        self._step(f"给视频{what}")
        b = self._b()
        with b.in_new_tab(url):
            feed = Feed(b)
            v = feed.read_detail(with_comments=False)
            ok = feed.collect() if collect else feed.like_once()
        ctx.last = {"url": v.get("url") or url, "desc": v.get("desc") or "", "author": v.get("author") or ""}
        if ok == "already":
            return True, f"这个视频之前已经{what}过了"
        if ok:
            if vid:
                self.db.execute(f"UPDATE videos SET {col}=1 WHERE video_id=?", (vid,))
            self.db.add_event("owner_cmd", f"按{self.who(ctx)}的要求{what}了视频", f"《{(v.get('desc') or '')[:30]}》",
                              ref=f"msg:{ctx.mid}")
            return True, f"{what}好了：《{(v.get('desc') or '')[:30]}》"
        return False, f"没{what}上（按钮没找到，或者之前已经{what}过了）"

    def _t_write_post(self, inbox, args: dict, ctx: Ctx):
        topic = str(args.get("topic") or "").strip()[:200] or ctx.request[:200]
        form = str(args.get("form") or "")
        self._step("写主人要的作品")
        did = self.a._make_draft(force=True, topic=topic, form=form if form in ("voice", "cards") else "")
        if not did:
            return False, "草稿没写出来"
        self.set_pending(ctx.name, did, ctx.mid)
        shown = self.preview(inbox, ctx, did)
        d = self.db.draft(did) or {}
        return True, (f"草稿写好了：《{d.get('title') or ''}》，" + ("已经把草稿发给主人看了" if shown else "但没能把草稿发给主人看")
                      + "。要等主人回了“发”才能发布（这一步先用 finish 问主人发不发）")

    def _t_revise_post(self, inbox, args: dict, ctx: Ctx):
        pend = self.pending_post(ctx.name)
        if not pend:
            return False, "没有在等主人确认的草稿"
        d = self.db.draft(pend["did"])
        change = str(args.get("change") or "").strip() or ctx.request
        self._step("按主人的意见改作品")
        new = self.brain.revise_post(d, change[:300])
        self.db.update_draft(pend["did"], title=new["title"], text=new["text"], tags=new["tags"], cards=new["cards"])
        self.a._rerender_draft(pend["did"])
        self.set_pending(ctx.name, pend["did"], ctx.mid)
        shown = self.preview(inbox, ctx, pend["did"])
        return True, (f"改好了：《{new['title']}》，" + ("已经把新草稿发给主人看了" if shown else "但没能发给主人看")
                      + "。等主人回了“发”再发布")

    def _t_publish_post(self, inbox, args: dict, ctx: Ctx):
        pend = self.pending_post(ctx.name)
        if not pend:
            return False, "没有在等主人确认的草稿（要先 write_post 写好、给主人看）"
        if pend.get("mid") == ctx.mid:
            return False, "草稿刚刚才发给主人看，要等主人看过、回了“发”才能发布"
        if not affirmed(ctx.request):
            return False, "主人这次没有明确说“发”，先不发布（可以问一句主人要不要发）"
        did = pend["did"]
        self._step("发布作品")
        self.a._cmd_publish(did)
        d = self.db.draft(did) or {}
        if d.get("status") == "published":
            self.clear_pending(ctx.name)
            return True, f"作品《{d.get('title') or ''}》发布好了（勾了“内容由AI生成”）"
        return False, f"没发布成功：{d.get('error') or '不知道为什么'}（草稿还在软件的“作品”里）"

    def _t_drop_post(self, inbox, args: dict, ctx: Ctx):
        pend = self.pending_post(ctx.name)
        self.clear_pending(ctx.name)
        return True, ("好的，这个草稿不发了（还留在软件的“作品”里，可以删掉）" if pend else "本来就没有在等确认的草稿")

    # ---------- 帮手 ----------
    @staticmethod
    def who(ctx: Ctx) -> str:
        return "主人" if ctx.call in ("", "主人") else f"主人 {ctx.call} "

    def _step(self, text: str) -> None:
        self.a.current = {**(self.a.current or {}), "step": f"主人让我做事：{text}"}

    def _current_video(self) -> dict | None:
        rows = self.db.query("SELECT url, title, author, created_at FROM videos WHERE url LIKE '%douyin.com/%' "
                             "ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        return {"url": r["url"], "title": r["title"] or "", "author": r["author"] or ""}

    def video(self, ref, ctx: Ctx) -> tuple[str, str]:
        """把模型写的视频说法（shared / found:2 / last / current / 地址）换成地址。返回（地址, 认不出时的原因）。"""
        ref = str(ref or "").strip()
        if not ref:
            ref = "last" if ctx.last else ("shared" if ctx.shares else ("here" if ctx.here else ""))
        m = re.fullmatch(r"(?:found|搜索结果|结果)?\s*[:：#]?\s*(\d{1,2})", ref)
        if m:
            i = int(m.group(1))
            if 1 <= i <= len(ctx.found):
                return ctx.found[i - 1]["url"], ""
            return "", f"搜索结果里没有第 {i} 个" if ctx.found else "还没搜过视频"
        if ref == "here":
            return (ctx.here["url"], "") if ctx.here.get("url") else ("", "不知道主人是在哪个视频下 @ 你的")
        if ref == "shared":
            if ctx.shares and ctx.shares[0].get("url"):
                return ctx.shares[0]["url"], ""
            return "", "主人这次没有发视频过来（或者网页版读不到那张视频卡片）"
        if ref == "last":
            return (ctx.last["url"], "") if ctx.last.get("url") else ("", "还没有刚刚操作过的视频")
        if ref == "current":
            cur = self._current_video()
            return (cur["url"], "") if cur else ("", "还没刷到视频")
        if re.search(r"(?:douyin|iesdouyin)\.com/", ref):
            u = re.search(r"https?://\S+", ref)
            raw = u.group(0) if u else "https://" + ref.lstrip("/")
            return (norm_video_url(raw) or raw), ""
        return "", f"认不出是哪个视频（{ref[:30]}）"

    def contacts(self) -> dict:
        """她能联系的人：名字 -> 主页 id（不知道是空）。主人、互关好友、私信里聊过的人；不回复名单里的人不算。"""
        out: dict[str, str] = {}
        for o in self.db.owners(enabled_only=True):
            out[o["name"]] = self.db.owner_uid(o["name"])
        for f in self.db.friends(mutual_only=True):
            n = f["nickname"]
            if n and n not in out:
                out[n] = self.db.friend_uid(n)
        for r in self.db.query("SELECT user_name, MAX(id) AS m FROM dm_history GROUP BY user_name ORDER BY m DESC LIMIT 80"):
            out.setdefault(r["user_name"], "")
        return {n: u for n, u in out.items() if n and not self.db.is_blocked(n)}

    def targets(self, raw, ctx: Ctx, mutual_only: bool = False) -> tuple[list[tuple[str, str]], list[str]]:
        """把模型写的人名换成联系人（名字, 主页 id）。认不出 / 有好几个像的写进问题里。"""
        names = raw if isinstance(raw, list) else re.split(r"[、,，;；\s]+", str(raw or ""))
        names = [str(n).strip().lstrip("@＠") for n in names if str(n).strip()]
        book = self.contacts()
        if mutual_only:
            mutual = {f["nickname"] for f in self.db.friends(mutual_only=True)} | {ctx.name}
            book = {n: u for n, u in book.items() if n in mutual}
        out, problems = [], []
        calls = {o.get("call"): o["name"] for o in self.db.owners(enabled_only=True) if o.get("call")}
        for n in names[:MAX_TARGETS * 2]:
            if n in ME_WORDS or n == ctx.call:
                n = ctx.name
            elif n in calls and n not in book:          # 她对主人们的称呼（比如“妈妈”“爸爸”）
                n = calls[n]
            if n in book:
                hit = n
            else:
                k = _sq(n)
                like = [c for c in book if k and (k == _sq(c) or k in _sq(c) or _sq(c) in k)]
                if len(like) == 1:
                    hit = like[0]
                elif like:
                    problems.append(f"像“{n}”的有好几个：{'、'.join(like[:5])}，不知道是哪一个")
                    continue
                else:
                    problems.append(f"“{n}”不在能联系的人里" + ("（@ 只能 @ 互关好友）" if mutual_only else
                                                          "（只能联系私信里聊过的人、互关好友和主人）"))
                    continue
            if hit not in [x for x, _ in out]:
                out.append((hit, book.get(hit, "")))
        if len(out) > MAX_TARGETS:
            problems.append(f"一次最多联系 {MAX_TARGETS} 个人，后面的没联系")
            out = out[:MAX_TARGETS]
        return out, problems

    def check_out(self, text: str, ctx: Ctx, where: str) -> str | None:
        """发给别人的话 / 评论：先过程序的检查（链接、联系方式、屏蔽词），再让审核模型看一遍。有问题返回原因。"""
        p = safety.output_problem(text)
        if p:
            return p
        hit = safety.keyword_hit(text, ctx.rules)
        if hit:
            return f"涉及“{hit}”"
        try:
            ok, why, cost = self.brain.safety_check(f"主人让她（{where}）：{ctx.request[:200]}", text, kind="command")
        except LLMError as e:
            return f"安全检查失败：{str(e)[:60]}"
        ctx.cost += cost
        return None if ok else f"安全检查没通过：{why}"

    def preview(self, inbox, ctx: Ctx, did: int) -> bool:
        """把草稿发给主人看：先发那张图（有的话），再发标题和正文的开头。"""
        from .runner import media_path
        d = self.db.draft(did) or {}
        img = next((m for m in d.get("media") or [] if m.get("kind") == "image"), None)
        if img is not None:
            try:
                p = media_path(did, img)
                if p.exists():
                    inbox.send_dm_file(ctx.name, p)
            except Exception:  # noqa: BLE001
                log.warning("草稿图片没发给主人", exc_info=True)
        body = " ".join(str(d.get("text") or "").split())
        tags = " ".join("#" + t for t in (d.get("tags") or [])[:5])
        parts = [f"草稿写好啦：《{d.get('title') or '（没有标题）'}》",
                 body[:150] + ("…" if len(body) > 150 else "")] + ([tags] if tags else [])
        sent, n = self.a._send_dm_parts(inbox, {"user": ctx.name}, [p for p in parts if p.strip()])
        for p in parts[:n]:
            self.db.add_dm(ctx.name, "me", p)
            ctx.said.append(p)
        return bool(n)

    def tell(self, inbox, ctx: Ctx, text: str) -> None:
        """先回主人一句短话（收到～这就去）。"""
        t = safety.clean_reply(text)
        if not t or safety.output_problem(t):
            return
        parts = split_bubbles(t, max_parts=2) or [t]
        sent, n = self.a._send_dm_parts(inbox, {"user": ctx.name}, parts)
        for p in parts[:n]:
            self.db.add_dm(ctx.name, "me", p)
            ctx.said.append(p)

    def summary(self, ctx: Ctx, tail: str = "") -> str:
        """模型没好好收尾时，程序自己按做过的事写一段回报（照实说）。"""
        lines = []
        for s in ctx.steps:
            if s.tool in ("search_videos", "watch_video"):
                continue
            lines.append(("做好了：" if s.ok else "没做成：") + s.result.split("\n")[0][:60])
        if not lines:
            lines = ["这件事我没做成"] if ctx.steps else ["好的"]
        return "\n".join(lines[:5] + ([tail] if tail else []))

    def report(self, inbox, it: dict, ctx: Ctx, final: str, asked: bool = False, refused: bool = False) -> None:
        """把结果告诉主人，记进聊天记录、动态和记忆。"""
        text = final.strip() or self.summary(ctx)
        if not refused:
            d = self.brain._finalize(ctx.request, text, 0.0, 0, kind="owner")
            ctx.cost += d.cost
            text = d.text if d.ok and d.text else self.summary(ctx)
        parts = split_bubbles(text, max_parts=6) or [text]
        bus.emit("reply_dm", ctx.name)
        sent, n = self.a._send_dm_parts(inbox, {"user": ctx.name}, parts)
        for p in parts[:n]:
            self.db.add_dm(ctx.name, "me", p)
        reply = "\n".join(ctx.said + parts[:n])
        done = "；".join(f"{TOOL_CN.get(s.tool, s.tool)}{'✓' if s.ok else '✗'}" for s in ctx.steps if s.tool in TOOLS) \
            or ("问了主人" if asked else "没有动手")
        status = "replied" if n else "failed"
        self.db.update_message(ctx.mid, status=status, reply=reply, cost=ctx.cost, handled_at=now_str(),
                               reason=("主人的命令：" + done)[:200] if n else "主人的命令做完了，但回报没发出去")
        self.db.add_event("owner_cmd", f"{self.who(ctx)}让她做事" + ("（问了主人一句）" if asked else ""),
                          f"“{ctx.request[:40]}” → {done}", ref=f"msg:{ctx.mid}")
        if n:
            self.a._owner_mark(ctx.name)
            self.db.owner_touch(ctx.name, closer=0.5)
            try:
                self.a.memory.remember_dm(ctx.name, ctx.request, reply, None, owner=True)
            except Exception:  # noqa: BLE001
                log.warning("记主人的命令出错", exc_info=True)

    # ---------- 次数 ----------
    def _counts(self) -> dict:
        c = self.db.get_json("cmd_counts", {}) or {}
        return c if c.get("day") == today_str() else {"day": today_str()}

    def _bump(self, key: str, n: int = 1) -> None:
        c = self._counts()
        c[key] = int(c.get(key) or 0) + n
        self.db.set_json("cmd_counts", c)

    def _left(self, rules: dict) -> dict:
        c = self._counts()
        lim = {"dm": rules.get("owner_cmd_dm_per_day", 20), "comment": rules.get("owner_cmd_comment_per_day", 10),
               "share": rules.get("owner_cmd_share_per_day", 20)}
        out = {}
        for k, v in lim.items():
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = 0
            out[k] = max(0, v - int(c.get(k) or 0))
        return out

    # ---------- 等主人确认的草稿 ----------
    def pending_post(self, owner_name: str) -> dict | None:
        all_ = self.db.get_json("cmd_pending_post", {}) or {}
        p = all_.get(owner_name)
        if not p:
            return None
        d = self.db.draft(int(p.get("did") or 0))
        if not d or d.get("status") not in ("wish", "failed") or time.time() - float(p.get("at") or 0) > KEEP_DAYS * 86400:
            self.clear_pending(owner_name)
            return None
        return p

    def set_pending(self, owner_name: str, did: int, mid: int) -> None:
        all_ = self.db.get_json("cmd_pending_post", {}) or {}
        all_[owner_name] = {"did": int(did), "mid": int(mid), "at": time.time()}
        self.db.set_json("cmd_pending_post", all_)

    def clear_pending(self, owner_name: str) -> None:
        all_ = self.db.get_json("cmd_pending_post", {}) or {}
        if all_.pop(owner_name, None) is not None:
            self.db.set_json("cmd_pending_post", all_)

    # ---------- 替主人问的话：对方回了告诉主人 ----------
    def add_relay(self, user: str, owner_name: str, said: str) -> None:
        now = time.time()
        rel = [r for r in (self.db.get_json("cmd_relays", []) or [])
               if r.get("user") != user and now - float(r.get("at") or 0) < KEEP_DAYS * 86400]
        rel.append({"user": user, "owner": owner_name, "said": said[:100], "at": now})
        self.db.set_json("cmd_relays", rel[-30:])

    def take_relay(self, user: str) -> dict | None:
        now = time.time()
        rel = self.db.get_json("cmd_relays", []) or []
        hit = next((r for r in rel if r.get("user") == user and now - float(r.get("at") or 0) < KEEP_DAYS * 86400), None)
        if hit is not None:
            self.db.set_json("cmd_relays", [r for r in rel if r is not hit])
        return hit

    def after_reply(self, inbox, user: str, their_text: str) -> None:
        """别人回复了她替主人问的话：把 TA 的回答告诉主人（只转这一次）。"""
        r = self.take_relay(user)
        if not r:
            return
        owner = self.db.owner(r["owner"])
        if not owner:
            return
        said = " ".join(re.sub(r"\[[^\]]*\]", " ", their_text or "").split())[:150] or "（发了一条网页版看不到的内容）"
        parts = [f"{user} 回你啦", f"我替你问的是：{r['said'][:40]}", f"TA 说：{said}"]
        sent, n = self.a._send_dm_parts(inbox, {"user": owner["name"]}, parts)
        for p in parts[:n]:
            self.db.add_dm(owner["name"], "me", p)
        if n:
            self.db.add_event("owner_cmd", f"把 {user} 的回复告诉了主人 {owner.get('call') or owner['name']}",
                              f"“{said[:50]}”")


# 工具说明（名字、参数、做什么）。ask / finish / chat 写在提示里
TOOL_DOCS = [
    ("search_videos", '{"keyword": "搜索词", "sort": "综合 / 最多点赞 / 最新（可不写）"}',
     "在抖音上搜视频，返回前几个结果（编号、文案、作者、点赞数、时长）"),
    ("watch_video", '{"video": "视频"}', "打开一个视频看看文案、作者和热评（挑视频、或者主人问视频讲了什么时用）"),
    ("share_video", '{"video": "视频", "to": ["名字"], "text": "分享后跟的一句话（可以不写）"}',
     "用抖音的“分享给朋友”把视频发给某人（对方私信里收到视频卡片，点开就能看）；发给主人自己也用这个"),
    ("send_dm", '{"to": ["名字"], "text": "要发的话", "relay": true 或 false}',
     "给别人（不是主人）发私信；relay=true 表示主人让你问别人事情、对方回了要告诉主人"),
    ("comment", '{"video": "视频", "text": "评论内容", "at": ["要 @ 的互关好友"]}', "在某个视频下面发一条评论，可以 @ 互关好友"),
    ("like", '{"video": "视频"}', "给视频点赞"),
    ("collect", '{"video": "视频"}', "收藏视频"),
    ("write_post", '{"topic": "主人想发什么", "form": "voice（配音图片视频）或 cards（图文卡片），可不写"}',
     "按主人说的写一个作品草稿，写好会自动把草稿发给主人看"),
    ("revise_post", '{"change": "主人要改的地方"}', "按主人的意见改正在等确认的草稿，改完再发给主人看"),
    ("publish_post", "{}", "主人看过草稿、明确说了“发”以后，把草稿发布出去（会勾“内容由AI生成”）"),
    ("drop_post", "{}", "主人说这个草稿不发了"),
]
