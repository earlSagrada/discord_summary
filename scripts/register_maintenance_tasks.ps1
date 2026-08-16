<#
注册两个维护类计划任务（对应第三阶段数据闭环 + 周期复盘）：
  1. DiscordBacktestDaily —— 每天回填 outcomes（T+1/3/5 走势 → 胜率台账）。
  2. DiscordWeeklyReview  —— 每周出一份复盘（有价值发言人 + 成功方法 + 信号校准），
     完整报告写 trade_notes/reviews/，TL;DR 推送 Discord。

前提：已装好 .venv、pip install -r requirements.txt、.env 里有 ANTHROPIC_API_KEY
（周报还需要，回填不需要）。回填只用行情，不花 AI 钱。

用法（仓库根目录）：
    powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1
卸载：
    powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1 -Remove
#>

param(
    [string]$BacktestTask = "DiscordBacktestDaily",
    [string]$ReviewTask   = "DiscordWeeklyReview",
    [string]$BacktestAt   = "23:30",     # 每天回填时刻（美股收盘后）
    [string]$ReviewAt     = "20:00",     # 每周复盘时刻
    [string]$ReviewDay    = "Sunday",    # 每周哪天出复盘
    [int]$ReviewDays      = 7,           # 复盘回看天数
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Backtest = Join-Path $RepoRoot "src\backtest.py"
$Review   = Join-Path $RepoRoot "src\review.py"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $BacktestTask -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $ReviewTask   -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务 $BacktestTask 和 $ReviewTask"
    return
}

if (-not (Test-Path $Python))   { throw "找不到 venv python：$Python（先建 .venv 并装依赖）" }
if (-not (Test-Path $Backtest)) { throw "找不到 backtest.py：$Backtest" }
if (-not (Test-Path $Review))   { throw "找不到 review.py：$Review" }

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

# 1) 每日回填
$aDaily = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Backtest`" --backfill" -WorkingDirectory $RepoRoot
$tDaily = New-ScheduledTaskTrigger -Daily -At $BacktestAt
Register-ScheduledTask -TaskName $BacktestTask -Action $aDaily -Trigger $tDaily `
    -Settings $Settings -Description "每日回填 signals→outcomes（T+1/3/5 胜率台账）" -Force | Out-Null
Write-Host "已注册 $BacktestTask：每天 $BacktestAt 回填 outcomes"

# 2) 每周复盘
$aReview = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Review`" --days $ReviewDays" -WorkingDirectory $RepoRoot
$tReview = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $ReviewDay -At $ReviewAt
Register-ScheduledTask -TaskName $ReviewTask -Action $aReview -Trigger $tReview `
    -Settings $Settings -Description "每周复盘：发言人价值 + 成功方法 + 信号校准 → Discord + 文件" -Force | Out-Null
Write-Host "已注册 $ReviewTask：每周 $ReviewDay $ReviewAt 出复盘"

Write-Host "查看：Get-ScheduledTask -TaskName $BacktestTask,$ReviewTask"
Write-Host "手动跑一次：Start-ScheduledTask -TaskName $BacktestTask"
