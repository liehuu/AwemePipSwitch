# -*- coding: utf-8 -*-
"""建仓库 + 推全量（全程只走 api.github.com，绕开 github.com:443 的 RST）。

为什么不用 git push：本机 `github.com:443` 会被 RST（curl 000 / exit 56），
但 `api.github.com` 直连可用。所以用 Git Data API 推：blobs → tree → commit → ref。

用法：python tools/init_and_push.py <owner/repo> <branch> "<commit message>"

★ 坑（都踩过）：
  · 仓库刚建、一个 commit 都没有时，POST /git/blobs 会返回 **409**
    （objects 库还没初始化）。这里用 auto_init=true 建库让远端先有初始
    commit，彻底绕开这个分支。
  · mode 必须从本地 git 索引取（git ls-files -s），不能靠 os.name 猜。
  · 空仓库 GET /git/ref 返回 409 而不是 404，两种都按「空」处理。
"""
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO = sys.argv[1] if len(sys.argv) > 1 else "liehuu/AwemeNextPrev"
BRANCH = sys.argv[2] if len(sys.argv) > 2 else "main"
MSG = sys.argv[3] if len(sys.argv) > 3 else "init"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GCM = r"C:/Users/fengb/AppData/Local/Programs/WorkBuddy/resources/vendor/PortableGit/mingw64/bin/git-credential-manager.exe"
API = "https://api.github.com"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ★★★ 推送前必须挡住「凭据/密钥入库」。
#   真实事故：调试时把 token 写进 .tok，`git add -A` 把它收进索引，
#   POST /git/blobs 被 GitHub secret scanning 以 **422** 拦下 ——
#   侥幸没进远端，但本地索引里已经有它了。
#   教训：这条防线不能靠「记得」。
_FORBIDDEN_NAME = re.compile(
    r"(\.tok$|\.token$|\.env($|\.)|id_rsa|\.p12$|\.pem$|\.key$|credentials)",
    re.I)
_SECRET_CONTENT = [
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),   # GitHub token
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def guard_no_secrets(paths):
    """发现疑似凭据就中止推送（宁可推不上去，也不泄露）。"""
    bad = []
    for p in paths:
        if _FORBIDDEN_NAME.search(p):
            bad.append("%s（文件名像凭据）" % p)
            continue
        try:
            raw = open(os.path.join(REPO_DIR, p), "rb").read()
        except OSError:
            continue
        if len(raw) > 2_000_000:
            continue
        for pat in _SECRET_CONTENT:
            if pat.search(raw):
                bad.append("%s（内容里有 token/私钥特征）" % p)
                break
    if bad:
        print("✗ 拒绝推送：索引里疑似包含凭据")
        for b in bad:
            print("    -", b)
        raise SystemExit("请先 git rm --cached 这些文件，并吊销已泄露的 token")


def token():
    p = subprocess.run([GCM, "get"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True)
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("拿不到 GitHub token")


TOKEN = token()


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "wb")
    if data:
        req.add_header("Content-Type", "application/json")
    with _OPENER.open(req, timeout=180) as r:
        raw = r.read()
    return json.loads(raw.decode()) if raw else {}


