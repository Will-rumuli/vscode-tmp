# -*- coding: utf-8 -*-
"""SSH 模式测试: 用本机回环 SSH 模拟远端编译机 (paramiko)。"""
import os
import sys
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from quecpi.models import ConnectionConfig
from quecpi.sdk_ops import SDK

ROOT = "/home/q/work/test/ai_sdk"
TMP_PROJ = "/tmp/qpi-ssh-test-projects"
KEY = "/tmp/qpi_test_key"

fails = 0


def check(name, cond, extra=""):
    global fails
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  -> ' + str(extra) if extra else ''}")
    if not cond:
        fails += 1


conn = ConnectionConfig(
    mode="ssh",
    host="localhost",
    port=22,
    user=os.environ.get("USER", "q"),
    key_path=KEY,
    sdk_root=ROOT,
    project_dir=TMP_PROJ,
)
sdk = SDK(conn)

# 1. 连接测试
t = sdk.executor.test_connection()
check("ssh connect ok", t.get("ok"), t.get("error", ""))
check("sdk dir exists", t.get("sdk_exists"))

# 2. 探测 (走远端命令)
probe = sdk.probe()
check("ssh probe ok", probe.get("ok"), probe.get("error", ""))
check("ssh templates == 12", sdk.list_templates().__len__() == 12)

# 3. SSH 创建工程
r = sdk.create_project("sshtest", "gpio", {"PROJECT_NAME": "sshtest", "GPIO_NUM": "520", "MODE": "steady"})
check("ssh create ok", r.get("ok"), r)
# 读回验证
main_c = os.path.join(TMP_PROJ, "sshtest", "main.c")
content = sdk.executor.read_file(main_c)
check("ssh placeholder replaced", "int mode = 1;" in content and "520" in content)
mk = sdk.executor.read_file(os.path.join(TMP_PROJ, "sshtest", "Makefile"))
check("ssh sysroot fixed", "SYSROOT ?=" in mk and "prebuilds/sysroot" in mk)

# 4. SSH 编译 check (流式)
task = sdk.build("check")
lines = sdk.executor.drain(task, timeout=120)
check("ssh build check done", task.status == "done" and task.exit_code == 0, task.status)

# 5. SSH 删除
sdk.delete_project("sshtest")
check("ssh delete", not sdk.executor.exec_sync(f"test -d {TMP_PROJ}/sshtest && echo 1")[1].strip())

print("\nALL PASS" if fails == 0 else f"\n{fails} FAILED")
sys.exit(0 if fails == 0 else 1)
