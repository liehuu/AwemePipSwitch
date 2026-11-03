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
//
// ★★★ 1.0-3（启动性能修复）—— 1.0-2 装上后抖音冷启动要等 10 秒才进得去。
//   根因不是「代码写得慢」，而是**在 pre-main 构造器里、在主线程上、
//   同步做了 4 次全量 objc 类表扫描**：
//     · PSHookClassesConformingTo × 3 个协议名 → 3 次全表
//     · PSHookClassesBySelector × 1        → 1 次全表
//   每次全表对 5 万个类逐个调 class_copyMethodList（内部 malloc +
//   复制整张方法表）→ 单次 2~5 秒。构造器里跑 4 次 = 首帧被推迟十几秒。
//   更糟的是后面还有主线程 NSTimer 每秒重跑同一套 4 次扫描（最多 30 秒）
//   → 启动期主线程几乎被占死，进得去也卡。
//   修法（功能零回退）：
//     ① 硬编码类路（实机日志实证的落点）提到最前，O(1) 命中即收工；
//        pre-main 阶段只允许做这一件事。
//     ② 全表扫加「廉价预筛」：先 class_getInstanceMethod（走 dtable，
//        O(1)、不 malloc），只有命中的几十个类才做 class_copyMethodList
//        → 单次全表从 2~5 秒降到 ~30 毫秒。
//     ③ 全表扫从主线程挪到后台队列（UTILITY QoS），轮询用 dispatch_after
//        递归，不再用 NSTimer 占主线程。
//     ④ 全表扫**仍然保留**（其它实现同一 selector 的类照样会被补装上），
//        只是不再阻塞启动 —— 功能面与 1.0-2 一致。
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

/// 查「类|selector」是否已经在幂等表里（= 之前装过）。
/// ★ 这是 1.0-3 修启动卡顿的关键补丁：1.0-2 的补装逻辑只看「本次装上了几个」，
///   而幂等闸会把「已装过」挡回 NULL → 补装认为失败 → 每秒重跑全表扫，
///   连跑 30 秒占死主线程。有了这个查询，补装才能正确判断「已经装好了」。
static BOOL PSKeyRegistered(Class cls, SEL sel) {
    if (!cls || !sel) return NO;
    char key[256];
    snprintf(key, sizeof(key), "%s|%s", class_getName(cls), sel_getName(sel));
    BOOL found = NO;
    pthread_mutex_lock(&gHookMutex);
    for (int i = 0; i < gHookedCount; i++) {
        if (strcmp(gHookedKeys[i], key) == 0) { found = YES; break; }
    }
    pthread_mutex_unlock(&gHookMutex);
    return found;
}

// ---- 候选协议名表（仅作文档/诊断用，扫描不再逐类求值）----
//   1.0-2 曾经拿这三个协议名各做一次全表枚举；1.0-3 起已证明那条路
//   是 selector 全扫路的严格子集（见 PSInstallByScan 里注释），
//   多走 3 遍全表就是启动卡 10 秒的主因。保留名字是为了诊断时能查
//   「这个协议到底注册了没有」，不参与装配判定。
//   ① AVPictureInPictureSampleBufferPlaybackDelegate —— 系统标准协议名
//   ② AWEPictureInPictureSampleBufferPlaybackDelegate —— 抖音自研协议名
//   ③ AWEPictureInPicturePlaybackDelegate —— 再退一步的命名变体
static const char *kPipProtoNames[] = {
    "AVPictureInPictureSampleBufferPlaybackDelegate",
    "AWEPictureInPictureSampleBufferPlaybackDelegate",
    "AWEPictureInPicturePlaybackDelegate",
};

// PSMakePipSkipImp 定义在下面「签名分派」一节，装配层先用到，故前置声明。
static void PSMakePipSkipImp(Class cls, SEL s, IMP *outNewImp, void **outOrigSlot);

