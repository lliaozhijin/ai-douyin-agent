"""AI 生图：调用你自己配置的服务商的 OpenAI 兼容接口（POST {接口地址}/images/generations）。

支持返回图片地址（url）或图片数据（b64_json）的两种格式。生成的图会标注为 AI 生成（作品发布时也会声明）。
"""
from __future__ import annotations

import base64
import io
import logging

import requests

from .. import cancel
from .. import crypto

log = logging.getLogger(__name__)


class ImageGenError(Exception):
    pass


def available(db, rules: dict) -> bool:
    p = db.provider(rules.get("image_gen_provider") or "")
    return bool(p and p.get("api_key_enc") and (rules.get("image_gen_model") or "").strip())


def generate(db, rules: dict, prompt: str) -> tuple[bytes, float]:
    """生成一张图，返回 (PNG/JPG 字节, 花费)。"""
    p = db.provider(rules.get("image_gen_provider") or "")
    model = (rules.get("image_gen_model") or "").strip()
    if not p or not model:
        raise ImageGenError("还没有在“规则与预算 → 发作品”里选择生图的服务商和模型")
    key = crypto.decrypt(p.get("api_key_enc", ""))
    base = (p.get("base_url") or "").rstrip("/")
    if not key or not base:
        raise ImageGenError(f"{p['name']} 还没有填写 API Key 或接口地址")
    proxy = (p.get("proxy") or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    payload = {"model": model, "prompt": prompt[:900], "n": 1}
    size = (rules.get("image_gen_size") or "").strip()
    if size:
        payload["size"] = size
    try:
        # 点了“让她休息一下”就不再等图生成
        resp = cancel.call(requests.post, f"{base}/images/generations", json=payload, proxies=proxies, timeout=180,
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise ImageGenError(f"网络连接失败：{e}") from e
    if resp.status_code >= 400:
        raise ImageGenError(f"生图接口返回 HTTP {resp.status_code}：{resp.text[:300]}")
    try:
        item = (resp.json().get("data") or [])[0]
    except (ValueError, IndexError, AttributeError) as e:
        raise ImageGenError(f"生图接口的返回内容认不出来：{resp.text[:200]}") from e
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        try:
            r2 = requests.get(item["url"], proxies=proxies, timeout=120)
            r2.raise_for_status()
            raw = r2.content
        except requests.RequestException as e:
            raise ImageGenError(f"下载生成的图片失败：{e}") from e
    else:
        raise ImageGenError("生图接口没有返回图片")
    _check_image(raw)
    return raw, float(rules.get("image_gen_price") or 0)


def _check_image(raw: bytes) -> None:
    from PIL import Image

    try:
        Image.open(io.BytesIO(raw)).verify()
    except Exception as e:  # noqa: BLE001
        raise ImageGenError("生图接口返回的不是图片") from e
