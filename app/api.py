"""界面（HTML/JS）调用的接口。每个方法都返回 {"ok": bool, "data"/"error": ...}。"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
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
        "reply": ["reply_at", "reply_dm", "thread_reply"],
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
        items = self._db.messages(flt)
        users = self._db.dm_users_map([m["user_name"] for m in items if m["kind"] == "dm"])
        for m in items:
            u = users.get(m["user_name"]) if m["kind"] == "dm" else None
            m["user"] = {k: u[k] for k in ("nickname", "douyin_id", "avatar", "url", "follows_me")} if u else None
        return {"items": items, "counts": self._db.message_counts()}

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
        p = self._db.persona()
        return {**p, "avatar_url": self._avatar_url(p), "douyin_bio_preview": douyin_bio(p)}

    @_api
    def save_persona(self, persona: dict):
        cur = self._db.persona()
        allowed = {"name", "bio", "signature", "traits", "interests", "catchphrases", "avoid", "extra"}
        for k, v in (persona or {}).items():
            if k in allowed:
                cur[k] = v
        cur["name"] = (cur.get("name") or "雨雨").strip()[:20]
        cur["signature"] = (cur.get("signature") or "").strip()[:80]
        for k in ("vibe", "archetype", "custom"):  # 旧版本的“给人的感觉”、试用过的人设类型：不再使用
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
        return rules

    @_api
    def save_rules(self, rules: dict):
        cur = self._db.rules()
        for k, v in (rules or {}).items():
            if k in cur:
                cur[k] = v
        self._db.set_json("rules", cur)
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
        self._db.save_friend(uid, {"nickname": name, "mutual": True})
        self._db.set_friend_choice(uid, True, "")
        return True

    # ---------- 作品 ----------
    def _media_view(self, did: int, media: list, width: int = 240) -> list:
        from .agent.runner import media_path
        from PIL import Image

        out = []
        for m in media:
            f = media_path(did, m)
            item = {**m, "exists": f.exists(), "thumb": "", "file": f.name}
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
        d["publish_step"] = ""
        if d["status"] == "publishing" and self._agent.activity == "publish":
            d["publish_step"] = str((self._agent.current or {}).get("step") or "").replace("发作品：", "")
        if d["status"] == "wish" and not d["seen"]:
            self._db.update_draft(did, seen=1)
        return d

    @_api
    def new_draft(self):
        did = self._db.add_draft(status="wish", kind="image", title="", text="", tags=[], media=[], cards=[],
                                 reason="你自己新建的作品", source="manual", seen=1)
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
        self._db.update_draft(did, **kw)
        return True

    @_api
    def draft_cards(self, did: int):
        """按现在的标题、正文和文字卡片重新画卡片图（替换掉原来的卡片图）。"""
        from .agent.runner import render_cards_for
        d = self._db.draft(did)
        if not d:
            raise ValueError("找不到这个草稿")
        new = render_cards_for(self._db, did, d)
        others = [m for m in d["media"] if m.get("source") != "card"]
        self._db.update_draft(did, media=new + others)
        return True

    # ---------- 文字卡片的背景图库 ----------
    def _backgrounds(self) -> list[dict]:
        from PIL import Image

        from .agent.runner import backgrounds_dir

        folder = backgrounds_dir()
        out = []
        if not folder.exists():
            return out
        for f in sorted(folder.glob("bg_*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            try:
                im = Image.open(f).convert("RGB")
                im.thumbnail((150, 200))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=78)
                out.append({"name": f.name, "thumb": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()})
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
        if d.get("cards") or d.get("title") or any(m.get("source") == "card" for m in d["media"]):
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
        return {"name": APP_NAME, "version": APP_VERSION, "data_dir": str(DATA_DIR)}


def _png_url(img, max_side: int = 0) -> str:
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _parse_cards(text: str) -> list:
    """文字卡片的编辑格式：每张卡片一段（空行分隔），第一行是小标题，后面每行一句。"""
    cards = []
    for block in str(text or "").replace("\r", "").split("\n\n"):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if lines:
            cards.append({"title": lines[0][:16], "lines": [ln[:24] for ln in lines[1:5]]})
    return cards[:4]


def _open_folder(path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
