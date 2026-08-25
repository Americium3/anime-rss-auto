# Screenshots with the hover and focus states painted in.
#
#   pwsh scripts\states.ps1                      # every scene, both themes
#   pwsh scripts\states.ps1 -Scene gauge         # just the instrument
#
# An ordinary screenshot cannot show a hover effect — nothing is under the
# pointer, so every :hover rule is inert and the shot shows the resting state
# of exactly the controls whose behaviour was the thing to look at. This drives
# static/_states.html, which dispatches real pointer events for the
# script-driven states and rewrites the frame's own :hover / :focus rules onto
# attributes for the CSS-driven ones.
#
# Scenes:
#   shelf      a volume hovered with its peek layer out, lit across the desk
#   gauge      the sounding mid-scrub, drift arrows released
#   long       the compressed 52-episode scale, mid-scrub
#   timetable  a day column lit under the pointer
#   picker     the subgroup picker open
#
# Fixture-driven; the live panel is never opened. See scripts/_bench.ps1.

[CmdletBinding()]
param(
    [ValidateSet("shelf", "gauge", "long", "timetable", "picker", "all")]
    [string]$Scene = "all",
    [ValidateSet("en", "zh", "both")] [string]$Lang = "en",
    [ValidateSet("dark", "light", "both")] [string]$Theme = "both",
    [int]$Width = 1920,
    [int]$Height = 1400
)

. (Join-Path $PSScriptRoot "_bench.ps1")

$scenes = if ($Scene -eq "all") { @("shelf", "gauge", "long", "timetable", "picker") } else { @($Scene) }
$langs  = if ($Lang -eq "both") { @("en", "zh") } else { @($Lang) }
$themes = if ($Theme -eq "both") { @("dark", "light") } else { @($Theme) }
$dir = New-ShotDir

$bench = $null
try {
    $bench = Start-Bench
    foreach ($s in $scenes) {
        foreach ($l in $langs) {
            foreach ($th in $themes) {
                $name = "state-$s-$l-$th.png"
                $path = Join-Path $dir $name
                $url = "http://127.0.0.1:$($bench.Port)/_states.html?" +
                       "scene=$s&lang=$l&theme=$th&w=$Width&h=$Height"
                # Reduced motion here too: the states page forces the *end*
                # state of every hover rule, and an entry animation still
                # mid-flight would paint over it at whatever opacity the first
                # frame happened to land on.
                Invoke-Chrome -ChromeArgs @(
                    "--screenshot=$path",
                    "--window-size=$Width,$Height",
                    "--force-prefers-reduced-motion",
                    "--hide-scrollbars",
                    "--virtual-time-budget=10000",
                    $url
                ) | Out-Null
                if (Test-Path $path) {
                    Write-Host "  $name  ($((Get-Item $path).Length) bytes)" -ForegroundColor Green
                } else {
                    Write-Host "  $name  NOT WRITTEN" -ForegroundColor Red
                }
            }
        }
    }
} finally {
    Stop-Bench $bench
}
Write-Host "`nshots in $dir"
