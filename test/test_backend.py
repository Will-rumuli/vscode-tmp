# -*- coding: utf-8 -*-
"""后端 API 集成测试 (直接调用 quecpi 模块, 不依赖 HTTP)。

覆盖: connect / probe / templates / create_project / build(check) / repo_status
"""
import os
import sys
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from quecpi.models import ConnectionConfig
from quecpi.sdk_ops import SDK

ROOT = "/home/q/work/test/ai_sdk"
TMP_PROJ = "/tmp/qpi-backend-test-projects"
shutil.rmtree(TMP_PROJ, ignore_errors=True)

fails = 0


def check(name, cond, extra=""):
    global fails
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  -> ' + str(extra) if extra else ''}")
    if not cond:
        fails += 1


# ---- local 模式 ----
conn = ConnectionConfig(mode="local", sdk_root=ROOT, project_dir=TMP_PROJ)
sdk = SDK(conn)

probe = sdk.probe()
check("probe ok", probe.get("ok"), probe.get("error", ""))
check("components build.sh", probe["components"].get("build.sh"))
check("templates == 12", sdk.list_templates().__len__() == 12,
      [t.get("id") for t in sdk.list_templates()])

r = sdk.create_project("bktest", "hello", {"PROJECT_NAME": "bktest", "MESSAGE": "backend msg"})
check("create project ok", r.get("ok"), r)
main_c = os.path.join(TMP_PROJ, "bktest", "main.c")
if os.path.exists(main_c):
    content = open(main_c, encoding="utf-8").read()
    check("placeholder replaced", "backend msg" in content and "Project : bktest" in content)
mk = os.path.join(TMP_PROJ, "bktest", "Makefile")
if os.path.exists(mk):
    mk_c = open(mk, encoding="utf-8").read()
    check("sysroot fixed", "SYSROOT ?=" in mk_c and "prebuilds/sysroot" in mk_c,
          [l for l in mk_c.splitlines() if l.startswith("SYSROOT")])

projs = sdk.list_projects()
check("list projects", len(projs) >= 1, [p["name"] for p in projs])

# 编译 check (同步等待任务完成)
task = sdk.build("check")
lines = sdk.executor.drain(task, timeout=120)
joined = "\n".join(lines)
check("build check done", task.status == "done" and task.exit_code == 0, task.status)

# repo status
repos = {"kernel": "git@example.com:kernel.git"}
comps = sdk.repo_status(repos)
kernel = next((c for c in comps if c["rel"] == "kernel"), None)
check("repo status kernel exists", kernel is not None and kernel["exists"])
check("repo status kernel url", kernel is not None and kernel["url"] == "git@example.com:kernel.git")

# 清理
sdk.delete_project("bktest")
check("delete project", not os.path.exists(os.path.join(TMP_PROJ, "bktest")))

print("\nALL PASS" if fails == 0 else f"\n{fails} FAILED")
sys.exit(0 if fails == 0 else 1)
