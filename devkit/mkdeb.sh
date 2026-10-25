#!/bin/sh
# 打 deb（不依赖 Theos / dpkg-deb，纯 Python 拼 ar）
#
# 用法：sh devkit/mkdeb.sh <arch> <version>
set -eu

ARCH="${1:?用法: mkdeb.sh <arch> <version>}"
VER="${2:?用法: mkdeb.sh <arch> <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILDDIR="${3:-$ROOT/.build}"
STAGE="$BUILDDIR/stage"
OUT="$BUILDDIR/out"
PKG="com.liehuu.awemepipswitch"
NAME="AwemePipSwitch"

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/Library/MobileSubstrate/DynamicLibraries" \
         "$OUT"

# ---- 载荷：dylib + 注入过滤 ----
cp "$BUILDDIR/$NAME.dylib" "$STAGE/Library/MobileSubstrate/DynamicLibraries/$NAME.dylib"
cp "$ROOT/$NAME.plist"     "$STAGE/Library/MobileSubstrate/DynamicLibraries/$NAME.plist"

# 权限
find "$STAGE" -type d -exec chmod 755 {} \;
find "$STAGE" -type f -exec chmod 644 {} \;
# ★ dylib 必须 755：644 会被 ellekit / RootHide 的加载器拒绝，
#   表现是「装了完全没反应」，dpkg 却不报任何错。
chmod 755 "$STAGE/Library/MobileSubstrate/DynamicLibraries/$NAME.dylib"

# ---- control ----
# ★★ 不写任何 Conflicts / Replaces —— 1.0-1 曾误写
#      Conflicts: com.liehuu.awemenextprev
#    结果装本插件时会把「AwemeNextPrev-pure 1.0-2（锁屏上一首/下一首）」
#    一起卸掉。这是错的，实测依据：
#      · pure 1.0-2 是**零 hook** 的纯公开 API 实现
#        （dylib 里 MSHookMessageEx / method_setImplementation /
#          libsubstrate 全部为 False，只链 Foundation/UIKit/MediaPlayer）；
#      · 它只用 MPRemoteCommandCenter + UIScrollView 滚动，
#        完全不碰画中画（dylib 里 Pip / PictureInPicture /
#        sampleBuffer / AVSampleBuffer 全部为 False）。
#    → 两者作用点不重叠、触发源不同（锁屏遥控 vs 画中画按钮），
#      文件不同名（AwemeNextPrev.dylib vs AwemePipSwitch.dylib），
#      可以同装：锁屏用 pure，小窗用本插件。
#   ★ 唯一需要用户自己避免的组合：与本插件同样 hook 了画中画 delegate
#     的旧「3.0-1 整合版」——那个包名相同，但用户已明确弃用，
#     由 Description 里提示，不用 dpkg 硬拦（硬拦会误伤 1.0-2）。
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Name: 抖音小窗切视频
Version: $VER
Architecture: $ARCH
Description: 抖音小窗（画中画）里点「±10 秒」→ 切上一条/下一条
 只有一个功能：小窗播放时能切换上一条/下一条视频。
 小窗上那对「±10 秒」按钮的样子改不了（系统画中画外壳绘制），
 本插件改的是点击后的行为 —— 点它不再跳 10 秒，而是切视频。
 不含锁屏遥控、不含后台保持播放、无开关、无日志、无弹窗。
 实现：拦画中画 sample-buffer 的 skipByInterval:completionHandler:，
 转成主信息流 scrollView 的相邻行滚动。
 与「AwemeNextPrev-pure 1.0-2（锁屏上一首/下一首）」不冲突，可同装：
 那个是纯公开 API、零 hook、只做锁屏遥控；本插件只做小窗。
Maintainer: liehuu
Author: liehuu
Section: Tweaks
Depends: mobilesubstrate
EOF
chmod 644 "$STAGE/DEBIAN/control"

# ---- postinst ----
# ★★ 绝不 respring / sbreload / killall SpringBoard。
#   postinst 是 dpkg 事务的最后一步，在 Sileo 里安装时重启 SpringBoard
#   会把前台的 Sileo 一起杀掉 → status 没写完、lock 没释放 →
#   锁残留 + 半装，之后任何 deb 都装不上。真实事故，别再加回来。
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
printf '\n'
printf '抖音小窗切视频 已安装。\n'
printf '\n'
printf '用法：把抖音切到小窗（画中画），点小窗上的「>10」或「<10」按钮，\n'
printf '      会切到下一条 / 上一条视频，不再跳转 10 秒。\n'
printf '\n'
printf '注意：那个按钮的图标改不了（系统画中画绘制），但它已经不是\n'
printf '      快进 10 秒了 —— 点它切视频。\n'
printf '\n'
printf '生效方式：完全退出抖音（上滑杀掉）再重新打开。\n'
printf '\n'
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

# ---- 打包 ----
DEB="$OUT/${PKG}_${VER}_${ARCH}.deb"
export STAGE OUT DEB
python3 - <<'PY'
import io, os, lzma, tarfile, gzip, hashlib, time

