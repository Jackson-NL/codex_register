[CmdletBinding()]
param(
    [switch]$NoRestart,
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath
$BackendDir = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$LogDir = Join-Path $ProjectRoot ".runtime"

function Get-ListeningProcessIds([int]$Port) {
    @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Stop-PortListeners([int]$Port) {
    $processIds = Get-ListeningProcessIds $Port
    if (-not $processIds.Count) {
        return
    }
    Write-Host "Stopping existing listener(s) on port ${Port}: $($processIds -join ', ')"
    Stop-Process -Id $processIds -Force
}

function Find-Python {
    $preferred = "E:\python\python3.13.3\python.exe"
    if (Test-Path $preferred) {
        return $preferred
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        return $python.Source
    }
    throw "Python was not found. Install Python 3.13+ or update Find-Python in start.ps1."
}

function Wait-HttpOk([string]$Url, [int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                return
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)
    throw "Service did not become healthy within $TimeoutSeconds seconds: $Url"
}

if (-not (Test-Path (Join-Path $BackendDir "app\main.py"))) {
    throw "Backend entrypoint not found: $BackendDir\app\main.py"
}
if (-not (Test-Path (Join-Path $FrontendDir "package.json"))) {
    throw "Frontend package.json not found: $FrontendDir\package.json"
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
if (-not $NoRestart) {
    Stop-PortListeners $BackendPort
    Stop-PortListeners $FrontendPort
}

$python = Find-Python
# 后端日志含中文（池告警等），统一 UTF-8 输出，避免重定向到日志文件后乱码。
$env:PYTHONUTF8 = "1"
$backendLog = Join-Path $LogDir "backend.log"
$backendErrorLog = Join-Path $LogDir "backend-error.log"
$frontendLog = Join-Path $LogDir "frontend.log"
$frontendErrorLog = Join-Path $LogDir "frontend-error.log"

if (-not (Get-ListeningProcessIds $BackendPort).Count) {
    $backend = Start-Process -FilePath $python `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", $BackendPort `
        -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $backendLog -RedirectStandardError $backendErrorLog
    Write-Host "Backend started (PID $($backend.Id))."
}

if (-not (Get-ListeningProcessIds $FrontendPort).Count) {
    $frontend = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/d", "/s", "/c", "npm run dev -- --host 127.0.0.1 --port $FrontendPort" `
        -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $frontendLog -RedirectStandardError $frontendErrorLog
    Write-Host "Frontend started (launcher PID $($frontend.Id))."
}

Wait-HttpOk "http://127.0.0.1:$BackendPort/"
Wait-HttpOk "http://127.0.0.1:$FrontendPort/"
Write-Host "OpenAI Register is ready: http://127.0.0.1:$FrontendPort/"
Write-Host "Logs: $LogDir"
