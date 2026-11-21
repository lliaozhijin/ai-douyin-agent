; AI也要刷抖音 安装包脚本（Inno Setup 6）
; 由 build.bat 自动调用，生成 installer\Output\aiWannaTikTok_Setup_v0.4.0.exe

#define AppName "AI也要刷抖音"
#define AppVersion "0.4.0"
#define AppNameEn "aiWannaTikTok"
#define AppExe "aiWannaTikTok.exe"

[Setup]
AppId={{8C4F2E7A-5B1D-4E3A-9F6C-2D7B8A1E4C90}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=liaozhijin
AppVerName={#AppName}（{#AppNameEn}）{#AppVersion}
VersionInfoProductName={#AppNameEn}
DefaultDirName={code:DefaultInstallDir}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename={#AppNameEn}_Setup_v{#AppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

#if FileExists(CompilerPath + "Languages\ChineseSimplified.isl")
[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#else
[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他："

[Files]
Source: "..\dist\aiWannaTikTok\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; DestName: "使用说明.md"; Flags: ignoreversion
#if FileExists(SourcePath + "redist\vc_redist.x64.exe")
; 微软官方的 VC++ 运行库安装程序（build.bat 下载）：电脑上的太旧（低于 14.40）才解压、才运行。
; 太旧的话，抠图组件和听声音的组件一启动就会崩溃。
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: VCRedistNeeded
#endif

[InstallDelete]
; 更早的版本程序文件名是 YuyuGirl.exe，升级时删掉，免得安装目录里留两个程序
Type: files; Name: "{app}\YuyuGirl.exe"
; 0.1.0 早期版本叫 aiWatchTikTok.exe，改名后删掉旧的
Type: files; Name: "{app}\aiWatchTikTok.exe"
; 0.2.0 起去掉了小黄鸭形象和语音转文字：升级时删掉旧版本留下的文件
Type: filesandordirs; Name: "{app}\_internal\assets\pet\duck"
Type: filesandordirs; Name: "{app}\_internal\assets\pet\default"
Type: filesandordirs; Name: "{app}\_internal\faster_whisper"
Type: filesandordirs; Name: "{app}\_internal\ctranslate2"
Type: filesandordirs; Name: "{app}\_internal\av"

[Dirs]
; 数据文件夹：SQLite 数据库、头像、抖音登录状态都在这里。卸载时不会删除。
Name: "{app}\data"; Permissions: users-modify

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
#if FileExists(SourcePath + "redist\vc_redist.x64.exe")
; 要管理员权限：用 shellexec 运行，Windows 会弹出确认窗口
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /passive /norestart"; StatusMsg: "正在更新 Microsoft VC++ 运行库……"; Flags: shellexec waituntilterminated; Check: VCRedistNeeded
#endif
Filename: "{app}\{#AppExe}"; Description: "立即启动{#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function VCRedistNeeded: Boolean;
var
  MS, LS: Cardinal;
begin
  { msvcp140.dll 低于 14.40（VS 2022 17.10）时，新版 onnxruntime、ctranslate2 一启动就访问冲突崩溃 }
  if GetVersionNumbers(ExpandConstant('{sys}\msvcp140.dll'), MS, LS) then
    Result := MS < ((14 shl 16) or 40)
  else
    Result := True;
end;

function GetDriveType(lpRootPathName: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';

function DefaultInstallDir(Param: String): String;
begin
  { 只有 D 盘是本机硬盘时才默认装到 D 盘；D 盘是光驱、读卡器、网络盘或者没有 D 盘，就装到当前用户的程序目录 }
  if DirExists('D:\') and (GetDriveType('D:\') = 3) then
    Result := 'D:\{#AppName}'
  else
    Result := ExpandConstant('{localappdata}\Programs\{#AppName}');
end;
