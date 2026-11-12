"""统一管理程序目录和数据目录。

安装后的目录结构：
    <安装目录>/AI也要刷抖音.exe
    <安装目录>/_internal/          程序依赖（PyInstaller 生成）
    <安装目录>/data/app.db         SQLite 数据库
    <安装目录>/data/avatars/       上传的头像
    <安装目录>/data/browser_profile/  抖音登录状态
    <安装目录>/data/logs/          运行日志
    <安装目录>/data/debug/         调试用的页面截图和 HTML
    <安装目录>/data/drafts/        作品草稿的图片和视频
    <安装目录>/data/pet/           你上传的桌宠形象
    <安装目录>/data/stickers/      她发过的表情包（只留最近 60 张）
    <安装目录>/_internal/assets/models/whisper/  安装包自带的语音模型（base）
    <安装目录>/data/models/        抠图模型、其他大小的语音模型（第一次用时下载）
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    """程序所在目录：打包后是 exe 所在目录，开发时是项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """界面文件等只读资源所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_root() -> Path:
    """数据目录：默认是程序旁边的 data 文件夹（装在 D 盘等普通位置时）。

    程序被放在了没有写入权限的地方（比如手动复制到 C:\\Program Files），就改用
    %LOCALAPPDATA%\\aiWannaTikTok\\data，保证换哪台电脑都能正常保存。"""
    import os

    d = app_root() / "data"
    if _writable(d):
        return d
    alt = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "aiWannaTikTok" / "data"
    alt.mkdir(parents=True, exist_ok=True)
    return alt


ROOT = app_root()
DATA_DIR = data_root()
DB_PATH = DATA_DIR / "app.db"
AVATAR_DIR = DATA_DIR / "avatars"
PROFILE_DIR = DATA_DIR / "browser_profile"
LOG_DIR = DATA_DIR / "logs"
DEBUG_DIR = DATA_DIR / "debug"
FRAMES_DIR = DATA_DIR / "frames"
SELECTORS_FILE = DATA_DIR / "selectors.json"
SESSION_FILE = DATA_DIR / "douyin_session.bin"  # 抖音登录状态的加密备份（退出登录时删除）
DRAFTS_DIR = DATA_DIR / "drafts"      # 作品草稿的图片 / 视频（每个草稿一个文件夹）
PET_DIR = DATA_DIR / "pet"            # 你上传的桌宠形象（抠好的图、拆好的部件）
STICKER_DIR = DATA_DIR / "stickers"   # 她发过的表情包（自动清理旧的）
MODELS_DIR = DATA_DIR / "models"      # 第一次用时下载的模型（抠图）
WEB_DIR = resource_root() / "web"
ASSETS_DIR = resource_root() / "assets"
FONT_FILE = WEB_DIR / "assets" / "fonts" / "ZCOOLKuaiLe-Regular.ttf"


def ensure_dirs() -> None:
    for d in (DATA_DIR, AVATAR_DIR, PROFILE_DIR, LOG_DIR, DEBUG_DIR, FRAMES_DIR, DRAFTS_DIR, PET_DIR, MODELS_DIR, STICKER_DIR):
        d.mkdir(parents=True, exist_ok=True)
