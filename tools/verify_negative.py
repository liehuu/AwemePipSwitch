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

# ★★ 注入锚点：一个「文件作用域、唯一、且删了也不影响别处」的声明。
#   ★ 1.0-3 换了全局状态变量（gPipInstalled → gPipHookedCount），
#     旧的注入锚点文本随之失效 —— 反向用例会静默变成「注入不进去」。
#     这正是「改实现必须同步改判据/用例」的实例。
#   ★ 用 grep 确认唯一性：ANCHOR 在 main.m 里只能出现 1 次，
#     否则 replace 会把多处同时改掉，注入结果不可控。
ANCHOR = "static int  gPipHookedCount = 0;"
assert open(os.path.join(ROOT, "main.m"), encoding="utf-8").read().count(ANCHOR) == 1, \
    "ANCHOR 在 main.m 里不唯一，注入会不可控"


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
    s = s.replace(ANCHOR,
                  ANCHOR + '\nstatic void *PSDl(void){ return dlopen("x", 2); }')
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
    old = "    @try {\n        // hook 必须在抖音创建画中画 delegate 之前装上，构造器里做最稳。"
    assert old in s, "注入点没找到（缩进/文本不符）"
    new = "    {\n        // hook 必须在抖音创建画中画 delegate 之前装上，构造器里做最稳。"
    s = s.replace(old, new)
    # 外层 @catch 一并去掉，否则语法不通（但内层 @catch 还在，仍能考验判据）
    s = s.replace("    } @catch (NSException *e) {\n"
                  "        // 绝不清已装好的 orig 槽位",
                  "    }\n"
                  "    if (0) {\n"
                  "        // 绝不清已装好的 orig 槽位")
    write("main.m", s)


case("B10 构造器去掉最外层 @try", m_b10, "B10b 构造器**最外层**就是 @try")


def m_b13():
    s = read("main.m")
    s = s.replace("(int)method_getNumberOfArguments(m) != 5", "(int)method_getNumberOfArguments(m) != 99")
    write("main.m", s)


case("B13 参数个数校验改错", m_b13, "B13b 校验参数个数为 5")


# ================================================================
# C 组反向用例
# ================================================================
def m_c1():
    s = read("main.m")
    # ★ 1.0-3 把 selector 收进了 PSPipSkipSelector()，字符串跨行书写，
    #   所以注入点要跟着换成实际文本。
    s = s.replace('@"pictureInPictureController:skipByInterval:completionHandler:"',
                  '@"pictureInPictureController:skipByInterval:completion:"')
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
    """删掉构造器里的后台补装调度（delegate 懒注册时就没有第二次机会）。"""
    s = read("main.m")
    old = "        PSStartBackgroundRetry();"
    assert old in s, "注入点没找到"
    s = s.replace(old, "        // 后台补装被删掉了", 1)
    write("main.m", s)


case("C11 去掉延迟补装", m_c11, "C11d 构造器有延迟补装")


def m_c12():
    s = read("main.m")
    # ★ 注入「装好也不收工」：把判据里的已装数去掉。
    #   （C12b 查的是 gPipHookedCount > 0 是否在 PSRetryTick 体内）
    s = s.replace(
        "if (ready || gPipHookedCount > 0 || gPipRetryTick > PS_RETRY_MAX_TICKS) return;",
        "if (gPipRetryTick > PS_RETRY_MAX_TICKS) return;")
    write("main.m", s)


case("C12 补装不看「已装数」→ 永不收工", m_c12, "C12b 装好即收工")


def m_c2e():
    """把 1.0-2 的「按协议逐类枚举」函数加回来 —— 就是那 10 秒的直接来源。"""
    s = read("main.m")
    s = s.replace(ANCHOR, ANCHOR + "\n"
                  "static int PSHookClassesConformingTo(Class c, SEL s) {\n"
                  "    (void)c; (void)s; return 0;\n"
                  "}")
    write("main.m", s)


case("C2e 加回协议全表枚举函数", m_c2e, "C2e 没有独立的全表协议枚举函数")


