// ============================================================================
// AwemePipSwitch —— 只做一件事：抖音小窗（画中画）里点「±10 秒」→ 切上一条/下一条
//
// ★★★ 本插件刻意「只有一个功能」。
//   3.0-1 把三个功能（锁屏 ⏮⏭ / 小窗切换 / 保持播放）合到一个 deb 里，
//   用户实测：小窗切换可用，另外两个（切小窗自动暂停、锁屏只回视频开头）
//   仍然不对。用户判断「整合不好」，要求单独做一个只有小窗切换的插件。
//
// ★★★ 本文件只保留 3.0-1 里**已由实机日志逐行证实有效**的代码：
//   证据（aweme-anp-diag(7).log）：
//     [..] PiP-skip delegate 第 1 次被调用, interval=10.000000,
//          obj=AWEPictureInPictureSampleBufferPlayerView
//     [..] 切视频: 入口 next=1 allowHidden=1（调用源：小窗 ±10 秒）
//     [..] 切视频(下一条): 找到 scrollView=AWEFeedTableView
//     [..] 路径1-UITableView 命中 cur.row=17 target=18 total=25
//     [..] 切视频(下一条): 成功
//   → delegate 拦得住、scrollView 找得到、行号算得对、视频真切了。
//
// ★★★ 本文件**全部删除**的东西（每一项都是 3.0-1 里没被证实、且可能有害的）：
//   ① AVPlayer / AVPlayerItem 的 8 个 seek 变体 hook —— 全删。
//      日志全程没有一条 `seek 命中`，说明小窗这条路根本不走 seek。
//      这段代码只会在别的路径上误吞 seek（把视频拉回 0 秒）——
//      正是「点上一首只回本视频开头」这类症状的嫌疑来源。
//   ② 锁屏 MPRemoteCommandCenter（nextTrack / previousTrack）—— 全删。
//   ③ 保持播放的全部 hook（pause / play / setRate / VC.pause / 后台轮询 /
//      触摸时间戳 sendEvent:）—— 全删。切小窗自动暂停就是因为这套
//      在「切小窗前 3.92 秒」的那次 pause 上判据没兜住
//      （日志：VC.pause #7 dt=3.92 block=0 userPaused=1），越修越乱。
//   ④ 设置页 / 开关 / 弹窗 / 诊断日志 —— 全删。零开关、零 IO、零弹窗。
//
// ★ 小窗上那个按钮的**样子改不了**：它是系统画中画外壳绘制的，
//   宿主 App 只能决定「显示/隐藏」，系统没提供「换成 ⏮⏭」的按钮位。
//   本插件改的是点击行为：点它不再跳 10 秒，而是切视频。
// ============================================================================

#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <AVFoundation/AVFoundation.h>
#import <AVKit/AVKit.h>
#import <CoreMedia/CoreMedia.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <pthread.h>
#import <string.h>
#import <stdio.h>

#pragma mark - 全局状态

// 防抖：一次点击系统可能连着回调好几次，只认第一次。
static CFTimeInterval gLastPipHit = 0;
#define PIP_DEBOUNCE 1.5

#pragma mark - 窗口 / 视图遍历

static NSArray<UIWindow *> *PSAllWindows(void) {
    NSMutableArray *wins = [NSMutableArray array];
    @try {
        if (@available(iOS 13.0, *)) {
            for (UIScene *scene in [UIApplication sharedApplication].connectedScenes) {
                if (![scene isKindOfClass:[UIWindowScene class]]) continue;
                for (UIWindow *w in ((UIWindowScene *)scene).windows) {
                    if (w) [wins addObject:w];
                }
            }
        }
        if (wins.count == 0) {
            // iOS 12 及更早，或 connectedScenes 还没建好
            NSArray *legacy = [UIApplication sharedApplication].windows;
            if (legacy.count) [wins addObjectsFromArray:legacy];
        }
    } @catch (NSException *e) { }
    return wins;
}

static void PSEnumerate(UIView *view, int depth, void (^block)(UIView *v, int depth)) {
    if (!view) return;
    block(view, depth);
    for (UIView *sub in view.subviews) {
        PSEnumerate(sub, depth + 1, block);
    }
}

