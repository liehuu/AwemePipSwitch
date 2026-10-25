#!/bin/sh
# AwemePipSwitch 构建（零 Theos：clang + codesign + mkdeb.sh 拼 ar）
#
# 用法：sh devkit/build.sh <arch-label> <version>
#   arch-label : iphoneos-arm64 | iphoneos-arm64e   （只影响 control 的 Architecture）
#   version    : 1.0-1
set -eu

ARCH_NAME="${1:?用法: build.sh <iphoneos-arm64|iphoneos-arm64e> <version>}"
VER="${2:?用法: build.sh <iphoneos-arm64|iphoneos-arm64e> <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/.build"
mkdir -p "$BUILD"

SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
echo "SDK : $SDK"

# ★★ 固定编 arm64，不跟 ARCH_NAME 走：
#   宿主进程是抖音 App。App Store 分发的 App 一律只有 arm64 切片。
#   dylib 的架构必须与宿主**进程**一致，不是与设备 CPU 一致 ——
#   在 arm64e 设备上抖音进程仍是 arm64 进程，编成 arm64e 的 dylib
#   带 ptrauth 指令，arm64 进程不会加载它（表现是「装了完全没反应」，
#   且没有任何报错）。ARCH_NAME 只用于 deb 的 Architecture 标签，
#   好让 arm64e 设备上的 Sileo 也愿意安装。
CPU="arm64"
TRIPLE="arm64-apple-ios14.0"

# ★ install_name 必须是 @loader_path/.jbroot/...：
#   RootHide 的加载器按这个约定定位 dylib。别的写法（空 install_name
#   或 @rpath）会被静默跳过 —— 这是「deb 装了没反应」的经典根因。
# ★ CoreGraphics 必须显式链接：CGRectGetMaxY 是 CG_EXTERN 函数
#   （不是内联宏），只链 UIKit 时链接器报 _CGRectGetMaxY 未定义。
xcrun clang -target "$TRIPLE" -isysroot "$SDK" \
  -dynamiclib -fobjc-arc -Os -Wall -Wextra \
  -Wno-unused-parameter \
  -framework Foundation -framework UIKit -framework CoreGraphics \
  -framework AVFoundation -framework AVKit \
  -framework CoreMedia -framework QuartzCore -framework CoreFoundation \
  "$ROOT/main.m" \
  -o "$BUILD/AwemePipSwitch.dylib" \
  -Xlinker -install_name -Xlinker \
    "@loader_path/.jbroot/Library/MobileSubstrate/DynamicLibraries/AwemePipSwitch.dylib"

# ★ 必须自签：RootHide / ellekit 会跳过没有 LC_CODE_SIGNATURE 的 dylib。
codesign --force --sign - --timestamp=none "$BUILD/AwemePipSwitch.dylib"

# ---------- 2) 产物闸门 ----------
# ★ 架构闸门：必须是 arm64（抖音进程是 arm64）
xcrun lipo -info "$BUILD/AwemePipSwitch.dylib" | grep -q "arm64" \
  || { echo "✗ dylib 缺 arm64 切片（抖音进程是 arm64）"; exit 1; }
echo "✓ dylib 架构校验通过（arm64）"

# ★ 文件类型必须是 MH_DYLIB（6），不能是 MH_BUNDLE（8）。
#   filetype 在 mach_header_64 偏移 12（4 字节，小端）。
DTYPE=$(od -An -tu4 -j12 -N4 "$BUILD/AwemePipSwitch.dylib" | tr -d ' ')
[ "$DTYPE" = "6" ] || { echo "✗ dylib Mach-O 类型 $DTYPE != 6（MH_DYLIB）"; exit 1; }
echo "✓ dylib 是 MH_DYLIB"

# ★★★ 签名段闸门：RootHide 会跳过没有 LC_CODE_SIGNATURE 的 dylib。
#   不能只看 codesign 命令返回 0 —— 要确认段真的在里面。
xcrun otool -l "$BUILD/AwemePipSwitch.dylib" | grep -q "LC_CODE_SIGNATURE" \
  || { echo "✗ dylib 缺 LC_CODE_SIGNATURE（RootHide 会跳过不加载）"; exit 1; }
echo "✓ dylib 含 LC_CODE_SIGNATURE"

# ★★★ 禁项闸门（本插件「只做一件事」的硬约束）：
#   下面这些符号一旦出现，说明代码被"顺手改进"了 —— 每一项都对应
#   3.0-1 里导致故障的那部分。宁可编译失败，也不许它们回来。
#   ★ 判据必须用**精确的 selector / 类名写法**，不能用 `pause` 这种泛词：
#     系统库里必然有 pause，泛词查法会把自己误判成违规。
#   ★ 也不用 grep 裸词 `UIBackgroundTask` 之外的宽泛名，见下面逐条。
for bad in "MPRemoteCommandCenter" \
           "nextTrackCommand" \
           "previousTrackCommand" \
           "seekToTime" \
           "sendEvent:" \
           "beginBackgroundTaskWithExpirationHandler:" \
           "setRate:" ; do
  if grep -aq "$bad" "$BUILD/AwemePipSwitch.dylib"; then
    echo "✗ 产物里出现禁项 [$bad]：本插件只做小窗切换，不许加其他功能"
    exit 1
  fi
done
echo "✓ 无禁项（无锁屏命令 / 无 seek hook / 无保持播放）"

# ★ 正项闸门：必须真的有落点
grep -aq "pictureInPictureController:skipByInterval:completionHandler:" \
  "$BUILD/AwemePipSwitch.dylib" \
  || { echo "✗ 缺 skip selector（少了它小窗点了没反应）"; exit 1; }
grep -aq "AWEPictureInPictureSampleBufferPlayerView" \
  "$BUILD/AwemePipSwitch.dylib" \
  || { echo "✗ 缺硬编码兜底类名（枚举路全落空时靠它）"; exit 1; }
# ★ 绝不用 stimulator/模拟器框架、绝不 dlopen 第二份 substrate
if grep -aq "libsubstrate" "$BUILD/AwemePipSwitch.dylib"; then
  echo "✗ 产物硬链了 libsubstrate（双引擎改同一张方法表会崩）"
  exit 1
fi
echo "✓ 正项指纹齐全"

echo "--- 产物体检 ---"
/usr/bin/file "$BUILD/AwemePipSwitch.dylib"
xcrun dwarfdump --uuid "$BUILD/AwemePipSwitch.dylib" || true
codesign -dv "$BUILD/AwemePipSwitch.dylib" 2>&1 | head -5 || true
ls -l "$BUILD/AwemePipSwitch.dylib"

sh "$ROOT/devkit/mkdeb.sh" "$ARCH_NAME" "$VER"