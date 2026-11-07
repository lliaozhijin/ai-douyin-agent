# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：生成 dist\aiWannaTikTok\aiWannaTikTok.exe（文件夹模式，启动快，方便 Playwright 使用）
from PyInstaller.utils.hooks import collect_all

import json

datas = [("web", "web")]  # 界面
# 桌宠默认形象：只打包 rig.json 里用到的部件图（文件夹里别的旧文件不会被带进安装包）
_rig = json.load(open("assets/pet/default/rig.json", encoding="utf-8"))
datas += [("assets/pet/default/rig.json", "assets/pet/default")]
datas += [(f"assets/pet/default/{name}.png", "assets/pet/default") for name in _rig["parts"]]
binaries = []
hiddenimports = []
for pkg in ("webview", "playwright"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# 桌宠换形象时的本机抠图（onnxruntime）：装了就一起打包进去
import importlib.util
for pkg in ("onnxruntime",):
    if importlib.util.find_spec(pkg) is not None:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="aiWannaTikTok",
    console=False,
    icon="assets/app.ico",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="aiWannaTikTok")