#pragma mark - 找信息流 scrollView（逐字照抄 3.0-1，已实证命中 AWEFeedTableView）

/// 找抖音主信息流的竖向分页 scrollView。
/// ★ allowHidden 必须是 YES：小窗（画中画）时抖音主窗口可能 hidden / alpha=0，
///   但视图树还在，不放行就永远找不到 → 小窗点了没反应。
static UIScrollView *PSFeedScrollView(BOOL allowHidden) {
    __block UIScrollView *best = nil;
    __block CGFloat bestScore = -1;
    for (UIWindow *w in PSAllWindows()) {
        if (!allowHidden && (w.hidden || w.alpha < 0.05)) continue;
        CGSize ws = w.bounds.size;
        if (ws.width <= 0 || ws.height <= 0) continue;
        PSEnumerate(w, 0, ^(UIView *v, int depth) {
            if (![v isKindOfClass:[UIScrollView class]]) return;
            UIScrollView *sv = (UIScrollView *)v;
            if (!allowHidden && (sv.hidden || sv.alpha < 0.05)) return;
            // 必须几乎铺满整个窗口（信息流是整屏分页的）
            if (sv.frame.size.width  < ws.width  * 0.85) return;
            if (sv.frame.size.height < ws.height * 0.85) return;
            if (sv.bounds.size.height <= 0) return;
            // 至少能滚两屏（一屏一个视频）
            if (sv.contentSize.height < sv.bounds.size.height * 2) return;
            CGFloat score = (CGFloat)depth * 10.0;
            if ([v isKindOfClass:[UITableView class]] ||
                [v isKindOfClass:[UICollectionView class]]) {
                score += 1000.0;
            }
            score += MIN(sv.contentSize.height / ws.height, 500.0);
            if (score > bestScore) {
                bestScore = score;
                best = sv;
            }
        });
    }
    return best;
}

#pragma mark - 切视频（逐字照抄 3.0-1 的三条路径）

/// 切上一条 / 下一条。返回是否真的动了。必须在主线程调用（UIKit）。
/// ★ 三条路径：table 行 → collection item → contentOffset 兜底。
/// ★★ 绝不在这里加任何「预快照 / 事后校正 / 延迟停旧」——
///   3.0-1 前身（2.1-11）加了一整套补救，结果小窗切视频直接黑屏。
static BOOL PSSwitchFeed(BOOL next) {
    @try {
        UIScrollView *sv = PSFeedScrollView(YES);
        if (!sv) return NO;

        CGFloat pageH = sv.bounds.size.height;
        if (pageH <= 0) return NO;
        BOOL acted = NO;

        // 路径 1：UITableView
        if ([sv isKindOfClass:[UITableView class]]) {
            UITableView *tv = (UITableView *)sv;
            NSIndexPath *cur = nil;
            for (NSIndexPath *ip in tv.indexPathsForVisibleRows) {
                CGRect r = [tv rectForRowAtIndexPath:ip];
                if (r.origin.y <= tv.contentOffset.y + 1 &&
                    CGRectGetMaxY(r) > tv.contentOffset.y + 1) {
                    cur = ip;
                    break;
                }
            }
            if (cur) {
                NSInteger target = cur.row + (next ? 1 : -1);
                NSInteger total  = [tv numberOfRowsInSection:cur.section];
                if (target >= 0 && target < total) {
                    [tv scrollToRowAtIndexPath:[NSIndexPath indexPathForRow:target
                                                                 inSection:cur.section]
                              atScrollPosition:UITableViewScrollPositionTop
                                      animated:YES];
                    acted = YES;
                }
            }
        }

        // 路径 2：UICollectionView
        if (!acted && [sv isKindOfClass:[UICollectionView class]]) {
            UICollectionView *cv = (UICollectionView *)sv;
            NSIndexPath *cur = nil;
            for (NSIndexPath *ip in cv.indexPathsForVisibleItems) {
                CGRect r = [cv layoutAttributesForItemAtIndexPath:ip].frame;
                if (r.origin.y <= cv.contentOffset.y + 1 &&
                    CGRectGetMaxY(r) > cv.contentOffset.y + 1) {
                    cur = ip;
                    break;
                }
            }
            if (cur) {
                NSInteger target = cur.item + (next ? 1 : -1);
                NSInteger total  = [cv numberOfItemsInSection:cur.section];
                if (target >= 0 && target < total) {
                    [cv scrollToItemAtIndexPath:[NSIndexPath indexPathForItem:target
                                                                   inSection:cur.section]
                                atScrollPosition:UICollectionViewScrollPositionTop
                                        animated:YES];
                    acted = YES;
                }
            }
        }

        // 路径 3：直接改 contentOffset（分页对齐）
        if (!acted) {
            CGFloat curPage = round(sv.contentOffset.y / pageH);
            CGFloat target  = curPage + (next ? 1 : -1);
            if (target < 0) target = 0;
            CGFloat maxPage = floor(sv.contentSize.height / pageH) - 1;
            if (maxPage < 0) maxPage = 0;
            if (target > maxPage) target = maxPage;
            CGFloat y = target * pageH;
            if (fabs(y - sv.contentOffset.y) >= 1.0) {
                [sv setContentOffset:CGPointMake(0, y) animated:YES];
                acted = YES;
            }
        }

        return acted;
    } @catch (NSException *e) {
        return NO;
    }
}