def git(*args):
    p = subprocess.run(["git"] + list(args), cwd=REPO_DIR,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout.strip()


# ---- 0) 本地仓库准备好 ----
if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
    print("→ git init")
    git("init", "-b", BRANCH)
    git("config", "user.email", "liehuu@users.noreply.github.com")
    git("config", "user.name", "liehuu")
    git("config", "core.autocrlf", "false")

git("add", "-A")
# ★ 必须在 commit 之前挡：一旦进了索引就麻烦（见 guard_no_secrets 注释）
guard_no_secrets(git("ls-files").splitlines())
if git("status", "--porcelain"):
    git("commit", "-m", MSG)
    print("→ 本地已提交:", git("rev-parse", "--short", "HEAD"))

# ---- 1) 建仓库（已存在就跳过）----
owner, name = REPO.split("/")
try:
    api("GET", "/repos/%s" % REPO)
    print("仓库已存在:", REPO)
except urllib.error.HTTPError as e:
    if e.code != 404:
        raise
    print("→ 创建仓库", REPO)
    api("POST", "/user/repos", {"name": name, "private": False, "auto_init": True})
    print("  ✓ 已创建（带初始 commit，避免 blobs 409）")

# ---- 2) 远端 head ----
try:
    r = api("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    remote_sha = r["object"]["sha"]
    print("remote head:", remote_sha[:8])
except urllib.error.HTTPError as e:
    if e.code in (404, 409):
        remote_sha = None
        print("remote head: (空仓库 HTTP %d)" % e.code)
    else:
        raise

# ★★★ 绝不用 base_tree：它是「增量合并」语义 —— 未列出的旧文件会保留在远端。
# 本地 git rm 掉的文件（例如 2.1-1 删掉的 plist-only 旧设置页）不显式删就永远活着，
# 会被 CI 打进 deb 的 layout，导致设置里出现重复入口。
# 这里全量重建 tree（entries 已含本地 ls-files 全部文件），远端即等于本地。
if remote_sha:
    rc = api("GET", "/repos/%s/git/commits/%s" % (REPO, remote_sha))
    print("old_tree   :", rc["tree"]["sha"][:8], "(全量替换，不再复用)")

# ---- 3) 索引里的 mode ----
idx_mode = {}
for line in git("ls-files", "-s").splitlines():
    try:
        meta, p = line.split("\t", 1)
        idx_mode[p] = meta.split()[0]
    except ValueError:
        pass

# ---- 4) blobs ----
entries = []
for path in git("ls-files").splitlines():
    full = os.path.join(REPO_DIR, path)
    if not os.path.exists(full):
        continue
    raw = open(full, "rb").read()
    blob = api("POST", "/repos/%s/git/blobs" % REPO, {
        "content": base64.b64encode(raw).decode(),
        "encoding": "base64",
    })
    entries.append({"path": path, "mode": idx_mode.get(path, "100644"),
                    "type": "blob", "sha": blob["sha"]})
    print("  blob %-58s %7d B  %s" % (path, len(raw), idx_mode.get(path, "100644")))

# ---- 5) tree / commit / ref ----
# 不传 base_tree ⇒ 全量替换。远端删除的文件在这里才真正消失。
tree = api("POST", "/repos/%s/git/trees" % REPO, {"tree": entries})
print("tree      :", tree["sha"][:8], "(全量)")

commit = api("POST", "/repos/%s/git/commits" % REPO, {
    "message": MSG,
    "tree": tree["sha"],
    "parents": [remote_sha] if remote_sha else [],
})
print("commit    :", commit["sha"])

if remote_sha:
    api("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, BRANCH),
        {"sha": commit["sha"], "force": True})
else:
    api("POST", "/repos/%s/git/refs" % REPO,
        {"ref": "refs/heads/" + BRANCH, "sha": commit["sha"]})
print("✓ 已推送 →", "%s @ %s" % (REPO, commit["sha"][:8]))

# ---- 6) 自证：远端文件集合必须与本地索引完全相等 ----
# 只信「我列了这些 blob」是不够的：base_tree 时代的残留文件正是这样漏掉的。
# 判据是双向差集都为空，不是「本地文件都在远端」。
rt = api("GET", "/repos/%s/git/trees/%s?recursive=1" % (REPO, commit["sha"]))
remote_set = set(e["path"] for e in rt["tree"] if e["type"] == "blob")
local_set = set(git("ls-files").splitlines())
missing = sorted(local_set - remote_set)      # 本地有、远端没有 → 推送漏了
extra = sorted(remote_set - local_set)        # 远端有、本地没有 → 删除没生效
bad = False
if missing:
    bad = True
    print("✗ 远端缺少 %d 个文件：" % len(missing))
    for p in missing:
        print("    -", p)
if extra:
    bad = True
    print("✗ 远端多余 %d 个文件（本地已删除但远端还在）：" % len(extra))
    for p in extra:
        print("    +", p)
if bad:
    raise SystemExit("推送自证失败：远端与本地不一致")
print("✓ 远端文件集合 == 本地索引（%d 个文件，无残留）" % len(local_set))
