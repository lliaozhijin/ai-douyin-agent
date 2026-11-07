"""模型调用：Anthropic Messages API 和 OpenAI 兼容的 Chat Completions。

国内大部分模型（DeepSeek、通义、豆包、Kimi、智谱）都提供 OpenAI 兼容接口，所以只需要两种实现。
"""
from __future__ import annotations

import base64
import copy
import logging
from dataclasses import dataclass, field

import requests

from .. import cancel
from .. import crypto
from ..defaults import ANTHROPIC_VERSION

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    provider: str = ""
    model: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _proxies(proxy: str) -> dict | None:
    proxy = (proxy or "").strip()
    return {"http": proxy, "https": proxy} if proxy else None


def _post(url: str, headers: dict, payload: dict, proxy: str, timeout: int = 90) -> dict:
    last_err = None
    for attempt in range(3):
        try:
            # 点了“让她休息一下”不用等这次回复回来
            resp = cancel.call(requests.post, url, headers=headers, json=payload, proxies=_proxies(proxy),
                               timeout=timeout)
        except requests.RequestException as e:
            last_err = LLMError(f"网络连接失败：{e}")
            cancel.sleep(2 * (attempt + 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 529):
            last_err = LLMError(f"服务繁忙（HTTP {resp.status_code}）")
            cancel.sleep(3 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text[:300]
            raise LLMError(f"HTTP {resp.status_code}：{detail}")
        try:
            return resp.json()
        except ValueError as e:
            raise LLMError(f"返回内容不是 JSON：{resp.text[:200]}") from e
    raise last_err or LLMError("请求失败")


def _sniff_media_type(img: bytes) -> str:
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if img[:4] == b"RIFF" and img[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _anthropic(p: dict, model: str, system: str, user_text: str, images: list[bytes],
               max_tokens: int, temperature: float) -> LLMResult:
    key = crypto.decrypt(p.get("api_key_enc", ""))
    if not key:
        raise LLMError(f"{p['name']} 还没有填写 API Key")
    base = (p.get("base_url") or "https://api.anthropic.com").rstrip("/")
    version = (p.get("extra") or {}).get("anthropic_version") or ANTHROPIC_VERSION
    content: list[dict] = []
    for img in images:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": _sniff_media_type(img), "data": base64.b64encode(img).decode()}})
    content.append({"type": "text", "text": user_text})
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {"x-api-key": key, "anthropic-version": version, "content-type": "application/json"}
    data = _post(f"{base}/v1/messages", headers, payload, p.get("proxy", ""))
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage") or {}
    return LLMResult(text=text.strip(), input_tokens=int(usage.get("input_tokens") or 0),
                     output_tokens=int(usage.get("output_tokens") or 0), provider=p["id"], model=model, raw=data)


def _openai(p: dict, model: str, system: str, user_text: str, images: list[bytes],
            max_tokens: int, temperature: float) -> LLMResult:
    key = crypto.decrypt(p.get("api_key_enc", ""))
    if not key:
        raise LLMError(f"{p['name']} 还没有填写 API Key")
    base = (p.get("base_url") or "").rstrip("/")
    if not base:
        raise LLMError(f"{p['name']} 还没有填写接口地址")
    if images:
        content: list[dict] | str = [{"type": "text", "text": user_text}]
        for img in images:
            url = f"data:{_sniff_media_type(img)};base64,{base64.b64encode(img).decode()}"
            content.append({"type": "image_url", "image_url": {"url": url}})
    else:
        content = user_text
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
    }
    payload.update(_request_extra(p))
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{base}/chat/completions"
    data = _post(url, headers, payload, p.get("proxy", ""))
    text, finish, reasoning = _openai_text(data)
    usage = data.get("usage") or {}
    in_tok, out_tok = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    if not text and (finish == "length" or reasoning):
        # 模型开着思考模式时，思考内容会先用掉回复长度，最后的回答是空的：放宽长度再试一次
        log.warning("%s · %s 只返回了思考内容，放宽回复长度重试", p["name"], model)
        payload = {**payload, "max_tokens": max(2000, max_tokens * 8)}
        data = _post(url, headers, payload, p.get("proxy", ""))
        text, finish, reasoning = _openai_text(data)
        usage = data.get("usage") or {}
        in_tok += int(usage.get("prompt_tokens") or 0)
        out_tok += int(usage.get("completion_tokens") or 0)
        if not text:
            raise LLMError(f"{p['name']} · {model} 只输出了思考过程，没有给出回答。"
                           "请在“额外请求参数”里关闭思考模式")
    return LLMResult(text=text, input_tokens=in_tok, output_tokens=out_tok, provider=p["id"], model=model, raw=data)


def _openai_text(data: dict) -> tuple[str, str, str]:
    """取出 (回答文字, 结束原因, 思考内容)。"""
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"返回格式无法识别：{str(data)[:200]}") from e
    text = msg.get("content") or ""
    if isinstance(text, list):  # 少数服务商返回分段内容
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return str(text).strip(), str(choice.get("finish_reason") or ""), str(msg.get("reasoning_content") or "")


def _request_extra(p: dict) -> dict:
    """服务商的额外请求参数，例如 DeepSeek 的 {"thinking": {"type": "disabled"}}。"""
    extra = (p.get("extra") or {}).get("request_extra") or {}
    return copy.deepcopy(extra) if isinstance(extra, dict) else {}


def vision_models(p: dict) -> list[str]:
    """这个服务商里哪些模型能识图。老数据没有逐个模型的设置时，按整个服务商的勾选算。"""
    vm = (p.get("extra") or {}).get("vision_models")
    if isinstance(vm, list):
        return [m for m in vm if m in (p.get("models") or [])] or list(vm)
    return list(p.get("models") or []) if p.get("vision") else []


def model_can_see(p: dict, model: str) -> bool:
    return model in vision_models(p)


def chat(provider: dict, model: str, system: str, user_text: str, images: list[bytes] | None = None,
         max_tokens: int = 600, temperature: float = 0.8) -> LLMResult:
    """调用一次模型。provider 是数据库里的一行（含加密后的 API Key）。"""
    images = images or []
    if images and not model_can_see(provider, model):
        raise LLMError(f"{provider['name']} · {model} 没有勾选“支持识图”")
    if not model:
        raise LLMError(f"{provider['name']} 还没有选择模型")
    fn = _anthropic if provider.get("kind") == "anthropic" else _openai
    result = fn(provider, model, system, user_text, images, max_tokens, temperature)
    price_in = float(provider.get("price_in") or 0)
    price_out = float(provider.get("price_out") or 0)
    result.cost = round(result.input_tokens / 1e6 * price_in + result.output_tokens / 1e6 * price_out, 6)
    return result
