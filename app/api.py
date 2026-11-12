"""界面（HTML/JS）调用的接口。每个方法都返回 {"ok": bool, "data"/"error": ...}。"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from functools import wraps

from . import APP_NAME, APP_NAME_EN, APP_VERSION, crypto
from .agent.brain import Brain
from .agent.budget import Budget
from .agent.persona import douyin_bio
from .agent.runner import Agent
from .db import Database
from .defaults import PROVIDER_PRESETS
from .llm.client import LLMError, chat, model_can_see, vision_models
from .paths import AVATAR_DIR, DATA_DIR, DEBUG_DIR, DRAFTS_DIR, PET_DIR

log = logging.getLogger(__name__)
_PRESET_BY_ID = {p["id"]: p for p in PROVIDER_PRESETS}


def _api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return {"ok": True, "data": fn(*args, **kwargs)}
        except LLMError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 —— 错误统一交给界面显示
            log.exception("接口 %s 出错", fn.__name__)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return wrapper


class Api:
    def __init__(self, db: Database, agent: Agent):
        self._db = db
        self._agent = agent
        self._brain = Brain(db)
        self._budget = Budget(db)
        self._pet = None      # 桌宠（main.py 里创建后设置）
        self._window = None   # 主窗口（选择文件用）
        self._ui_req = {"page": "", "seq": 0}
        self._jobs: dict[str, dict] = {}

    # ---------- 桌宠 / 其他线程要求界面切到某一页 ----------
    def request_page(self, page: str) -> None:
        self._ui_req = {"page": page, "seq": self._ui_req["seq"] + 1}
        from . import winfocus
        try:
            if self._window:
                self._window.restore()
                self._window.show()
        except Exception:  # noqa: BLE001
            pass
        winfocus.call_attention(APP_NAME)

    # ---------- 总览 ----------
    @_api
    def get_state(self, ev_page: int = 1):
        persona = self._db.persona()
        return {
            "app": {"name": APP_NAME, "name_en": APP_NAME_EN, "version": APP_VERSION},
            "persona": {**persona, "avatar_url": self._avatar_url(persona)},
            "ui": self._db.ui(),
            "agent": self._agent.status(),
            "usage": self._budget.summary(),
            # 仪表盘“她刚刚在做什么”：一页 10 条，只展示最新的 10 页（更早的去“历史记录”看）
            "events": self._db.events_page(ev_page, 10, max_pages=10),
            "todo": self._db.messages("pending", 5),
            "counts": self._db.message_counts(),
            "rules_brief": {k: self._db.rules().get(k) for k in ("active_start", "active_end", "dm_reply_mode")},
            "models_ready": self._models_ready(),
            "mood": self._mood_state(),
            "wishes": [{"id": d["id"], "title": d["title"], "reason": d["reason"], "seen": d["seen"]}
                       for d in self._db.drafts(include_done=False, limit=20) if d["status"] == "wish"],
            "pet": self._pet.status() if self._pet else {"running": False, "error": "", "visible": False},
            "ui_request": self._ui_req,
        }

    EVENT_GROUPS = {
        "interact": ["like", "collect", "comment", "at_friend", "comment_like", "comment_reply"],
        "reply": ["reply_at", "reply_dm", "thread_reply", "owner_chat", "owner_share"],
        "watch": ["skip"],
        "publish": ["publish_wish", "published"],
        "system": ["pause", "info", "error"],
    }

    @_api
    def get_history(self, day: str = "", group: str = "", page: int = 1):
        """历史记录：按日期（空 = 全部日期）、按类型筛选，分页看她做过的所有操作。"""
        import re as _re

        day = day if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or "") else ""
        kinds = self.EVENT_GROUPS.get(group or "")
        data = self._db.events_page(page, 20, day=day, kinds=kinds)
        data["days"] = self._db.event_days()
        data["day"], data["group"] = day, group if kinds else ""
        return data

    def _mood_state(self) -> dict:
        m = self._brain.mood.current()
        line = getattr(self, "_mood_line", None)
        if not line or line[0] != m["key"] or time.time() - line[2] > 90:
            self._mood_line = (m["key"], self._brain.mood.inner_line(m["key"]), time.time())
        return {**m, "line": self._mood_line[1]}

    def _models_ready(self) -> bool:
        w = self._db.tasks().get("write") or {}
        p = self._db.provider(w.get("provider") or "")
        return bool(p and p.get("api_key_enc") and w.get("model"))

    # ---------- 运行控制 ----------
    @_api
    def start_agent(self):
        if not self._models_ready():
            raise LLMError("请先在“模型与 API Key”里填好 API Key，并给“写评论和回复”选一个模型")
        self._agent.send("start")
        return True

    @_api
    def get_detail(self, ref: str):
        d = self._db.detail(ref)
        if d is None:
            raise ValueError("这条动态没有详情（可能是旧版本记录的）")
        return d

    @_api
    def list_activity(self, kind: str):
        return self._db.activity(kind)

    @_api
    def open_video(self, url: str):
        """在你自己的默认浏览器里打开视频（不打扰她正在刷的那个浏览器）。"""
        import webbrowser

        url = str(url or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith(("https://www.douyin.com/", "https://douyin.com/")):
            raise ValueError("只能打开抖音的视频链接")
        webbrowser.open(url)
        return True

    @_api
    def pause_agent(self):
        self._agent.request_pause()
        return True

    @_api
    def douyin_login(self):
        self._agent.send("login")
        return True

    @_api
    def browser_window(self, show: bool):
        """显示 / 藏起她的浏览器窗口（平时藏在屏幕外后台运行）。"""
        self._agent.send("show_browser" if show else "hide_browser")
        return True

    @_api
    def douyin_logout(self):
        self._agent.request_pause()
        self._agent.send("logout")
        return True

    @_api
    def check_inbox_now(self):
        self._agent.send("check_inbox")
        return True

    @_api
    def dump_page(self):
        self._agent.send("dump")
        return str(DEBUG_DIR)

    @_api
    def dismiss_alert(self):
        self._agent.alert = ""
        return True

    # ---------- 消息中心 ----------
    @_api
    def get_messages(self, flt: str = "all"):
        self._purge_comment_messages()
        items = self._db.messages(flt, limit=1000)
        owners = {o["name"]: o for o in self._db.owners()}
        for m in items:
            m["owner"] = (owners.get(m["user_name"]) or {}).get("call", "") if m["kind"] == "dm" and m["user_name"] in owners else None
        # 同一个人的私信和评论区消息合在一个框里：评论区那几条也带上 TA 的头像、昵称（私信里读到过的话）
        users = self._db.dm_users_map(list({m["user_name"] for m in items if m["user_name"]}))
        for m in items:
            u = users.get(m["user_name"])
            m["user"] = {k: u[k] for k in ("nickname", "douyin_id", "avatar", "url", "follows_me")} if u else None
        return {"items": items, "counts": self._db.message_counts(), "comment_keep_days": self._db.COMMENT_KEEP_DAYS}

    def _purge_comment_messages(self) -> None:
        """评论区聊完的记录过期就删（私信一直留着）。一小时最多整理一次。"""
        now = time.time()
        if now - getattr(self, "_purged_at", 0.0) < 3600:
            return
        self._purged_at = now
        try:
            n = self._db.purge_comment_messages()
            if n:
                log.info("消息中心：删掉了 %d 条评论区的旧记录（和对方 %d 天没有新一轮对话了）", n, self._db.COMMENT_KEEP_DAYS)
        except Exception:  # noqa: BLE001
            log.warning("整理评论区的旧记录出错", exc_info=True)

    @_api
    def message_action(self, mid: int, action: str, text: str = ""):
        rows = self._db.query("SELECT * FROM messages WHERE id=?", (int(mid),))
        if not rows:
            raise ValueError("找不到这条消息")
        m = rows[0]
        if action == "block":
            self._db.block_user(m["user_name"])
            self._db.update_message(m["id"], status="blocked", reason="你设置了以后不回复此人")
        elif action == "manual":
            text = (text or "").strip()
            if not text:
                raise ValueError("回复内容不能为空")
            self._agent.send("manual_reply", mid=m["id"], text=text)
        elif action == "open":
            if m.get("video_url"):
                self._agent.send("open_url", url=m["video_url"])
        elif action == "done":
            self._db.update_message(m["id"], status="manual", reason="你已处理")
        else:
            raise ValueError(f"未知操作 {action}")
        return True

    # ---------- 人设 ----------
    @_api
    def get_persona(self):
        from .agent.persona import ARCHETYPES, SELF_NAMES, TRAIT_DEFAULTS
        p = self._db.persona()
        p["traits"] = {**TRAIT_DEFAULTS, **(p.get("traits") or {})}
        return {**p, "avatar_url": self._avatar_url(p), "douyin_bio_preview": douyin_bio(p),
                "archetypes": [{"id": k, "name": v[0], "desc": v[1]} for k, v in ARCHETYPES.items()],
                "self_names": SELF_NAMES}

    @_api
    def save_persona(self, persona: dict):
        cur = self._db.persona()
        allowed = {"name", "bio", "signature", "traits", "interests", "catchphrases", "avoid", "extra",
                   "archetype", "self_name", "emote", "mbti", "catch_freq"}
        for k, v in (persona or {}).items():
            if k in allowed:
                cur[k] = v
        cur["name"] = (cur.get("name") or "雨雨").strip()[:20]
        cur["signature"] = (cur.get("signature") or "").strip()[:80]
        from .agent.persona import ARCHETYPES, CATCH_FREQ, EMOTE_STYLES
        if cur.get("archetype") not in ARCHETYPES:
            cur["archetype"] = ""
        if cur.get("catch_freq") not in CATCH_FREQ:
            cur["catch_freq"] = "sometimes"
        if cur.get("emote") not in EMOTE_STYLES:
            cur["emote"] = "auto"
        cur["self_name"] = (str(cur.get("self_name") or "我").strip() or "我")[:6]
        cur["mbti"] = str(cur.get("mbti") or "").strip().upper()[:4]
        cur["extra"] = str(cur.get("extra") or "")[:1500]
        for k in ("vibe", "custom"):  # 旧版本的“给人的感觉”：不再使用
            cur.pop(k, None)
        self._db.set_json("persona", cur)
        if self._pet:
            self._pet.send("name")
        return True

    @_api
    def upload_avatar(self, data_url: str):
        from PIL import Image

        if "," not in data_url:
            raise ValueError("图片数据不正确")
        raw = base64.b64decode(data_url.split(",", 1)[1])
        if len(raw) > 15 * 1024 * 1024:
            raise ValueError("图片太大了，请选 15MB 以内的照片")
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize((320, 320))
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        name = f"avatar_{int(time.time())}.png"
        img.save(AVATAR_DIR / name, format="PNG")
        p = self._db.persona()
        old = p.get("avatar")
        p["avatar"] = name
        self._db.set_json("persona", p)
        if old and old != name:
            try:
                (AVATAR_DIR / old).unlink(missing_ok=True)
            except OSError:
                pass
        return self._avatar_url(p)

    @_api
    def reset_avatar(self):
        p = self._db.persona()
        p["avatar"] = ""
        self._db.set_json("persona", p)
        return ""

    def _avatar_url(self, persona: dict) -> str:
        name = persona.get("avatar")
        if not name:
            return ""
        path = AVATAR_DIR / name
        if not path.exists():
            return ""
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()

    @_api
    def sync_signature(self):
        self._agent.send("sync_bio")
        return douyin_bio(self._db.persona())

    @_api
    def try_chat(self, text: str):
        text = (text or "").strip()
        if not text:
            raise ValueError("先输入一句话")
        d = self._brain.try_chat(text)
        return {"text": d.text, "ok": d.ok, "reason": d.reason, "cost": d.cost}

    # ---------- 模型 ----------
    @_api
    def get_providers(self):
        out = []
        for p in self._db.providers():
            preset = _PRESET_BY_ID.get(p["id"], {})
            key = ""
            try:
                key = crypto.decrypt(p["api_key_enc"])
            except Exception:  # noqa: BLE001
                key = ""
            req_extra = (p["extra"] or {}).get("request_extra") or {}
            out.append({
                "id": p["id"], "kind": p["kind"], "name": p["name"], "base_url": p["base_url"],
                "proxy": p["proxy"], "models": p["models"], "vision_models": vision_models(p),
                "price_in": p["price_in"], "price_out": p["price_out"],
                "has_key": bool(key), "key_mask": crypto.mask(key), "key_url": preset.get("key_url", ""),
                "request_extra": json.dumps(req_extra, ensure_ascii=False) if req_extra else "",
            })
        return {"providers": out, "tasks": self._db.tasks()}

    @_api
    def save_provider(self, data: dict):
        p = self._db.provider(data["id"])
        if not p:
            raise ValueError("找不到这个服务商")
        models = data.get("models", p["models"])
        if isinstance(models, str):
            models = [m.strip() for m in models.replace("，", ",").split(",") if m.strip()]
        vis = data.get("vision_models")
        vis = [m for m in vis if m in models] if isinstance(vis, list) else vision_models({**p, "models": models})
        extra = dict(p.get("extra") or {})
        extra["vision_models"] = vis
        raw_extra = (data.get("request_extra") or "").strip()
        if raw_extra:
            try:
                parsed = json.loads(raw_extra)
            except json.JSONDecodeError as e:
                raise ValueError(f"“额外请求参数”不是正确的 JSON：{e.msg}") from e
            if not isinstance(parsed, dict):
                raise ValueError("“额外请求参数”必须是 { } 包起来的 JSON 对象")
            extra["request_extra"] = parsed
        elif "request_extra" in data:
            extra["request_extra"] = {}
        p.update({
            "base_url": (data.get("base_url") or "").strip(),
            "proxy": (data.get("proxy") or "").strip(),
            "models": models,
            "vision": bool(vis),
            "price_in": float(data.get("price_in") or 0),
            "price_out": float(data.get("price_out") or 0),
            "extra": extra,
        })
        self._db.save_provider(p)
        new_key = (data.get("api_key") or "").strip()
        if new_key:
            self._db.set_provider_key(p["id"], crypto.encrypt(new_key))
        if data.get("clear_key"):
            self._db.set_provider_key(p["id"], "")
        return True

    @_api
    def save_tasks(self, tasks: dict):
        cur = self._db.tasks()
        for k in ("vision", "write", "safety", "fallback"):
            if k in (tasks or {}):
                cur[k] = {"provider": tasks[k].get("provider", ""), "model": tasks[k].get("model", "")}
        self._db.set_json("tasks", cur)
        return True

    @_api
    def test_provider(self, pid: str, model: str = ""):
        """逐个测试这家填写的模型（最多 5 个）：能不能对话；勾了识图的再测一次看图。"""
        p = self._db.provider(pid)
        if not p:
            raise ValueError("找不到这个服务商")
        models = [model] if model else list(p["models"])[:5]
        if not models:
            raise ValueError("先在“模型名称”里填至少一个模型")
        results = []
        for m in models:
            item = {"model": m, "ok": False, "seconds": 0, "reply": "", "error": "", "vision_ok": None}
            t0 = time.time()
            try:
                r = chat(p, m, "你是一个测试助手。", "请只回复两个字：你好", max_tokens=20, temperature=0)
                item.update(ok=True, reply=r.text[:40])
            except LLMError as e:
                item["error"] = str(e)[:160]
            item["seconds"] = round(time.time() - t0, 1)
            if item["ok"] and model_can_see(p, m):
                try:
                    from PIL import Image
                    buf = io.BytesIO()
                    Image.new("RGB", (64, 64), (240, 98, 146)).save(buf, format="PNG")
                    chat(p, m, "你是一个测试助手。", "这张图片主要是什么颜色？用一个词回答。",
                         images=[buf.getvalue()], max_tokens=20, temperature=0)
                    item["vision_ok"] = True
                except LLMError as e:
                    item["vision_ok"] = False
                    item["error"] = "看图失败：" + str(e)[:140]
            results.append(item)
        return {"results": results}

    # ---------- 规则 ----------
    @_api
    def get_rules(self):
        rules = self._db.rules()
        # AI 生图可以选的服务商（OpenAI 兼容接口的）
        rules["image_options"] = [{"id": p["id"], "name": p["name"], "has_key": bool(p.get("api_key_enc"))}
                                  for p in self._db.providers() if p["kind"] == "openai"]
        from .publish import stickers
        from .publish import voice as tts
        mutual = self._db.mutual_names()
        rules["owners"] = [dict(o, mutual=o["name"] in mutual) for o in self._db.owners()]
        rules["owner_choices"] = mutual           # 只有互关好友可以设为主人
        rules["voice_has_key"] = tts.has_key(self._db)
        rules["voice_ready"] = tts.available(self._db, rules)
        for k in ("voice_name", "voice_model"):      # 以前 CosyVoice 的设置不显示
            rules[k] = tts.cfg(rules)[k]
        rules["sticker_moods"] = stickers.mood_names()
        rules["listen_status"] = self._agent.listener.status() if hasattr(self._agent, "listener") else {}
        return rules

    @_api
    def save_rules(self, rules: dict):
        cur = self._db.rules()
        for k, v in (rules or {}).items():
            if k in cur:
                cur[k] = v
        cur["auto_replies"] = [{"keys": str(a.get("keys") or "").strip()[:200], "reply": str(a.get("reply") or "").strip()[:300]}
                               for a in (cur.get("auto_replies") or []) if isinstance(a, dict)
                               and str(a.get("keys") or "").strip() and str(a.get("reply") or "").strip()][:50]
        self._db.set_json("rules", cur)
        refused = []
        if isinstance((rules or {}).get("owners"), list):
            refused = self._db.save_owners(rules["owners"])
        return {"ok": True, "owners_refused": refused}

    # ---------- 她的声音 / 表情包 ----------
    def _voice_rules(self, override: dict | None) -> dict:
        """试听、设计音色时用界面上还没保存的设置。"""
        rules = self._db.rules()
        for k, v in (override or {}).items():
            if k.startswith("voice_") and k in rules:
                rules[k] = v
        return rules

    @_api
    def voice_test(self, text: str = "", override: dict | None = None):
        """用现在选的音色读一句话，返回可以直接播放的音频。"""
        from .publish import voice as tts
        text = tts.speech_text("", (text or "").strip() or "你好呀，我是" + (self._db.persona().get("name") or "雨雨")
                               + "，这是我的声音，好听吗？")
        audio, ext, cost = tts.synthesize(self._db, self._voice_rules(override), text[:120])
        if cost:
            self._budget.use("cost", cost)
        return {"audio": f"data:audio/{'mpeg' if ext == 'mp3' else ext};base64," + base64.b64encode(audio).decode()}

    @_api
    def voice_save_key(self, key: str):
        """保存（或清掉）BreezeBlue 的 API Key：加密后存在本机，不会再显示出来。"""
        from .publish import voice as tts
        tts.save_key(self._db, key)
        return {"has_key": tts.has_key(self._db)}

    @_api
    def voice_list(self, kind: str = "personal", override: dict | None = None):
        """读 BreezeBlue 账号里的音色（personal 你自己的 / default 官方中文女声），方便直接选。"""
        from .publish import voice as tts
        return {"voices": tts.list_voices(self._db, self._voice_rules(override), kind)}

    @_api
    def draft_voice(self, did: int):
        """给草稿配音：把文案读出来，和图片合成一个视频。"""
        from .agent.runner import narrate_draft
        from .publish import voice as tts
        d = self._db.draft(did)
        if not d or d["status"] in ("publishing", "published"):
            raise ValueError("这个作品已经在发布或已发布，不能再改了")
        if not tts.available(self._db, self._db.rules()):
            raise ValueError("还没设置她的声音：到“规则与预算 → 她的声音”里填好 BreezeBlue 的 Key 和音色 ID")
        cost = narrate_draft(self._db, int(did))
        if cost:
            self._budget.use("cost", cost)
        return True

    @_api
    def draft_voice_audio(self, did: int):
        """草稿里配音的那段声音（界面上试听）。"""
        from .agent.runner import media_path
        d = self._db.draft(did)
        for m in (d or {}).get("media", []):
            if m.get("source") == "voice" and m.get("audio"):
                f = media_path(did, {"name": m["audio"]})
                if f.exists():
                    return {"audio": "data:audio/mpeg;base64," + base64.b64encode(f.read_bytes()).decode()}
        raise ValueError("这个草稿还没有配音")

    @_api
    def sticker_preview(self, mood: str = "", text: str = ""):
        """看看她的表情包：不给情绪就返回全部情绪的预览图，给了就画那一张（会动的）。"""
        from .pet.host import pet_settings
        from .pet.rig import load as load_rig
        from .publish import stickers
        rig = load_rig(str(pet_settings(self._db).get("rig") or "default"))
        buf = io.BytesIO()
        if not mood:
            stickers.sheet(rig, cols=4, size=200).save(buf, format="JPEG", quality=88)
            return {"image": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}
        frames = stickers.render_gif(rig, mood, (text or "").strip()[:8])
        pal = [f.quantize(colors=128) for f in frames]
        pal[0].save(buf, format="GIF", save_all=True, append_images=pal[1:], duration=90, loop=0, disposal=1)
        return {"image": "data:image/gif;base64," + base64.b64encode(buf.getvalue()).decode()}

    # ---------- 记忆 ----------
    @_api
    def get_memory(self, tab: str = "events", q: str = "", person: str = ""):
        from .agent.memory import FORGET_BELOW, HALF_LIFE_DAYS
        data = self._agent.memory.listing(tab, (q or "").strip(), (person or "").strip())
        data["half_life"] = HALF_LIFE_DAYS
        data["forget_below"] = FORGET_BELOW
        return data

    @_api
    def memory_action(self, action: str, target="", value=0):
        mem = self._agent.memory
        if action == "delete":
            mem.delete(int(target))
        elif action == "importance":
            mem.set_importance(int(target), int(value))
        elif action == "forget_person":
            n = mem.forget_person(str(target))
            self._db.add_event("info", f"忘掉了 {target}", f"印象卡、聊天记录和 {n} 条有关的记忆都删掉了")
        elif action == "refresh_card":
            mem.update_card(str(target), force=True)
        elif action == "delete_diary":
            mem.delete_diary(int(target))
        elif action == "add_note":
            text = str(value or "").strip()
            if not text:
                raise ValueError("内容不能为空")
            mem.remember("note", text, importance=5)
        else:
            raise ValueError(f"未知操作 {action}")
        return True

    # ---------- 互关好友 ----------
    @_api
    def get_friends(self):
        return {"friends": self._db.friends(mutual_only=True), "fetch": self._db.get_json("friends_fetch", {}) or {}}

    @_api
    def fetch_friends(self):
        self._agent.send("fetch_friends")
        return True

    @_api
    def save_friends(self, items: list):
        for it in items or []:
            self._db.set_friend_choice(str(it.get("uid")), bool(it.get("enabled")), str(it.get("tags") or ""))
        return True

    @_api
    def add_friend(self, nickname: str):
        name = (nickname or "").strip().lstrip("@")[:30]
        if not name:
            raise ValueError("先输入好友的抖音昵称")
        uid = "name:" + name
        self._db.save_friend(uid, {"nickname": name, "mutual": True, "manual": True})
        self._db.set_friend_choice(uid, True, "")
        return True

    # ---------- 作品 ----------
    def _media_view(self, did: int, media: list, width: int = 240) -> list:
        from .agent.runner import media_path
        from PIL import Image

        from . import media_server
        media_server.serve(self._media_file)       # 预览用的本机小服务（只启动一次）
        out = []
        for m in media:
            f = media_path(did, m)
            item = {**m, "exists": f.exists(), "thumb": "", "file": f.name,
                    # 预览：图片点开看大图、视频和配音直接播放
                    "url": media_server.url(did, m["name"]) if f.exists() else "", "audio_url": ""}
            if m.get("audio") and media_path(did, {"name": m["audio"]}).exists():
                item["audio_url"] = media_server.url(did, m["audio"])
            if f.exists() and m.get("kind") == "image":
                try:
                    im = Image.open(f).convert("RGB")
                    im.thumbnail((width, width * 4 // 3))
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=80)
                    item["thumb"] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
                except Exception:  # noqa: BLE001
                    pass
            out.append(item)
        return out

    def _media_file(self, did: int, name: str):
        """预览服务来问“这个草稿的这个文件在哪”：只认草稿里登记过的图片 / 视频 / 配音，别的一律不给。"""
        from .agent.runner import media_path
        d = self._db.draft(did)
        for m in (d or {}).get("media", []):
            if m.get("name") == name:
                return media_path(did, m)
            if m.get("audio") == name:
                return media_path(did, {"name": name})
        return None

    @_api
    def open_draft_media(self, did: int, name: str):
        """用电脑上的播放器 / 看图软件打开草稿里的一个文件（软件里播放不了的视频格式用它）。"""
        f = self._media_file(int(did), str(name))
        if f is None or not f.is_file():
            raise ValueError("文件不见了")
        _open_folder(f)
        return True

    @_api
    def delete_draft(self, did: int):
        """删掉一条“不发了”的作品记录，连同它的文字卡片、AI 配图、配音文件。你从电脑上添加的原文件不会动。"""
        import shutil
        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个作品")
        if d["status"] != "discarded":
            raise ValueError("只有点过“不发了”的作品才能删除")
        self._db.delete_draft(did)
        shutil.rmtree(DRAFTS_DIR / str(int(did)), ignore_errors=True)
        return True

    @_api
    def list_drafts(self):
        out = []
        for d in self._db.drafts(include_done=True, limit=60):
            first = self._media_view(d["id"], d["media"][:1], 120)
            out.append({k: d[k] for k in ("id", "status", "title", "reason", "source", "created_at", "published_at",
                                          "error", "mood", "kind")}
                       | {"cover": first[0]["thumb"] if first else "", "count": len(d["media"])})
        return {"drafts": out, "limit": int(self._db.rules()["limits"].get("publishes") or 0),
                "today": self._db.usage().get("publishes", 0)}

    @_api
    def get_draft(self, did: int):
        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        from .publish import imagegen
        d["media_view"] = self._media_view(d["id"], d["media"], 240)
        rules = self._db.rules()
        d["backgrounds"] = self._backgrounds()
        d["bg_default"] = str(rules.get("card_bg_default") or "")
        d["panel_default"] = bool(rules.get("card_panel_default", True))
        d["can_ai_image"] = imagegen.available(self._db, self._db.rules())
        from .publish import voice as tts
        d["can_voice"] = tts.available(self._db, rules)
        d["has_voice"] = any(m.get("source") == "voice" for m in d["media"])
        from .agent.runner import draft_form
        d["form"] = draft_form(d.get("form"))       # voice 配音图片视频 / cards 图文卡片 / upload 你上传的
        d["publish_step"] = ""
        if d["status"] == "publishing" and self._agent.activity == "publish":
            d["publish_step"] = str((self._agent.current or {}).get("step") or "").replace("发作品：", "")
        if d["status"] == "wish" and not d["seen"]:
            self._db.update_draft(did, seen=1)
        return d

    @_api
    def new_draft(self):
        from .agent.runner import draft_form
        did = self._db.add_draft(status="wish", kind="image", title="", text="", tags=[], media=[], cards=[],
                                 reason="你自己新建的作品", source="manual", seen=1,
                                 form=draft_form(self._db.rules().get("post_form") or "voice"))
        return did

    @_api
    def save_draft(self, did: int, data: dict):
        d = self._db.draft(did)
        if not d or d["status"] in ("publishing", "published"):
            raise ValueError("这个作品已经在发布或已发布，不能再改了")
        kw = {}
        if "title" in data:
            kw["title"] = str(data["title"] or "")[:30]
        if "text" in data:
            kw["text"] = str(data["text"] or "")[:1000]
        if "tags" in data:
            tags = data["tags"] if isinstance(data["tags"], list) else str(data["tags"]).replace("，", ",").replace("#", " ").replace(",", " ").split()
            kw["tags"] = [t.strip()[:16] for t in tags if t.strip()][:5]
        if "cards" in data:
            kw["cards"] = _parse_cards(data["cards"]) if isinstance(data["cards"], str) else data["cards"]
        if "order" in data:
            by = {m["name"]: m for m in d["media"]}
            kw["media"] = [by[n] for n in data["order"] if n in by]
        if d["status"] == "failed":
            kw["status"], kw["error"] = "wish", ""
        changed = kw.get("title", d["title"]) != d["title"] or kw.get("text", d["text"]) != d["text"]
        if changed and any(m.get("source") == "voice" for m in d["media"]):
            from .agent.runner import drop_voice           # 文案改了：旧的配音不对了，去掉（可以重新配）
            kw["media"] = drop_voice(did, kw.get("media", d["media"]))
        self._db.update_draft(did, **kw)
        if changed and any(m.get("source") == "voicecard" for m in d["media"]):
            # 配音图片：图上写的是旧文案，马上按新文案重画（配音要你点“重新生成图片并配音”）
            from .agent.runner import drop_source, render_voice_image
            d = self._db.draft(did)
            self._db.update_draft(did, media=render_voice_image(self._db, did, d)
                                  + drop_source(did, d["media"], ("voicecard",)))
        return True

    @_api
    def draft_cards(self, did: int, from_text: bool = True):
        """重新生成文字卡片（替换掉原来的卡片图）。

        from_text=True（“重新生成文字卡片”按钮）：按正文输入框里现在的全部内容重新分卡片——正文里每一句都在卡片上；
        from_text=False（“按上面的内容重画”）：按“编辑文字卡片的内容”里写的来画。"""
        from .agent.runner import drop_source, render_cards_for
        from .publish.cards import cards_from_text
        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        if d["status"] in ("publishing", "published"):
            raise ValueError("这个作品已经在发布或已发布，不能再改了")
        if from_text:
            if not (d.get("text") or "").strip():
                raise ValueError("正文还是空的，先写点内容再生成卡片")
            d["cards"] = cards_from_text(d["text"])
            self._db.update_draft(did, cards=d["cards"])
        new = render_cards_for(self._db, did, d)
        self._db.update_draft(did, media=new + drop_source(did, d["media"], ("card", "voice", "voicecard")))
        return True

    @_api
    def draft_set_form(self, did: int, form: str):
        """换作品的形式：voice 配音图片视频（文案写在一张带桌宠的图上，她读出来）/ cards 图文卡片 /
        upload 只用你上传的图片或视频。换的时候把上一种形式自动生成的图、配音去掉（你上传的不动）。"""
        from .agent.runner import FORMS, drop_source, narrate_draft, render_cards_for, render_voice_image
        from .publish import voice as tts
        from .publish.cards import cards_from_text
        if form not in FORMS:
            raise ValueError("没有这种作品形式")
        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        if d["status"] in ("publishing", "published"):
            raise ValueError("这个作品已经在发布或已发布，不能再改了")
        media = drop_source(did, d["media"], ("card", "voice", "voicecard"))
        self._db.update_draft(did, form=form, media=media)
        d = self._db.draft(did)
        has_text = bool((d.get("title") or "").strip() or (d.get("text") or "").strip())
        voiced = False
        if form == "voice" and has_text:
            if tts.available(self._db, self._db.rules()) and len(tts.speech_text(d["title"], d["text"])) >= 4:
                cost = narrate_draft(self._db, int(did))        # 画那一张图 + 配音 + 合成视频
                if cost:
                    self._budget.use("cost", cost)
                voiced = True
            else:
                self._db.update_draft(did, media=render_voice_image(self._db, did, d) + media)
        elif form == "cards" and has_text:
            if not d.get("cards") and (d.get("text") or "").strip():
                d["cards"] = cards_from_text(d["text"])
                self._db.update_draft(did, cards=d["cards"])
            self._db.update_draft(did, media=render_cards_for(self._db, did, d) + media)
        return {"form": form, "voiced": voiced}

    # ---------- 文字卡片的背景图库 ----------
    def _backgrounds(self) -> list[dict]:
        from PIL import Image

        from .agent.runner import backgrounds_dir

        from .publish import backgrounds as builtin
        folder = backgrounds_dir()
        out = []
        try:      # 内置的可爱背景：第一次用到时画好放进图库（你删掉过的不再放）
            builtin.ensure(folder, self._db.get_json("bg_builtin_removed", []))
        except Exception:  # noqa: BLE001
            log.warning("内置背景没画出来", exc_info=True)
        if not folder.exists():
            return out
        files = sorted(folder.glob("bg_*.jpg"), key=lambda p: (builtin.is_builtin(p.name), -p.stat().st_mtime if not
                                                              builtin.is_builtin(p.name) else 0, p.name))
        for f in files[:70]:      # 你上传的排前面（新的在前），内置的排后面
            try:
                im = Image.open(f).convert("RGB")
                im.thumbnail((150, 200))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=78)
                out.append({"name": f.name, "label": builtin.LABELS.get(f.name, "你上传的"),
                            "thumb": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()})
            except Exception:  # noqa: BLE001
                continue
        return out

    @_api
    def card_bg_upload(self, did: int = 0):
        """从电脑选背景图放进背景图库（自动转成 JPG、太大的缩小）。给了草稿就直接换上第一张。"""
        import webview
        from PIL import Image, ImageOps

        from .agent.runner import backgrounds_dir

        if not self._window:
            raise ValueError("窗口还没准备好")
        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("图片 (*.jpg;*.jpeg;*.png;*.webp;*.bmp)",)) or []
        folder = backgrounds_dir()
        folder.mkdir(parents=True, exist_ok=True)
        names = []
        for i, p in enumerate(paths):
            try:
                im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"打不开这张图片：{os.path.basename(p)}（{e}）") from e
            if im.width < 300 or im.height < 300:
                raise ValueError(f"图片太小了：{os.path.basename(p)}（{im.width}×{im.height}），至少要 300×300，建议 1080×1440")
            im.thumbnail((2160, 2880))
            name = f"bg_{int(time.time() * 1000) % 10**10}_{i}.jpg"
            im.save(folder / name, quality=92)
            names.append(name)
        if did and names:
            self._apply_bg(did, names[0], None)
        return names

    def _apply_bg(self, did: int, name: str, panel) -> None:
        from .agent.runner import render_cards_for

        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        if d["status"] in ("publishing", "published"):
            raise ValueError("这个作品已经在发布或已发布，不能再改了")
        kw = {"card_bg": name or ""}
        if panel is not None:
            kw["card_panel"] = 1 if panel else 0
        self._db.update_draft(did, **kw)
        d = self._db.draft(did)
        if any(m.get("source") == "voicecard" for m in d["media"]):
            # 配音图片也用这张背景：重画那一张图（旧的配音视频里是旧图，去掉，要重新配）
            from .agent.runner import drop_source, render_voice_image
            self._db.update_draft(did, media=render_voice_image(self._db, did, d)
                                  + drop_source(did, d["media"], ("voicecard", "voice")))
        elif d.get("cards") or d.get("title") or any(m.get("source") == "card" for m in d["media"]):
            new = render_cards_for(self._db, did, d)
            others = [m for m in d["media"] if m.get("source") != "card"]
            self._db.update_draft(did, media=new + others)

    @_api
    def draft_set_bg(self, did: int, name: str, panel=None):
        """给这个草稿换文字卡片的背景（none = 内置粉色样式；空 = 跟默认设置走），并马上重画卡片。"""
        name = str(name or "")
        if name not in ("", "none"):
            from .agent.runner import backgrounds_dir
            name = os.path.basename(name)
            if not (backgrounds_dir() / name).exists():
                raise ValueError("这张背景图不见了，请重新上传")
        self._apply_bg(did, name, panel)
        return True

    @_api
    def card_bg_set_default(self, name: str, panel: bool = True):
        """以后她自己写的作品默认用这张背景（空 = 内置粉色样式）。"""
        rules = self._db.rules()
        rules["card_bg_default"] = "" if name in ("", "none") else os.path.basename(str(name))
        rules["card_panel_default"] = bool(panel)
        self._db.set_json("rules", rules)
        return True

    @_api
    def card_bg_delete(self, name: str):
        """从背景图库删掉一张（已经画好的卡片不受影响）。"""
        from .agent.runner import backgrounds_dir

        f = backgrounds_dir() / os.path.basename(str(name or ""))
        if f.exists() and f.name.startswith("bg_"):
            f.unlink()
            from .publish import backgrounds as builtin
            if builtin.is_builtin(f.name):      # 内置背景：记下来，以后不再自动放回图库
                gone = self._db.get_json("bg_builtin_removed", [])
                self._db.set_json("bg_builtin_removed", sorted(set(gone) | {f.name}))
        rules = self._db.rules()
        if rules.get("card_bg_default") == f.name:
            rules["card_bg_default"] = ""
            self._db.set_json("rules", rules)
        return True

    @_api
    def draft_ai_image(self, did: int, prompt: str = ""):
        from .publish import imagegen
        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        prompt = (prompt or d.get("image_prompt") or d.get("title") or "").strip()
        if not prompt:
            raise ValueError("先写一句画面描述")
        rules = self._db.rules()
        raw, cost = imagegen.generate(self._db, rules, prompt)
        if cost:
            self._db.add_usage("cost", cost)
        name = f"ai_{int(time.time())}.png"
        (DRAFTS_DIR / str(did)).mkdir(parents=True, exist_ok=True)
        (DRAFTS_DIR / str(did) / name).write_bytes(raw)
        self._db.update_draft(did, media=d["media"] + [{"name": name, "kind": "image", "source": "ai"}],
                              image_prompt=prompt)
        return True

    @_api
    def draft_add_files(self, did: int):
        """从电脑选图片 / 视频加进草稿（图片复制到草稿文件夹；大视频直接用原来的位置）。"""
        import shutil
        import webview

        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        if not self._window:
            raise ValueError("窗口还没准备好")
        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("图片和视频 (*.jpg;*.jpeg;*.png;*.webp;*.mp4;*.mov;*.m4v;*.webm)",)) or []
        media = list(d["media"])
        folder = DRAFTS_DIR / str(did)
        folder.mkdir(parents=True, exist_ok=True)
        for p in paths:
            src = os.path.abspath(p)
            ext = os.path.splitext(src)[1].lower()
            kind = "video" if ext in (".mp4", ".mov", ".m4v", ".webm") else "image"
            name = f"up_{int(time.time() * 1000) % 10**9}{ext}"
            if kind == "video" and os.path.getsize(src) > 300 * 1024 * 1024:
                media.append({"name": os.path.basename(src), "kind": kind, "source": "upload", "path": src})
            else:
                shutil.copyfile(src, folder / name)
                media.append({"name": name, "kind": kind, "source": "upload"})
        self._db.update_draft(did, media=media)
        return len(paths)

    @_api
    def draft_remove_media(self, did: int, name: str):
        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        self._db.update_draft(did, media=[m for m in d["media"] if m["name"] != name])
        return True

    @_api
    def publish_draft(self, did: int):
        d = self._db.draft(did)
        if not d or d["status"] not in ("wish", "failed"):
            raise ValueError("这个作品现在不能发布")
        if not self._agent.logged_in:
            raise ValueError("还没登录抖音，请先扫码登录")
        self._db.update_draft(did, status="publishing", error="")
        self._agent.send("publish", did=int(did))
        return True

    @_api
    def discard_draft(self, did: int):
        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        if d["status"] == "wish" and d["source"] == "auto":
            self._brain.mood.event("publish_no")
        self._db.update_draft(did, status="discarded")
        return True

    @_api
    def draft_now(self):
        if not self._models_ready():
            raise LLMError("请先在“模型与 API Key”里配置好模型")
        self._agent.send("draft_now")
        return True

    # ---------- 桌宠 ----------
    @_api
    def get_pet(self):
        from .pet import matting
        from .pet.host import pet_settings
        from .pet.rig import CUSTOM_DIR, RIG_NAMES
        return {"settings": pet_settings(self._db), "status": self._pet.status() if self._pet else {},
                "rig_names": RIG_NAMES,
                "has_custom": (CUSTOM_DIR / "rig.json").exists(), "model_ready": matting.model_ready(),
                "onnx": matting.onnx_available(), "job": self._jobs.get("pet")}

    @_api
    def save_pet(self, data: dict):
        from .pet.host import pet_settings
        s = pet_settings(self._db)
        import re as _re
        for k in ("enabled", "size", "chatter", "hide", "sync"):
            if k in (data or {}):
                s[k] = data[k]
        for k in ("bubble_bg", "bubble_border", "bubble_text"):
            v = str((data or {}).get(k) or "")
            if _re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
                s[k] = v.upper()
        self._db.set_json("pet", s)
        if self._pet:
            if s.get("enabled") and not self._pet.running:
                self._pet.start()
            self._pet.send("settings")
        return True

    @_api
    def pet_preview(self, which: str = "current"):
        from .pet.host import pet_settings
        from .pet.rig import Rig, load
        from .pet.snapshot import pose_grid
        if which == "new":
            rig = Rig(PET_DIR / "custom_new")
        else:
            rig = load(which if which in ("default", "custom") else (pet_settings(self._db).get("rig") or "default"))
        return _png_url(pose_grid(rig, 0.5, cols=6))

    @_api
    def pet_action(self, act: int):
        if not self._pet or not self._pet.running:
            raise ValueError("桌宠还没打开")
        self._pet.send("act", id=int(act))
        return True

    @_api
    def pet_upload(self, data_url: str):
        """上传形象图：在后台抠图、估关节点。用 pet_job 查进度。"""
        if "," not in data_url:
            raise ValueError("图片数据不正确")
        raw = base64.b64decode(data_url.split(",", 1)[1])
        if len(raw) > 25 * 1024 * 1024:
            raise ValueError("图片太大了，请选 25MB 以内的")
        self._run_job("pet", self._pet_cut, raw)
        return True

    def _pet_cut(self, job: dict, raw: bytes) -> dict:
        from PIL import Image
        from .pet import autorig, matting
        job["message"] = "正在抠图（第一次可能要十几秒）…"
        img = Image.open(io.BytesIO(raw))
        cut, how = matting.cutout(img)
        up = PET_DIR / "upload"
        up.mkdir(parents=True, exist_ok=True)
        cut.save(up / "cutout.png")
        job["message"] = "正在估计关节位置…"
        joints = autorig.guess_joints(cut)
        return {"how": how, "size": list(cut.size), "image": _png_url(cut, max_side=900),
                "joints": joints, "arm_width": autorig.default_arm_width(cut, joints),
                "labels": autorig.JOINT_LABELS}

    @_api
    def pet_job(self, name: str = "pet"):
        return self._jobs.get(name)

    @_api
    def pet_download_model(self):
        from .pet import matting
        if not matting.onnx_available():
            raise ValueError("这个版本没有带抠图组件（onnxruntime），请重新打包或安装新版")

        def run(job: dict) -> dict:
            def prog(done, total):
                job["message"] = (f"正在下载抠图模型 {done / 1048576:.0f} / {total / 1048576:.0f} MB" if total
                                  else f"正在下载抠图模型 {done / 1048576:.0f} MB")
                job["progress"] = round(done / total * 100) if total else 0
            matting.download_model(prog)
            return {"ok": True}
        self._run_job("model", run)
        return True

    @_api
    def pet_guess_ai(self, joints_hint: dict | None = None):
        """用你配置的识图模型估一遍关节位置（可选，会花一点点钱）。"""
        from PIL import Image
        from .agent.brain import parse_json
        cut = Image.open(PET_DIR / "upload" / "cutout.png")
        small = cut.copy()
        small.thumbnail((640, 640))
        bg = Image.new("RGB", small.size, (255, 255, 255))
        bg.paste(small, mask=small.getchannel("A"))
        buf = io.BytesIO()
        bg.save(buf, format="JPEG", quality=85)
        names = ", ".join(f"{k}（{v}）" for k, v in __import__("app.pet.autorig", fromlist=["x"]).JOINT_LABELS.items())
        prompt = ("这是一个卡通形象的正面全身图（宽 1000、高 1000 的归一化坐标，左上角是 0,0）。"
                  "左 / 右指图片的左边 / 右边。请估计这些关节点的位置：" + names +
                  '。只输出 JSON：{"neck": [x, y], "shoulder_l": [x, y], ...}')
        r = self._brain.router.call("vision", "你是精确的图像标注员。", prompt, images=[buf.getvalue()],
                                    max_tokens=400, temperature=0)
        data = parse_json(r.text)
        out = {}
        for k in joints_hint or data:
            v = data.get(k)
            if isinstance(v, list) and len(v) == 2:
                out[k] = [round(float(v[0]) / 1000 * cut.width, 1), round(float(v[1]) / 1000 * cut.height, 1)]
        if not out:
            raise ValueError("模型没给出关节位置，手动拖一下就好")
        return out

    @_api
    def pet_build(self, joints: dict, arm_width: float):
        from PIL import Image
        from .pet import autorig
        from .pet.rig import Rig
        from .pet.snapshot import pose_grid
        cut = Image.open(PET_DIR / "upload" / "cutout.png")
        for k in autorig.JOINTS:
            if k not in joints:
                raise ValueError(f"缺少关节点：{autorig.JOINT_LABELS[k]}")
        autorig.build(cut, joints, float(arm_width), PET_DIR / "custom_new", self._db.persona().get("name") or "")
        return _png_url(pose_grid(Rig(PET_DIR / "custom_new"), 0.5, cols=6))

    @_api
    def pet_apply(self, which: str = "new"):
        import shutil
        from .pet.host import pet_settings
        from .pet.rig import CUSTOM_DIR
        s = pet_settings(self._db)
        if which == "new":
            new = PET_DIR / "custom_new"
            if not (new / "rig.json").exists():
                raise ValueError("先生成预览")
            if CUSTOM_DIR.exists():
                shutil.rmtree(CUSTOM_DIR)
            shutil.move(str(new), str(CUSTOM_DIR))
            s["rig"] = "custom"
        else:
            s["rig"] = "custom" if which == "custom" else "default"
        self._db.set_json("pet", s)
        if self._pet:
            self._pet.send("rig")
        return s["rig"]

    def _run_job(self, name: str, fn, *args) -> None:
        import threading
        job = {"state": "running", "message": "准备中…", "progress": 0, "result": None, "error": ""}
        self._jobs[name] = job

        def work():
            try:
                job["result"] = fn(job, *args)
                job["state"] = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("后台任务 %s 失败", name)
                job["state"], job["error"] = "error", str(e)
        threading.Thread(target=work, daemon=True).start()

    # ---------- 设置 ----------
    @_api
    def set_theme(self, theme: str):
        if theme not in ("pink", "blue"):
            raise ValueError("主题只有 pink 和 blue")
        ui = self._db.ui()
        ui["theme"] = theme
        self._db.set_json("ui", ui)
        return theme

    @_api
    def open_data_dir(self):
        _open_folder(DATA_DIR)
        return str(DATA_DIR)

    @_api
    def about(self):
        return {"name": APP_NAME, "version": APP_VERSION, "data_dir": str(DATA_DIR), "oss": OSS_COMPONENTS}


# 软件运行时用到的开源组件（开发、打包时才用的工具不在这里）。“关于”里照着这张表显示
OSS_COMPONENTS = [
    {"name": "Python", "use": "软件的运行环境", "license": "PSF License", "url": "https://www.python.org/"},
    {"name": "pywebview", "use": "软件窗口和界面", "license": "BSD-3-Clause", "url": "https://github.com/r0x0r/pywebview"},
    {"name": "Microsoft Edge WebView2", "use": "显示界面的内核（Windows 自带）", "license": "Microsoft 软件许可",
     "url": "https://developer.microsoft.com/microsoft-edge/webview2/"},
    {"name": "pythonnet / clr-loader", "use": "pywebview 在 Windows 上调用 WebView2", "license": "MIT",
     "url": "https://github.com/pythonnet/pythonnet"},
    {"name": "bottle / proxy_tools / typing_extensions", "use": "pywebview 的依赖", "license": "MIT / PSF",
     "url": "https://github.com/bottlepy/bottle"},
    {"name": "Playwright for Python", "use": "操作她的浏览器（刷视频、评论、私信）", "license": "Apache-2.0",
     "url": "https://github.com/microsoft/playwright-python"},
    {"name": "greenlet / pyee", "use": "Playwright 的依赖", "license": "MIT", "url": "https://github.com/python-greenlet/greenlet"},
    {"name": "Requests", "use": "调用你配置的模型服务商接口", "license": "Apache-2.0", "url": "https://github.com/psf/requests"},
    {"name": "urllib3 / certifi / idna / charset-normalizer", "use": "Requests 的依赖", "license": "MIT / MPL-2.0 / BSD",
     "url": "https://github.com/urllib3/urllib3"},
    {"name": "Pillow", "use": "头像、文字卡片、桌宠图片处理", "license": "MIT-CMU（HPND）", "url": "https://github.com/python-pillow/Pillow"},
    {"name": "NumPy", "use": "桌宠形象的抠图和拆部件", "license": "BSD-3-Clause", "url": "https://github.com/numpy/numpy"},
    {"name": "ONNX Runtime", "use": "桌宠换形象时在本机抠图", "license": "MIT", "url": "https://github.com/microsoft/onnxruntime"},
    {"name": "anime-segmentation（isnetis 模型）", "use": "抠图模型", "license": "Apache-2.0",
     "url": "https://github.com/SkyTNT/anime-segmentation"},
    {"name": "faster-whisper", "use": "听视频里的声音（语音转文字）", "license": "MIT", "url": "https://github.com/SYSTRAN/faster-whisper"},
    {"name": "OpenAI Whisper（模型）", "use": "语音识别模型", "license": "MIT", "url": "https://github.com/openai/whisper"},
    {"name": "CTranslate2", "use": "在本机运行 Whisper 模型", "license": "MIT", "url": "https://github.com/OpenNMT/CTranslate2"},
    {"name": "PyAV（FFmpeg）", "use": "读取视频里的声音", "license": "BSD-3-Clause（FFmpeg：LGPL）", "url": "https://github.com/PyAV-Org/PyAV"},
    {"name": "tokenizers / huggingface_hub", "use": "Whisper 的分词和模型下载", "license": "Apache-2.0",
     "url": "https://github.com/huggingface/tokenizers"},
    {"name": "imageio-ffmpeg", "use": "听视频声音、给作品配音时调用 ffmpeg", "license": "BSD-2-Clause",
     "url": "https://github.com/imageio/imageio-ffmpeg"},
    {"name": "FFmpeg（独立程序）", "use": "解出视频里的声音；把她读文案的声音和文字卡片合成视频", "license": "LGPL / GPL",
     "url": "https://ffmpeg.org/"},
    {"name": "Breeze TTS 2（BreezeBlue 的在线服务）", "use": "她的声音：语音合成（需要你自己的 Key，声音在对方服务器上生成）",
     "license": "在线服务按 BreezeBlue 的条款；商用需要付费订阅", "url": "https://breezeblue.ai/"},
    {"name": "SQLite", "use": "保存人设、记忆、记录（Python 自带）", "license": "Public Domain", "url": "https://www.sqlite.org/"},
    {"name": "站酷快乐体 ZCOOL KuaiLe", "use": "界面和文字卡片的字体", "license": "SIL Open Font License 1.1",
     "url": "https://github.com/googlefonts/zcool-kuaile"},
]


def _png_url(img, max_side: int = 0) -> str:
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _parse_cards(text: str) -> list[dict]:
    """把编辑框里的文字变回卡片：空一行隔开每张卡片；想要小标题就把它写在第一行、后面每行一句；
    第一行写一个“-”表示这张没有小标题；一张卡片只有一行，就是一句话的卡片（没有小标题）。"""
    from .publish.cards import MAX_CARDS, MAX_LINES
    cards = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        rows = [r.strip() for r in block.splitlines() if r.strip()]
        if not rows:
            continue
        if len(rows) == 1:
            cards.append({"title": "", "lines": [rows[0][:80]]})
        else:
            title = "" if rows[0] in ("-", "－", "—") else rows[0][:20]
            cards.append({"title": title, "lines": [r[:60] for r in rows[1:MAX_LINES + 1]]})
    return cards[:MAX_CARDS]


def _open_folder(path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