def m_c2f():
    """在扫描循环里加回 class_conformsToProtocol —— 5 万次遍历协议链。"""
    s = read("main.m")
    old = "            if (!class_respondsToSelector(cls, sel)) continue;"
    assert old in s, "注入点没找到"
    s = s.replace(old,
                  "            if (!class_respondsToSelector(cls, sel) &&\n"
                  "                !class_conformsToProtocol(cls, @protocol(NSObject))) continue;")
    write("main.m", s)


case("C2f 扫表内加回逐类判协议", m_c2f, "C2f 扫表内不逐类判 conform 协议")


# ================================================================
# D 组反向用例（把"整合版"的东西加回来，闸门必须拦住）
# ================================================================
def m_d1():
    s = read("main.m")
    s = s.replace(ANCHOR, ANCHOR + "\n"
                  "static void PSCmd(void){ MPRemoteCommandCenter *c=nil; (void)c; }")
    write("main.m", s)


case("D1 加回锁屏遥控", m_d1, "D1 不含锁屏遥控 MPRemoteCommandCenter")


def m_d2():
    s = read("main.m")
    s = s.replace(ANCHOR, ANCHOR + "\n"
                  "static void PSSeek(id p){ [p seekToTime:kCMTimeZero]; }")
    write("main.m", s)


case("D2 加回 seek hook", m_d2, "D2 不 hook seek seekToTime")


def m_d3():
    s = read("main.m")
    s = s.replace(ANCHOR, ANCHOR + "\n"
                  "static UIApplication *gAppTmp = nil;\n"
                  "static void PSTouch(id o, id e){ [o sendEvent:e]; (void)gAppTmp; }")
    write("main.m", s)


case("D3 加回保持播放", m_d3, "D3 不含保持播放 sendEvent:")


def m_d4():
    s = read("main.m")
    s = s.replace(ANCHOR, ANCHOR + "\n"
                  "static void PSLog(void){ [[NSUserDefaults standardUserDefaults] synchronize]; }")
    write("main.m", s)


case("D4 加回 NSUserDefaults", m_d4, "D4 无开关/弹窗/文件 IO NSUserDefaults")


def m_d5():
    s = read("devkit/mkdeb.sh")
    s = s.replace('"$OUT"', '"$OUT"\nmkdir -p "$STAGE/Library/PreferenceBundles/X.bundle"')
    write("devkit/mkdeb.sh", s)


case("D5 加回设置页目录", m_d5, "D5 不打设置页 bundle")


def m_d6():
    """往 control 正文里加回 Conflicts/Replaces（1.0-1 的真实错误）。

    ★ 必须注入到 here-doc **正文内部**（Depends 那行之后），
      不能加到 mkdeb.sh 注释里 —— 判据锚的是 control 正文，
      注入到注释里判据根本看不到，会得到「加了违规却仍全绿」的假象。
    """
    s = read("devkit/mkdeb.sh")
    s = s.replace("Depends: mobilesubstrate\nEOF",
                  "Depends: mobilesubstrate\n"
                  "Conflicts: com.liehuu.awemenextprev\n"
                  "Replaces: com.liehuu.awemenextprev\nEOF")
    write("devkit/mkdeb.sh", s)


case("D6 加回 Conflicts 旧包", m_d6, "D6 Conflicts 不针对 awemenextprev")


def m_d6b():
    """只加 Conflicts 不加 Replaces —— 仍必须被拦（判据逐字段查）。"""
    s = read("devkit/mkdeb.sh")
    s = s.replace("Depends: mobilesubstrate\nEOF",
                  "Depends: mobilesubstrate\n"
                  "Breaks: com.liehuu.awemenextprev\nEOF")
    write("devkit/mkdeb.sh", s)


case("D6b 加回 Breaks 旧包", m_d6b, "D6 Breaks 不针对 awemenextprev")


def m_d6z():
    """删掉 Description 里「可同装」的说明 —— 用户会误以为二选一。"""
    s = read("devkit/mkdeb.sh")
    old = (" 与「AwemeNextPrev-pure 1.0-2（锁屏上一首/下一首）」不冲突，可同装：\n"
           " 那个是纯公开 API、零 hook、只做锁屏遥控；本插件只做小窗。\n")
    s = s.replace(old, "")
    write("devkit/mkdeb.sh", s)


