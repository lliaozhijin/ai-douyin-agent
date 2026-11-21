"""字幕文件 → 一段段带时间的文字 → 文本 / SRT。

认得这几种：WebVTT（YouTube、抖音）、SRT、YouTube 的 json3、B 站的 json（body: [{from, to, content}]）。
YouTube 的自动字幕是“滚动”的（同一句话会在好几条里重复出现），这里会把重复的去掉。
"""
from __future__ import annotations

import html
import json
import re

_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE = re.compile(_TIME + r"\s*-->\s*" + _TIME)
_TAG = re.compile(r"<[^>]+>")


def _sec(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int((ms or "0").ljust(3, "0")[:3]) / 1000


def parse_vtt_srt(text: str) -> list[dict]:
    segs = []
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = _CUE.search(lines[i])
        if not m:
            i += 1
            continue
        start, end = _sec(*m.groups()[:4]), _sec(*m.groups()[4:])
        i += 1
        buf = []
        while i < len(lines) and lines[i].strip() and not _CUE.search(lines[i]):
            buf.append(lines[i])
            i += 1
        t = html.unescape(_TAG.sub("", " ".join(buf))).strip()
        t = re.sub(r"\s+", " ", t)
        if t:
            segs.append({"start": start, "end": end, "text": t})
    return dedupe(segs)


def parse_json3(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    segs = []
    for ev in data.get("events") or []:
        parts = [s.get("utf8", "") for s in (ev.get("segs") or []) if isinstance(s, dict)]
        t = re.sub(r"\s+", " ", "".join(parts)).strip()
        if not t:
            continue
        start = (ev.get("tStartMs") or 0) / 1000
        segs.append({"start": start, "end": start + (ev.get("dDurationMs") or 0) / 1000, "text": t})
    return dedupe(segs)


def parse_bili_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    segs = []
    for b in data.get("body") or []:
        t = str(b.get("content") or "").strip()
        if t:
            segs.append({"start": float(b.get("from") or 0), "end": float(b.get("to") or 0), "text": t})
    return segs


def parse_any(text: str, fmt: str = "") -> list[dict]:
    fmt = (fmt or "").lower()
    s = (text or "").lstrip("﻿").strip()
    if fmt == "json3" or (s.startswith("{") and '"events"' in s[:2000]):
        return parse_json3(s)
    if s.startswith("{") and '"body"' in s[:2000]:
        return parse_bili_json(s)
    return parse_vtt_srt(s)


def dedupe(segs: list[dict]) -> list[dict]:
    """去掉滚动字幕里重复的部分：后一条以前一条（原文）开头的，只留新增的部分；完全一样的去掉。"""
    out: list[dict] = []
    prev = ""                      # 上一条的原文（没去掉重复之前）
    for s in segs:
        t = s["text"]
        if out and (t == prev or t == out[-1]["text"]):
            out[-1]["end"] = max(out[-1]["end"], s["end"])
            prev = t if len(t) > len(prev) else prev
            continue
        raw = t
        if out and prev and t.startswith(prev):
            t = t[len(prev):].strip()
            if not t:
                prev = raw
                continue
        out.append({**s, "text": t})
        prev = raw
    return out


def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(segs: list[dict]) -> str:
    return "\n".join(f"{i}\n{_ts(s['start'])} --> {_ts(max(s['end'], s['start'] + 0.5))}\n{s['text']}\n"
                     for i, s in enumerate(segs, 1))


_CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")


def to_paragraphs(segs: list[dict], gap: float = 2.5, max_chars: int = 220) -> str:
    """把一段段字幕接成好读的段落：停顿超过 gap 秒、或者一段太长，就另起一段。中文之间不加空格。"""
    paras, cur, last_end = [], "", None
    for s in segs:
        t = s["text"].strip()
        if not t:
            continue
        new_para = cur and ((last_end is not None and s["start"] - last_end > gap) or len(cur) >= max_chars)
        if new_para:
            paras.append(cur)
            cur = ""
        if cur:
            joiner = "" if (_CJK.search(cur[-1]) or _CJK.search(t[0])) else " "
            if _CJK.search(cur[-1]) and _CJK.search(t[0]) and cur[-1] not in "，。！？、；：…":
                joiner = "，"
            cur += joiner + t
        else:
            cur = t
        last_end = s["end"]
    if cur:
        paras.append(cur)
    out = []
    for p in paras:
        if _CJK.search(p[-1:]) and p[-1] not in "。！？…":
            p += "。"
        out.append(p)
    return "\n\n".join(out)