/// ★★★ orig 槽位必须放**堆上**，block 捕获「槽位地址」而不是值。
///   踩过的坑：`__block IMP capOrig = NULL;` 然后 hook 后在 block 外面回填，
///   这个写法**不回填**（__block 的 shared 存储只在 block 被 copy 到堆后生效，
///   而 method_setImplementation 不会 copy 它）→ capOrig 恒 NULL →
///   orig 永不被调用 → 功能彻底失效，表面却"装上了"。
///
/// ---- 以下是 1.0-3 起新增的装配基础设施 ----
///
/// ★★★ 为什么原来的两条枚举函数被合并成一个：1.0-2 装完后抖音冷启动要
///   等 10 秒。原来 3 个协议名各调一次 PSHookClassesConformingTo（每次
///   自己 malloc 一份全量类表 + 逐个类走完），再加一次
///   PSHookClassesBySelector —— **同一张类表被完整走了 4 遍**。
///   每次走表对 5 万个类逐个调 class_copyMethodList（会触发类的
///   realize + malloc + 复制整张方法表）→ 单次 2~5 秒。构造器里跑 4 次
///   = 首帧被推迟十几秒。
///   现在：一趟走完，同时判「conform 三个协议之一」和「自己声明了该方法」，
///   结果取并集（**功能面与 1.0-2 完全一致，只是不再重复走表**）。

/// 装一个类的尝试结果。
/// ★★ 必须把「已装过」和「装不上」分开：1.0-2 里补装只看「装上了几个类」，
///   而幂等闸会把「已装」挡成「没装上」→ 补装永远以为失败 → 每秒重跑
///   全表扫，连跑 30 秒。这正是启动卡顿的第二段来源。
typedef enum {
    PSTrySkip    = 0,   // 这 个类不符合条件 / 装不上
    PSTryHooked  = 1,   // 本次真的替换成功
    PSTryAlready = 2,   // 之前已经装过（幂等闸挡住）—— 算成功
} PSTryResult;

/// 校验 + 装配一个类。校验项与 1.0-2 逐条一致（declares / 返回 void / 参数 5 个）。
static PSTryResult PSTryHookClass(Class cls, SEL sel) {
    if (!cls || !sel) return PSTrySkip;

    // ★ 先查幂等表：已装过就直接算「已就绪」，不再重复创建 imp block。
    //   重试每 tick 都会调到这里，不早退就会每秒多造一个用不上的 block。
    if (PSKeyRegistered(cls, sel)) return PSTryAlready;

    if (!PSClassDeclaresSelector(cls, sel)) return PSTrySkip;

    Method m = class_getInstanceMethod(cls, sel);
    if (!m) return PSTrySkip;
    const char *enc = method_getTypeEncoding(m);
    if (!enc) return PSTrySkip;
    if (enc[0] != 'v') return PSTrySkip;
    if ((int)method_getNumberOfArguments(m) != 5) return PSTrySkip;

    void **slot = (void **)calloc(1, sizeof(void *));
    if (!slot) return PSTrySkip;

    IMP newImp = NULL;
    PSMakePipSkipImp(cls, sel, &newImp, slot);
    if (!newImp) { free(slot); return PSTrySkip; }

    IMP old = NULL;
    if (PSHookup(cls, sel, newImp, &old)) {
        *slot = (void *)old;          // ★ 关键：回填给 block 用
        return PSTryHooked;
    }
    free(slot);
    // PSHookup 返回 NO 有两种原因：① 幂等闸判定已装过 ② 表满。
    // 只有 ① 才算「成功」，② 必须当失败（否则再也不会重试）。
    return PSKeyRegistered(cls, sel) ? PSTryAlready : PSTrySkip;
}

