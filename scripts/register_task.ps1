<#
注册 Windows 计划任务：每 15 分钟跑一轮 cycle.py（入库→脉搏简报→信号→推送）。

前提：
  - 已装好 .venv 并 pip install -r requirements.txt
  - .env 里填了 ANTHROPIC_API_KEY 和 DISCORD_WEBHOOK_URL
  - 那个 Discord 标签页保持打开、油猴脚本已开“定时导出”（负责把文件下到 data/inbox）

用法（在仓库根目录，管理员或普通用户均可）：
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
卸载：
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove
#>

param(
    [string]$TaskName = "DiscordDigestCycle",
    [int]$Minutes = 15,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

# 仓库根 = 本脚本的上一级目录
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script = Join-Path $RepoRoot "src\cycle.py"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务 $TaskName"
    return
}

if (-not (Test-Path $Python)) { throw "找不到 venv python：$Python（先建 .venv 并装依赖）" }
if (-not (Test-Path $Script)) { throw "找不到 cycle.py：$Script" }

# 每次触发跑一轮就退出
$Action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Script`" --once" -WorkingDirectory $RepoRoot

# 从现在起，每 $Minutes 分钟一次，持续约 25 年（约等于永久）
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
    -RepetitionDuration (New-TimeSpan -Days 9000)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "每 $Minutes 分钟：入库+脉搏简报+信号+推送到 Discord" -Force | Out-Null

Write-Host "已注册计划任务 $TaskName：每 $Minutes 分钟跑一次 `"$Python`" `"$Script`" --once"
Write-Host "查看：Get-ScheduledTask -TaskName $TaskName ；手动跑一次：Start-ScheduledTask -TaskName $TaskName"
