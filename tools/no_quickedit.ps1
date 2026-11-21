# Turn off QuickEdit for this console window (called by build.bat).
# With QuickEdit on, a mouse click inside the window starts a text selection (the title then starts
# with "Select") and the running program freezes until Esc / Enter is pressed - the build looks stuck.
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page.
$sig = @'
[DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr GetStdHandle(int nStdHandle);
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool GetConsoleMode(IntPtr hConsole, out uint mode);
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool SetConsoleMode(IntPtr hConsole, uint mode);
'@
try {
    $k = Add-Type -MemberDefinition $sig -Name ConsoleModeTools -Namespace AiWtt -PassThru
    $h = $k::GetStdHandle(-10)          # STD_INPUT_HANDLE: the console input buffer shared with build.bat
    $mode = [uint32]0
    if ($k::GetConsoleMode($h, [ref]$mode)) {
        if ($mode -band 0x40) { $mode = $mode - 0x40 }   # ENABLE_QUICK_EDIT_MODE off
        $mode = $mode -bor 0x80                           # ENABLE_EXTENDED_FLAGS (needed for the change to stick)
        [void]$k::SetConsoleMode($h, $mode)
    }
} catch { }
exit 0
