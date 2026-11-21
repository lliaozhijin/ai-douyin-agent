"""抖音视频 / 图文：从网页自己加载的作品数据里挑出无水印、最高画质的地址。

不用登录、不用她的浏览器，有两条路拿作品数据（见 jobs.py）：
- 作品分享页（iesdouyin.com/share/video/<id>）里的 _ROUTER_DATA：纯网络请求，和网上各种“抖音解析”工具一样；
- 后台单独开一个看不见的浏览器打开作品页，读网页自己请求的 /aweme/v1/web/aweme/detail/（要带网页自己算的签名，
  所以不自己去请求）或者写在网页里的 RENDER_DATA——和 Chrome 上的“抖音视频下载器”插件一样（见 guest_browser.py）。
数据里：
- video.bit_rate：同一个视频的多档清晰度（gear_name 如 normal_1080_0、adapt_lowest_1440_1……），
  每档的 play_addr 都是**不带水印**的地址；按分辨率、码率挑最高的就是原画质。
- video.download_addr 是带水印的（“抖音号”水印），不用。
- images：图文作品的每张图（url_list 里的也是不带水印的原图）；图里有 video 的是“实况照片”。
读不到网页数据时的后备：作品分享页（iesdouyin.com/share/video/<id>）里的 _ROUTER_DATA 有 play_addr.uri，
用 aweme/v1/play/?video_id=<uri>&ratio=1080p 拿到的也是不带水印的（GitHub 上各种抖音下载工具的通用做法）。
"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.5 Mobile/15E148 Safari/604.1")


def _urls(addr) -> list[str]:
    if not isinstance(addr, dict):
        return []
    return [u for u in (addr.get("url_list") or []) if isinstance(u, str) and u.startswith("http")]


def _num(*vals) -> int:
    for v in vals:
        try:
            if v not in (None, ""):
                return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def video_choices(detail: dict) -> list[dict]:
    """作品数据里所有不带水印的视频地址，按画质从高到低排：[{urls, width, height, bitrate, codec, size, gear}]。"""
    video = (detail or {}).get("video") or {}
    out = []
    for br in video.get("bit_rate") or []:
        if not isinstance(br, dict):
            continue
        pa = br.get("play_addr") or {}
        urls = _urls(pa)
        if not urls:
            continue
        gear = str(br.get("gear_name") or "")
        h265 = bool(br.get("is_h265") or br.get("is_bytevc1")) or "bytevc1" in gear
        out.append({"urls": urls, "width": _num(pa.get("width"), video.get("width")),
                    "height": _num(pa.get("height"), video.get("height")), "bitrate": _num(br.get("bit_rate")),
                    "codec": "h265" if h265 else "h264", "size": _num(pa.get("data_size")), "gear": gear})
    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        urls = [u for u in _urls(video.get(key)) if "playwm" not in u]
        if urls:
            pa = video.get(key) or {}
            out.append({"urls": urls, "width": _num(pa.get("width"), video.get("width")),
                        "height": _num(pa.get("height"), video.get("height")), "bitrate": 0,
                        "codec": "h265" if key.endswith("265") else "h264", "size": _num(pa.get("data_size")),
                        "gear": key})
    out.sort(key=lambda c: (c["width"] * c["height"], c["bitrate"], c["size"], c["codec"] == "h264"), reverse=True)
    return out


def best_video(detail: dict, prefer_h264: bool = False) -> dict | None:
    """画质最高的那一档。prefer_h264：同样的分辨率里优先 H.264（老电脑、老播放器放 H.265 可能要装解码器）。"""
    cands = video_choices(detail)
    if not cands:
        return None
    if prefer_h264:
        top = cands[0]["width"] * cands[0]["height"]
        same = [c for c in cands if c["width"] * c["height"] == top and c["codec"] == "h264"]
        if same:
            return same[0]
    return cands[0]


def pick_video(detail: dict, cap: int = 0, prefer_h264: bool = False, smallest: bool = False) -> dict | None:
    """按要的画质挑一档：cap = 1080 / 720 / 480 表示画面短边不超过这么多（竖屏 1080×1920 算 1080p）；0 = 不限。
    smallest：只要声音时下最小的那档。没有合适的档位就退回最接近的。"""
    if smallest:
        return smallest_video(detail)
    if not cap:
        return best_video(detail, prefer_h264)
    cands = video_choices(detail)
    ok = [c for c in cands if not (c["width"] and c["height"]) or min(c["width"], c["height"]) <= cap]
    if not ok:
        return min(cands, key=lambda c: min(c["width"], c["height"]) or 1 << 30) if cands else None
    if prefer_h264:
        top = ok[0]["width"] * ok[0]["height"]
        same = [c for c in ok if c["width"] * c["height"] == top and c["codec"] == "h264"]
        if same:
            return same[0]
    return ok[0]


def smallest_video(detail: dict) -> dict | None:
    """最小的一档（只为了转文字，不用下载原画质）。"""
    cands = video_choices(detail)
    if not cands:
        return None
    return min(cands, key=lambda c: (c["size"] or 1 << 40, c["width"] * c["height"] or 1 << 30))


def images(detail: dict) -> list[dict]:
    """图文作品的每张图：[{urls, live: [实况照片的视频地址]}]。不是图文返回空。"""
    out = []
    for im in (detail or {}).get("images") or []:
        if not isinstance(im, dict):
            continue
        urls = _urls(im) or _urls(im.get("download_url_list") and {"url_list": im["download_url_list"]})
        live = []
        if isinstance(im.get("video"), dict):
            c = best_video({"video": im["video"]})
            live = c["urls"] if c else []
        if urls or live:
            out.append({"urls": urls, "live": live})
    return out


def info(detail: dict) -> dict:
    """标题（文案）、作者、时长（秒）、封面。"""
    d = detail or {}
    video = d.get("video") or {}
    dur = _num(video.get("duration"), d.get("duration"))
    if dur > 10000:            # 毫秒
        dur //= 1000
    author = (d.get("author") or {}).get("nickname") or ""
    cover = (_urls(video.get("origin_cover")) or _urls(video.get("cover")) or [""])[0]
    return {"id": str(d.get("aweme_id") or ""), "desc": str(d.get("desc") or "").strip(), "author": author,
            "duration": dur, "cover": cover}


def _all_http(o, depth: int = 0) -> list[str]:
    """一个结构里所有的 http 地址（作品数据里声音地址的写法不统一：url_list / main_url / backup_url……）。"""
    out: list[str] = []
    if depth > 5:
        return out
    if isinstance(o, str):
        if o.startswith("http"):
            out.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            out += _all_http(v, depth + 1)
    elif isinstance(o, list):
        for v in o:
            out += _all_http(v, depth + 1)
    return out


def audio_candidates(detail: dict, share_page: bool = False) -> list[tuple[str, list[str]]]:
    """为了转文字要拿到“有声音的文件”：按顺序列出可以试的地址 [(说明, [地址…])]。
    抖音网页版有的清晰度档位只有画面、没有声音（画面和声音分开放），所以下载后要检查有没有声音，没有就试下一个。
    转文字不在乎水印，带水印的地址也可以用来听。"""
    d = detail or {}
    video = d.get("video") or {}
    out: list[tuple[str, list[str]]] = []
    # 1. 视频档位：从小到大（小的下得快）
    for c in sorted(video_choices(d), key=lambda c: (c["size"] or 1 << 40, c["width"] * c["height"] or 1 << 30))[:4]:
        out.append((f"视频档位 {c['gear'] or c['height']}", c["urls"]))
    # 2. 单独的声音（网页版把声音分开放的那种）
    for i, a in enumerate(video.get("bit_rate_audio") or []):
        urls = list(dict.fromkeys(_all_http(a)))
        if urls:
            out.append((f"单独的声音 {i + 1}", urls))
    # 3. 用 uri 换出来的不带水印地址
    if share_page or not out:
        for ratio in ("540p", "720p"):
            urls = nowm_from_uri(d, ratio=ratio)
            if urls:
                out.append((f"播放地址 {ratio}", urls))
    # 4. 带水印的下载地址（转文字不在乎水印）
    for key in ("download_addr", "play_addr"):
        urls = _urls(video.get(key))
        if urls:
            out.append((f"{key}（可能带水印，只用来听）", urls))
    # 5. 原声：作品用的是“@作者创作的原声”时，这段音乐就是视频本身的声音
    music = d.get("music") or {}
    title = str(music.get("title") or "")
    author = str((d.get("author") or {}).get("nickname") or "")
    if "原声" in title or (author and str(music.get("author") or "") == author):
        urls = _urls(music.get("play_url"))
        if urls:
            out.append(("作品的原声", urls))
    seen: set[str] = set()
    uniq = []
    for label, urls in out:
        urls = [u for u in urls if u not in seen]
        seen.update(urls)
        if urls:
            uniq.append((label, urls))
    return uniq


def subtitle_urls(detail: dict) -> list[dict]:
    """作品数据里自带的字幕（有的视频有自动字幕）：[{url, lang, fmt}]。"""
    found = []

    def walk(o, depth=0):
        if depth > 6:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in ("subtitle_infos", "subtitleinfos", "caption_infos") and isinstance(v, list):
                    for s in v:
                        if not isinstance(s, dict):
                            continue
                        url = s.get("Url") or s.get("url") or (s.get("url_list") or [""])[0]
                        if isinstance(url, str) and url.startswith("http"):
                            found.append({"url": url, "lang": str(s.get("LanguageCodeName") or s.get("language") or
                                                                 s.get("lang") or ""),
                                          "fmt": str(s.get("Format") or s.get("format") or "").lower()})
                elif isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o[:50]:
                walk(v, depth + 1)

    walk(detail)
    return found


_ROUTER = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", re.S)


def detail_from_share_page(html: str) -> dict | None:
    """作品分享页（iesdouyin.com/share/video/<id>）里的作品数据。"""
    m = _ROUTER.search(html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    for v in (data.get("loaderData") or {}).values():
        if isinstance(v, dict):
            items = ((v.get("videoInfoRes") or {}).get("item_list") or [])
            if items and isinstance(items[0], dict):
                return items[0]
    return None


def nowm_from_uri(detail: dict, ratio: str = "1080p") -> list[str]:
    """分享页的数据里只有带水印的 playwm 地址时：用 play_addr.uri 换成不带水印的播放地址（ratio：要多清楚，默认 1080p）。"""
    pa = ((detail or {}).get("video") or {}).get("play_addr") or {}
    uri = pa.get("uri") or ""
    urls = []
    if uri and not uri.startswith("http"):
        urls.append(f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio={ratio}&line=0")
        urls.append(f"https://www.iesdouyin.com/aweme/v1/play/?video_id={uri}&ratio={ratio}&line=0")
    urls += [u.replace("/playwm/", "/play/") for u in _urls(pa)]
    return list(dict.fromkeys(urls))


# ---------------------------------------------------------------- 网页里直接带的作品数据（RENDER_DATA）
RENDER_JS = """() => { const s = document.getElementById('RENDER_DATA'); return s ? s.textContent : ''; }"""


def _abs(u: str) -> str:
    return ("https:" + u) if isinstance(u, str) and u.startswith("//") else u


def _src_list(lst) -> dict:
    urls = []
    for x in lst or []:
        u = x.get("src") if isinstance(x, dict) else x
        if isinstance(u, str) and u:
            urls.append(_abs(u))
    return {"url_list": urls}


def from_render(o: dict) -> dict:
    """直接打开作品页时，作品数据是写在网页里的（RENDER_DATA，驼峰写法），换成和接口一样的写法。"""
    v = o.get("video") or {}
    video = {
        "width": v.get("width"), "height": v.get("height"), "duration": v.get("duration"),
        "play_addr": {**_src_list(v.get("playAddr")), "width": v.get("width"), "height": v.get("height"),
                      "uri": v.get("uri") or ""},
        "bit_rate": [{"gear_name": br.get("gearName") or "", "bit_rate": br.get("bitRate"),
                      "is_h265": br.get("isH265"),
                      "play_addr": {**_src_list(br.get("playAddr")), "width": br.get("width"),
                                    "height": br.get("height"), "data_size": br.get("dataSize")}}
                     for br in (v.get("bitRateList") or []) if isinstance(br, dict)],
        "origin_cover": {"url_list": [_abs(v["originCover"])] if v.get("originCover") else []},
    }
    if v.get("subtitleInfos"):
        video["subtitle_infos"] = v["subtitleInfos"]
    imgs = [{"url_list": [_abs(u) for u in (im.get("urlList") or []) if isinstance(u, str)]}
            for im in (o.get("images") or []) if isinstance(im, dict)]
    return {"aweme_id": str(o.get("awemeId") or ""), "desc": o.get("desc") or "",
            "author": {"nickname": (o.get("authorInfo") or {}).get("nickname") or ""}, "video": video, "images": imgs}


def from_render_text(raw: str, aweme_id: str = "") -> dict | None:
    try:
        data = json.loads(unquote(raw or ""))
    except ValueError:
        return None
    found = []

    def walk(o, depth=0):
        if depth > 8 or found:
            return
        if isinstance(o, dict):
            if o.get("awemeId") and (isinstance(o.get("video"), dict) or o.get("images")):
                if not aweme_id or str(o.get("awemeId")) == aweme_id:
                    found.append(o)
                    return
            for x in o.values():
                if isinstance(x, (dict, list)):
                    walk(x, depth + 1)
        elif isinstance(o, list):
            for x in o[:40]:
                walk(x, depth + 1)

    walk(data)
    return from_render(found[0]) if found else None
