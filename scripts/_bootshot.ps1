# Temporary: the opening overlay held with ?boothold, both themes.
. (Join-Path $PSScriptRoot "_bench.ps1")
$dir = New-ShotDir
$bench = $null
try {
    $bench = Start-Bench
    foreach ($th in @("dark", "light")) {
        $path = Join-Path $dir "boot-zh-$th.png"
        $url = "http://127.0.0.1:$($bench.Port)/index.html?fixture=demo&lang=zh&theme=$th&boothold"
        Invoke-Chrome -ChromeArgs @("--screenshot=$path", "--window-size=1920,1400",
            "--force-prefers-reduced-motion", "--hide-scrollbars", "--virtual-time-budget=6000", $url) | Out-Null
        Write-Host "  boot-zh-$th.png"
    }
} finally { Stop-Bench $bench }
