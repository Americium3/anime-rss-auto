# Shared plumbing for probe.ps1 / shot.ps1 / states.ps1.
#
# Two rules the rest of these scripts are built around:
#
#   1. Never open the live panel. Everything here serves static/ from a
#      throwaway HTTP server on its own port and loads it with ?fixture, which
#      replaces the network inside the page with a canned dataset and refuses
#      every non-GET. The panel's mutating routes delete files and write
#      bangumi collections; a validation run must be unable to reach one.
#
#   2. Never touch the real browser profile. Every launch gets a fresh
#      --user-data-dir under $env:TEMP, which is also the only way headless
#      Chrome starts reliably here — pointed at a real profile it hangs in a
#      sign-in retry loop, and that profile belongs to a browser that is open.

$ErrorActionPreference = "Stop"

function Get-RepoRoot { Split-Path -Parent $PSScriptRoot }

function Find-Chrome {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    throw "No Chrome or Edge found. Set `$env:CHROME_PATH to a browser executable."
}

function Get-FreePort {
    $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $l.Start()
    $port = $l.LocalEndpoint.Port
    $l.Stop()
    return $port
}

# Serves static/ only. Not webui.py: this must not be able to answer an API
# call even if the fixture stub were somehow bypassed.
function Start-Bench {
    $root = Get-RepoRoot
    $port = Get-FreePort
    $proc = Start-Process -FilePath "python" `
        -ArgumentList @((Join-Path $root "scripts\bench_server.py"), "$port") `
        -PassThru -WindowStyle Hidden
    # Wait for it to answer rather than sleeping a guessed amount.
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$port/index.html" -UseBasicParsing `
                -TimeoutSec 2 -Method Head | Out-Null
            return [pscustomobject]@{ Port = $port; Process = $proc }
        } catch { Start-Sleep -Milliseconds 150 }
    }
    try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch {}
    throw "bench server on port $port never answered"
}

function Stop-Bench($bench) {
    if ($null -eq $bench) { return }
    try { Stop-Process -Id $bench.Process.Id -Force -ErrorAction Stop } catch {}
}

# Start-Process, always. Calling the browser directly from this shell gets the
# child swallowed with no output and no error, which looks exactly like a page
# that rendered nothing.
function Invoke-Chrome {
    # NOT $Args. That is an automatic variable in PowerShell, and declaring it
    # as a parameter binds without complaint while the value the body reads is
    # not the one that was passed — the browser is launched with no URL, makes
    # no request at all, and sits there until the timeout. Indistinguishable
    # from a page that hangs.
    param([string[]]$ChromeArgs, [int]$TimeoutSec = 90)
    $chrome = if ($env:CHROME_PATH) { $env:CHROME_PATH } else { Find-Chrome }
    $profileDir = Join-Path $env:TEMP ("autopilot-bench-" + [guid]::NewGuid().ToString("N"))
    $stdout = Join-Path $env:TEMP ("autopilot-out-" + [guid]::NewGuid().ToString("N") + ".txt")
    $stderr = "$stdout.err"
    $base = @(
        "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync", "--no-sandbox",
        "--user-data-dir=$profileDir"
    )
    $p = Start-Process -FilePath $chrome -ArgumentList ($base + $ChromeArgs) -PassThru `
        -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    # HasExited, not WaitForExit($ms). A Process handed back by Start-Process
    # -PassThru has no cached wait handle, so the timed WaitForExit overload
    # returns $false forever even though the process finished in milliseconds —
    # which looks exactly like a page that hangs, and cost an afternoon once.
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not $p.HasExited) {
        if ((Get-Date) -gt $deadline) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch {}
            throw "chrome did not exit within $TimeoutSec s"
        }
        Start-Sleep -Milliseconds 80
    }
    Start-Sleep -Milliseconds 120     # let the redirected stdout finish flushing
    $text = if (Test-Path $stdout) { Get-Content $stdout -Raw -Encoding UTF8 } else { "" }
    Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
    Remove-Item $profileDir -Recurse -Force -ErrorAction SilentlyContinue
    return $text
}

function New-ShotDir {
    $dir = Join-Path (Get-RepoRoot) "scripts\shots"
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    return $dir
}
