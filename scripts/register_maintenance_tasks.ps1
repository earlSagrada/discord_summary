<#
注册五个维护类计划任务（数据闭环 + 自我改进 + 周期复盘 + 磁盘清理）：
  1. DiscordBacktestDaily  —— 每天回填 outcomes（T+1/3/5 走势 → 胜率台账）。
  2. DiscordSelfReviewDaily —— 每天自我复盘：学群里的推理方法 + 找我们自己分析的
     短板，把学到的写回 prompts/playbook.md，下一轮 pulse 立刻用上（闭环的核心）。
  3. DiscordWeeklyReview   —— 每周出一份复盘（有价值发言人 + 成功方法 + 信号校准），
     完整报告写 trade_notes/reviews/，TL;DR 推送 Discord。
  4. DiscordOptimizeWeekly —— 每周自我优化（贵模型）：按胜率统计调阈值、增删方法论，
     并把「改了什么、为什么、否决了哪些替代方案」写进决策记录。
     注意：新的 Substack 复盘落地时 cycle.py 会自动触发同一条流水线，
     这个定时任务只是兜底（万一某周没更新 Substack）。
  5. DiscordCleanupWeekly  —— 每周清理可再生缓存/旧导出，并按大小轮转日志。

前提：已装好 .venv、pip install -r requirements.txt、.env 里有 ANTHROPIC_API_KEY
（周报还需要，回填不需要）。回填只用行情，不花 AI 钱。

用法（仓库根目录）：
    powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1
卸载：
    powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1 -Remove
#>

param(
    [string]$BacktestTask = "DiscordBacktestDaily",
    [string]$SelfTask     = "DiscordSelfReviewDaily",
    [string]$ReviewTask   = "DiscordWeeklyReview",
    [string]$OptimizeTask = "DiscordOptimizeWeekly",
    [string]$CleanupTask  = "DiscordCleanupWeekly",
    [string]$BacktestAt   = "23:30",     # 每天回填时刻（美股收盘后）
    [string]$SelfAt       = "23:45",     # 每天自我复盘时刻（排在回填之后，能用上当天结果）
    [string]$ReviewAt     = "20:00",     # 每周复盘时刻
    [string]$ReviewDay    = "Sunday",    # 每周哪天出复盘
    [string]$OptimizeAt   = "21:00",     # 每周自我优化时刻（排在周复盘之后）
    [string]$OptimizeDay  = "Sunday",
    [string]$CleanupAt    = "22:00",     # 每周清理时刻
    [string]$CleanupDay   = "Sunday",    # 每周哪天清理
    [int]$ReviewDays      = 7,           # 复盘回看天数
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Backtest = Join-Path $RepoRoot "src\backtest.py"
$Self     = Join-Path $RepoRoot "src\selfreview.py"
$Review   = Join-Path $RepoRoot "src\review.py"
$Optimize = Join-Path $RepoRoot "src\optimize.py"
$Cleanup  = Join-Path $RepoRoot "src\cleanup.py"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $BacktestTask -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $SelfTask     -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $ReviewTask   -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $OptimizeTask -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $CleanupTask  -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务 $BacktestTask、$SelfTask、$ReviewTask、$OptimizeTask 和 $CleanupTask"
    return
}

if (-not (Test-Path $Python))   { throw "找不到 venv python：$Python（先建 .venv 并装依赖）" }
if (-not (Test-Path $Backtest)) { throw "找不到 backtest.py：$Backtest" }
if (-not (Test-Path $Self))     { throw "找不到 selfreview.py：$Self" }
if (-not (Test-Path $Review))   { throw "找不到 review.py：$Review" }
if (-not (Test-Path $Optimize)) { throw "找不到 optimize.py：$Optimize" }
if (-not (Test-Path $Cleanup))  { throw "找不到 cleanup.py：$Cleanup" }

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

# 2) 每日自我复盘（排在回填之后：这样它能看到当天信号的兑现情况）
$aSelf = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Self`"" -WorkingDirectory $RepoRoot
$tSelf = New-ScheduledTaskTrigger -Daily -At $SelfAt
Register-ScheduledTask -TaskName $SelfTask -Action $aSelf -Trigger $tSelf `
    -Settings $Settings -Description "每日自我复盘：学方法、找短板 → 更新 playbook + 推 TL;DR" -Force | Out-Null
Write-Host "已注册 $SelfTask：每天 $SelfAt 自我复盘并更新 playbook"

# 3) 每周复盘
$aReview = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Review`" --days $ReviewDays" -WorkingDirectory $RepoRoot
$tReview = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $ReviewDay -At $ReviewAt
Register-ScheduledTask -TaskName $ReviewTask -Action $aReview -Trigger $tReview `
    -Settings $Settings -Description "每周复盘：发言人价值 + 成功方法 + 信号校准 → Discord + 文件" -Force | Out-Null
Write-Host "已注册 $ReviewTask：每周 $ReviewDay $ReviewAt 出复盘"

# 4) 每周自我优化（排在周复盘之后，能读到当周的复盘产出）
$aOpt = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Optimize`"" -WorkingDirectory $RepoRoot
$tOpt = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $OptimizeDay -At $OptimizeAt
Register-ScheduledTask -TaskName $OptimizeTask -Action $aOpt -Trigger $tOpt `
    -Settings $Settings -Description "每周自我优化：调阈值+改方法库+写决策记录（含否决的替代方案）" -Force | Out-Null
Write-Host "已注册 $OptimizeTask：每周 $OptimizeDay $OptimizeAt 自我优化"

# 5) 每周磁盘清理
$aCleanup = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Cleanup`" --apply" -WorkingDirectory $RepoRoot
$tCleanup = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $CleanupDay -At $CleanupAt
Register-ScheduledTask -TaskName $CleanupTask -Action $aCleanup -Trigger $tCleanup `
    -Settings $Settings -Description "每周清理旧导出/可再生缓存，并轮转 cycle/watch 日志" -Force | Out-Null
Write-Host "已注册 $CleanupTask：每周 $CleanupDay $CleanupAt 清理磁盘"

Write-Host "查看：Get-ScheduledTask -TaskName $BacktestTask,$SelfTask,$ReviewTask,$OptimizeTask,$CleanupTask"
Write-Host "手动跑一次：Start-ScheduledTask -TaskName $BacktestTask"
