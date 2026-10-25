# -*- coding: utf-8 -*-
"""等 CI 跑完并下载 artifact（只用 curl 走 blob，urllib 直连 blob 会 RST）。

用法：python tools/fetch_artifacts.py <run_id> <输出目录>
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = "liehuu/AwemePipSwitch"
GCM = r"C:/Users/fengb/AppData/Local/Programs/WorkBuddy/resources/vendor/PortableGit/mingw64/bin/git-credential-manager.exe"
API = "https://api.github.com"

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else None
OUTDIR = sys.argv[2] if len(sys.argv) > 2 else "_dl"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ★★ 产物绝不能落在仓库根目录：`packages-*.zip` 曾被 git add -A 收进索引
#   并推到远端公开仓库（本地真实事故）。默认落到 _dl/（已在 .gitignore），
#   并且显式拒绝「输出目录 == 仓库根」。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.abspath(OUTDIR) == _REPO_ROOT:
    raise SystemExit("拒绝把产物下载到仓库根目录（会污染索引），请给一个子目录，如 _dl")


def token():
    p = subprocess.run([GCM, "get"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True)
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no token")


TOK = token()


def api(path):
    req = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + TOK, "User-Agent": "wb",
        "Accept": "application/vnd.github+json"})
    raw = _OPENER.open(req, timeout=90).read()
    return json.loads(raw.decode()) if raw else {}


def latest_run():
    d = api("/repos/%s/actions/runs?per_page=1" % REPO)
    return d["workflow_runs"][0]


run_id = RUN_ID
if not run_id:
    run_id = str(latest_run()["id"])
print("run:", run_id)

# ---- 等完成 ----
for _ in range(120):
    r = api("/repos/%s/actions/runs/%s" % (REPO, run_id))
    st = r["status"]
    print("  ", st, r.get("conclusion") or "")
    if st == "completed":
        if r.get("conclusion") != "success":
            print("✗ CI 失败:", r.get("conclusion"))
            for j in api("/repos/%s/actions/runs/%s/jobs" % (REPO, run_id))["jobs"]:
                for s in j.get("steps", []):
                    if s.get("conclusion") not in (None, "success", "skipped"):
                        print("   失败步骤:", j["name"], "/", s["name"])
            sys.exit(1)
        break
    time.sleep(10)
else:
    raise SystemExit("等待超时")

# ---- 下载 artifact ----
os.makedirs(OUTDIR, exist_ok=True)
arts = api("/repos/%s/actions/runs/%s/artifacts" % (REPO, run_id))["artifacts"]
if not arts:
    raise SystemExit("没有 artifact")

for a in arts:
    zpath = os.path.join(OUTDIR, a["name"] + ".zip")
    url = "%s/repos/%s/actions/artifacts/%s/zip" % (API, REPO, a["id"])
    print("下载", a["name"], a["size_in_bytes"], "B")
    # ★ 必须 curl：urllib 直连 azure blob 会被 RST。
    #   -L 跟随 302；curl 在跨 host 重定向时会自动丢掉 Authorization。
    rc = subprocess.run(["curl", "-sL", "--noproxy", "*",
                         "-H", "Authorization: Bearer " + TOK,
                         "-H", "User-Agent: wb",
                         url, "-o", zpath]).returncode
    if rc != 0:
        raise SystemExit("curl 失败 rc=%d" % rc)
    print("  ->", zpath, os.path.getsize(zpath), "B")
print("✓ 完成")
