# Assertion run against the panel. Exits non-zero if anything failed.
#
#   pwsh scripts\probe.ps1              # both languages, both themes
#   pwsh scripts\probe.ps1 -Lang zh     # one language
#
# What it does: serves static/ on a throwaway port, opens static/_probe.html in
# headless Chrome, and reads back the results the probe page wrote. The probe
# page drives the panel with real events inside a same-origin iframe — it does
# not open the live panel on :8767 and it cannot reach a mutating route (see
# scripts/_bench.ps1 and the ?fixture stub in index.html).
#
# The last pass injects a fault into one panel and asserts the other panels
# still draw. That regression is the reason the guard exists.

[CmdletBinding()]
param(
    [ValidateSet("en", "zh", "both")] [string]$Lang = "both",
    [ValidateSet("dark", "light", "both")] [string]$Theme = "both"
)

. (Join-Path $PSScriptRoot "_bench.ps1")

$langs  = if ($Lang -eq "both") { @("en", "zh") } else { @($Lang) }
$themes = if ($Theme -eq "both") { @("dark", "light") } else { @($Theme) }

$bench = $null
$failed = 0
$ran = 0
try {
    $bench = Start-Bench
    Write-Host "bench serving static/ on 127.0.0.1:$($bench.Port)" -ForegroundColor DarkGray

    $runs = @()
    foreach ($l in $langs) {
        foreach ($th in $themes) {
            $runs += [pscustomobject]@{ Label = "$l/$th"; Query = "lang=$l&theme=$th" }
        }
    }
    # The guard regression, once. It asserts a structural property (the panels
    # after the broken one still draw), which does not vary by language — but
    # the fault notice itself does, so it runs in whichever language was asked
    # for first to prove the notice is not a bare key in that language either.
    $runs += [pscustomobject]@{
        Label = "fault:upcoming/$($langs[0])"
        Query = "lang=$($langs[0])&theme=dark&fault=upcoming"
    }

    foreach ($run in $runs) {
        $url = "http://127.0.0.1:$($bench.Port)/_probe.html?$($run.Query)"
        Write-Host "`n=== $($run.Label) ===" -ForegroundColor Cyan
        # --virtual-time-budget fast-forwards the page's own timers so the
        # boot floor and the peek dwell resolve immediately. It also collapses
        # network waiting, which is fine here: the fixture answers from memory,
        # so there is no load ordering left to measure.
        $dom = Invoke-Chrome -ChromeArgs @(
            "--dump-dom", "--virtual-time-budget=20000",
            "--window-size=1920,1200", $url
        )
        $ran++
        if ($dom -notmatch "PROBE-RESULT") {
            Write-Host "  no result written — the probe page never finished" -ForegroundColor Red
            if ($dom.Length -lt 4000) { Write-Host $dom }
            $failed++
            continue
        }
        # The <pre> is the whole report; everything before it is the page.
        $body = [regex]::Match($dom, '(?s)<pre id="out">(.*?)</pre>').Groups[1].Value
        $body = [System.Net.WebUtility]::HtmlDecode($body)
        foreach ($line in ($body -split "`n")) {
            $trimmed = $line.Trim()
            if (-not $trimmed) { continue }
            if ($trimmed.StartsWith("FAIL")) { Write-Host "  $trimmed" -ForegroundColor Red }
            elseif ($trimmed.StartsWith("PASS")) { Write-Host "  $trimmed" -ForegroundColor DarkGreen }
            else { Write-Host "  $trimmed" -ForegroundColor Yellow }
        }
        if ($body -match "PROBE-RESULT FAIL") { $failed++ }
    }
} finally {
    Stop-Bench $bench
}

Write-Host ""
if ($failed -gt 0) {
    Write-Host "$failed of $ran probe run(s) FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "all $ran probe run(s) passed" -ForegroundColor Green
exit 0
