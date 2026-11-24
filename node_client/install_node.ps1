<#
远程电脑代理 · Windows 节点客户端安装脚本
用法（以管理员身份打开 PowerShell，进入本目录）：
    powershell -ExecutionPolicy Bypass -File .\install_node.ps1
可选参数：
    -NoPassword   不保存 Windows 密码（只能“登录后自启”，断电重启后要有人登录才会运行）
    -KeepLid      不修改“合上盖子”的行为
做的事：
  1. 安装依赖 websockets，并只读检查一遍配置（不建 node_data、不写日志）；
  2. 注册计划任务 PCAgentNode：开机即启动（不需要登录）+ 登录时启动，崩溃后每分钟自动重启，永不超时；
  3. 关闭睡眠 / 休眠 / 快速启动，插电时合盖不睡眠。
#>
param([switch]$NoPassword, [switch]$KeepLid)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "pc_agent_node.py"
$config = Join-Path $here "client_config.json"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "请右键“以管理员身份运行” PowerShell 后再执行本脚本。" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $here "client_config.example.json") $config
    Write-Host "已生成 client_config.json，请先填好 server_url / node_id / token / allowed_roots 再重新运行本脚本。" -ForegroundColor Yellow
    notepad $config; exit 1
}

# 1. Python 与依赖
$py = $null
try { $py = (& py -3 -c "import sys;print(sys.executable)").Trim() } catch {}
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Host "找不到 Python，请先安装 Python 3.9+（勾选 Add to PATH）。" -ForegroundColor Red; exit 1 }
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
Write-Host "使用 Python：$py"
& $py -m pip install --user -q -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Host "依赖安装失败。请手动执行：`"$py`" -m pip install --user `"websockets>=12`"，成功后再运行本脚本。" -ForegroundColor Red
    exit 1
}
# 只读检查（不建 node_data、不写日志）：配置有错就停下；网络不通只提示，不影响安装
& $py $script --config $config --check
if ($LASTEXITCODE -ne 0) {
    Write-Host "配置检查没通过（见上面打 ✗ 的项），改好 client_config.json 后再运行本脚本。" -ForegroundColor Red
    exit 1
}

# 2. 计划任务
$taskName = "PCAgentNode"
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$script`" --config `"$config`"" -WorkingDirectory $here
$tBoot = New-ScheduledTaskTrigger -AtStartup
$tBoot.Delay = "PT30S"
$tLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
if ($NoPassword) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $tLogon -Settings $settings `
        -RunLevel Limited -Description "远程电脑代理节点客户端（登录后自启）" | Out-Null
    Write-Host "已注册：登录后自启。注意：断电重启后要有人登录 Windows 才会运行（可开启自动登录）。" -ForegroundColor Yellow
} else {
    Write-Host "为了断电重启后不用登录也能自启，需要保存你的 Windows 登录密码（存在系统计划任务里）。"
    $cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" -Message "输入当前 Windows 账户密码（用 PIN 登录的也要填账户密码）"
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($tBoot, $tLogon) -Settings $settings `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited `
        -Description "远程电脑代理节点客户端（开机即启动，不需要登录）" | Out-Null
    Write-Host "已注册：开机 30 秒后自动启动（不需要登录）+ 登录时启动，崩溃自动重启。" -ForegroundColor Green
}

# 3. 电源：不睡眠、不休眠、关快速启动
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
powercfg /hibernate off
if (-not $KeepLid) {
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    powercfg /setactive SCHEME_CURRENT
}
Write-Host "已关闭睡眠/休眠/快速启动（插电合盖不睡眠）。" -ForegroundColor Green

Start-ScheduledTask -TaskName $taskName
Write-Host "已启动。日志：$(Join-Path $here 'node_data\node.log')"
Write-Host "别忘了在 BIOS 里把 “AC Power Recovery / Restore on AC Power Loss” 设成 Power On（来电自动开机）。" -ForegroundColor Yellow
