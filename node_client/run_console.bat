@echo off
rem Run the node client in a console window (for first-time testing / debugging).
cd /d "%~dp0"
py -3 pc_agent_node.py --config client_config.json %*
pause