case("D6z 删掉可同装说明", m_d6z, "D6z Description 说明与 pure 1.0-2 可同装")


def m_d7():
    s = read("devkit/mkdeb.sh")
    s = s.replace("Depends: mobilesubstrate", "Depends: mobilesubstrate, preferenceloader")
    write("devkit/mkdeb.sh", s)


case("D7 加回 preferenceloader 依赖", m_d7, "D7b 不依赖 preferenceloader")


# ================================================================
# E 组反向用例（把 1.0-2 的启动卡顿写法加回来，闸门必须拦住）
#   ★★★ 这一组是 1.0-2 的真实事故：装完抖音冷启动要等 10 秒。
#     根因是 pre-main 构造器里同步做了 4 次全量 objc 类表扫描，
#     外加主线程 NSTimer 每秒重跑、连跑 30 秒。
#     每条反向用例都对应一个具体的「退化写法」。
# ================================================================
def m_e1():
    """构造器里直接调全表扫 —— 1.0-2 的原样写法。"""
    s = read("main.m")
    old = "        PSInstallFast();"
    assert old in s, "注入点没找到"
    s = s.replace(old, "        PSInstallByScan(PSPipSkipSelector());", 1)
    write("main.m", s)


case("E1 构造器里直接扫全表", m_e1, "E1 构造器体内不含 PSInstallByScan")


def m_e1b():
    """构造器里直接调 objc_getClassList。"""
    s = read("main.m")
    old = "        PSInstallFast();"
    assert old in s, "注入点没找到"
    s = s.replace(old,
                  "        (void)objc_getClassList(NULL, 0);\n        PSInstallFast();", 1)
    write("main.m", s)


case("E1b 构造器里取全量类表", m_e1b, "E1 构造器体内不含 objc_getClassList")


def m_e1c():
    """构造器里不再做 O(1) 快装 —— 前 8 秒进小窗会失效。"""
    s = read("main.m")
    old = "        PSInstallFast();"
    assert old in s, "注入点没找到"
    s = s.replace(old, "        // 快装被删掉了", 1)
    write("main.m", s)


case("E1c 删掉构造器的 O(1) 快装", m_e1c, "E1b 构造器调 O(1) 的 PSInstallFast")


def m_e2():
    """重扫排回主队列 —— 启动期 UI 直接卡住。"""
    s = read("main.m")
    old = """        dispatch_after(
            dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.0 * NSEC_PER_SEC)),
            PSWorkQueue(), ^{ PSRetryTick(); });"""
    assert old in s, "注入点没找到"
    s = s.replace(old, """        dispatch_after(
            dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.0 * NSEC_PER_SEC)),
            dispatch_get_main_queue(), ^{ PSRetryTick(); });""")
    write("main.m", s)


case("E2 重扫改排主队列", m_e2, "E2b 全表扫调度到后台队列")


def m_e3():
    """后台队列改成并发 —— 多路扫表互相抢 CPU。"""
    s = read("main.m")
    s = s.replace("DISPATCH_QUEUE_SERIAL", "DISPATCH_QUEUE_CONCURRENT")
    write("main.m", s)


case("E3 后台队列改并发", m_e3, "E3b 队列是串行的")


def m_e3c():
    """后台队列改成默认优先级 —— 与启动抢 CPU。"""
    s = read("main.m")
    s = s.replace("QOS_CLASS_UTILITY", "QOS_CLASS_USER_INTERACTIVE")
    write("main.m", s)


case("E3c 队列优先级提到交互级", m_e3c, "E3c 队列是低优先级")


def m_e4():
    """去掉装好即停 —— 装上了还继续每秒重扫，白烧 CPU。"""
    s = read("main.m")
    old = "        if (ready || gPipHookedCount > 0 || gPipRetryTick > PS_RETRY_MAX_TICKS) return;"
    assert old in s, "注入点没找到"
    s = s.replace(old, "        if (gPipRetryTick > PS_RETRY_MAX_TICKS) return;")
    write("main.m", s)


