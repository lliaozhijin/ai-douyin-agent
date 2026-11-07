"""作品卡片底部署名用的小头像：你上传的头像；没上传就用软件默认头像（和界面上的默认头像一样）。"""
from __future__ import annotations

from PIL import Image

from ..paths import AVATAR_DIR, WEB_DIR

DEFAULT_AVATAR = WEB_DIR / "assets" / "mascot.png"  # 由 mascot.svg 导出


def persona_avatar(persona: dict):
    name = (persona or {}).get("avatar")
    if name and (AVATAR_DIR / name).exists():
        try:
            return Image.open(AVATAR_DIR / name).convert("RGBA")
        except OSError:
            pass
    try:
        return Image.open(DEFAULT_AVATAR).convert("RGBA")
    except OSError:
        return None