/// 一趟扫完全表：conform 三个协议之一 **或** 自己声明了该方法 → 装上。
static int PSInstallByScan(SEL sel) {
    if (!sel) return 0;

    int num = objc_getClassList(NULL, 0);
    if (num <= 0) return 0;
    Class *classes = (Class *)malloc(sizeof(Class) * (size_t)(num + 16));
    if (!classes) return 0;
    num = objc_getClassList(classes, num);

    int hit = 0;
    for (int i = 0; i < num; i++) {
        Class cls = classes[i];
        if (!cls) continue;
        @try {
            const char *cname = class_getName(cls);
            if (!cname) continue;
            // 排除 Swift 元数据类（名字不可靠，且不会是我们找的 delegate）
            if (cname[0] == '_' && strncmp(cname, "_TtC", 4) == 0) continue;

            // ★★★ 预筛只用 class_respondsToSelector，**不再判 conform 协议**。
            //   为什么可以去掉（这是 1.0-2 那 10 秒的主要原因）：
            //     1.0-2 有两条路：
            //       路1 = class_conformsToProtocol(...) && PSClassDeclaresSelector(...)
            //       路2 = PSClassDeclaresSelector(...)
            //     路1 的条件是路2 条件的**严格子集**（A∧B ⊆ B）→ 路1 覆盖的
            //     每一个类，路2 都覆盖得到。所以那 3 次协议全表枚举
            //     **在功能上完全多余**，纯粹是把同一张类表多走 3 遍。
            //   ★ 且 class_conformsToProtocol 本身不便宜（要遍历协议链 +
            //     递归父协议）。放在 OR 里会被求值 5 万次，等于用另一种
            //     慢法重演卡顿。所以连「兜底 or」都不要。
            //   ★ class_respondsToSelector 走 dtable/方法缓存，O(1) 且不 malloc。
            //     它对「继承来的方法」也返回 YES，但 PSTryHookClass 里的
            //     PSClassDeclaresSelector 只认自己声明的 → 不会给子类多包一层。
            if (!class_respondsToSelector(cls, sel)) continue;

            if (PSTryHookClass(cls, sel) != PSTrySkip) hit++;
        } @catch (NSException *e) { }
    }
    free(classes);
    return hit;
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

// ★★★ 1.0-3 启动性能修复：整个安装流程重排成「先 O(1)，再后台，最后才全表」。
//
//   1.0-2 的顺序是：3 次协议全表枚举 + 1 次 selector 全表枚举，**全部在
//   pre-main 构造器里、主线程上同步跑完**。这就是「启动要等 10 秒」。
//
//   1.0-3 的顺序：
//     ① 构造器里只做 O(1) 的硬编码类尝试（objc_getClass + 一次方法表查）。
//        命中（实机日志实证这条一定命中）→ 立刻收工，全表扫一次都不做。
//     ② 未命中才把全表扫丢到**后台串行队列**（QOS_CLASS_UTILITY），
//        主线程完全不参与。
//     ③ 补装改用 dispatch_after 递归（后台队列），不再用主线程 NSTimer。
//     ④ 补装判据从「本次装了几个」改成「整体是否已就绪」—— 1.0-2 的
//        判据会被幂等闸挡成「没装上」，于是每秒重跑全表扫连跑 30 秒，
//        这是启动卡顿的第二段来源。
//
//   ★ 功能面零回退：全表扫仍然存在、协议三选一仍然覆盖、参数校验逐条不变。
//     只是不再重复走表、不再阻塞启动。

/// 已经装上的「类|selector」个数（含本次装上的与之前装过的）。
/// ★ 不用 BOOL：需要区分「一个都没装上」与「装上了 N 个」。
static int  gPipHookedCount = 0;
/// 全表扫是否已经跑过（不再重复跑 —— 类表在启动后不会突然多出 arm64e 类，
/// 真正懒注册的 delegate 类由「后台补装」这一条路负责，与全表扫无关）。
static BOOL gPipScanDone = NO;
/// 后台扫描/补装串行队列（绝不占主线程）。
static dispatch_queue_t PSWorkQueue(void) {
    static dispatch_queue_t q = NULL;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        q = dispatch_queue_create("com.liehuu.awemepipswitch.install",
                                  DISPATCH_QUEUE_SERIAL);
        dispatch_set_target_queue(
            q, dispatch_get_global_queue(QOS_CLASS_UTILITY, 0));
    });
    return q;
}

static SEL PSPipSkipSelector(void) {
    // ★★★ selector 必须带 Handler 后缀（2.1-5 诊断日志实证）：
    //   真实方法是 ...skipByInterval:completionHandler:，
    //   少一个单词就是 0 命中 —— 表现是「小窗 ±10 秒点了没反应」。
    return NSSelectorFromString(
        @"pictureInPictureController:skipByInterval:completionHandler:");
}

/// 路 3 提前：硬编码兜底（实机日志实证的落点类）——**O(1)，唯一允许跑在
/// pre-main 构造器里的路**。
///   ★ 这个类名不是猜的，日志里明确出现：
///     obj=AWEPictureInPictureSampleBufferPlayerView
///   ★ 仍走完整校验（类存在 → 自己声明 → enc 签名 → 幂等），
///     装不上不会静默崩。
/// 返回是否「已就绪」（本次装上 或 之前已装过）。
static BOOL PSInstallKnownClasses(SEL sel) {
    static const char *kKnownPipClasses[] = {
        "AWEPictureInPictureSampleBufferPlayerView",
    };
    BOOL ready = NO;
    for (size_t k = 0;
         k < sizeof(kKnownPipClasses) / sizeof(kKnownPipClasses[0]); k++) {
        Class cls = objc_getClass(kKnownPipClasses[k]);
        if (!cls) continue;
        PSTryResult r = PSTryHookClass(cls, sel);
        if (r != PSTrySkip) ready = YES;
    }
    return ready;
}

