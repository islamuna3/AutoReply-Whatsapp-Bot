#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build/AutoReply-Whatsapp-Bot dist/mac dist/dmg-root
mkdir -p dist/mac dist/dmg-root
python3 -m PyInstaller --noconfirm --clean --windowed \
  --name "AutoReply WhatsApp Bot" \
  --osx-bundle-identifier "com.autoreply.whatsappbot" \
  --distpath dist/mac --workpath build/AutoReply-Whatsapp-Bot \
  desktop_app.py
codesign --force --deep --sign - "dist/mac/AutoReply WhatsApp Bot.app"
cp -R "dist/mac/AutoReply WhatsApp Bot.app" dist/dmg-root/
cp -R chrome-extension dist/dmg-root/Chrome-Extension
cp CHROME_EXTENSION_INSTALL.txt dist/dmg-root/
ln -s /Applications dist/dmg-root/Applications
hdiutil create -volname "AutoReply WhatsApp Bot" -srcfolder dist/dmg-root \
  -ov -format UDZO "dist/mac/AutoReply-Whatsapp-Bot-macOS-arm64.dmg"
rm -rf dist/dmg-root
echo "Built: dist/mac/AutoReply WhatsApp Bot.app"
echo "Built: dist/mac/AutoReply-Whatsapp-Bot-macOS-arm64.dmg"
