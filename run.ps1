# AI 面试助手启动器：进程异常退出时自动重启，避免面试中途服务消失
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"

# 双击/快捷方式启动时把控制台窗口藏起来；从已有终端启动则保留，避免把终端一起藏了
try {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class InterviewLauncherWin {
    [DllImport("kernel32.dll")] public static extern uint GetConsoleProcessList(uint[] list, uint size);
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@
    $buffer = New-Object uint32[] 8
    $count = [InterviewLauncherWin]::GetConsoleProcessList($buffer, 8)
    if ($count -le 1) {
        [InterviewLauncherWin]::ShowWindow([InterviewLauncherWin]::GetConsoleWindow(), 0) | Out-Null
    }
} catch {
    # 隐藏失败不影响启动
}

$restarts = 0
while ($true) {
    Write-Host ""
    Write-Host "[启动器] 启动服务（第 $($restarts + 1) 次）..." -ForegroundColor Cyan
    uv run python -m app @args
    $code = $LASTEXITCODE
    $restarts++
    Write-Host "[启动器] 服务退出（退出码 $code），3 秒后自动重启；按 Ctrl+C 终止。" -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}
