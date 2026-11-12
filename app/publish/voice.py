"""她的声音：用 BreezeBlue（Breeze TTS 2）的在线接口把作品的文案读出来。

接口（https://docs.breezeblue.ai）：
- 合成：POST {地址}/v1/text-to-speech/{音色ID}?output_format=mp3_24000_128，请求头 xi-api-key，
  正文 {text, language_code, model_id?, instructions?, voice_settings{speed, volume, guidance_scale}}，直接返回音频。
  一次最多 1000 字（中文超过 500 字时服务端自己分段再拼起来）。
- 音色列表：GET {地址}/v1/voices（voice_type=personal 是你账号里设计 / 克隆的音色，default 是官方音色）。
音色 ID（voc_ 开头）在 BreezeBlue 的控制台里拿：可以用官方音色，也可以在那边用文字描述设计一个只属于她的音色，
把 ID 填到“规则与预算 → 她的声音”里。Key 用 Windows DPAPI 加密后存在本机，不会显示出来。

合成的声音是 AI 生成的，作品发布时照样声明“内容由 AI 生成”。
音色请用官方音色、设计出来的原创音色或你本人的声音，不要克隆别人的声音。
"""
from __future__ import annotations

import logging
import re
import time

import requests

from .. import cancel, crypto

log = logging.getLogger(__name__)

BASE = "https://api.breeze.blue"
KEY_NAME = "voice_key_enc"        # 存在 kv 表里的加密 Key
MAX_CHARS = 1000                  # 接口一次最多读这么多字
OUTPUT = "mp3_24000_128"
DEFAULTS = {"voice_auto": False, "voice_base": "", "voice_model": "", "voice_name": "", "voice_rate": 1.0,
            "voice_volume": 100, "voice_instruction": "", "voice_guidance": 3.0, "voice_proxy": "", "voice_price": 5.0}


class VoiceError(Exception):
    pass


def cfg(rules: dict) -> dict:
    c = {k: (rules.get(k) if rules.get(k) not in (None, "") else v) for k, v in DEFAULTS.items()}
    c["voice_auto"] = bool(rules.get("voice_auto"))
    # 以前用 CosyVoice 时存下来的模型 / 音色在这里不能用：当成没填
    if re.search(r"cosyvoice", str(c["voice_model"]), re.I):
        c["voice_model"] = ""
    if re.search(r"cosyvoice|^long[a-z]", str(c["voice_name"]), re.I):
        c["voice_name"] = ""
    return c


def has_key(db) -> bool:
    return bool(db.get_json(KEY_NAME, ""))


def save_key(db, key: str) -> None:
    key = (key or "").strip()
    db.set_json(KEY_NAME, crypto.encrypt(key) if key else "")


def available(db, rules: dict) -> bool:
    return has_key(db) and bool(str(cfg(rules)["voice_name"]).strip())


def _auth(db, rules: dict, need_voice: bool = True) -> tuple[dict, str, dict | None, dict]:
    c = cfg(rules)
    key = crypto.decrypt(db.get_json(KEY_NAME, "") or "")
    if not key:
        raise VoiceError("还没有填 BreezeBlue 的 API Key（规则与预算 → 她的声音）")
    if need_voice and not str(c["voice_name"]).strip():
        raise VoiceError("还没有填音色 ID（voc_ 开头，在 BreezeBlue 控制台里复制）")
    base = (str(c["voice_base"]).strip() or BASE).rstrip("/")
    base = re.sub(r"/v1$", "", base)
    if not base.startswith("https://"):
        raise VoiceError("接口地址要以 https:// 开头")
    proxy = str(c["voice_proxy"]).strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return {"xi-api-key": key, "Content-Type": "application/json"}, base, proxies, c


def speech_text(title: str, text: str) -> str:
    """把作品的标题和正文整理成适合朗读的话：去掉话题标签、[抖音表情]、颜文字、网址和读不出来的符号。"""
    t = f"{(title or '').strip()}。\n{(text or '').strip()}" if title else (text or "")
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"#\S+", " ", t)                       # 话题
    t = re.sub(r"\[[^\[\]\s]{1,8}\]", "", t)          # [比心] 这样的抖音表情
    t = re.sub(r"[（(][^（）()一-鿿A-Za-z0-9]{2,16}[）)]", "", t)     # (｡･ω･｡) 这样的颜文字
    t = re.sub(r"[^一-鿿A-Za-z0-9，。！？、；：,.!?;:…—\-~～“”\"'‘’《》%％\s]", "", t)
    t = re.sub(r"[~～]+", "～", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t).strip()
    t = re.sub(r"([。！？!?])\1+", r"\1", t)
    return t[:MAX_CHARS]


