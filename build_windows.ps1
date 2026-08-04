$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pip install -r requirements.txt
Remove-Item -Recurse -Force "build\AutoReply-Whatsapp-Bot" -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "dist\windows" | Out-Null
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "AutoReply-Whatsapp-Bot" `
  --distpath "dist\windows" --workpath "build\AutoReply-Whatsapp-Bot" desktop_app.py
Write-Host "Built: dist\windows\AutoReply-Whatsapp-Bot.exe"
