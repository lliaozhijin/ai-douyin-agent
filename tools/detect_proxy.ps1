# Detect the proxy from env vars or Windows system settings and print it as http://host:port
# (pip cannot use proxy addresses that start with https://). Prints nothing when no proxy is set.
$p = $env:HTTPS_PROXY
if (-not $p) { $p = $env:HTTP_PROXY }
if (-not $p) {
  $s = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
  if ($s -and $s.ProxyEnable -eq 1 -and $s.ProxyServer) {
    $p = $s.ProxyServer
    if ($p -match '=') {
      $parts = @{}
      foreach ($kv in ($p -split ';')) {
        $a = $kv -split '=', 2
        if ($a.Count -eq 2) { $parts[$a[0].Trim().ToLower()] = $a[1].Trim() }
      }
      $p = $parts['https']
      if (-not $p) { $p = $parts['http'] }
    }
  }
}
if ($p) {
  $p = ($p -replace '^\s*https?://', '').Trim().TrimEnd('/')
  if ($p) { Write-Output ("http://" + $p) }
}
