<#
.SYNOPSIS
    One trading day of VN30 futures capture: record the session, then backfill and curate.

.DESCRIPTION
    Intended for a daily Scheduled Task. Every session that is not recorded is lost
    permanently -- the trade endpoint serves only the current day, board snapshots are
    not reproducible at all, and contract-level history disappears at expiry.

    Fire this EARLY in local time. The recorder waits for the Hanoi open itself, against
    the exchange clock, so the task does not need re-timing when local daylight saving
    starts or ends while Vietnam stays on UTC+7 year round.

    Weekends and post-close starts exit on their own without recording.

.PARAMETER RepoRoot
    Project root holding the `vn30f` package. Defaults to the main working copy.
    Point this at the permanent working copy, not a temporary checkout: the task
    breaks silently when that checkout is removed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\vn30f_record.ps1
#>

param(
    [string]$RepoRoot = "C:\Users\Admin\Downloads\Stock_VN_Pred",
    [switch]$SkipBackfill
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path (Join-Path $RepoRoot "vn30f"))) {
    Write-Error "No vn30f package under $RepoRoot. Merge the futures branch first, or pass -RepoRoot."
    exit 1
}
Set-Location $RepoRoot

$logDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd"
$log = Join-Path $logDir "vn30f_daily_$stamp.log"

function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Output $line
}

Write-Log "=== VN30F daily run starting (local $(Get-Date)) ==="

# 1. The perishable part. Blocks until the Hanoi close, then runs a full tick sweep
#    so the day is complete even if the polling loop dropped out partway.
Write-Log "recorder: waiting for the Hanoi open"
python -m vn30f.ingest.kbs_recorder --wait-for-open 2>&1 | Add-Content -Path $log -Encoding utf8
$recExit = $LASTEXITCODE
Write-Log "recorder exited $recExit"

# 2. Belt-and-braces recovery sweep. The recorder's own exit codes are:
#      0 = session recorded and verified complete
#      1 = nothing to record (weekend, or started after the close)
#      2 = incomplete -- trades are missing and the day has not rolled yet
#
#    Anything OTHER than 0 or 1 means the recorder died without finishing: killed by
#    the OS on sleep/shutdown, an unhandled crash, a network drop that outlasted the
#    retries. Those show up as large Windows status codes (e.g. 3221225786 for a
#    Ctrl-C/kill, -1073741510), NOT as 2, so keying recovery on "== 2" missed exactly
#    the case that loses data. Recover on any nonzero-except-1 exit.
#
#    This is safe even if the exit was actually a weekend edge case, because
#    --final-sweep now self-guards: it refuses to run when the endpoint might be
#    serving a different trading day, so it can never file yesterday's trades under
#    today's date.
if ($recExit -eq 1) {
    Write-Log "nothing to record today (weekend or post-close start) -- skipping sweep"
} elseif ($recExit -ne 0) {
    Write-Log "recorder exited $recExit (incomplete or killed) -- running a standalone final sweep before the day rolls"
    python -m vn30f.ingest.kbs_recorder --final-sweep 2>&1 | Add-Content -Path $log -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        Write-Log "WARNING: session still incomplete after the retry sweep (exit $LASTEXITCODE). Re-run manually NOW: python -m vn30f.ingest.kbs_recorder --final-sweep"
    }
}

# 3. Bars. Rolling windows, so this widens history a little every day it runs.
if (-not $SkipBackfill) {
    Write-Log "backfill: bar history"
    python -m vn30f.ingest.backfill 2>&1 | Add-Content -Path $log -Encoding utf8
}

# 4. Type and deduplicate today's raw parts into curated daily files.
Write-Log "curate"
python -m vn30f.ingest.curate 2>&1 | Add-Content -Path $log -Encoding utf8

Write-Log "=== done ==="
