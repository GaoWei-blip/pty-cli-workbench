#!/bin/bash
# 编译原生 App：build.sh <输出 .app 路径> [bundle id]
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"; OUT="$1"; BID="${2:-local.claude-workbench}"
ARCH="$(uname -m)"
rm -rf "$OUT"; mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
swiftc -O -swift-version 5 -target "${ARCH}-apple-macos13.0" -o "$OUT/Contents/MacOS/ClaudeWorkbench" "$HERE/main.swift" "$HERE/shot.swift" \
  -framework Cocoa -framework WebKit -framework UserNotifications
cp "$HERE/AppIcon.icns" "$OUT/Contents/Resources/AppIcon.icns"
cat > "$OUT/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$BID</string>
  <key>CWBDir</key><string>$ROOT</string>
  <key>CFBundleName</key><string>Claude 工作台</string>
  <key>CFBundleDisplayName</key><string>Claude 工作台</string>
  <key>CFBundleExecutable</key><string>ClaudeWorkbench</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
  <key>NSDocumentsFolderUsageDescription</key><string>在你的项目文件夹里运行 Claude Code。</string>
  <key>NSDesktopFolderUsageDescription</key><string>在你的项目文件夹里运行 Claude Code。</string>
  <key>NSDownloadsFolderUsageDescription</key><string>在你的项目文件夹里运行 Claude Code，导出对话。</string>
</dict></plist>
PL
codesign --force --deep -s - "$OUT" >/dev/null 2>&1
codesign -v "$OUT"
