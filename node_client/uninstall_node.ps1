# 卸载节点客户端的计划任务（本地队列和日志保留在 node_data 里）
Stop-ScheduledTask -TaskName "PCAgentNode" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "PCAgentNode" -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like "*pc_agent_node.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "已移除自启任务。电源设置如需恢复，请在“电源和睡眠”里手动调整。"