#pragma mark - hook 基础设施（method_setImplementation + 堆槽位）

/// 判断类是否**自己**声明了某 selector（不走继承链）。
/// ★ class_getInstanceMethod 会顺继承链返回父类方法，据此 hook 会让子类
///   被多包一层 trampoline，且「签名判断」失真。
static BOOL PSClassDeclaresSelector(Class cls, SEL sel) {
    if (!cls || !sel) return NO;
    unsigned int count = 0;
    Method *methods = class_copyMethodList(cls, &count);
    if (!methods) return NO;
    BOOL found = NO;
    for (unsigned int i = 0; i < count; i++) {
        if (sel_isEqual(method_getName(methods[i]), sel)) { found = YES; break; }
    }
    free(methods);
    return found;
}

/// 幂等闸：同一「类|selector」只允许替换一次。
/// ★★ 第二次 method_setImplementation 返回的 old 是第一次装进去的 imp 本身，
///   不是原实现 → hook 调「orig」= 调自己 → 自递归。必须挡住。
///
/// ★★★ 为什么不用 NSMutableSet：它是懒初始化的，一旦构造器里那行赋值
///   因为任何原因没执行（异常、顺序变动），`@synchronized(nil)` 只是不加锁，
///   `containsObject:` 恒返回 NO、`addObject:` 是空操作 → 幂等闸**静默失效**
///   → 每次都重复替换 → hook 调自己 → 无限递归 → 崩宿主。
///   这里改用静态 C 数组：存储本身不需要初始化，永远可用。
#define PS_MAX_HOOKS 64
static char gHookedKeys[PS_MAX_HOOKS][256];
static int  gHookedCount = 0;
static pthread_mutex_t gHookMutex = PTHREAD_MUTEX_INITIALIZER;

static BOOL PSHookup(Class cls, SEL sel, IMP newImp, IMP *outOld) {
    if (!cls || !sel || !newImp) return NO;
    Method m = class_getInstanceMethod(cls, sel);
    if (!m) return NO;

    char key[256];
    snprintf(key, sizeof(key), "%s|%s", class_getName(cls), sel_getName(sel));

    pthread_mutex_lock(&gHookMutex);
    for (int i = 0; i < gHookedCount; i++) {
        if (strcmp(gHookedKeys[i], key) == 0) {
            pthread_mutex_unlock(&gHookMutex);
            if (outOld) *outOld = NULL;   // 已装过 → 不覆盖，调用方据 NULL 放弃
            return NO;
        }
    }
    BOOL registered = NO;
    if (gHookedCount < PS_MAX_HOOKS) {
        strncpy(gHookedKeys[gHookedCount], key, sizeof(gHookedKeys[0]) - 1);
        gHookedKeys[gHookedCount][sizeof(gHookedKeys[0]) - 1] = '\0';
        gHookedCount++;
        registered = YES;
    }
    pthread_mutex_unlock(&gHookMutex);

    // ★ 表满了：宁可不装，也不能装成自递归。
    if (!registered) {
        if (outOld) *outOld = NULL;
        return NO;
    }

    IMP old = method_setImplementation(m, newImp);
    if (outOld) *outOld = old;
    return YES;
}