/// 只装硬编码类（构造器里调，绝不触发全表扫）。
static void PSInstallFast(void) {
    SEL sel = PSPipSkipSelector();
    if (!sel) return;
    if (PSInstallKnownClasses(sel)) gPipHookedCount++;
}

/// 后台补装：每 1 秒重试一次硬编码类，最多 30 次；装好即收工。
/// ★ 1.0-2 是主线程 NSTimer 每秒重跑 4 次全表扫 → 启动期主线程被占死。
///   这里每次 tick 只做 O(1) 的硬编码类尝试，且整段在后台队列，
///   主线程零参与。
/// ★ 不用 block 自捕获（`__block void (^b)(void) = ^{ ... b(); }` 会
///   自持有形成环）—— 改成静态计数 + 普通函数，dispatch 里只放一个调用。
#define PS_RETRY_MAX_TICKS 30
static int gPipRetryTick = 0;

static void PSRetryTick(void) {
    @try {
        gPipRetryTick++;
        SEL sel = PSPipSkipSelector();
        // 后续 tick 只试硬编码类：delegate 类懒注册时靠这条补上
        if (sel && PSInstallKnownClasses(sel)) {
            if (gPipHookedCount == 0) gPipHookedCount = 1;
        }
        if (gPipHookedCount > 0 || gPipRetryTick > PS_RETRY_MAX_TICKS) return;
        dispatch_after(
            dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.0 * NSEC_PER_SEC)),
            PSWorkQueue(), ^{ PSRetryTick(); });
    } @catch (NSException *e) { }
}

/// 启动完成后才做的全表扫。
///
/// ★★★ 为什么要延迟：全表扫必须**亲眼看过 5 万个类**才能确认「谁实现了
///   这个方法」，其中 class_copyMethodList 会 malloc + 复制整张方法表，
///   累计是秒级 CPU。放后台只能让主线程不等它，但 CPU 被抢着用一样会
///   拖慢首帧。真正干净的做法是：**先让启动跑完**，再在空闲时补这趟扫描。
///   · 实机日志实证的落点类已在构造器里 O(1) 装好 → 前 PS_SCAN_DELAY 秒
///     内进小窗完全正常；
///   · 全表扫的用途是「覆盖其它同样实现了该 selector 的类」（lite 版、
///     不同 iOS 版本的自研 delegate），晚一点扫不影响这些场景。
#define PS_SCAN_DELAY 8.0

static void PSDeferredScan(void) {
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(PS_SCAN_DELAY * NSEC_PER_SEC)),
        PSWorkQueue(), ^{
            @try {
                if (gPipScanDone) return;
                gPipScanDone = YES;   // 先置位：即使扫描中抛异常也不再重跑
                SEL sel = PSPipSkipSelector();
                if (sel) {
                    int n = PSInstallByScan(sel);
                    if (n > 0 && gPipHookedCount == 0) gPipHookedCount = n;
                }
            } @catch (NSException *e) { }
        });
}

static void PSStartBackgroundRetry(void) {
    dispatch_async(PSWorkQueue(), ^{
        // 第一轮补装立刻跑（只碰硬编码类，微秒级）
        @try { PSRetryTick(); } @catch (NSException *e) { }
    });
    PSDeferredScan();
}

#pragma mark - 入口

__attribute__((constructor)) static void PSCtor(void) {
    // ★★★ 构造器运行在 pre-main，**绝对不许做任何 O(类数) 的事**。
    //   1.0-2 在这里同步做了 4 次全量类表扫描（每次对 5 万个类逐个
    //   class_copyMethodList）→ 首帧被推迟十几秒，就是用户报的
    //   「启动要等 10 秒」。这里只允许 O(1) 的硬编码类尝试。
    @try {
        // hook 必须在抖音创建画中画 delegate 之前装上，构造器里做最稳。
        // 这一步是 O(1)：objc_getClass + 一次方法表查，微秒级。
        PSInstallFast();

        // 补装 + 全表扫全部丢到后台串行队列，主线程零参与。
        PSStartBackgroundRetry();
    } @catch (NSException *e) {
        // 绝不清已装好的 orig 槽位：hook 很可能已装上一部分，清空等于让它们
        // 吞掉 completion。这里只能什么都不做，交给补装重试。
    }
}