#!/usr/bin/env bash
# Builds menubar/app/build/CaucionettaBar.app from CaucionettaBar.swift (no Xcode project needed, just the Command Line
# Tools). It does not install anything: copy the .app to ~/Applications yourself if you want it.
#   ./build.sh            build for this Mac's architecture
#   ./build.sh --sign     also ad-hoc sign it (codesign -s -)
set -euo pipefail
cd "$(dirname "$0")"
APP="build/CaucionettaBar.app"
ARCH="$(uname -m)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
xcrun swiftc -parse-as-library -swift-version 5 -O -target "${ARCH}-apple-macos14.0" \
  CaucionettaBar.swift -o "$APP/Contents/MacOS/CaucionettaBar"
if [[ "${1:-}" == "--sign" ]]; then
  codesign --force --sign - "$APP"
fi
echo "$PWD/$APP"