/// ★★★ orig 槽位必须放**堆上**，block 捕获「槽位地址」而不是值。
///   踩过的坑：`__block IMP capOrig = NULL;` 然后 hook 后在 block 外面回填，
///   这个写法**不回填**（__block 的 shared 存储只在 block 被 copy 到堆后生效，
///   而 method_setImplementation 不会 copy 它）→ capOrig 恒 NULL →
///   orig 永不被调用 → 功能彻底失效，表面却"装上了"。
static int PSHookClassesBySelector(
        SEL sel, int needArgCount,
        void (^makeImp)(Class cls, SEL sel, IMP *outNewImp, void **outOrigSlot)) {
    if (!sel || !makeImp) return 0;

    int num = objc_getClassList(NULL, 0);
    if (num <= 0) return 0;
    Class *classes = (Class *)malloc(sizeof(Class) * (size_t)(num + 16));
    if (!classes) return 0;
    num = objc_getClassList(classes, num);

    int hooked = 0;
    for (int i = 0; i < num; i++) {
        Class cls = classes[i];
        if (!cls) continue;
        @try {
            const char *cname = class_getName(cls);
            if (!cname) continue;
            // 排除 Swift 元数据类
            if (cname[0] == '_' && strncmp(cname, "_TtC", 4) == 0) continue;
            if (!PSClassDeclaresSelector(cls, sel)) continue;

            Method m = class_getInstanceMethod(cls, sel);
            if (!m) continue;
            const char *enc = method_getTypeEncoding(m);
            if (!enc) continue;
            if (enc[0] != 'v') continue;                     // 必须返回 void
            if (needArgCount > 0 &&
                (int)method_getNumberOfArguments(m) != needArgCount) continue;

            void **slot = (void **)calloc(1, sizeof(void *));
            if (!slot) continue;

            IMP newImp = NULL;
            makeImp(cls, sel, &newImp, slot);
            if (!newImp) { free(slot); continue; }

            IMP old = NULL;
            if (PSHookup(cls, sel, newImp, &old) && old) {
                *slot = (void *)old;      // ★ 关键：回填给 block 用
                hooked++;
            } else {
                free(slot);
            }
        } @catch (NSException *e) { }
    }
    free(classes);
    return hooked;
}

/// 枚举所有 conform 某协议的类，逐个装 hook。返回值同 PSHookClassesBySelector。
static int PSHookClassesConformingTo(
        Protocol *proto, SEL sel, int needArgCount,
        void (^makeImp)(Class cls, SEL sel, IMP *outNewImp, void **outOrigSlot)) {
    if (!proto || !sel || !makeImp) return 0;

    int num = objc_getClassList(NULL, 0);
    if (num <= 0) return 0;
    Class *classes = (Class *)malloc(sizeof(Class) * (size_t)(num + 16));
    if (!classes) return 0;
    num = objc_getClassList(classes, num);

    int hooked = 0;
    for (int i = 0; i < num; i++) {
        Class cls = classes[i];
        if (!cls) continue;
        @try {
            if (!class_conformsToProtocol(cls, proto)) continue;
            const char *cname = class_getName(cls);
            if (!cname) continue;
            if (cname[0] == '_' && strncmp(cname, "_TtC", 4) == 0) continue;
            if (!PSClassDeclaresSelector(cls, sel)) continue;

            Method m = class_getInstanceMethod(cls, sel);
            if (!m) continue;
            const char *enc = method_getTypeEncoding(m);
            if (!enc) continue;
            if (enc[0] != 'v') continue;
            if (needArgCount > 0 &&
                (int)method_getNumberOfArguments(m) != needArgCount) continue;

            void **slot = (void **)calloc(1, sizeof(void *));
            if (!slot) continue;

            IMP newImp = NULL;
            makeImp(cls, sel, &newImp, slot);
            if (!newImp) { free(slot); continue; }

            IMP old = NULL;
            if (PSHookup(cls, sel, newImp, &old) && old) {
                *slot = (void *)old;
                hooked++;
            } else {
                free(slot);
            }
        } @catch (NSException *e) { }
    }
    free(classes);
    return hooked;
}

