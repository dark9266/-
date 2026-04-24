# 크림봇 watchdog Windows 작업스케줄러 등록 — 1회 실행.
# 실행 방법: PowerShell 일반 권한으로 우클릭 → "PowerShell로 실행"
#   또는: powershell.exe -ExecutionPolicy Bypass -File .\bot_runner_install_task.ps1

$TaskName = 'KreamBotWatchdog'
$Distro   = 'Ubuntu'
# WSL 측 ASCII symlink — Windows 작업스케줄러 등록 시 한글 경로 인코딩 깨짐 회피.
# (선행: ln -sf "/mnt/c/Users/USER/Desktop/크림봇" /home/wonhee/kreambot)
$Script   = '/home/wonhee/kreambot/scripts/bot_runner.sh'

$action = New-ScheduledTaskAction `
    -Execute 'wsl.exe' `
    -Argument "-d $Distro -- bash -c `"$Script`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force

Write-Host "✅ 작업 '$TaskName' 등록 완료. 다음 Windows 로그온 시 자동 가동." -ForegroundColor Green
Write-Host "   확인: schtasks /query /tn $TaskName" -ForegroundColor Cyan
Write-Host "   즉시 1회 실행: schtasks /run /tn $TaskName" -ForegroundColor Cyan
Write-Host "   해제: schtasks /delete /tn $TaskName /f" -ForegroundColor Cyan
