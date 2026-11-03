# -*- coding: utf-8 -*-
"""AwemePipSwitch 源码闸门。

★ 设计原则（沿用本项目踩过的教训）：
  · 判据必须锚「被检查的那一段」：查函数必须限定到函数体内，
    不能全文件搜索 —— 否则别处的同名串会代答。
  · 「用 in 判代码在不在」要先想「删掉后子串还在不在」。
  · 新增检查项必须反向验证（见 verify_negative.py），否则「通过」
    可能只是「没在跑」。
  · 查字符串字面量要分清编码：含中文的 @"..." 会被 clang 存成 UTF-16
    （__ustring），ASCII 搜索命中 0。所以本插件的可搜 tag 全是纯 ASCII。

检查维度（都是「装了会崩 / 装了不生效 / 装了卡死」，不只查「编不过」）：
  A 组：注入与加载的前提（错了 = 装了完全没反应）
  B 组：hook 装配的正确性（错了 = 崩宿主 或 静默失效）
  C 组：功能判据（错了 = 点了没反应）
  D 组：只做一件事的禁项（错了 = 又变成"整合版"）
  E 组：启动路径性能（错了 = 启动慢十几秒，1.0-2 的真实事故）
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(ROOT, "main.m")
BUILD_SH = os.path.join(ROOT, "devkit", "build.sh")
MKDEB_SH = os.path.join(ROOT, "devkit", "mkdeb.sh")
PLIST = os.path.join(ROOT, "AwemePipSwitch.plist")

errors = []
checks = 0


def ok(cond, name, hint=""):
    global checks
    checks += 1
    if not cond:
        errors.append("%s%s" % (name, ("｜" + hint) if hint else ""))


def strip_comments(s):
    """去掉 // 与 /* */ 注释（保留字符串字面量，判据要靠它）。"""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                if s[i] == "\\":
                    out.append(s[i:i + 2])
                    i += 2
                    continue
                out.append(s[i])
                if s[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def body_of(code, func_name):
    """取出 C 函数体（配平花括号）。

    ★★★ 为什么必须这么做：本项目真实事故是「新增了正确实现，
    另一处旧的错误写法还在」，全文件搜索会被正确的那份代答。
    判据一旦锚到函数体，就只能被这个函数自己的内容满足。
    """
    m = re.search(r"\b" + re.escape(func_name) + r"\s*\([^;{]*\)\s*\{", code)
    if not m:
        return None
    start = m.end() - 1
    depth = 0
    i = start
    n = len(code)
    while i < n:
        ch = code[i]
        if ch == '"':
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2
                    continue
                if code[i] == '"':
                    break
                i += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
        i += 1
    return None


src_raw = open(SRC_PATH, encoding="utf-8").read()
src = strip_comments(src_raw)
build = open(BUILD_SH, encoding="utf-8").read()
mkdeb = open(MKDEB_SH, encoding="utf-8").read()

# ============================================================
# A 组：注入与加载的前提
# ============================================================

# A1 dylib 必须自签（RootHide/ellekit 会跳过无 LC_CODE_SIGNATURE 的 dylib）
ok("codesign --force --sign -" in build,
   "A1 build.sh 自签 dylib",
   "RootHide 会静默跳过未签名 dylib → 装了完全没反应")

# A2 install_name 必须是 @loader_path/.jbroot/...
ok("@loader_path/.jbroot/Library/MobileSubstrate/DynamicLibraries/AwemePipSwitch.dylib"
   in build,
   "A2 install_name 用 RootHide 约定路径",
   "其它写法（空/@rpath）会被静默跳过")

# A3 必须编 arm64（抖音进程是 arm64，不是 arm64e）
ok('CPU="arm64"' in build and 'TRIPLE="arm64-apple-ios14.0"' in build,
   "A3 主 dylib 编 arm64",
   "编成 arm64e 带 ptrauth，arm64 进程不会加载 → 装了没反应")

# A4 Arch 标签必须跟着参数走（否则 Sileo 不肯装）
ok('ARCH_NAME="${1:' in build, "A4 ARCH 取第一个参数")
ok('Architecture: $ARCH' in mkdeb, "A4b control 的 Architecture 用变量")

# A5 注入过滤必须限定抖音两个 bundle
pl = open(PLIST, encoding="utf-8").read()
ok("com.ss.iphone.ugc.Aweme" in pl, "A5 注入过滤含 Aweme 主包")
ok("com.ss.iphone.ugc.aweme.lite" in pl, "A5b 注入过滤含 lite 包")
ok("Filter" in pl and "Bundles" in pl, "A5c Filter/Bundles 结构正确")

# A6 data.tar 不得含 DEBIAN/（rootless 上 / 只读 → 整包装不上）
ok("rel_root == 'DEBIAN' or rel_root.startswith('DEBIAN/')" in mkdeb,
   "A6 data.tar 排除 DEBIAN/",
   "含 DEBIAN/ 会解到只读的 /DEBIAN → 整包装不上")

# A7 postinst 绝不许 respring / killall SpringBoard
#    ★★★ 判据必须锚到 postinst 的**实际内容**，不能全文件搜：
#      mkdeb.sh 的注释里正好写着「绝不 respring / sbreload / killall SpringBoard」
#      这句说明——全文件搜会命中这条注释，把「遵守纪律」判成「违反纪律」。
#      （这就是本项目的老坑：判据锚错地方 → 注释代答。）
def postinst_body(script):
    """取出 `cat > .../postinst` here-doc 的实际内容。"""
    m = re.search(
        r"cat\s*>\s*\"?\$?\{?\w*\}?/DEBIAN/postinst\"?\s*<<\s*'?(\w+)'?\s*\n(.*?)\n\1\s*$",
        script, re.S | re.M)
    return m.group(2) if m else None


pi_body = postinst_body(mkdeb)
ok(pi_body is not None, "A7 能找到 postinst here-doc 内容",
   "找不到就无法检查，不能当成通过")
if pi_body:
    for bad in ("sbreload", "killall SpringBoard", "respring", "launchctl kickstart"):
        ok(bad not in pi_body, "A7 postinst 正文不含 %s" % bad,
           "dpkg 事务最后一步杀前台 Sileo → 锁残留 + 半装")
    # 正项：postinst 必须真的存在并且有提示（用户要知道怎么用）
    ok("已安装" in pi_body, "A7z postinst 有安装提示")


# ★★ 同理：control 的字段判据也必须锚到 control here-doc 正文。
#    mkdeb.sh 的注释里写着「不写任何 Conflicts / Replaces —— 1.0-1 曾误写」，
#    全文件搜 "Conflicts" 会命中这句**说明**，把「没有冲突声明」判成「有」。
#    （和 A7 一模一样的坑，判据必须锚被检查的那一段。）
def control_body(script):
    """取出 `cat > .../DEBIAN/control` here-doc 的实际内容。"""
    m = re.search(
        r"cat\s*>\s*\"?\$\{?\w*\}?/DEBIAN/control\"?\s*<<\s*'?(\w+)'?\s*\n(.*?)\n\1\s*$",
        script, re.S | re.M)
    return m.group(2) if m else None


ct_body = control_body(mkdeb)
ok(ct_body is not None, "D6 能找到 control here-doc 内容",
   "找不到就无法检查，不能当成通过")

# A8 tar 必须是 GNU_FORMAT（PAX 扩展头 dpkg 不认）
ok("format=tarfile.GNU_FORMAT" in mkdeb, "A8 tar 用 GNU_FORMAT")
ok("_check_no_pax_headers" in mkdeb, "A8b 有 PAX 扩展头自检")

# A9 .sh 必须是 LF（CRLF 会让 macOS runner 报 set: -: invalid option）
for p, label in ((BUILD_SH, "build.sh"), (MKDEB_SH, "mkdeb.sh")):
    raw = open(p, "rb").read()
    ok(b"\r" not in raw, "A9 %s 是 LF（无 CR）" % label,
       "CRLF 会让 CI 报 set: -: invalid option，脚本一启动就退出")
    ok(raw.startswith(b"#!"), "A9b %s 有 shebang" % label)

# A10 dylib 755（644 会被加载器拒绝）
ok("chmod 755" in mkdeb and ".dylib" in mkdeb, "A10 mkdeb 给 dylib 755")

# ============================================================
# B 组：hook 装配的正确性（错了会崩宿主或静默失效）
# ============================================================

# B1 只能用 method_setImplementation，不许 MSHook（父子类交叉会无限递归）
ok("method_setImplementation" in src, "B1 用 method_setImplementation")
for bad in ("MSHookMessageEx", "MSHookMessage"):
    ok(bad not in src, "B1b 不用 %s" % bad,
       "MSHook 的 trampoline 在父子类交叉时 orig 会指向 trampoline → 无限递归")

# B2 绝不许 dlopen 第二份 substrate（双引擎改同一张方法表 → 崩）
ok(not re.search(r'\bdlopen\s*\(', src), "B2 不 dlopen",
   "RootHide 下同库多路径可见 → 可能加载第二份 → 双引擎崩")

# B3 orig 槽位必须在堆上，block 捕获「地址」而不是值
#    ★★ 判据必须要求**两个** block（double 版 + CMTime 版）都传槽位。
#      只要求「出现过 outOrigSlot」会被另一条签名代答
#      —— 反向验证实证：只把 double 版改坏，旧判据照样全绿。
pb = body_of(src, "PSMakePipSkipImp")
ok(pb is not None, "B3 能找到 PSMakePipSkipImp 函数体")
if pb:
    n_slot = pb.count("outOrigSlot)")
    ok(n_slot >= 2, "B3b 两个签名 block 都传堆槽位（出现 %d 次，需 >=2）" % n_slot,
       "捕获 __block 值不会回填 → orig 恒 NULL → 功能静默失效")
    # 正项：两条签名分支都要在
    ok("NSTimeInterval interval" in pb, "B3c double 版 block 在函数体内")
    ok("CMTime interval" in pb, "B3d CMTime 版 block 在函数体内")

# B4 幂等闸必须真的在（重复替换 → orig 指向自己 → 自递归崩）
ok("gHookedKeys" in src and "gHookedCount" in src,
   "B4 有幂等闸（gHookedKeys/gHookedCount）")
hb = body_of(src, "PSHookup")
ok(hb is not None, "B4b 能找到 PSHookup 函数体")
if hb:
    ok("strcmp(gHookedKeys[i], key)" in hb, "B4c 幂等判据在 PSHookup 体内",
       "全文件搜索会被别处同名串代答")
    ok("method_setImplementation" in hb, "B4d PSHookup 内真的替换实现")

# B5 幂等存储不得依赖懒初始化对象（NSMutableSet 为 nil 时闸门静默失效）
ok("NSMutableSet" not in src, "B5 幂等存储不用 NSMutableSet",
   "为 nil 时 containsObject 恒 NO → 闸门静默失效 → 重复替换 → 递归崩")
ok("PTHREAD_MUTEX_INITIALIZER" in src, "B5b 用静态初始化的互斥锁")

# B6 签名分派必须有（CMTime vs double 装错 → block 从错误寄存器读 → 崩）
ok("PSEncArg3IsDouble" in src, "B6 有第三参签名分派",
   "装错签名 block 指针从错误寄存器读 → 调 completion 直接崩宿主")
eb = body_of(src, "PSEncArg3IsDouble")
ok(eb is not None, "B6b 能找到 PSEncArg3IsDouble 函数体")
if eb:
    ok("@16" in eb, "B6c 签名判据从 @16 之后的类型字符取",
       "@16 = self/_cmd 之后的第一实参之后")
    ok("'d'" in eb or '"d"' in eb or "'d' ==" in eb, "B6d 判 double 类型字符")

# B7 两种签名都要有对应 block（double 与 CMTime）
ok("NSTimeInterval interval" in src, "B7 有 double 签名 block")
ok("CMTime interval" in src, "B7b 有 CMTime 签名 block")

# B8 所有出口都必须回调 completion（不回调 → 系统画中画卡死/被关）
sb = body_of(src, "PSPipSkipBody")
ok(sb is not None, "B8 能找到 PSPipSkipBody 函数体")
if sb:
    # ★★ 判据按「出口路径」验，不按 cb( 出现次数数：
    #   PSPipSkipBody 里正常路径把 cb 传给 PSHandlePipSkip 与 orig 转发，
    #   只有「orig 不存在」时才自己 cb(NO)。数次数会数错，验路径才准。
    #   第一条出口：接管成功走 PSHandlePipSkip（它会回调 completion）
    ok("PSHandlePipSkip(interval, cb)" in sb,
       "B8b 接管路径把 cb 交给 PSHandlePipSkip")
    #   第二条出口：切视频抛异常 / 不接管 → 两条路都要转发原实现（带 cb）
    #   ★★ 必须要求**两处**转发都带 cb：源码里有两处 `if (orig) orig(...)`
    #      （异常回退一处、不接管一处）。只要求「出现过一次」会被另一处代答
    #      —— 反向验证实证过。
    n_fwd = len(re.findall(
        r"if\s*\(orig\)\s*orig\(obj,\s*origSel,\s*ctl,\s*interval,\s*cb\)", sb))
    ok(n_fwd >= 2, "B8c 两处转发原实现都带上 cb（出现 %d 次，需 >=2）" % n_fwd)
    #   第三条出口：orig 为 NULL 时必须自己回调，否则系统永远等
    #   ★★ 同样要求 >=2 处（异常回退、不接管各一处）
    n_null = len(re.findall(r"else if \(cb\) cb\(NO\)", sb))
    ok(n_null >= 2, "B8d orig 为 NULL 时自己回调 cb(NO)（出现 %d 次，需 >=2）" % n_null,
       "漏一条 → 系统一直等 completion → 按钮点了没反应甚至关小窗")
    ok("@catch" in sb, "B8e 异常有兜底")
    # ★★★ 崩宿主级：槽位指针必须判空再解引用。
    #   `(PSPipSkipImp)*outOrigSlot` 在 outOrigSlot 为 NULL 时是空指针解引用，
    #   而且崩在系统画中画回调线程里 —— 日志都留不下，只能看到闪退。
    ok("outOrigSlot ?" in sb, "B8h 解引用槽位前判空",
       "*outOrigSlot 不判空 = 空指针解引用，崩在系统回调线程里，日志留不下")
    # 不许出现「判 YES 进来却直接 return 不回调」的早退
    ok(not re.search(r"if\s*\(handle\)\s*\{\s*return;", sb),
       "B8f 接管分支不许直接 return（必须走回调或转发）")

# B8g 冷却期分支也必须回调（它是另一条独立出口）
hd0 = body_of(src, "PSHandlePipSkip")
if hd0:
    ok(re.search(r"PIP_DEBOUNCE\)\s*\{[^}]*completion\(YES\)", hd0) is not None,
       "B8g 冷却期分支也回调 completion(YES)")

# B9 切视频抛异常时必须退回原实现，不许吞掉 completion
ok("done" in src and "if (done) return;" in src,
   "B9 切视频异常时退回原实现",
   "吞掉 completion 会让系统超时")

# B10 构造器整体包 @try（任何异常都不许影响宿主启动）
ctor = body_of(src, "PSCtor")
ok(ctor is not None, "B10 能找到 PSCtor 函数体")
if ctor:
    # ★★ 判据不能只查「@try 出现过」：函数体里有一堆内层 @try
    #   （延迟补装、定时器），外层没了也照样命中 → 反向验证会绿。
    #   ★★ 也不能用 `\{\s*@try` 直接匹配：函数体开头还有注释，
    #      \s* 跨不过注释。必须先在 body 内**去掉注释**再判。
    ctor_code = strip_comments(ctor)
    ok(re.match(r"^\{\s*@try\s*\{", ctor_code) is not None,
       "B10b 构造器**最外层**就是 @try（去注释后必须以 {@try{ 开头）",
       "只查 @try 出现过会被内层 @try 代答")
    ok("@catch" in ctor_code, "B10c 构造器有 @catch")
    # 构造器里所有可能抛异常的调用都必须在 @try 内
    # ★ 1.0-3 起构造器只调 O(1) 的 PSInstallFast + 后台调度（原来直接调
    #   全表扫的 PSInstallPipHook，正是启动卡 10 秒的根因）。
    ok("PSInstallFast();" in ctor_code, "B10d 构造器里真的调用安装函数")

# B11 枚举遍历逐个 @try（单个类抛异常不能中断整轮）
for fn in ("PSInstallByScan",):
    b = body_of(src, fn)
    ok(b is not None, "B11 能找到 %s 函数体" % fn)
    if b:
        ok("@try" in b and "@catch" in b, "B11b %s 逐个 @try 兜底" % fn)

# B12 只 hook「类自己声明」的方法（继承来的由父类覆盖，避免多包一层）
cd = body_of(src, "PSClassDeclaresSelector")
ok(cd is not None, "B12 能找到 PSClassDeclaresSelector 函数体")
if cd:
    ok("class_copyMethodList" in cd, "B12b 用 class_copyMethodList 查自身方法表",
       "class_getInstanceMethod 会顺继承链返回父类方法，据此 hook 会多包一层")

# B13 校验签名参数个数 = 5（self/_cmd/ctl/interval/completion）
#   ★ 1.0-3 起这段校验集中在 PSTryHookClass 里（三个入口共用一份）。
tb = body_of(src, "PSTryHookClass")
ok(tb is not None, "B13 能找到 PSTryHookClass 函数体")
if tb:
    ok("method_getNumberOfArguments(m) != 5" in tb, "B13b 校验参数个数为 5")
    ok("enc[0] != 'v'" in tb, "B13c 校验返回 void")
    ok("PSClassDeclaresSelector" in tb, "B13d 仍只认「自己声明」的方法")
    ok("PSHookup" in tb, "B13e 走幂等闸装配")
    ok("*slot = (void *)old" in tb, "B13f 装上后回填 orig 槽位",
       "不回填 → orig 恒 NULL → 功能静默失效")

# B14 「已装过」必须与「装不上」区分开
#   ★★★ 这是 1.0-2 启动卡顿的第二段根因：补装只看「本次装上了几个」，
#     而幂等闸会把「已装过」挡回 NO → 补装永远认为失败 → 每秒重跑全表扫
#     连跑 30 秒。
ok("PSKeyRegistered" in src, "B14 有「是否已登记」的查询函数")
if tb:
    ok("PSTryAlready" in tb, "B14b 已装过单独返回 PSTryAlready（不算失败）")
kb = body_of(src, "PSKeyRegistered")
ok(kb is not None, "B14c 能找到 PSKeyRegistered 函数体")
if kb:
    ok("gHookedKeys" in kb and "gHookedCount" in kb,
       "B14d 查询读的是同一份幂等表（不能另建一份）")
    ok("pthread_mutex_lock" in kb, "B14e 查询也加锁（补装在后台线程）")

# ============================================================
# C 组：功能判据（错了就没反应）
# ============================================================

# C1 selector 必须带 Handler 后缀（少一个单词 = 0 命中 = 完全没反应）
ok("pictureInPictureController:skipByInterval:completionHandler:" in src,
   "C1 selector 带 completionHandler 后缀",
   "写成 completion: 是 0 命中，小窗点了没任何反应（2.1-5 实锤）")
ok("pictureInPictureController:skipByInterval:completion:" not in src,
   "C1b 不用错误的旧 selector 名")

# C2 装配路必须覆盖「谁实现了这个方法」（1.0-3 起合并为一趟全扫）
#   ★★★ 1.0-2 有三条路：协议枚举（3 个协议名 → 3 次全表扫）+ selector 全扫
#     + 硬编码兜底。前两条里，协议路的条件是 selector 路的**严格子集**
#     （A∧B ⊆ B）→ 那 3 次全表扫是纯浪费，正是启动卡 10 秒的主因。
#     1.0-3 只保留「selector 全扫 + 硬编码兜底」两条，覆盖范围不变。
ok("PSInstallByScan(" in src, "C2b 有 selector 全扫路")
ok("AWEPictureInPictureSampleBufferPlayerView" in src, "C2c 有硬编码兜底类名",
   "这个类名是实机日志实证的落点，三条枚举路都可能落空")
# 正项：兜底类必须在构造器阶段就被尝试（O(1)，不等全表扫）
ok("PSInstallKnownClasses" in src, "C2d 硬编码类有独立安装入口")

# C2e 绝不许把「按协议逐类枚举」加回来（启动卡 10 秒的直接来源）
ok("PSHookClassesConformingTo" not in src,
   "C2e 没有独立的全表协议枚举函数",
   "协议路的条件是 selector 路的严格子集 → 多走一遍全表纯浪费，就是那 10 秒")

# C3 三个协议候选名仍保留（诊断用，不参与装配）
for proto in ("AVPictureInPictureSampleBufferPlaybackDelegate",
              "AWEPictureInPictureSampleBufferPlaybackDelegate",
              "AWEPictureInPicturePlaybackDelegate"):
    ok(proto in src, "C3 协议候选含 %s" % proto)

# C3d 全表扫必须**只走一趟**（1.0-2 走了四趟 → 启动卡 10 秒）
sc = body_of(src, "PSInstallByScan")
ok(sc is not None, "C3d 能找到 PSInstallByScan 函数体")
if sc:
    ok(sc.count("objc_getClassList") == 2,
       "C3e 一趟扫表只取两次类表（NULL 探长度 + 填充）",
       "多次 objc_getClassList 循环 = 同一张表走多遍 = 启动卡死")
    # 扫表内必须有廉价预筛（class_respondsToSelector），否则逐类复制方法表
    ok("class_respondsToSelector" in sc,
       "C3f 扫表内先做廉价预筛 class_respondsToSelector",
       "对 5 万个类逐个 class_copyMethodList 就是那 10 秒")
    # ★★ 判据锚 PSInstallByScan 函数体：扫描循环里不许出现 class_conformsToProtocol。
    #   它不便宜（要遍历协议链），5 万个类逐个求值等于换一种慢法。
    ok("class_conformsToProtocol" not in sc,
       "C2f 扫表内不逐类判 conform 协议",
       "class_conformsToProtocol 要遍历协议链，5 万次同样卡启动；且判它没有功能收益")

# C4 时间差判据 8.5~16.5（覆盖 ±10 与 ±15）
w = body_of(src, "PSPipSkipWanted")
ok(w is not None, "C4 能找到 PSPipSkipWanted 函数体")
if w:
    ok("8.5" in w and "16.5" in w, "C4b 判据 8.5~16.5 秒在函数体内",
       "只认 ±10 会在 ±15 的 iOS 版本上失效")
    ok("CMTIME_IS_NUMERIC" in w, "C4c 先判 CMTime 有效")
    ok("isfinite" in w, "C4d 判 NaN/Inf")

# C5 判据只能有一处（两处不同步 → completion 永不回调）
ok("PSPipSkipWanted" in src, "C5 判据函数存在")

# C6 防抖 1.5 秒，且防抖期内返回 YES（吞掉）而非放行
hd = body_of(src, "PSHandlePipSkip")
ok(hd is not None, "C6 能找到 PSHandlePipSkip 函数体")
if hd:
    ok("PIP_DEBOUNCE" in hd, "C6b 防抖窗口用在函数体内")
    ok("PIP_DEBOUNCE 1.5" in src, "C6c 防抖窗口是 1.5 秒")
    # 防抖期内必须回调 YES
    ok(re.search(r"PIP_DEBOUNCE\)\s*\{[^}]*completion\(YES\)", hd) is not None,
       "C6d 防抖期内回调 YES（吞掉）",
       "放行会让系统真的把画面拉走 10 秒，切视频就白切了")

# C7 切视频必须 dispatch 到主队列（UIKit 只能主线程动）
ok("dispatch_get_main_queue()" in hd if hd else False,
   "C7 切视频切到主队列",
   "UIKit 只能在主线程动，否则崩或无效")

# C8 小窗场景必须 allowHidden=YES（PiP 时主窗口 hidden/alpha=0）
sf = body_of(src, "PSSwitchFeed")
ok(sf is not None, "C8 能找到 PSSwitchFeed 函数体")
if sf:
    ok("PSFeedScrollView(YES)" in sf, "C8b PSSwitchFeed 用 allowHidden=YES",
       "PiP 时抖音主窗口可能 hidden，不放行就永远找不到 → 点了没反应")

# C9 三条切换路径都要在
if sf:
    ok("[tv scrollToRowAtIndexPath:" in sf, "C9 保留 UITableView 路径")
    ok("[cv scrollToItemAtIndexPath:" in sf, "C9b 保留 UICollectionView 路径")
    ok("[sv setContentOffset:" in sf, "C9c 保留 contentOffset 兜底路径")

# C10 找 scrollView 的筛选条件必须完整（缺一条就找不到）
fz = body_of(src, "PSFeedScrollView")
ok(fz is not None, "C10 能找到 PSFeedScrollView 函数体")
if fz:
    ok("isKindOfClass:[UIScrollView class]" in fz, "C10b 判 UIScrollView")
    ok("0.85" in fz, "C10c 尺寸门槛 85% 窗口")
    ok("bounds.size.height * 2" in fz, "C10d 至少能滚两屏",
       "少于两屏就不是信息流")
    ok("score += 1000.0" in fz, "C10e UITableView/UICollectionView 加分优先")
    ok("score > bestScore" in fz, "C10f 取最高分")

# C11 补装机制必须存在（delegate 类是懒注册的）
ok("PSInstallKnownClasses" in src, "C11 有安装函数")
kc = body_of(src, "PSInstallKnownClasses")
ok(kc is not None, "C11b 能找到 PSInstallKnownClasses 函数体")
if kc:
    ok("PSTryHookClass" in kc, "C11c 硬编码类走完整校验装配")
# ★ 1.0-3 起构造器不再自己 dispatch_after，而是交给 PSStartBackgroundRetry
#   统一调度（全表扫还要额外延后到启动完成之后）。
ok("PSStartBackgroundRetry" in ctor if ctor else False,
   "C11d 构造器有延迟补装（委派 PSStartBackgroundRetry）")
ok("PS_RETRY_MAX_TICKS" in src, "C11e 有轮询补装上限",
   "delegate 类可能第一次进小窗才注册，两次补装都可能错过")

# C12 补装必须能识别「已经装好了」（否则永远认为失败 → 每秒重跑全表扫）
rt = body_of(src, "PSRetryTick")
ok(rt is not None, "C12 能找到 PSRetryTick 函数体")
if rt:
    ok("gPipHookedCount > 0" in rt, "C12b 装好即收工（按已装数判，不按本次装数判）",
       "按「本次装了几个」判 → 幂等闸挡住后永远认为失败 → 每秒重扫")
    ok("PS_RETRY_MAX_TICKS" in rt, "C12c 有次数上限（不会无限跑）")

# ============================================================
# E 组：启动路径性能（1.0-2 的真实事故：装完启动要等 10 秒）
#   背景：objc_getClassList 拿到 5 万个类，对每个类调 class_copyMethodList
#   （malloc + 复制整张方法表）→ 单次全表 2~5 秒。1.0-2 在 pre-main
#   构造器里同步跑了 4 次，还在主线程 NSTimer 里每秒再跑 4 次、连跑 30 秒。
# ============================================================

# E1 构造器绝不许直接做全表扫（pre-main 阻塞 = 启动慢的直接原因）
ok(ctor is not None, "E1 能找到 PSCtor 函数体")
if ctor:
    ctor_code = strip_comments(ctor)
    for bad in ("objc_getClassList", "PSInstallByScan", "class_copyMethodList"):
        ok(bad not in ctor_code,
           "E1 构造器体内不含 %s" % bad,
           "pre-main 里扫全表 = 首帧被推迟十几秒（1.0-2 的事故）")
    # 正项：构造器必须只调 O(1) 的快装路 + 后台调度
    ok("PSInstallFast()" in ctor_code, "E1b 构造器调 O(1) 的 PSInstallFast")
    ok("PSStartBackgroundRetry()" in ctor_code,
       "E1c 构造器把重活交给后台（PSStartBackgroundRetry）")

# E2 全表扫绝不许跑在主线程
#   ★ 判据锚 dispatch 的目标队列：必须是 PSWorkQueue()（后台串行），
#     不许是 dispatch_get_main_queue()。
ds = body_of(src, "PSDeferredScan")
ok(ds is not None, "E2 能找到 PSDeferredScan 函数体")
if ds:
    ok("PSWorkQueue()" in ds, "E2b 全表扫调度到后台队列")
    ok("dispatch_get_main_queue" not in ds, "E2c 全表扫不排到主队列",
       "排到主队列 = 启动期 UI 直接卡住")
#   ★★ 注意：不能一刀切「全文件不许出现主队列」——切视频
#     （PSHandlePipSkip → PSSwitchFeed）**必须**回主线程，那是 UIKit 硬要求。
#     所以只约束「装配/扫描这条链」。
for fn in ("PSRetryTick", "PSStartBackgroundRetry", "PSInstallFast",
           "PSInstallKnownClasses", "PSInstallByScan", "PSInstallPipHook"):
    fb = body_of(src, fn)
    if fb is None:
        continue
    ok("dispatch_get_main_queue" not in fb,
       "E2d %s 不占主队列" % fn,
       "装配链排主队列 = 启动期 UI 直接卡住")

# E3 后台队列必须是串行 + 低优先级（并发扫表会互相抢 CPU 放大卡顿）
wq = body_of(src, "PSWorkQueue")
ok(wq is not None, "E3 能找到 PSWorkQueue 函数体")
if wq:
    ok("DISPATCH_QUEUE_SERIAL" in wq, "E3b 队列是串行的")
    ok("QOS_CLASS_UTILITY" in wq or "QOS_CLASS_BACKGROUND" in wq,
       "E3c 队列是低优先级（不抢启动的 CPU）")
    ok("dispatch_once" in wq, "E3d 队列只建一次")

# E4 全表扫一辈子只跑一次（不能每 tick 重跑）
ok("gPipScanDone" in src, "E4 有全表扫的「已跑过」标记")
if ds:
    ok("gPipScanDone = YES" in ds, "E4b 扫描前置位标记（异常也不会重跑）")
    ok("if (gPipScanDone) return;" in ds, "E4c 已跑过就早退")
# ★ 全表扫只允许在这两处被调用：延时扫描、以及 PSInstallPipHook 之外的地方。
#   数调用点能防「又在某个循环里加回全表扫」。
ok(src.count("PSInstallByScan(") == 2,
   "E4d PSInstallByScan 只有一个调用点（实际 %d 处）" % src.count("PSInstallByScan("),
   "多于一处 = 又在重复扫全表")

# E5 全表扫必须延后到启动完成之后（不能和首帧抢 CPU）
ok("PS_SCAN_DELAY" in src, "E5 全表扫有独立延时常量")
ok(re.search(r"#define\s+PS_SCAN_DELAY\s+([0-9.]+)", src) is not None,
   "E5b PS_SCAN_DELAY 是秒数常量")
_m = re.search(r"#define\s+PS_SCAN_DELAY\s+([0-9.]+)", src)
if _m:
    ok(float(_m.group(1)) >= 5.0,
       "E5c 全表扫延时 >= 5 秒（实际 %s）" % _m.group(1),
       "太早扫会与首帧抢 CPU，启动照样慢")

# E6 扫表内必须用「廉价预筛」，不能对每个类都复制方法表
if sc:
    ok("class_respondsToSelector" in sc, "E6 扫表内有廉价预筛 class_respondsToSelector",
       "对 5 万个类逐个 class_copyMethodList 就是那 10 秒")
    # ★ 昂贵调用只允许出现在 PSTryHookClass 里，不许出现在扫表循环体内
    ok("class_copyMethodList" not in sc,
       "E6b 扫表循环体内不直接复制方法表")
    # 预筛必须在 PSTryHookClass 之前（否则预筛白做）
    i_pre = sc.find("class_respondsToSelector")
    i_try = sc.find("PSTryHookClass")
    ok(i_pre >= 0 and i_try > i_pre, "E6c 预筛在装配之前（先筛后装）")

# E7 绝不许用 NSTimer 做主线程轮询（1.0-2 的卡顿来源之一）
ok("NSTimer" not in src, "E7 不用 NSTimer（改用 dispatch_after 后台递归）",
   "主线程 NSTimer 每秒重跑装配 = 启动后仍持续卡")
# 反向：递归重试用 dispatch_after
if rt:
    ok("dispatch_after" in rt, "E7b 补装用 dispatch_after 递归")

# ============================================================
# D 组：只做一件事的禁项（这是本次返工的核心纪律）
# ============================================================

# D1 绝不许出现锁屏遥控
for bad in ("MPRemoteCommandCenter", "nextTrackCommand", "previousTrackCommand",
            "skipForwardCommand", "skipBackwardCommand"):
    ok(bad not in src, "D1 不含锁屏遥控 %s" % bad,
       "用户明确要求只做小窗切换这一个功能")

# D2 绝不许 hook seek（日志证明小窗这条路不走 seek）
for bad in ("seekToTime", "seekToTime:toleranceBefore"):
    ok(bad not in src, "D2 不 hook seek %s" % bad,
       "小窗走 sample-buffer delegate，不碰 AVPlayer.seek；hook 它只会误吞别的 seek")

# D3 绝不许有保持播放相关（切小窗自动暂停就是这套导致的）
for bad in ("sendEvent:", "gResignAt", "gUserPausedAt", "UIBackgroundTask",
            "beginBackgroundTaskWithExpirationHandler",
            "UIApplicationWillResignActiveNotification",
            "UIApplicationDidEnterBackgroundNotification",
            "VC.pause", "DYVCPauseImpl"):
    ok(bad not in src, "D3 不含保持播放 %s" % bad,
       "保持播放那套判据没兜住切小窗前的 pause，正是自动暂停的根因")

# D4 不许有开关 / 设置页 / 弹窗 / 日志
for bad in ("CFPreferences", "UIAlertController", "NSUserDefaults",
            "writeToFile", "NSFileHandle"):
    ok(bad not in src, "D4 无开关/弹窗/文件 IO %s" % bad,
       "用户要求只做一件事：零开关、零弹窗、零日志")

# D5 包不许带设置页
ok("PreferenceBundles" not in mkdeb, "D5 不打设置页 bundle")

# ------------------------------------------------------------
# D6 绝不许声明与 com.liehuu.awemenextprev 的 Conflicts/Replaces
# ★★ 这是 1.0-1 的真实事故：当时写了
#      Conflicts: com.liehuu.awemenextprev
#      Replaces:  com.liehuu.awemenextprev
#    结果是装本插件会把用户仍在用的
#    「AwemeNextPrev-pure 1.0-2（锁屏上一首/下一首）」一起卸掉。
#    判据锚在 control here-doc 正文，不能全文件搜（注释里就是这几个字）。
#
#    为什么不冲突（产物级实测，不是推理）：
#      · pure 1.0-2 是零 hook：MSHookMessageEx / method_setImplementation /
#        libsubstrate 在 dylib 里全是 False，只链 Foundation/UIKit/MediaPlayer；
#      · 它只用 MPRemoteCommandCenter + UIScrollView，完全不碰画中画
#        （Pip / PictureInPicture / sampleBuffer / AVSampleBuffer 全 False）；
#      · 两个 dylib 也不同名（AwemeNextPrev.dylib / AwemePipSwitch.dylib）。
#    → 作用点不重叠、触发源不同、无共享方法表 → 可同装。
# ------------------------------------------------------------
if ct_body:
    # 逐字段判：只看 control 字段行，不看说明文字
    fields = {}
    for line in ct_body.splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    for fld in ("Conflicts", "Breaks", "Replaces"):
        val = fields.get(fld, "")
        ok("com.liehuu.awemenextprev" not in val,
           "D6 %s 不针对 awemenextprev" % fld,
           "写了会把用户仍在用的 pure 1.0-2（锁屏上一首/下一首）一起卸掉")
    # 正项：既然不加 Conflicts，就必须在 Description 里说明与 pure 可同装，
    #       否则用户会以为二选一（信息缺失也是设计缺陷）。
    #   ★ 大小写不敏感：Description 里写的是「AwemeNextPrev-pure」，
    #     包里名是 com.liehuu.awemenextprev，两者大小写不同，不能直接 in。
    ok("awemenextprev" in ct_body.lower(),
       "D6z Description 说明与 pure 1.0-2 可同装",
       "不加 Conflicts 就必须写清楚，否则用户会以为要二选一")

# D7 依赖只有 mobilesubstrate（不要 preferenceloader，本包没设置页）
if ct_body:
    dep = ""
    for line in ct_body.splitlines():
        if line.startswith("Depends:"):
            dep = line.split(":", 1)[1]
    ok("mobilesubstrate" in dep, "D7 Depends 含 mobilesubstrate")
    ok("preferenceloader" not in ct_body, "D7b 不依赖 preferenceloader")

# ============================================================
# 汇总
# ============================================================
if errors:
    print("✗ %d 项未通过 / 共 %d 项：" % (len(errors), checks))
    for e in errors:
        print("   ·", e)
    sys.exit(1)

print("✓ 源码闸门全过（%d 项）" % checks)