case("E4 补装去掉装好即停", m_e4, "C12b 装好即收工")


def m_e4c():
    """把重扫间隔改成每秒 —— 1.0-2 的卡顿成因（主线程 + 每秒 4 次）。"""
    s = read("main.m")
    s = s.replace("#define PS_SCAN_EVERY_TICKS  5", "#define PS_SCAN_EVERY_TICKS  1")
    write("main.m", s)


case("E4c 重扫间隔改每秒", m_e4c, "E4c 全表扫间隔 >= 3 秒")


def m_e4d():
    """再加一个重扫调用点 —— 多头重扫。"""
    s = read("main.m")
    old = "        if (ready || gPipHookedCount > 0 || gPipRetryTick > PS_RETRY_MAX_TICKS) return;"
    assert old in s, "注入点没找到"
    s = s.replace(old,
                  "        PSInstallByScan(sel);\n" + old, 1)
    write("main.m", s)


case("E4d 加第二个重扫调用点", m_e4d, "E4d PSInstallByScan 只有一个调用点")


def m_e5():
    """把首次全表扫延后改到 1 秒 —— 与首帧抢 CPU。"""
    s = read("main.m")
    s = s.replace("#define PS_SCAN_FIRST_TICK   8", "#define PS_SCAN_FIRST_TICK   1")
    write("main.m", s)


case("E5 首次扫描延后改 1 秒", m_e5, "E5c 首次全表扫延后 >= 5 秒")


def m_e5d():
    """定义了延后常量却不用它 —— 判据必须能发现「常量空转」。"""
    s = read("main.m")
    old = "            gPipRetryTick >= PS_SCAN_FIRST_TICK &&"
    assert old in s, "注入点没找到"
    s = s.replace(old, "            YES &&")
    write("main.m", s)


case("E5d 首次延后常量空转（不用它）", m_e5d, "E5d 补装内真的校验了首次延后")


def m_c13():
    """★ 功能回退防护：把补装缩成「只试硬编码类」。

    这正是我第一版修法犯的错 —— 为了让启动更快，把补装里的全表扫删了。
    后果：抖音 lite 版 / 类名不同 / 懒注册的 delegate 永远装不上
    → 小窗点了完全没反应。性能修好但功能没了，必须被拦住。
    """
    s = read("main.m")
    old = """        // 还没装好 → 周期性重扫全表兜住「类名未知 / 懒注册」的情况
        if (!ready && gPipHookedCount == 0 && sel &&
            gPipRetryTick >= PS_SCAN_FIRST_TICK &&
            (gPipRetryTick % PS_SCAN_EVERY_TICKS) == 0) {
            int n = PSInstallByScan(sel);
            if (n > 0) {
                gPipHookedCount = n;
                ready = YES;
            }
        }"""
    assert old in s, "注入点没找到"
    s = s.replace(old, "        // 补装里不再重扫全表")
    write("main.m", s)


case("C13 补装缩成只试硬编码类（功能回退）", m_c13, "C13 补装内仍会重扫全表")


def m_e6():
    """去掉廉价预筛 —— 对 5 万个类逐个复制方法表（就是那 10 秒）。"""
    s = read("main.m")
    old = "            if (!class_respondsToSelector(cls, sel)) continue;"
    assert old in s, "注入点没找到"
    s = s.replace(old, "")
    write("main.m", s)


case("E6 去掉扫表的廉价预筛", m_e6, "E6 扫表内有廉价预筛 class_respondsToSelector")


def m_e7():
    """改回主线程 NSTimer 轮询 —— 1.0-2 的卡顿来源之一。"""
    s = read("main.m")
    old = "        PSStartBackgroundRetry();"
    assert old in s, "注入点没找到"
    s = s.replace(old,
                  "        [NSTimer scheduledTimerWithTimeInterval:1.0\n"
                  "                                        repeats:YES\n"
                  "                                          block:^(NSTimer *t) { }];\n"
                  "        PSStartBackgroundRetry();", 1)
    write("main.m", s)


case("E7 加回主线程 NSTimer 轮询", m_e7, "E7 不用 NSTimer")


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