def _fail(resp) -> str:
    """把接口的报错变成看得懂的一句话。"""
    code = detail = ""
    try:
        data = resp.json()
        code, detail = str(data.get("code") or ""), str(data.get("detail") or data.get("error") or "")
    except ValueError:
        detail = (resp.text or "")[:160]
    hint = {401: "API Key 不对或已失效", 402: "余额或额度不够了", 403: "这个 Key 没有权限（或账号还不能用接口）",
            404: "音色 ID 不存在（检查是不是复制全了）", 422: "参数不对", 429: "请求太频繁，等一会儿再试",
            503: "BreezeBlue 现在忙不过来，等一会儿再试", 504: "BreezeBlue 超时了，等一会儿再试"}.get(resp.status_code, "")
    return "，".join(x for x in (f"{resp.status_code} {hint}".strip(), code, detail[:160]) if x)


def _post(url: str, headers: dict, payload: dict, proxies: dict | None):
    """发请求；对方说“忙，过几秒再来”（429 / 503 带 Retry-After）时等一等再试一次。"""
    for attempt in range(2):
        cancel.checkpoint()
        r = requests.post(url, headers=headers, json=payload, timeout=180, proxies=proxies)
        if r.status_code in (429, 503) and attempt == 0:
            try:
                wait = min(15, max(1, int(r.headers.get("Retry-After") or 3)))
            except ValueError:
                wait = 3
            log.info("BreezeBlue 让等 %d 秒再试", wait)
            cancel.sleep(wait)
            continue
        return r
    return r


def synthesize(db, rules: dict, text: str) -> tuple[bytes, str, float]:
    """把一段话读出来。返回 (音频数据, 扩展名 mp3, 估算的花费元)。"""
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        raise VoiceError("没有可以读的文字")
    headers, base, proxies, c = _auth(db, rules)
    settings = {"speed": max(0.5, min(2.0, float(c["voice_rate"] or 1.0))),
                "volume": max(0.01, min(2.0, float(c["voice_volume"] or 100) / 100.0))}
    payload = {"text": text, "voice_settings": settings}
    if re.search(r"[一-鿿]", text):
        payload["language_code"] = "zh"            # 明确是中文：长文案由服务端分段，读得更稳
    if str(c["voice_model"]).strip():
        payload["model_id"] = str(c["voice_model"]).strip()
    ins = str(c["voice_instruction"] or "").strip()
    if ins:
        payload["instructions"] = ins[:300]
        settings["guidance_scale"] = max(1.0, min(10.0, float(c["voice_guidance"] or 3.0)))
    voice_id = requests.utils.quote(str(c["voice_name"]).strip(), safe="")
    try:
        r = _post(f"{base}/v1/text-to-speech/{voice_id}?output_format={OUTPUT}", headers, payload, proxies)
    except requests.RequestException as e:
        raise VoiceError(f"连不上 BreezeBlue：{str(e).splitlines()[0][:120]}（网络不通的话可以在下面填代理）") from e
    if r.status_code != 200:
        raise VoiceError(f"语音合成失败：{_fail(r)}")
    audio = r.content
    if len(audio) < 800 or "json" in (r.headers.get("Content-Type") or ""):
        raise VoiceError("语音合成返回的不是音频")
    # 计费：汉字、假名、韩文一个算两个单位，其他算一个；价格按“元 / 万字（汉字）”估算
    units = sum(2 if re.match(r"[⺀-鿿가-힯぀-ヿ]", ch) else 1 for ch in text)
    cost = units / 20000.0 * float(c["voice_price"] or 0)
    return audio, "mp3", round(cost, 4)


def list_voices(db, rules: dict, kind: str = "personal") -> list[dict]:
    """读账号里能用的音色：personal 你自己设计 / 克隆的，default 官方的中文音色。"""
    headers, base, proxies, _c = _auth(db, rules, need_voice=False)
    params = {"voice_type": "personal" if kind == "personal" else "default", "page_size": 50}
    if kind != "personal":
        params.update(language_code="zh", gender="female")
    try:
        cancel.checkpoint()
        r = requests.get(f"{base}/v1/voices", headers={"xi-api-key": headers["xi-api-key"]}, params=params,
                         timeout=40, proxies=proxies)
    except requests.RequestException as e:
        raise VoiceError(f"连不上 BreezeBlue：{str(e).splitlines()[0][:120]}") from e
    if r.status_code != 200:
        raise VoiceError(f"读音色列表失败：{_fail(r)}")
    out = []
    for v in (r.json().get("voices") or []):
        if not v.get("voice_id") or v.get("age") == "child":       # 她的设定是成年人：不列童声
            continue
        tags = "、".join(str(t) for t in (v.get("tone") or [])[:3])
        out.append({"id": v["voice_id"], "name": str(v.get("name") or v["voice_id"])[:40],
                    "desc": " · ".join(x for x in (str(v.get("origin") or ""), tags, str(v.get("description") or "")[:60]) if x)})
    return out