#pragma mark - PiP skip：签名分派（防崩的关键）

/// 读方法 enc 的第三参（self/_cmd/ctl 之后的第一个实参）是否为 double。
///
/// ★★★ 为什么必须有（2.1-6 防崩修复，2.1-5 诊断日志实证）：
///   AVPictureInPictureSampleBufferPlaybackDelegate 的
///   pictureInPictureController:skipByInterval:completionHandler:
///   第三参标准类型是 **NSTimeInterval(double)**，而早期代码一律按
///   **CMTime（24 字节结构体）** 收。arm64 调用约定下两者布局不同：
///     · double 版：x0=self x1=_cmd x2=ctl x3=double x4=block
///     · CMTime 版：x0=self x1=_cmd x2=ctl [栈]=CMTime x3=block
///   （CMTime 24 字节 > 16，AAPCS64 规定结构体走栈传副本，不占寄存器）
///   ★ 装错签名的后果不是「参数是垃圾」这么温和 —— **block 指针会从错误
///     寄存器读**，调 completion 时直接崩宿主。
static BOOL PSEncArg3IsDouble(const char *enc) {
    if (!enc) return NO;
    // enc 形如 v32@0:8@16d24@?32（double 版）
    //      或 v48@0:8@16{CMTime=qii}24@?48（CMTime 版）
    const char *p = strstr(enc, "@16");
    if (!p) return NO;
    p += 3;
    return (*p == 'd');
}

typedef void (*PSPipSkipImp)(id, SEL, id, CMTime, void (^)(BOOL));

/// 一次 skip 要不要被我们接管 —— 唯一的判据函数（纯函数，无副作用）。
///
/// ★★ 判据只能有**一处**：hook 里判一次、处理函数里再判一次，两处迟早
///   不同步，症状是「hook 判 YES 进来、处理函数判 NO 直接 return，
///   completion 永不回调」→ 系统画中画一直等这个 completion →
///   按钮点了没反应，甚至系统超时后把画中画关掉。极难从现象反推。
///
/// ★ 这条路不需要「锁屏歧义」判断：sample-buffer delegate 的 skip
///   **只可能来自画中画**。锁屏控制中心的 ±10 秒走 MPRemoteCommandCenter，
///   不会到这里。所以本插件无开关、无歧义，直接按时间差认。
static BOOL PSPipSkipWanted(CMTime interval) {
    if (!CMTIME_IS_NUMERIC(interval)) return NO;
    double d = CMTimeGetSeconds(interval);
    if (!isfinite(d)) return NO;
    double ad = fabs(d);
    // 8.5~16.5 覆盖 ±10 与 ±15 两种系统取值；更小的偏移（内部对齐、
    // 缓冲回跳）不会落进来。
    return (ad >= 8.5 && ad <= 16.5);
}

/// 接管一次画中画 skip（调用方已确认 PSPipSkipWanted 为 YES）。
/// ★ 所有出口都必须回调 completion，一条不漏。
static void PSHandlePipSkip(CMTime interval, void (^completion)(BOOL)) {
    double d = CMTimeGetSeconds(interval);

    // 冷却期内：照样回调 YES（吞掉），不把控制权交回系统。
    // ★ 这里**不能**放行 —— 放行会让系统真的把画面拉走 10 秒，切视频就白切了。
    CFTimeInterval now = CACurrentMediaTime();
    if (now - gLastPipHit < PIP_DEBOUNCE) {
        if (completion) completion(YES);
        return;
    }
    gLastPipHit = now;

    BOOL next = (d > 0);
    dispatch_async(dispatch_get_main_queue(), ^{
        PSSwitchFeed(next);
    });
    if (completion) completion(YES);
}

