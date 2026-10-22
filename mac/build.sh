#!/bin/bash
# 编译并组装「健身问答.app」；加 --install 则装到 ~/Applications 并注册 Launch Services
# 只有改 main.swift / Info.plist / 图标才需要重跑；改 index.html、server.py、ask.py 不用。
# 图标：AppIcon-1024.png = Claude/projects/tool-icons 的 fitness-qa-02「气泡里的哑铃」（10-22 用户从 8 个变体里选定，源文件 icon.svg）
set -euo pipefail
cd "$(dirname "$0")"
APP="build/健身问答.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -target arm64-apple-macos13.0 main.swift -o "$APP/Contents/MacOS/FitnessQA"
cp Info.plist "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :FitnessQAProjectDir string $(cd .. && pwd)" "$APP/Contents/Info.plist"
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
for base in 16 32 128 256 512; do
  sips -z $base $base AppIcon-1024.png --out "$ICONSET/icon_${base}x${base}.png" >/dev/null
  sips -z $((base * 2)) $((base * 2)) AppIcon-1024.png --out "$ICONSET/icon_${base}x${base}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
codesign --force --deep -s - "$APP"   # ad-hoc 签名，本机运行足够
echo "built: $PWD/$APP"
if [[ "${1:-}" == "--install" ]]; then
  DEST="$HOME/Applications/健身问答.app"
  pkill -x FitnessQA 2>/dev/null || true
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST"
  echo "installed: $DEST"
fi
