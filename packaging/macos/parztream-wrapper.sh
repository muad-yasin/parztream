#!/bin/bash
# Real entry point for parztream.app -- referenced as CFBundleExecutable in
# Info.plist (see parztream.spec), placed at Contents/MacOS/parztream by
# the build workflow, alongside the actual PyInstaller-built binary
# (Contents/MacOS/parztream-bin, named that on purpose so this script isn't
# overwritten by it).
#
# A double-clicked .app gets no visible console by default on macOS. This
# app's whole "how do I stop it" story is "close the window (or Ctrl+C)",
# same as the Windows/Linux builds -- so this opens a real Terminal window
# and runs the actual server inside it, instead of running invisibly in
# the background with no way to see it's running or stop it short of
# Activity Monitor.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/parztream-bin"

osascript <<EOF
tell application "Terminal"
    activate
    do script "\"$BIN\""
end tell
EOF