/// PiP skip 的公共处理体（double / CMTime 两种 block 签名都汇到这里）。
/// ★ 保持「一份实现」：两种签名只在「怎么收参数」上有别，拦不拦、怎么切
///   的逻辑必须同源，否则会出现「double 路径切了、CMTime 路径没切」
///   这种按设备/版本分裂的行为。
static void PSPipSkipBody(id obj, SEL origSel, id ctl, CMTime interval,
                          void (^cb)(BOOL), void **outOrigSlot) {
    BOOL handle = NO;
    @try {
        handle = PSPipSkipWanted(interval);
    } @catch (NSException *e) { handle = NO; }

    // ★★ 槽位指针必须判空再解引用：outOrigSlot 为 NULL 时直接 *outOrigSlot
    //   是空指针解引用 —— 崩在系统画中画回调线程里，日志都留不下。
    PSPipSkipImp orig = outOrigSlot ? (PSPipSkipImp)*outOrigSlot : NULL;

    if (handle) {
        BOOL done = NO;
        @try {
            PSHandlePipSkip(interval, cb);
            done = YES;
        } @catch (NSException *e) { }
        if (done) return;
        // 切视频抛异常 → 退回原实现，绝不吞掉 completion
        if (orig) orig(obj, origSel, ctl, interval, cb);
        else if (cb) cb(NO);
        return;
    }
    if (orig) orig(obj, origSel, ctl, interval, cb);
    else if (cb) cb(NO);
}

/// 画中画 skip 的 imp 工厂（**C 函数**）。
/// ★ 调用点必须用 `^` 包一层再传给枚举器（形参是 block 类型，
///   C 函数名不能隐式转换）。
static void PSMakePipSkipImp(Class cls, SEL s, IMP *outNewImp, void **outOrigSlot) {
    SEL origSel = s;    // ★ block 里拿不到 selector，必须在这里捕获
    Method m = class_getInstanceMethod(cls, s);
    BOOL arg3Double = PSEncArg3IsDouble(m ? method_getTypeEncoding(m) : NULL);
    IMP imp = NULL;
    if (arg3Double) {
        imp = imp_implementationWithBlock(
            ^(id obj, id ctl, NSTimeInterval interval, void (^cb)(BOOL)) {
                PSPipSkipBody(obj, origSel, ctl,
                              CMTimeMakeWithSeconds(interval, 1000), cb, outOrigSlot);
            });
    } else {
        imp = imp_implementationWithBlock(
            ^(id obj, id ctl, CMTime interval, void (^cb)(BOOL)) {
                PSPipSkipBody(obj, origSel, ctl, interval, cb, outOrigSlot);
            });
    }
    *outNewImp = imp;
}

#pragma mark - 安装

static BOOL gPipInstalled = NO;

