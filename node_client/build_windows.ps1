$ErrorActionPreference = 'Stop'
$buildDir = Join-Path $PSScriptRoot '.windows-build'
$venv = Join-Path $buildDir 'venv'

& py -3.13 -m venv $venv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$python = Join-Path $venv 'Scripts\python.exe'
& $python -c "import platform, tkinter; assert platform.machine() == 'AMD64', '64-bit x86 Python is required'"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m pip install 'pyinstaller==6.22.3' 'websockets==16.0'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$pythonLicense = Join-Path (& $python -c 'import sys; print(sys.base_prefix)') 'LICENSE.txt'

& $python -m PyInstaller --noconfirm --noupx --onefile --windowed --name AstrBotPCAgent `
    --hidden-import websockets.asyncio.client --add-data "${pythonLicense}:licenses/CPython" `
    --distpath (Join-Path $buildDir 'dist') `
    --workpath (Join-Path $buildDir 'work') --specpath $buildDir (Join-Path $PSScriptRoot 'windows_app.py')
exit $LASTEXITCODE
