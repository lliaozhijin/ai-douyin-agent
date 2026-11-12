@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title AI也要刷抖音 一键打包

echo ==================================================
echo   AI也要刷抖音 一键打包
echo   会依次：准备 Python 环境 - 安装依赖 - 运行测试 - 打包程序 - 生成安装包
echo   需要联网，第一次大约 10 分钟
echo ==================================================
echo.

rem ---------- 1. 找 Python 3.11 / 3.12 ----------
rem 注意：Python 3.10.0 有已知 bug，打包工具 PyInstaller 无法在它上面工作，所以只用 3.11 或 3.12
set "PY="
call :try_py py -3.12
if not defined PY call :try_py py -3.11
if not defined PY call :try_py "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :try_py "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :try_py python
if not defined PY (
  echo 没找到 Python 3.11 或 3.12，正在用 winget 自动安装 Python 3.12，只装给当前用户，不影响电脑上已有的 Python...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  call :try_py "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if not defined PY call :try_py py -3.12
)
if not defined PY (
  echo [错误] 自动安装 Python 3.12 没有成功。
  echo 请到 https://www.python.org/downloads/ 手动下载安装 Python 3.12，然后重新运行本脚本。
  pause
  exit /b 1
)
echo [1/6] 使用 Python：%PY%

rem ---------- 2. 虚拟环境和依赖 ----------
rem 之前用不合适的 Python 建的 .venv 要删掉重建
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; v=sys.version_info[:3]; assert (3,11,0) <= v < (3,13,0)" >nul 2>&1 || (
    echo 旧的 .venv 用的 Python 版本不合适，删除后重建...
    rmdir /s /q ".venv"
  )
)
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto :fail
)
set "VPY=.venv\Scripts\python.exe"

rem 开着代理软件时，系统代理地址可能是 https:// 开头，pip 不支持，这里统一改成 http://
set "FIXED_PROXY="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "tools\detect_proxy.ps1"`) do set "FIXED_PROXY=%%P"
if defined FIXED_PROXY set "HTTP_PROXY=%FIXED_PROXY%"
if defined FIXED_PROXY set "HTTPS_PROXY=%FIXED_PROXY%"
if defined FIXED_PROXY echo 检测到代理，下载依赖时使用 %FIXED_PROXY%

echo [2/6] 安装依赖...
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 goto :pip_mirror
goto :pip_done

:pip_mirror
echo.
echo 安装依赖失败，改用清华镜像、不走代理再试一次...
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "NO_PROXY=*"
"%VPY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt -r requirements-build.txt || goto :fail
set "NO_PROXY="
if defined FIXED_PROXY set "HTTP_PROXY=%FIXED_PROXY%"
if defined FIXED_PROXY set "HTTPS_PROXY=%FIXED_PROXY%"

:pip_done


rem ---------- 3. 可爱字体（下载失败也没关系，会用系统字体） ----------
if not exist "web\assets\fonts\ZCOOLKuaiLe-Regular.ttf" (
  echo [3/6] 下载站酷快乐体字体...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { New-Item -ItemType Directory -Force 'web\assets\fonts' | Out-Null; Invoke-WebRequest -UseBasicParsing 'https://github.com/google/fonts/raw/main/ofl/zcoolkuaile/ZCOOLKuaiLe-Regular.ttf' -OutFile 'web\assets\fonts\ZCOOLKuaiLe-Regular.ttf'; Write-Host '字体下载完成' } catch { Write-Host '字体下载失败，将使用系统字体' }"
) else (
  echo [3/6] 字体已存在
)

rem ---------- 3.5 语音模型（打进安装包，用户不用再下载；下载失败也没关系，软件第一次用时会再下载） ----------
if not exist "assets\models\whisper\faster-whisper-base\model.bin" (
  echo [3/6] 下载语音模型 base（约 145MB，只需要下载一次）...
  "%VPY%" tools\fetch_whisper.py base
) else (
  echo [3/6] 语音模型已存在
)

rem ---------- 4. 测试 ----------
echo [4/6] 运行测试...
"%VPY%" -m unittest discover -s tests || goto :fail

rem ---------- 5. 打包程序 ----------
echo [5/6] 打包程序...
"%VPY%" -m PyInstaller --noconfirm --clean aiWannaTikTok.spec || goto :fail

rem ---------- 6. 生成安装包 ----------
echo [6/6] 生成安装包...
call :find_iscc
if not defined ISCC (
  echo 没找到 Inno Setup，尝试用 winget 自动安装...
  winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
  call :find_iscc
)
if not defined ISCC (
  echo.
  echo [提示] 没能安装 Inno Setup，所以这次没有生成安装包。
  echo 程序已经打包好了，在 dist\aiWannaTikTok\aiWannaTikTok.exe，可以直接双击运行。
  echo 想要安装包的话，请到 https://jrsoftware.org/isdl.php 安装 Inno Setup 6，再运行一次本脚本。
  pause
  exit /b 0
)
"%ISCC%" "installer\aiWannaTikTok.iss" || goto :fail

echo.
echo ==================================================
echo   完成！安装包在 installer\Output 文件夹里
echo ==================================================
explorer "installer\Output"
pause
exit /b 0

:try_py
%* -c "import sys; v=sys.version_info[:3]; assert (3,11,0) <= v < (3,13,0)" >nul 2>&1 && set "PY=%*"
exit /b 0

:find_iscc
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
exit /b 0

:fail
echo.
echo [失败] 上面的步骤出错了。请把这个窗口的内容截图发给开发者。
pause
exit /b 1
