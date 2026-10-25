# -*- coding: utf-8 -*-
"""反向验证：证明 check_source.py 的判据**真的在跑**。

★ 为什么必须有（本项目血泪教训）：
  「全部通过」有两个截然不同的原因：
    ① 判据真的检查了，源码确实合规；
    ② 判据压根没执行（函数名改了 / 目标找不到 / 判据锚错了地方）。
  只有「注入违规 → 检查必须非 0」才能证明是 ①。

★ 实现方式（性能优先，来自 verify_negative 的老坑）：
  往**同一份副本**里逐例注入 → 跑闸门 → 还原。
  ★ 绝不每例 rmtree + copytree：Windows Defender 下 rmtree 76 个文件要 79 秒，
    29 例就是 40 分钟。复制一次、逐例覆写/还原即可。

★ 每例必须报告「是哪个判据拦下的」，否则无法区分「拦对了」与「被别的判据代答」。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="aps_neg_")

FILES = ["main.m", "devkit/build.sh", "devkit/mkdeb.sh",
         "AwemePipSwitch.plist", "tools/check_source.py"]

# ---------------------------------------------------------------- 准备副本
# ★ 只复制检查用得到的文件，不 rmtree（见文件头说明）
for f in FILES:
    src = os.path.join(ROOT, f)
    dst = os.path.join(TMP, f)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)

_backup = {}
for f in FILES:
    _backup[f] = open(os.path.join(ROOT, f), "rb").read()


def restore():
    for f, data in _backup.items():
        with open(os.path.join(TMP, f), "wb") as fh:
            fh.write(data)


def write(rel, content):
    with open(os.path.join(TMP, rel), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def read(rel):
    return open(os.path.join(TMP, rel), encoding="utf-8").read()


def run_gate():
    """跑闸门，返回 (rc, stdout)。"""
    p = subprocess.run(
        [sys.executable, os.path.join(TMP, "tools/check_source.py")],
        cwd=TMP, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


total = 0
skipped = 0
failed = []


def case(name, mutate, expect_gate):
    """注入一处违规，断言闸门必须红，且必须是 expect_gate 这条判据拦下的。

    ★★ expect_gate 不能省：只验「红了」不够 —— 注入可能顺带破坏了别的
      判据（比如换了引号让括号不配平），于是「红了」但红错了地方，
      真正想验的那条闸门可能压根没跑。
    """
    global total, skipped
    total += 1
    restore()
    try:
        mutate()
    except Exception as e:
        failed.append("%s：注入失败 %s" % (name, e))
        return
    rc, out = run_gate()
    if rc == 0:
        failed.append("%s：✗ 注入后闸门仍然全过（判据没在跑！）" % name)
        return
    if expect_gate not in out:
        failed.append("%s：✗ 闸门红了但不是 %r 拦下的\n      实际输出：%s"
                      % (name, expect_gate, out.strip().replace("\n", " | ")[:400]))
        return
    print("  ✓ %-42s 已拦下（%s）" % (name, expect_gate))


# ================================================================
# A 组反向用例
# ================================================================
def m_a1():
    s = read("devkit/build.sh")
    s = s.replace("codesign --force --sign - --timestamp=none \"$BUILD/AwemePipSwitch.dylib\"",
                  "# codesign removed")
    write("devkit/build.sh", s)


case("A1 去掉 dylib 自签", m_a1, "A1 build.sh 自签 dylib")


def m_a2():
    s = read("devkit/build.sh")
    s = s.replace("@loader_path/.jbroot/Library/MobileSubstrate/DynamicLibraries/AwemePipSwitch.dylib",
                  "@rpath/AwemePipSwitch.dylib")
    write("devkit/build.sh", s)


case("A2 install_name 改成 @rpath", m_a2, "A2 install_name 用 RootHide 约定路径")


def m_a3():
    s = read("devkit/build.sh")
    s = s.replace('CPU="arm64"', 'CPU="arm64e"')
    write("devkit/build.sh", s)


case("A3 CPU 改 arm64e", m_a3, "A3 主 dylib 编 arm64")


def m_a5():
    s = read("AwemePipSwitch.plist")
    s = s.replace("com.ss.iphone.ugc.Aweme\"", "com.example.other\"")
    write("AwemePipSwitch.plist", s)


case("A5 注入过滤弄错 bundle", m_a5, "A5 注入过滤含 Aweme 主包")


def m_a6():
    s = read("devkit/mkdeb.sh")
    s = s.replace("if rel_root == 'DEBIAN' or rel_root.startswith('DEBIAN/'):",
                  "if False:")
    write("devkit/mkdeb.sh", s)


case("A6 data.tar 不再排除 DEBIAN/", m_a6, "A6 data.tar 排除 DEBIAN/")


def m_a7():
    s = read("devkit/mkdeb.sh")
    # 往 postinst here-doc 正文里塞 respring
    s = s.replace("printf '\\n'\nexit 0", "printf '\\n'\nsbreload\nexit 0")
    write("devkit/mkdeb.sh", s)


case("A7 postinst 正文加 sbreload", m_a7, "A7 postinst 正文不含 sbreload")


def m_a8():
    s = read("devkit/mkdeb.sh")
    s = s.replace("format=tarfile.GNU_FORMAT", "format=tarfile.PAX_FORMAT")
    write("devkit/mkdeb.sh", s)


case("A8 tar 改 PAX_FORMAT", m_a8, "A8 tar 用 GNU_FORMAT")


def m_a9():
    s = read("devkit/build.sh")
    write("devkit/build.sh", s.replace("\n", "\r\n"))


case("A9 build.sh 刷成 CRLF", m_a9, "A9 build.sh 是 LF（无 CR）")


# ================================================================
# B 组反向用例
# ================================================================
def m_b1():
    s = read("main.m")
    s = s.replace("IMP old = method_setImplementation(m, newImp);",
                  "IMP old = MSHookMessageEx(m, newImp);")
    write("main.m", s)


case("B1 改回 MSHook", m_b1, "B1b 不用 MSHookMessageEx")


def m_b2():
    s = read("main.m")
    s = s.replace("static BOOL gPipInstalled = NO;",
                  "static BOOL gPipInstalled = NO;\nstatic void *PSDl(void){ return dlopen(\"x\", 2); }")
    write("main.m", s)


case("B2 加入 dlopen", m_b2, "B2 不 dlopen")


def m_b3():
    """把堆槽位改成传 NULL（经典的『不回填』bug）。两处都要改坏。"""
    s = read("main.m")
    n = s.count("cb, outOrigSlot);")
    assert n >= 2, "注入点应有两处（double/CMTime），实际 %d" % n
    s = s.replace("cb, outOrigSlot);", "cb, NULL);")
    write("main.m", s)


case("B3 两个签名 block 都不传槽位", m_b3, "B3b 两个签名 block 都传堆槽位")


def m_b4():
    s = read("main.m")
    s = s.replace("IMP old = method_setImplementation(m, newImp);",
                  "IMP old = newImp;")
    write("main.m", s)


case("B4 PSHookup 内不再替换实现", m_b4, "B4d PSHookup 内真的替换实现")


def m_b5():
    s = read("main.m")
    s = s.replace("static int  gHookedCount = 0;",
                  "static int  gHookedCount = 0;\nstatic id gTmp = [NSMutableSet set];")
    write("main.m", s)


case("B5 引入 NSMutableSet", m_b5, "B5 幂等存储不用 NSMutableSet")


def m_b6():
    s = read("main.m")
    s = s.replace("    return (*p == 'd');", "    return YES;")
    write("main.m", s)


case("B6 签名分派永远当 double", m_b6, "B6d 判 double 类型字符")


def m_b8():
    """删掉所有 orig 为 NULL 时的自回调 —— 最危险的一条。"""
    s = read("main.m")
    n = s.count("    else if (cb) cb(NO);")
    assert n >= 1, "注入点没找到"
    s = s.replace("    else if (cb) cb(NO);", "")
    write("main.m", s)


case("B8 orig 为 NULL 时不回调（全部删掉）", m_b8, "B8d orig 为 NULL 时自己回调")


def m_b8h():
    """去掉槽位判空 —— 空指针解引用，崩在系统回调线程。"""
    s = read("main.m")
    old = "PSPipSkipImp orig = outOrigSlot ? (PSPipSkipImp)*outOrigSlot : NULL;"
    assert old in s, "注入点没找到"
    s = s.replace(old, "PSPipSkipImp orig = (PSPipSkipImp)*outOrigSlot;")
    write("main.m", s)


case("B8h 槽位不判空（空指针解引用）", m_b8h, "B8h 解引用槽位前判空")


def m_b10():
    """去掉构造器最外层的 @try（内层 @try 保留 → 考验判据是否锚对了地方）。"""
    s = read("main.m")
    old = """    @try {
        // hook 必须在抖音创建画中画 delegate 之前装上，构造器里做最稳。
        PSInstallPipHook();"""
    assert old in s, "注入点没找到（缩进/文本不符）"
    new = """    {
        // hook 必须在抖音创建画中画 delegate 之前装上，构造器里做最稳。
        PSInstallPipHook();"""
    s = s.replace(old, new)
    write("main.m", s)


case("B10 构造器去掉最外层 @try", m_b10, "B10b 构造器**最外层**就是 @try")


def m_b13():
    s = read("main.m")
    s = s.replace("(int)method_getNumberOfArguments(m) != 5", "(int)method_getNumberOfArguments(m) != 99")
    write("main.m", s)


case("B13 参数个数校验改错", m_b13, "B13 校验参数个数为 5")


# ================================================================
# C 组反向用例
# ================================================================
def m_c1():
    s = read("main.m")
    s = s.replace(
        'NSSelectorFromString(@"pictureInPictureController:skipByInterval:completionHandler:")',
        'NSSelectorFromString(@"pictureInPictureController:skipByInterval:completion:")')
    write("main.m", s)


case("C1 selector 少 Handler 后缀", m_c1, "C1 selector 带 completionHandler 后缀")


def m_c2():
    s = read("main.m")
    s = s.replace('"AWEPictureInPictureSampleBufferPlayerView",', '"SomeOtherClass",')
    write("main.m", s)


case("C2 硬编码兜底类名改错", m_c2, "C2c 有硬编码兜底类名")


def m_c4():
    s = read("main.m")
    s = s.replace("return (ad >= 8.5 && ad <= 16.5);", "return (ad >= 9.5 && ad <= 10.5);")
    write("main.m", s)


case("C4 时间差判据收窄到 ±10", m_c4, "C4b 判据 8.5~16.5 秒在函数体内")


def m_c6():
    s = read("main.m")
    s = s.replace("#define PIP_DEBOUNCE 1.5", "#define PIP_DEBOUNCE 10.0")
    write("main.m", s)


case("C6 防抖窗口放大到 10 秒", m_c6, "C6c 防抖窗口是 1.5 秒")


def m_c7():
    s = read("main.m")
    s = s.replace("""    dispatch_async(dispatch_get_main_queue(), ^{
        PSSwitchFeed(next);
    });""",
                  """    PSSwitchFeed(next);""")
    write("main.m", s)


case("C7 切视频不在主队列", m_c7, "C7 切视频切到主队列")


def m_c8():
    s = read("main.m")
    s = s.replace("UIScrollView *sv = PSFeedScrollView(YES);",
                  "UIScrollView *sv = PSFeedScrollView(NO);")
    write("main.m", s)


case("C8 不放行 hidden 窗口", m_c8, "C8b PSSwitchFeed 用 allowHidden=YES")


def m_c9():
    s = read("main.m")
    s = s.replace("""                    [tv scrollToRowAtIndexPath:[NSIndexPath indexPathForRow:target
                                                                 inSection:cur.section]
                              atScrollPosition:UITableViewScrollPositionTop
                                      animated:YES];
                    acted = YES;""", "                    /* removed */")
    write("main.m", s)


case("C9 删掉 UITableView 路径", m_c9, "C9 保留 UITableView 路径")


def m_c10():
    s = read("main.m")
    s = s.replace("if (sv.contentSize.height < sv.bounds.size.height * 2) return;", "")
    write("main.m", s)


case("C10 去掉两屏门槛", m_c10, "C10d 至少能滚两屏")


def m_c11():
    s = read("main.m")
    s = s.replace("""        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{
            @try { PSInstallPipHook(); } @catch (NSException *e) { }
        });""", "")
    write("main.m", s)


case("C11 去掉延迟补装", m_c11, "C11d 构造器有延迟补装")


def m_c12():
    s = read("main.m")
    s = s.replace("""    if (byProto + byScan + byKnown > 0) {
        gPipInstalled = YES;   // 只有真装上了才标记完成，否则留给补枚举
    }""",
                  """    gPipInstalled = YES;""")
    write("main.m", s)


case("C12 无条件置完成标记", m_c12, "C12 只有装上类才置 gPipInstalled")


# ================================================================
# D 组反向用例（把"整合版"的东西加回来，闸门必须拦住）
# ================================================================
def m_d1():
    s = read("main.m")
    s = s.replace("static BOOL gPipInstalled = NO;",
                  "static BOOL gPipInstalled = NO;\n"
                  "static void PSCmd(void){ MPRemoteCommandCenter *c=nil; (void)c; }")
    write("main.m", s)


case("D1 加回锁屏遥控", m_d1, "D1 不含锁屏遥控 MPRemoteCommandCenter")


def m_d2():
    s = read("main.m")
    s = s.replace("static BOOL gPipInstalled = NO;",
                  "static BOOL gPipInstalled = NO;\n"
                  "static void PSSeek(id p){ [p seekToTime:kCMTimeZero]; }")
    write("main.m", s)


case("D2 加回 seek hook", m_d2, "D2 不 hook seek seekToTime")


def m_d3():
    s = read("main.m")
    s = s.replace("static BOOL gPipInstalled = NO;",
                  "static BOOL gPipInstalled = NO;\n"
                  "static UIApplication *gAppTmp = nil;\n"
                  "static void PSTouch(id o, id e){ [o sendEvent:e]; (void)gAppTmp; }")
    write("main.m", s)


case("D3 加回保持播放", m_d3, "D3 不含保持播放 sendEvent:")


def m_d4():
    s = read("main.m")
    s = s.replace("static BOOL gPipInstalled = NO;",
                  "static BOOL gPipInstalled = NO;\n"
                  "static void PSLog(void){ [[NSUserDefaults standardUserDefaults] synchronize]; }")
    write("main.m", s)


case("D4 加回 NSUserDefaults", m_d4, "D4 无开关/弹窗/文件 IO NSUserDefaults")


def m_d5():
    s = read("devkit/mkdeb.sh")
    s = s.replace('"$OUT"', '"$OUT"\nmkdir -p "$STAGE/Library/PreferenceBundles/X.bundle"')
    write("devkit/mkdeb.sh", s)


case("D5 加回设置页目录", m_d5, "D5 不打设置页 bundle")


def m_d6():
    s = read("devkit/mkdeb.sh")
    s = s.replace("Conflicts: com.liehuu.awemenextprev\n", "")
    s = s.replace("Replaces: com.liehuu.awemenextprev\n", "")
    write("devkit/mkdeb.sh", s)


case("D6 去掉 Conflicts", m_d6, "D6 声明 Conflicts 旧包")


def m_d7():
    s = read("devkit/mkdeb.sh")
    s = s.replace("Depends: mobilesubstrate", "Depends: mobilesubstrate, preferenceloader")
    write("devkit/mkdeb.sh", s)


case("D7 加回 preferenceloader 依赖", m_d7, "D7b 不依赖 preferenceloader")


# ================================================================
# 还原 + 汇总
# ================================================================
restore()

# 自证：还原后必须仍然全绿（证明还原有效，结论不是被注入污染的）
rc, out = run_gate()
if rc != 0:
    print("\n✗ 还原后闸门不绿，反向验证过程污染了副本：\n%s" % out)
    sys.exit(1)

print()
if failed:
    print("✗ 反向验证失败 %d 例 / 共 %d 例：" % (len(failed), total))
    for f in failed:
        print("   ·", f)
    print("\n（副本保留在 %s）" % TMP)
    sys.exit(1)

print("✓ 反向验证全过（%d 例，零 SKIP）" % total)
shutil.rmtree(TMP, ignore_errors=True)