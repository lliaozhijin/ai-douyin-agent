"""“其他功能”里的任务：下载无水印原画质视频、导出视频文案。

- 粘贴一段话（可以有好几个地址），每个地址一个任务，后台排队做（同时最多 2 个），界面上能看到进度、能取消。
  做完的任务不留记录：进度条消失，成功时给个小提示，失败时弹窗说原因（见 pop_events）。
- 抖音：**不用她去操作、不用她的浏览器、不用登录**，直接用技术手段拿到不带水印的地址：
  1. 作品分享页（iesdouyin.com/share/…）——和网上各种“抖音解析 / 去水印”工具一样，纯网络请求，一两秒；
  2. 后台单独开一个看不见的浏览器（不登录、临时的配置文件夹），打开作品页读网页自己加载的作品数据——
     和 Chrome 上的“抖音视频下载器”插件一样（见 guest_browser.py），能拿到 2K / 4K 这些原画质档位。
  默认“原画质优先”：先 2 再 1；导出文案只要声音，先 1 再 2。
- 其他网站：yt-dlp（见 ytdl.py）。和 en.loader.to 这类网站一样可以选格式：最高画质 / 1080p / 720p / 480p 的 MP4，
  或者只要声音（MP3 / M4A）。
- 导出文案：先用平台自带的字幕（YouTube、B 站有 CC / AI 字幕时最准）；没有字幕就下载声音、本机语音转文字
  （Whisper）；可以选“用大模型加标点、分段”。导出 .txt（标题、作者、地址、简介、正文），可选同时导出 .srt 字幕。
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from ..db import Database, now_str
from ..paths import DATA_DIR
from . import douyin as dy
from . import subtitles, ytdl
from .links import NAMES, douyin_id, extract_urls, is_short, platform, safe_name

log = logging.getLogger(__name__)

DEFAULT_DIR = DATA_DIR / "downloads"
DEFAULTS = {"dir": "", "proxy": "", "cookies": "", "prefer_h264": False, "asr_model": "base", "lang": "auto",
            "polish": False, "srt": True, "format": "best", "douyin_best": True, "rewrite": False, "rewrite_extra": ""}
# 下载格式（和 en.loader.to 这类网站一样）：键 → (界面上的名字, 画面最高多少 p；0 = 不限；None = 只要声音)
FORMATS = {"best": ("最高画质 MP4", 0), "1080": ("1080p MP4", 1080), "720": ("720p MP4", 720),
           "480": ("480p MP4", 480), "mp3": ("只要声音 MP3", None), "m4a": ("只要声音 M4A", None)}
UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
         "Safari/537.36 Edg/131.0.0.0")
SUB_LANGS = {"zh": ("zh-Hans", "zh-CN", "zh", "ai-zh", "zh-SG", "zh-Hant", "zh-TW", "zh-HK", "cmn-Hans-CN"),
             "en": ("en", "en-US", "en-GB", "en-orig", "ai-en")}
SUB_EXTS = ("json3", "srv3", "vtt", "srt", "json", "ass")


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind: str, url: str, opts: dict):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind               # video 下载视频 / text 导出文案 / setup 安装组件
        self.url = url
        self.site = platform(url) if url.startswith("http") else ""
        self.opts = dict(opts)
        self.status = "queued"         # queued / running / done / failed / cancelled
        self.progress = 0.0
        self.stage = "排队中"
        self.title = ""
        self.files: list[str] = []
        self.error = ""
        self.note = ""
        self.warning = ""              # 做完了但有一部分没成功（比如大模型改写失败）：也要弹窗说原因
        self.created_at = now_str()
        self.finished_at = ""
        self.stop = threading.Event()

    def view(self) -> dict:
        return {"id": self.id, "kind": self.kind, "url": self.url, "site": self.site,
                "site_name": NAMES.get(self.site, ""), "status": self.status, "progress": round(self.progress, 3),
                "stage": self.stage, "title": self.title, "files": self.files, "error": self.error, "note": self.note,
                "created_at": self.created_at, "finished_at": self.finished_at}

    def step(self, stage: str, progress: float | None = None) -> None:
        if self.stop.is_set():
            raise Cancelled()
        self.stage = stage
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))


class ToolJobs:
    def __init__(self, db: Database, agent=None):
        self.db = db
        self.agent = agent
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aiw-tools")
        self.lock = threading.Lock()
        self.events: list[dict] = []        # 刚做完的任务：界面取走一次就没了（成功给个小提示，失败弹窗说原因）
        self._forget_history()

    # ---------- 设置 ----------
    def settings(self) -> dict:
        s = {**DEFAULTS, **(self.db.get_json("tools", {}) or {})}
        s["dir_effective"] = str(Path(s["dir"]) if s["dir"] else DEFAULT_DIR)
        return s

    def save_settings(self, data: dict) -> dict:
        cur = {**DEFAULTS, **(self.db.get_json("tools", {}) or {})}
        for k in DEFAULTS:
            if k in data:
                cur[k] = data[k] if not isinstance(DEFAULTS[k], bool) else bool(data[k])
        if cur["asr_model"] not in ("tiny", "base", "small"):
            cur["asr_model"] = "base"
        if cur["lang"] not in ("auto", "zh", "en"):
            cur["lang"] = "auto"
        if cur["format"] not in FORMATS:
            cur["format"] = "best"
        cur["rewrite_extra"] = str(cur.get("rewrite_extra") or "").strip()[:300]
        self.db.set_json("tools", cur)
        return self.settings()

    def out_dir(self, sub: str) -> Path:
        d = Path(self.settings()["dir_effective"]) / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- 任务 ----------
    def start(self, kind: str, text: str, opts: dict | None = None) -> list[dict]:
        urls = extract_urls(text)
        if not urls:
            raise ValueError("没找到视频地址：把视频的分享链接（或者整段分享文字）粘贴进来")
        out = []
        for u in urls[:20]:
            job = Job(kind, u, {**self.settings(), **(opts or {})})
            with self.lock:
                self.jobs[job.id] = job
            self.pool.submit(self._run, job)
            out.append(job.view())
        return out

    def start_setup(self, what: str) -> dict:
        job = Job("setup", what, self.settings())
        job.title = {"deno": "安装 deno（下载 YouTube 要用）", "ytdlp": "更新下载组件 yt-dlp"}.get(what, what)
        with self.lock:
            self.jobs[job.id] = job
        self.pool.submit(self._run, job)
        return job.view()

    def list(self) -> list[dict]:
        """只有排队中、正在做的任务（做完的不留记录）。"""
        with self.lock:
            items = sorted((j for j in self.jobs.values() if j.status in ("queued", "running")),
                           key=lambda j: j.created_at, reverse=True)
        return [j.view() for j in items[:60]]

    def pop_events(self) -> list[dict]:
        """刚做完的任务（成功 / 失败），取走就清空：界面成功时给个小提示，失败时弹窗说原因。"""
        with self.lock:
            out, self.events = self.events, []
        return out

    def cancel(self, jid: str) -> bool:
        job = self.jobs.get(jid)
        if not job or job.status not in ("queued", "running"):
            return False
        job.stop.set()
        if job.status == "queued":
            job.status, job.stage, job.finished_at = "cancelled", "取消了", now_str()
            self._finish(job)
        return True

    def _forget_history(self) -> None:
        """不保存任务的历史记录：以前版本留下的记录也清掉（下载好的文件不动）。"""
        try:
            self.db.execute("DELETE FROM tool_jobs")
        except Exception:  # noqa: BLE001
            pass

    def _finish(self, job: Job) -> None:
        """任务结束：从列表里拿掉（界面上的进度条随之消失），成功 / 失败留一条一次性的通知。"""
        with self.lock:
            self.jobs.pop(job.id, None)
            if job.status in ("done", "failed"):
                ev = {"id": job.id, "kind": job.kind, "status": job.status, "title": job.title or job.url,
                      "url": job.url, "error": job.error, "note": job.note, "warning": job.warning,
                      "files": [Path(f).name for f in job.files]}
                self.events = (self.events + [ev])[-20:]

    def _run(self, job: Job) -> None:
        if job.stop.is_set():
            return
        job.status, job.stage = "running", "开始"
        t0 = time.time()
        try:
            if job.kind == "setup":
                self._setup(job)
            elif job.kind == "video":
                self._download(job)
            else:
                self._transcript(job)
            job.status, job.progress, job.stage = "done", 1.0, "完成"
            log.info("其他功能：%s %s 完成（%.0f 秒）：%s", job.kind, job.url, time.time() - t0, job.files)
        except Cancelled:
            job.status, job.stage = "cancelled", "取消了"
        except Exception as e:  # noqa: BLE001
            if job.stop.is_set():                       # yt-dlp 自己的“取消下载”异常
                job.status, job.stage = "cancelled", "取消了"
                job.finished_at = now_str()
                self._finish(job)
                return
            job.status, job.stage = "failed", "没成功"
            job.error = friendly_error(e)
            log.warning("其他功能：%s %s 没成功：%s", job.kind, job.url, e, exc_info=True)
        job.finished_at = now_str()
        self._finish(job)

    # ---------- 安装组件 ----------
    def _setup(self, job: Job) -> None:
        prog = lambda f: job.step("下载中", f)      # noqa: E731
        stop = job.stop.is_set
        if job.url == "deno":
            path = ytdl.install_deno(prog, job.opts.get("proxy") or "", stop)
            job.files, job.note = [path], "装好了：以后下载 YouTube 会用它"
        else:
            v = ytdl.update_ytdlp(prog, job.opts.get("proxy") or "", stop)
            job.note = f"下载好了 yt-dlp {v}，重启软件后生效"

    # ---------- 抖音 ----------
    def _douyin_detail(self, job: Job, best: bool = False) -> dict:
        """拿到抖音作品数据：{detail, cookies, ua, share_page?}。不用她、不用登录：
        分享页（纯网络请求，和“抖音解析”网站一样）和后台单独的看不见的浏览器（和浏览器插件一样）两条路，
        best=True（要原画质）先走浏览器那条。"""
        url = job.url
        aid = douyin_id(url)
        if not aid and is_short(url):
            job.step("打开分享链接")
            try:
                url = self._follow(url, job)
                aid = douyin_id(url)
            except Exception as e:  # noqa: BLE001
                log.info("抖音短链接跳转失败（%s），交给后台浏览器打开", e)
        ways = [("浏览器", self._douyin_guest), ("分享页", self._douyin_share)]
        if not best:
            ways.reverse()
        errors = []
        for name, fn in ways:
            if job.stop.is_set():
                raise Cancelled()
            try:
                res = fn(job, url, aid)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                res = None
                errors.append(f"{name}：{str(e).splitlines()[0][:100] if str(e) else type(e).__name__}")
                log.info("抖音作品数据（%s）没拿到：%s", name, e)
            if res and res.get("detail"):
                return res
        if not aid and not errors:
            raise RuntimeError("认不出这是哪个抖音作品")
        raise RuntimeError("没读到这个抖音作品的数据（作品可能已删除、设为私密或仅粉丝可见）"
                           + (f"：{'；'.join(errors)}" if errors else ""))

    def _douyin_share(self, job: Job, url: str, aid: str) -> dict | None:
        """作品分享页里的作品数据（纯网络请求，不开浏览器、不登录）。"""
        if not aid:
            return None
        import requests
        job.step("读作品分享页（不用登录）")
        kinds = ("note", "video") if "/note/" in url else ("video", "note")
        for kind in kinds:
            r = requests.get(f"https://www.iesdouyin.com/share/{kind}/{aid}/",
                             headers={"User-Agent": dy.UA_MOBILE, "Referer": "https://www.douyin.com/"},
                             timeout=20, proxies=self._proxies(job))
            detail = dy.detail_from_share_page(r.text)
            if detail and (dy.video_choices(detail) or dy.images(detail) or dy.nowm_from_uri(detail)):
                return {"detail": detail, "cookies": {}, "ua": dy.UA_MOBILE, "share_page": True}
        return None

    def _douyin_guest(self, job: Job, url: str, aid: str) -> dict | None:
        """后台单独开一个看不见的浏览器（不是她的那个、不登录），打开作品页读网页自己加载的作品数据。"""
        from . import guest_browser
        job.step("在后台单独开一个看不见的浏览器读作品页（不用她的浏览器、不用登录）")
        page = f"https://www.douyin.com/{'note' if '/note/' in url else 'video'}/{aid}" if aid else url
        res = guest_browser.read_detail(page, aid, proxy=job.opts.get("proxy") or "", stop=job.stop.is_set)
        if not res.get("detail") and res.get("sniffed"):
            # 作品数据没读到，但网页确实在播放：用网页实际在播放的视频文件（和嗅探插件一样，同样不带水印）
            res["detail"] = {"aweme_id": res.get("aweme_id") or aid, "desc": "",
                             "video": {"play_addr": {"url_list": res["sniffed"]}}}
            res["sniffed_only"] = True
        return res if res.get("detail") else None

    def _follow(self, url: str, job: Job) -> str:
        import requests
        r = requests.get(url, headers={"User-Agent": dy.UA_MOBILE}, timeout=15, allow_redirects=True,
                         proxies=self._proxies(job), stream=True)
        r.close()
        return r.url

    @staticmethod
    def _proxies(job: Job):
        p = job.opts.get("proxy") or ""
        return {"http": p, "https": p} if p else None

    def _http_get(self, job: Job, urls: list[str], dest: Path, headers: dict, cookies: dict, share: tuple = (0, 1),
                  what: str = "下载视频") -> Path:
        """按顺序试每个地址，下到 dest。share：这一步占总进度的哪一段。"""
        import requests

        a, b = share
        last = None
        for u in urls:
            try:
                with requests.get(u, headers=headers, cookies=cookies, stream=True, timeout=(15, 60),
                                  allow_redirects=True, proxies=self._proxies(job)) as r:
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    if "text/html" in (r.headers.get("content-type") or ""):
                        raise RuntimeError("拿到的是网页，不是文件")
                    total = int(r.headers.get("content-length") or 0)
                    got = 0
                    tmp = dest.with_name(dest.name + ".part")
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(512 * 1024):
                            job.step(f"{what} {got / 1048576:.1f} MB" + (f" / {total / 1048576:.1f} MB" if total else ""),
                                     a + (b - a) * (got / total) if total else None)
                            f.write(chunk)
                            got += len(chunk)
                    if got < 10 * 1024:
                        raise RuntimeError("下载下来的文件太小，不对")
                    tmp.replace(dest)
                    return dest
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                log.info("这个地址没下载下来（%s）：%s", e, u[:90])
        raise RuntimeError(f"{what}失败：{last}")

    def _douyin_download(self, job: Job) -> None:
        fmt = job.opts.get("format") or "best"
        label, cap = FORMATS.get(fmt, FORMATS["best"])
        audio_only = cap is None
        res = self._douyin_detail(job, best=bool(job.opts.get("douyin_best", True)) and not audio_only and not cap)
        d = res["detail"]
        meta = dy.info(d)
        job.title = meta["desc"][:60] or f"抖音作品 {meta['id'] or douyin_id(job.url)}"
        name = safe_name(f"{meta['author']} - {meta['desc']}" if meta["author"] else (meta["desc"] or job.title)) \
            + f" [{meta['id'] or douyin_id(job.url)}]"
        headers = {"User-Agent": res.get("ua") or UA_PC, "Referer": "https://www.douyin.com/"}
        cookies = res.get("cookies") or {}
        pics = dy.images(d)
        if pics:
            folder = self.out_dir("视频") / name
            folder.mkdir(parents=True, exist_ok=True)
            n = len(pics)
            for i, p in enumerate(pics, 1):
                if p["urls"]:
                    ext = ".webp" if ".webp" in p["urls"][0].split("?")[0] else ".jpg"
                    self._http_get(job, p["urls"], folder / f"{i:02d}{ext}", headers, cookies,
                                   ((i - 1) / n, i / n), what=f"下载第 {i}/{n} 张图")
                if p["live"]:
                    self._http_get(job, p["live"], folder / f"{i:02d}_实况.mp4", headers, cookies,
                                   ((i - 1) / n, i / n), what=f"下载第 {i}/{n} 张实况照片")
            job.files = [str(folder)]
            job.note = f"图文作品：{n} 张原图（不带水印）"
            return
        pick = dy.pick_video(d, cap or 0, prefer_h264=bool(job.opts.get("prefer_h264")), smallest=audio_only)
        urls = pick["urls"] if pick else []
        if res.get("share_page") or not urls:
            urls = urls + dy.nowm_from_uri(d, ratio=f"{cap}p" if cap else "1080p")
        if not urls:
            raise RuntimeError("这个作品里没找到视频地址")
        if audio_only:
            tmp = Path(tempfile.mkdtemp(prefix="aiwtt_dy_"))
            try:
                video = self._http_get(job, urls, tmp / "v.mp4", headers, cookies, (0, 0.85), what="下载")
                dest = self.out_dir("视频") / f"{name}.{fmt}"
                job.step(f"把声音存成 {fmt.upper()}", 0.9)
                extract_audio(video, dest, fmt)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            job.files, job.note = [str(dest)], f"只要声音（{fmt.upper()}）"
            return
        dest = self.out_dir("视频") / f"{name}.mp4"
        self._http_get(job, urls, dest, headers, cookies)
        if pick and pick["height"]:
            job.note = f"{pick['width']}×{pick['height']}，{'H.265' if pick['codec'] == 'h265' else 'H.264'}，不带水印"
        else:
            job.note = f"不带水印（{cap or 1080}p 以内）" if res.get("share_page") else "不带水印"
        job.files = [str(dest)]

    # ---------- 其他网站（yt-dlp） ----------
    def _ydl(self, job: Job, extra_args: list[str], extra: dict | None = None):
        m = ytdl.load()
        if m is None:
            raise RuntimeError("没有下载组件 yt-dlp（重新运行 build.bat 安装，或者点“更新下载组件”）")
        args = ytdl.cli_args(job.opts.get("proxy") or "", job.opts.get("cookies") or "") + extra_args
        opts = ytdl.options(args, {"quiet": True, "no_warnings": True, "noprogress": True, **(extra or {})})
        return m, m.YoutubeDL(opts)

    def _hook(self, job: Job, label: str, base: float = 0.0, span: float = 1.0):
        """yt-dlp 的下载进度 → 任务进度。画面和声音分开下的：画面算 85%，声音算 15%。"""
        seen: list[str] = []

        def hook(d):
            if job.stop.is_set():
                m = ytdl.load()
                raise m.utils.DownloadCancelled("取消了") if m else Cancelled()
            if d.get("status") == "downloading":
                fn = d.get("filename") or ""
                if fn not in seen:
                    seen.append(fn)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                frac = min(1.0, got / total) if total else 0.0
                second = len(seen) >= 2
                job.stage = (f"{label}{'（声音）' if second else ''} {got / 1048576:.1f} MB"
                             + (f" / {total / 1048576:.1f} MB" if total else ""))
                job.progress = base + span * ((0.85 + 0.15 * frac) if second else 0.85 * frac)
            elif d.get("status") == "finished":
                job.stage = "下载完，正在合并画面和声音"
        return hook

    def _ytdlp_download(self, job: Job) -> None:
        out = self.out_dir("视频")
        tmpl = str(out / "%(uploader,channel|未知作者).30B - %(title).70B [%(id)s].%(ext)s")
        m, ydl = self._ydl(job, ytdlp_format_args(job.opts.get("format") or "best") + ["-o", tmpl],
                           {"progress_hooks": [self._hook(job, "下载")],
                            "postprocessor_hooks": [lambda d: setattr(job, "stage", "正在合并画面和声音")
                                                    if d.get("status") == "started" else None]})
        job.step("读视频信息")
        with ydl:
            info = ydl.extract_info(job.url, download=True)
        if info and info.get("entries"):
            info = next((e for e in info["entries"] if e), info)
        job.title = (info or {}).get("title") or job.url
        paths = [r.get("filepath") for r in (info or {}).get("requested_downloads") or [] if r.get("filepath")]
        if not paths and info:
            paths = [ydl.prepare_filename(info)]
        job.files = [p for p in paths if p and Path(p).exists()]
        if not job.files:
            raise RuntimeError("下载完了但没找到文件")
        fmt = job.opts.get("format") or "best"
        if FORMATS.get(fmt, FORMATS["best"])[1] is None:
            job.files = [str(Path(p).with_suffix(f".{fmt}")) if Path(p).with_suffix(f".{fmt}").exists() else p
                         for p in job.files]
            job.note = f"只要声音（{fmt.upper()}）"
            return
        h, w = (info or {}).get("height"), (info or {}).get("width")
        job.note = f"{w}×{h}" if h and w else ""

    def _download(self, job: Job) -> None:
        if job.site == "douyin":
            self._douyin_download(job)
        else:
            self._ytdlp_download(job)

    # ---------- 导出文案 ----------
    def _transcript(self, job: Job) -> None:
        lang = job.opts.get("lang") or "auto"
        tmp = Path(tempfile.mkdtemp(prefix="aiw_text_"))
        try:
            if job.site == "douyin":
                meta, segs, source = self._douyin_text(job, tmp, lang)
            else:
                meta, segs, source = self._ytdlp_text(job, tmp, lang)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        job.title = meta.get("title") or job.url
        body = subtitles.to_paragraphs(segs)
        if body and job.opts.get("polish"):
            job.step("用大模型加标点、分段", 0.92)
            try:
                body = polish(body, self.agent)
                source += "，大模型整理过标点和分段"
            except Exception as e:  # noqa: BLE001 —— 整理不了就用原来的
                log.warning("大模型整理文案失败：%s", e)
                job.warning = f"大模型整理标点没成功（{friendly_error(e)}），导出的是原始文字"
        rewritten = None
        if job.opts.get("rewrite"):
            src = body or (meta.get("desc") or "").strip()
            if not src:
                job.warning = "这个视频里没有人说话、也没有简介，没有可以改写的内容"
            else:
                job.step("用大模型改写成短视频文案", 0.94)
                try:
                    from . import rewrite as rw
                    rewritten = rw.rewrite(src, self.agent, desc=meta.get("desc") or "",
                                           extra=job.opts.get("rewrite_extra") or "",
                                           progress=lambda f: job.step(f"用大模型改写成短视频文案 {int(f * 100)}%", 0.94 + 0.05 * f))
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 —— 改写不了也把原文导出来
                    log.warning("大模型改写文案失败：%s", e, exc_info=True)
                    job.warning = f"原文导出了，但大模型改写没成功：{friendly_error(e)}"
        out = self.out_dir("文案")
        base = safe_name(meta.get("title") or "视频文案", 50) + (f" [{meta['id']}]" if meta.get("id") else "")
        lines = [f"标题：{meta.get('title') or ''}", f"作者：{meta.get('author') or ''}", f"地址：{job.url}"]
        if meta.get("duration"):
            lines.append(f"时长：{int(meta['duration']) // 60} 分 {int(meta['duration']) % 60} 秒")
        lines += [f"文字来源：{source}", f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
        if meta.get("desc"):
            lines += ["———— 视频简介 / 文案 ————", meta["desc"].strip(), ""]
        lines += ["———— 视频里说的话（原文）————", body or "（这个视频里没有人说话）", ""]
        if rewritten:
            from . import rewrite as rw
            lines += ["", "———— 改写后的文案（大模型按“改写规则”改写：内容和原文一致、更精简、原创表达、适合短视频发布）————", ""]
            if rewritten["titles"]:
                lines += ["备选标题："] + [f"{i}. {t}" for i, t in enumerate(rewritten["titles"], 1)] + [""]
            lines += ["正文：", rewritten["body"], ""]
            if rewritten["tags"]:
                lines += ["话题：" + " ".join(rewritten["tags"]), ""]
            lines += ["改写检查：" + rw.report(rewritten["check"]), ""]
            if rewritten["check"]["ratio"] < rw.MIN_RATIO:
                job.warning = "改写稿偏短，可能漏了内容：" + rw.report(rewritten["check"])
        txt = out / f"{base}.txt"
        txt.write_text("\n".join(lines), encoding="utf-8-sig")       # 带 BOM：Windows 记事本打开不乱码
        job.files = [str(txt)]
        if job.opts.get("srt") and segs:
            srt = out / f"{base}.srt"
            srt.write_text(subtitles.to_srt(segs), encoding="utf-8-sig")
            job.files.append(str(srt))
        if not job.note:
            job.note = source + ("，已改写" if rewritten else "")

    def _asr(self, job: Job, media: Path, lang: str, base: float) -> tuple[list[dict], str]:
        from .asr import transcribe
        size = job.opts.get("asr_model") or "base"
        job.step(f"本机语音转文字（Whisper {size}，视频越长越久）", base)
        segs, got = transcribe(str(media), size, lang,
                               progress=lambda f: job.step(f"本机语音转文字 {int(f * 100)}%", base + (0.9 - base) * f),
                               cancelled=job.stop.is_set)
        return segs, f"本机语音识别（Whisper {size}，语言 {got or '自动'}）"

    def _douyin_text(self, job: Job, tmp: Path, lang: str):
        res = self._douyin_detail(job, best=False)          # 只要声音：分享页最快
        d = res["detail"]
        meta = dy.info(d)
        meta["title"] = meta["desc"][:50] or f"抖音作品 {meta['id']}"
        headers = {"User-Agent": res.get("ua") or UA_PC, "Referer": "https://www.douyin.com/"}
        cookies = res.get("cookies") or {}
        for sub in dy.subtitle_urls(d):
            try:
                import requests
                r = requests.get(sub["url"], headers=headers, cookies=cookies, timeout=20, proxies=self._proxies(job))
                segs = subtitles.parse_any(r.text, sub["fmt"])
                if segs:
                    return meta, segs, "抖音自带的字幕"
            except Exception as e:  # noqa: BLE001
                log.info("抖音字幕没读到：%s", e)
        tried: list[str] = []
        media = self._listenable(job, d, res, headers, cookies, tmp, tried)
        if media is None and res.get("share_page"):
            # 分享页里的地址都没有声音：在后台单独开一个看不见的浏览器读网页数据，再试那边的地址
            try:
                res2 = self._douyin_guest(job, job.url, meta["id"] or douyin_id(job.url))
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                res2 = None
                tried.append(f"后台浏览器：{str(e)[:60]}")
            if res2:
                headers = {"User-Agent": res2.get("ua") or UA_PC, "Referer": "https://www.douyin.com/"}
                media = self._listenable(job, res2["detail"], res2, headers, res2.get("cookies") or {}, tmp, tried)
        if media is None:
            if dy.images(d) and not dy.video_choices(d):
                return meta, [], "图文作品（没有声音）"
            raise RuntimeError("这个作品的地址都试过了，下载下来的文件里都没有声音（" + "；".join(tried[-6:]) + "）")
        segs, source = self._asr(job, media, lang, 0.3)
        return meta, segs, source

    def _listenable(self, job: Job, d: dict, res: dict, headers: dict, cookies: dict, tmp: Path,
                    tried: list[str]) -> Path | None:
        """一个个试作品里的地址，返回第一个“下载下来确实有声音”的文件；都没有返回 None。"""
        from ..agent.listen import decode_audio, has_audio
        for i, (label, urls) in enumerate(dy.audio_candidates(d, share_page=bool(res.get("share_page")))):
            if job.stop.is_set():
                raise Cancelled()
            dest = tmp / f"listen_{len(tried)}_{i}.media"
            try:
                self._http_get(job, urls, dest, headers, cookies, (0.05, 0.3), what=f"下载声音（{label}）")
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                tried.append(f"{label}：没下载下来")
                log.info("转文字用的地址没下载下来（%s）：%s", label, e)
                continue
            ok = has_audio(str(dest))
            if ok is None:                       # 看不出来：直接解一下声音试试
                audio = decode_audio(str(dest), seconds=60)
                ok = audio is None or len(audio) > 0
            if ok:
                log.info("转文字用的声音：%s", label)
                return dest
            tried.append(f"{label}：只有画面没有声音")
            log.info("这个地址只有画面、没有声音（%s），换下一个", label)
            try:
                dest.unlink()
            except OSError:
                pass
        return None

    def _ytdlp_text(self, job: Job, tmp: Path, lang: str):
        m, ydl = self._ydl(job, ["--skip-download"])
        job.step("读视频信息和字幕列表", 0.05)
        with ydl:
            info = ydl.extract_info(job.url, download=False)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), info)
            info = info or {}
            meta = {"id": info.get("id") or "", "title": info.get("title") or "", "duration": info.get("duration") or 0,
                    "author": info.get("uploader") or info.get("channel") or "",
                    "desc": (info.get("description") or "")[:3000]}
            pick = choose_subtitle(info, lang)
            if pick:
                job.step(f"读字幕（{pick['label']}）", 0.4)
                try:
                    raw = ydl.urlopen(pick["url"]).read().decode("utf-8", "ignore")
                    segs = subtitles.parse_any(raw, pick["ext"])
                    if segs:
                        return meta, segs, pick["label"]
                except Exception as e:  # noqa: BLE001
                    log.info("字幕没读到（%s），改成语音转文字", e)
        m, ydl = self._ydl(job, ["-f", "ba/b", "-o", str(tmp / "a.%(ext)s")],
                           {"progress_hooks": [self._hook(job, "下载声音", 0.05, 0.25)]})
        with ydl:
            info2 = ydl.extract_info(job.url, download=True)
        from ..agent.listen import has_audio
        _ = info2
        media = self._newest(tmp, "a.")
        # 个别网站“最好的声音”那一项其实拿不到声音：换成“带声音的整个视频”再试
        for i, fmt in enumerate(("b", "bv*+ba/b")):
            if media is not None and has_audio(str(media)) is not False:
                break
            job.step("下载下来的文件里没有声音，换一种格式再下载", 0.2)
            m, ydl = self._ydl(job, ["-f", fmt, "--merge-output-format", "mp4/mkv", "-o", str(tmp / f"b{i}.%(ext)s")],
                               {"progress_hooks": [self._hook(job, "下载视频（用来听）", 0.05, 0.25)]})
            with ydl:
                ydl.extract_info(job.url, download=True)
            media = self._newest(tmp, f"b{i}.")
        if media is None:
            raise RuntimeError("声音没下载下来")
        if has_audio(str(media)) is False:
            raise RuntimeError("这个视频下载下来的文件里都没有声音（换了几种格式都一样）")
        segs, source = self._asr(job, media, lang, 0.3)
        return meta, segs, source

    @staticmethod
    def _newest(tmp: Path, prefix: str) -> Path | None:
        files = [f for f in tmp.glob(prefix + "*") if not f.name.endswith((".part", ".ytdl"))]
        return max(files, key=lambda p: p.stat().st_size) if files else None


def ytdlp_format_args(fmt: str) -> list[str]:
    """下载格式 → yt-dlp 的参数（和 en.loader.to 这类网站的选项一样）。"""
    label, cap = FORMATS.get(fmt, FORMATS["best"])
    if cap is None:
        if fmt == "m4a":
            return ["-f", "ba[ext=m4a]/ba/b", "-x", "--audio-format", "m4a"]
        return ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if cap:
        return ["-f", f"bv*[height<={cap}]+ba/b[height<={cap}]/bv*+ba/b", "--merge-output-format", "mp4/mkv"]
    return ["-f", "bv*+ba/b", "--merge-output-format", "mp4/mkv"]


def extract_audio(src: Path, dest: Path, fmt: str) -> Path:
    """用软件自带的 ffmpeg 把视频里的声音存成 MP3 / M4A。"""
    import subprocess
    import sys
    exe = ytdl.ffmpeg_path()
    if not exe:
        raise RuntimeError("没有 ffmpeg，存不了声音文件")
    codec = ["-c:a", "aac", "-b:a", "192k"] if fmt == "m4a" else ["-c:a", "libmp3lame", "-q:a", "2"]
    tmp = dest.with_name(dest.stem + ".part" + dest.suffix)
    r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(src), "-vn", *codec, str(tmp)],
                       capture_output=True, timeout=1800, creationflags=0x08000000 if sys.platform == "win32" else 0)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
        raise RuntimeError("这个视频里的声音存不出来：" + (r.stderr or b"").decode("utf-8", "replace").strip()[-150:])
    tmp.replace(dest)
    return dest


def choose_subtitle(info: dict, lang: str = "auto") -> dict | None:
    """在 yt-dlp 读到的字幕里挑一个：先人工字幕、后自动字幕；语言按设置（自动 = 视频本来的语言 > 中文 > 英文）。"""
    orig = str(info.get("language") or "").split("-")[0]
    order = []
    if lang in ("zh", "en"):
        order += list(SUB_LANGS[lang])
    else:
        if orig:
            order += [orig, f"{orig}-orig"] + list(SUB_LANGS.get(orig, ()))
        order += list(SUB_LANGS["zh"]) + list(SUB_LANGS["en"])
    order = list(dict.fromkeys(order))
    for key, label in (("subtitles", "平台字幕"), ("automatic_captions", "平台自动字幕")):
        subs = info.get(key) or {}
        if not subs:
            continue
        langs = [lg for lg in order if lg in subs] + ([] if lang in ("zh", "en") or key == "automatic_captions"
                                                       else [lg for lg in subs if lg != "live_chat"])
        for lg in langs:
            fmts = [f for f in subs.get(lg) or [] if isinstance(f, dict) and f.get("url")]
            fmts.sort(key=lambda f: SUB_EXTS.index(f.get("ext")) if f.get("ext") in SUB_EXTS else 99)
            if fmts:
                f = fmts[0]
                if key == "automatic_captions" and "tlang=" in f["url"] and lg not in (orig, f"{orig}-orig"):
                    continue          # 机器翻译出来的自动字幕不用（不准）
                return {"url": f["url"], "ext": f.get("ext") or "", "lang": lg, "label": f"{label}（{lg}）"}
    return None


def polish(text: str, agent) -> str:
    """用大模型给语音转写加标点、分段、改明显的同音错字，不增不减内容。长的分段处理。"""
    if agent is None:
        raise RuntimeError("没有可用的大模型")
    router = agent.brain.router
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) > 2400 and cur:
            chunks.append(cur)
            cur = ""
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        chunks.append(cur)
    out = []
    for c in chunks:
        r = router.call("write", "你是认真细致的文字编辑。",
                        "下面是一段视频语音的自动转写。请只做这几件事：加上标点、按意思分段、改正明显的同音错别字；"
                        "不要增加、删减或改写内容，不要总结，不要加标题。只输出整理后的文字。\n\n" + c,
                        max_tokens=min(4000, len(c) * 2 + 200), temperature=0.1)
        out.append((r.text or "").strip() or c)
    return "\n\n".join(out)


def friendly_error(e: Exception) -> str:
    """把常见的下载错误说成人话。"""
    s = str(e) or type(e).__name__
    low = s.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube 要求登录确认不是机器人：在设置里填一个导出的 cookies.txt 再试"
    if "private video" in low or "video is private" in low or "this video is private" in low:
        return "这个视频是私密的，看不了"           # yt-dlp 的英文报错（自己写的中文原因原样显示，不改写）
    if "unsupported url" in low:
        return "这个网址不支持下载（不是视频页面，或者网站暂时不支持）"
    if "js runtime" in low or "javascript runtime" in low or "challenge" in low and "youtube" in low:
        return "下载 YouTube 需要 JavaScript 运行环境：点上面的“安装 deno”再试"
    if "timed out" in low or "timeout" in low or "connection" in low or "proxy" in low:
        return f"网络连不上（{s.splitlines()[0][:120]}）：检查网络；YouTube 等要在设置里填代理"
    if "ffmpeg" in low:
        return "合并画面和声音时出错（ffmpeg）：" + s.splitlines()[0][:120]
    return s.splitlines()[0][:200]
