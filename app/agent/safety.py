"""敏感话题过滤和发出内容的检查。"""
from __future__ import annotations

import re

URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|cn|net|top|xyz|cc|me)\b)", re.I)
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
CONTACT_RE = re.compile(r"(微信|vx|v信|wx|QQ|qq|扣扣|电话|手机号|加我|私聊我|联系方式)", re.I)


def keyword_hit(text: str, rules: dict) -> str | None:
    """命中敏感话题或屏蔽词时，返回命中的话题名 / 屏蔽词；没命中返回 None。"""
    if not text:
        return None
    low = text.lower()
    topic_kw: dict = rules.get("topic_keywords") or {}
    for topic in rules.get("topics") or []:
        for kw in topic_kw.get(topic, []):
            if kw and kw.lower() in low:
                return topic
    for kw in rules.get("keywords") or []:
        kw = (kw or "").strip()
        if kw and kw.lower() in low:
            return f"屏蔽词「{kw}」"
    return None


def output_problem(text: str) -> str | None:
    """检查模型写出来、准备发出去的内容。有问题返回原因，没问题返回 None。"""
    if not text or not text.strip():
        return "内容为空"
    if URL_RE.search(text):
        return "包含链接"
    if PHONE_RE.search(text):
        return "包含手机号"
    if CONTACT_RE.search(text):
        return "包含联系方式相关的词"
    if len(text) > 300:
        return "内容太长"
    return None


def clean_reply(text: str) -> str:
    """去掉模型偶尔会加的引号、前缀和 markdown。"""
    t = (text or "").strip()
    t = re.sub(r"^(回复|评论|我的回复|雨雨)[:：]\s*", "", t)
    t = t.strip().strip("“”\"'")
    # 只去掉 markdown 记号（**加粗**、行首 #、`代码`），保留颜文字里的 * 之类的字符
    t = re.sub(r"\*\*|__|`+", "", t)
    t = re.sub(r"(?m)^\s*#+\s*", "", t)
    return t.strip()
