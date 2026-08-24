# Screenshots of the panel, in every language and lighting.
#
#   pwsh scripts\shot.ps1                    # 4 shots: en/zh x dark/light
#   pwsh scripts\shot.ps1 -Tab schedule      # the timetable instead of the shelf
#   pwsh scripts\shot.ps1 -Width 1400        # below the room's 1800px breakpoint
#
# Entry animations are frozen with --force-prefers-reduced-motion. Without it
# headless captures the first frame of a waterfall that will never advance, so
# every card is at opacity 0 and the shot looks like a page that did not
# render — which is indistinguishable from a page that really did not.
#
# Driven by the fixture dataset, not the live panel. See scripts/_bench.ps1.

[CmdletBinding()]
param(
    [ValidateSet("en", "zh", "both")] [string]$Lang = "both",
    [ValidateSet("dark", "light", "both")] [string]$Theme = "both",
    [string]$Tab = "watching",
    [int]$Width = 1920,
    [int]$Height = 1400
)

. (Join-Path $PSScriptRoot "_bench.ps1")

$langs  = if ($Lang -eq "both") { @("en", "zh") } else { @($Lang) }
$themes = if ($Theme -eq "both") { @("dark", "light") } else { @($Theme) }
$dir = New-ShotDir

$bench = $null
try {
    $bench = Start-Bench
    foreach ($l in $langs) {
        foreach ($th in $themes) {
            $name = "panel-$Tab-$l-$th-$Width.png"
            $path = Join-Path $dir $name
            $url = "http://127.0.0.1:$($bench.Port)/index.html?" +
                   "fixture=demo&lang=$l&theme=$th&tab=$Tab"
            Invoke-Chrome -ChromeArgs @(
                "--screenshot=$path",
                "--window-size=$Width,$Height",
                "--force-prefers-reduced-motion",
                "--hide-scrollbars",
                "--virtual-time-budget=8000",
                $url
            ) | Out-Null
            if (Test-Path $path) {
                Write-Host "  $name  ($((Get-Item $path).Length) bytes)" -ForegroundColor Green
            } else {
                Write-Host "  $name  NOT WRITTEN" -ForegroundColor Red
            }
        }
    }
} finally {
    Stop-Bench $bench
}
Write-Host "`nshots in $dir"