stage = os.environ['STAGE']
deb   = os.environ['DEB']
_MTIME = int(os.environ.get('SOURCE_DATE_EPOCH') or 1700000000)


def _is_exec(rel):
    if rel.endswith('.dylib'):
        return True
    if rel.startswith('DEBIAN/') and rel.split('/')[-1] in \
            ('postinst', 'postrm', 'preinst', 'prerm'):
        return True
    return False


def _tar():
    """★ GNU_FORMAT 是硬要求：Python 默认 PAX_FORMAT 会给每个成员插
    typeflag='x' 的扩展头，dpkg 不认（unsupported PAX tar header type 'x'），
    整个包装不上。mtime 也必须取整数秒，否则又触发扩展头。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.GNU_FORMAT) as tf:
        for root, dirs, files in os.walk(stage):
            rel_root = os.path.relpath(root, stage).replace('\\', '/')
            # ★★ DEBIAN/ 绝不进 data.tar：dpkg 会解到 /DEBIAN，
            #   rootless 上 / 只读 → 整包装不上。
            if rel_root == 'DEBIAN' or rel_root.startswith('DEBIAN/'):
                dirs[:] = []
                continue
            if rel_root == '.':
                dirs[:] = [d for d in dirs if d != 'DEBIAN']
            dirs.sort()
            files.sort()
            for name in dirs:
                full = os.path.join(root, name)
                ti = tf.gettarinfo(full, arcname='./' + os.path.relpath(full, stage))
                ti.mode = 0o755
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = 'root'
                ti.mtime = _MTIME
                tf.addfile(ti)
            for name in files:
                full = os.path.join(root, name)
                ti = tf.gettarinfo(full, arcname='./' + os.path.relpath(full, stage))
                rel = os.path.relpath(full, stage).replace('\\', '/')
                # 不读 stat().st_mode：Windows / 某些挂载上拿到的是 666
                ti.mode = 0o755 if _is_exec(rel) else 0o644
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = 'root'
                ti.mtime = _MTIME
                with open(full, 'rb') as fh:
                    tf.addfile(ti, fh)
    return buf.getvalue()


data = lzma.compress(_tar(), format=lzma.FORMAT_XZ)

ctl = io.BytesIO()
with tarfile.open(fileobj=ctl, mode='w', format=tarfile.GNU_FORMAT) as tf:
    for name in ('control', 'postinst'):
        p = os.path.join(stage, 'DEBIAN', name)
        if os.path.exists(p):
            ti = tf.gettarinfo(p, arcname='./' + name)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = 'root'
            ti.mode = 0o755 if name in ('postinst', 'prerm', 'preinst', 'postrm') else 0o644
            ti.mtime = _MTIME
            with open(p, 'rb') as fh:
                tf.addfile(ti, fh)
control = gzip.compress(ctl.getvalue(), mtime=0)


def _check_no_pax_headers(raw, label):
    """按 tar 真实结构走（读头 → 按 size 跳过数据区），不能每 512 字节盲扫：
    二进制载荷里某个 512 块偏移 156 处恰好是 'x' 就会假报。"""
    bad = []
    off = 0
    n = len(raw)
    while off + 512 <= n:
        blk = raw[off:off + 512]
        if blk == b'\x00' * 512:
            break
        tf_ = blk[156:157]
        size_f = blk[124:136].rstrip(b'\x00 ').decode('ascii', 'replace')
        try:
            size = int(size_f, 8) if size_f else 0
        except ValueError:
            size = 0
        if tf_ in (b'x', b'g'):
            nm = blk[0:100].rstrip(b'\x00').decode('ascii', 'replace')
            bad.append('%s@%d' % (nm, off))
        off += 512 + ((size + 511) // 512) * 512
    if bad:
        print('  ✗ %s 里有 %d 个 PAX 扩展头（dpkg 不认，装包必失败）' % (label, len(bad)))
        for b in bad[:6]:
            print('      ', b)
        raise SystemExit(1)


_check_no_pax_headers(lzma.decompress(data), 'data.tar')
_check_no_pax_headers(gzip.decompress(control), 'control.tar')
print('  ✓ tar 无 PAX 扩展头（GNU_FORMAT）')

members = [
    ('debian-binary', b'2.0\n'),
    ('control.tar.gz', control),
    ('data.tar.xz', data),
]

out = io.BytesIO()
out.write(b'!<arch>\n')
for name, blob in members:
    hdr = ('%-16s' % name).encode()
    hdr += ('%-12s' % str(int(time.time()))).encode()
    hdr += ('%-6s' % '0').encode()
    hdr += ('%-6s' % '0').encode()
    hdr += ('%-8s' % '100644').encode()
    hdr += ('%-10s' % str(len(blob))).encode()
    hdr += b'`\n'
    out.write(hdr)
    out.write(blob)
    if len(blob) % 2:
        out.write(b'\n')

blob = out.getvalue()
open(deb, 'wb').write(blob)
print('  %s  %d B  sha256=%s' % (os.path.basename(deb), len(blob),
                                 hashlib.sha256(blob).hexdigest()[:16]))
PY

echo "  -> $DEB"