/// 装 PiP skip 拦截。可重复调用（构造器一次 + 延迟补装 + 30 秒轮询补装）。
/// ★★ 绝不在这里见 gPipInstalled 就整体早退 —— 首轮 delegate 类往往还没
///   注册（装到 0 个类），早退会让补枚举永远不重试 → 小窗始终无效。
static void PSInstallPipHook(void) {
    if (gPipInstalled) return;

    // ★★★ selector 必须带 Handler 后缀（2.1-5 诊断日志实证）：
    //   真实方法是 ...skipByInterval:completionHandler:，
    //   少一个单词就是 0 命中 —— 表现是「小窗 ±10 秒点了没反应」。
    SEL pipSkipSel =
        NSSelectorFromString(@"pictureInPictureController:skipByInterval:completionHandler:");
    if (!pipSkipSel) return;

    int byProto = 0, byScan = 0, byKnown = 0;

    // ---- 路 1：按协议枚举 ----
    //   ① AVPictureInPictureSampleBufferPlaybackDelegate —— 系统标准协议名
    //   ② AWEPictureInPictureSampleBufferPlaybackDelegate —— 抖音自研协议名
    //   ③ AWEPictureInPicturePlaybackDelegate —— 再退一步的命名变体
    //   抖音的自研 delegate 常常只 conform 自家协议，查系统协议会 0 命中，
    //   所以三个都试。
    static const char *kPipProtoNames[] = {
        "AVPictureInPictureSampleBufferPlaybackDelegate",
        "AWEPictureInPictureSampleBufferPlaybackDelegate",
        "AWEPictureInPicturePlaybackDelegate",
    };
    for (size_t pi = 0;
         pi < sizeof(kPipProtoNames) / sizeof(kPipProtoNames[0]); pi++) {
        Protocol *pp = objc_getProtocol(kPipProtoNames[pi]);
        if (!pp) continue;
        byProto += PSHookClassesConformingTo(pp, pipSkipSel, 5,
            ^(Class c, SEL sl, IMP *oi, void **os) {
                PSMakePipSkipImp(c, sl, oi, os);
            });
    }

    // ---- 路 2：按 selector 全扫（谁**真的实现了**这个方法就装谁，最可靠）----
    byScan = PSHookClassesBySelector(pipSkipSel, 5,
        ^(Class c, SEL sl, IMP *oi, void **os) {
            PSMakePipSkipImp(c, sl, oi, os);
        });

    // ---- 路 3：硬编码兜底（实机日志实证的落点类）----
    //   ★ 前三路都依赖「运行时能发现这个类」；抖音的自研 delegate 未必
    //     conform 任何已注册协议，selector 扫也有上限和时机限制。
    //   ★ 这个类名不是猜的，日志里明确出现：
    //     obj=AWEPictureInPictureSampleBufferPlayerView
    //   ★ 仍走完整校验（类存在 → 自己声明 → enc 签名 → 幂等），
    //     装不上不会静默崩。
    static const char *kKnownPipClasses[] = {
        "AWEPictureInPictureSampleBufferPlayerView",
    };
    for (size_t k = 0;
         k < sizeof(kKnownPipClasses) / sizeof(kKnownPipClasses[0]); k++) {
        Class cls = objc_getClass(kKnownPipClasses[k]);
        if (!cls) continue;
        if (!PSClassDeclaresSelector(cls, pipSkipSel)) continue;
        Method m = class_getInstanceMethod(cls, pipSkipSel);
        const char *enc = m ? method_getTypeEncoding(m) : NULL;
        if (!enc || enc[0] != 'v' ||
            (int)method_getNumberOfArguments(m) != 5) continue;

        void **slot = (void **)calloc(1, sizeof(void *));
        if (!slot) continue;
        IMP newImp = NULL;
        PSMakePipSkipImp(cls, pipSkipSel, &newImp, slot);
        IMP kbOld = NULL;
        if (newImp && PSHookup(cls, pipSkipSel, newImp, &kbOld) && kbOld) {
            *slot = (void *)kbOld;
            byKnown++;
        } else {
            free(slot);
        }
    }

    if (byProto + byScan + byKnown > 0) {
        gPipInstalled = YES;   // 只有真装上了才标记完成，否则留给补枚举
    }
}

#pragma mark - 入口

__attribute__((constructor)) static void PSCtor(void) {
    // ★ 整个构造器包死：任何异常都不允许影响宿主启动。
    @try {
        // hook 必须在抖音创建画中画 delegate 之前装上，构造器里做最稳。
        PSInstallPipHook();

        // ---- 延迟补装：delegate 类常常是第一次进小窗时才注册 ----
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{
            @try { PSInstallPipHook(); } @catch (NSException *e) { }
        });

        // ---- 30 秒轮询补装：装成功就停 ----
        //   ★ 真实必要性：构造器 + 2 秒两次都可能错过 lazy 注册的类。
        //     症状是小窗点 ±10 秒完全不切视频。
        //   ★ 每秒读一个 BOOL（gPipInstalled），开销可忽略。
        __block int tick = 0;
        [NSTimer scheduledTimerWithTimeInterval:1.0 repeats:YES block:^(NSTimer *timer) {
            tick++;
            @try {
                PSInstallPipHook();      // 内部自带幂等早退
                if (gPipInstalled || tick > 30) {
                    [timer invalidate];
                }
            } @catch (NSException *e) {
                if (tick > 30) [timer invalidate];
            }
        }];
    } @catch (NSException *e) {
        // 绝不清 gOrig*：hook 很可能已装上一部分，清空等于让它们吞掉 completion。
        // 这里只能什么都不做，交给补装重试。
    }
}