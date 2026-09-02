#!/usr/bin/env bash
# Compila la APK de TrackCam (debug y release firmado)
# Uso: ./compilar.sh [debug|release|todo]  (por defecto: todo)
set -euo pipefail
cd "$(dirname "$0")/.."   # sube a apk/

export JAVA_HOME=/home/alvaro/.local/jre21
export PATH="$JAVA_HOME/bin:$PATH"
export ANDROID_HOME=/home/alvaro/android-sdk
export ANDROID_SDK_ROOT=/home/alvaro/android-sdk
GRADLE=/home/alvaro/gradle/gradle-8.7/bin/gradle
KS=~/.android/trackcam.keystore
KS_PASS=trackcam123
APKSIGNER=$ANDROID_HOME/build-tools/34.0.0/apksigner

modo="${1:-todo}"
case "$modo" in
  debug)   "$GRADLE" assembleDebug --no-daemon ;;
  release) "$GRADLE" assembleRelease --no-daemon
           "$APKSIGNER" sign --ks "$KS" --ks-pass "pass:$KS_PASS" \
             --out trackcam-release.apk \
             app/build/outputs/apk/release/app-release-unsigned.apk ;;
  todo)    "$GRADLE" assembleDebug assembleRelease --no-daemon
           "$APKSIGNER" sign --ks "$KS" --ks-pass "pass:$KS_PASS" \
             --out trackcam-release.apk \
             app/build/outputs/apk/release/app-release-unsigned.apk ;;
esac

echo ""
echo "✅ APK listas:"
ls -la app/build/outputs/apk/debug/app-debug.apk trackcam-release.apk 2>/dev/null || true
