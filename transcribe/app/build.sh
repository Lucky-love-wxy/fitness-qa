#!/bin/bash
# 编译并组装「批量转逐字稿.app」；加 --install 则装到 ~/Applications 并注册 Launch Services
# 转写逻辑在上一级的 transcribe.py，App 运行时直接调用它（改 Python 不用重新编译 App）
set -euo pipefail
cd "$(dirname "$0")"
APP="build/批量转逐字稿.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -target arm64-apple-macos13.0 main.swift -o "$APP/Contents/MacOS/VideoTranscribe"
cp Info.plist "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :VTProjectDir string $(cd .. && pwd)" "$APP/Contents/Info.plist"
# 图标：AppIcon-1024.png = Claude/projects/tool-icons 的 video-transcribe-01「胶片框里的文字行」（10-20 默认，待用户挑）
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
  DEST="$HOME/Applications/批量转逐字稿.app"
  pkill -x VideoTranscribe 2>/dev/null || true
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST"
  echo "installed: $DEST"
fi
