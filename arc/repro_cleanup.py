"""验证「进程/端口清理」在本地真的发生 —— 不联网、不花 token。

改动前这四个函数在 Windows 上全是**空转**：函数正常返回、一个进程都不杀，
日志里也看不出区别。因为判分机上「谁占着端口、这进程是不是我们的」靠
lsof / ps / /proc，而本地三样都没有（lsof 和 free 根本不存在，ps 是 Git 自带的
读不到 cwd，/proc 不存在）。

tb-v3-tool-c 节点 1 的 `Failed at: build/start / code: 'EADDRINUSE'` 就是这么来的：
回合里起的自测服务器占着 43101，验收测试要在同一个端口起服务器，「开跑前先腾空
端口」那一步是空的 —— 节点拿 0 分，而它的功能根本没被测过。

每个用例起**真的 node 监听进程**（形状和回合里残留的 server.js 一样），再看清理
函数有没有把它清掉。用 node 不用 python，是因为 reap 只认 node/npm/npx/sh/bash。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acceptance import (  # noqa: E402
    free_owned_ports, free_port, listening_pids, owned_by_workspace,
    port_open, reap_workspace_processes, win_process_table, win_kill_tree,
)

FAILURES: list[str] = []
SPAWNED: list[subprocess.Popen] = []

INNER_JS = """
const net = require('net');
const port = Number(process.argv[2]);
net.createServer(() => {}).listen(port, '127.0.0.1', () => console.log('listening ' + port));
setInterval(() => {}, 1 << 30);
"""

# npm 那一层的形状：外层活着，真正占端口的是它拉起来的那个子进程。
OUTER_JS = """
const { spawn } = require('child_process');
const port = process.argv[2];
spawn(process.execPath, [__dirname + '/inner.js', port], { stdio: 'inherit' });
setInterval(() => {}, 1 << 30);
"""


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label:54} {detail}")
    if not ok:
        FAILURES.append(label)


def wait_until(pred, timeout: float = 20.0, step: float = 0.25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def free_local_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def write_app(where: Path) -> None:
    where.mkdir(parents=True, exist_ok=True)
    (where / "inner.js").write_text(INNER_JS, encoding="utf-8")
    (where / "outer.js").write_text(OUTER_JS, encoding="utf-8")


def spawn(cmd: list[str], cwd: Path, detach: bool = False) -> subprocess.Popen:
    if detach:
        # 走一层 sh 再 `&`：sh 立刻退出，监听进程变成孤儿、父进程链断掉。
        # 判分机上「外来进程」的形状就是这条 —— 不是我们的子孙，也不是从我们的
        # 工作目录里起来的。
        cmd = ["sh", "-c", " ".join(f"'{c}'" for c in cmd) + " &"]
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not detach:
        SPAWNED.append(proc)
    return proc


def listener_pid(port: int) -> int | None:
    pids = listening_pids(port)
    return pids[0] if pids else None


# ------------------------------------------------------------------ 用例

def case_free_port() -> None:
    """free_port 必须把端口腾空 —— 这是 EADDRINUSE 那一次的现场。"""
    ws = Path(tempfile.mkdtemp(prefix="cleanup-a-"))
    write_app(ws)
    port = free_local_port()
    spawn(["node", str(ws / "inner.js"), str(port)], ws)
    if not wait_until(lambda: port_open(port)):
        check("残留监听起来了（用例前提）", False, f"port={port} 没起来")
        return
    check("残留监听起来了（用例前提）", True, f"port={port} pid={listener_pid(port)}")
    free_port(port)
    check("free_port 之后端口腾空", wait_until(lambda: not port_open(port), 15),
          f"port={port} 仍然被占" if port_open(port) else "")


def case_tree_kill() -> None:
    """reap 杀外层，/T 必须连它拉起来的那层一起带走（npm → node server.js）。"""
    ws = Path(tempfile.mkdtemp(prefix="cleanup-b-"))
    write_app(ws)
    port = free_local_port()
    spawn(["node", str(ws / "outer.js"), str(port)], ws)
    if not wait_until(lambda: port_open(port)):
        check("外层拉起了内层监听（用例前提）", False, f"port={port} 没起来")
        return
    check("外层拉起了内层监听（用例前提）", True, f"port={port} 内层 pid={listener_pid(port)}")
    killed = reap_workspace_processes(ws, lambda m: None)
    check("reap 认出了工作目录里的 node", killed >= 1, f"killed={killed}")
    check("杀外层之后端口也腾空（taskkill /T 生效）",
          wait_until(lambda: not port_open(port), 15),
          f"port={port} 仍然被占" if port_open(port) else "")


def case_ownership() -> None:
    """认得出自己人，也放得过外来进程。"""
    ws = Path(tempfile.mkdtemp(prefix="cleanup-c-"))
    outside = Path(tempfile.mkdtemp(prefix="cleanup-x-"))
    write_app(ws)
    write_app(outside)

    port_ours = free_local_port()
    ours = spawn(["node", str(ws / "inner.js"), str(port_ours)], ws)
    wait_until(lambda: port_open(port_ours))
    check("工作目录里起来的监听 -> 认为是我们的",
          ours.pid is not None and owned_by_workspace(ours.pid, ws),
          f"pid={ours.pid}")

    port_foreign = free_local_port()
    spawn(["node", str(outside / "inner.js"), str(port_foreign)], outside, detach=True)
    if not wait_until(lambda: port_open(port_foreign)):
        check("外来监听起来了（用例前提）", False, f"port={port_foreign} 没起来")
        return
    foreign_pid = listener_pid(port_foreign)
    check("外来监听起来了（用例前提）", True, f"port={port_foreign} pid={foreign_pid}")
    check("工作目录外的孤儿监听 -> 不认为是我们的",
          foreign_pid is not None and not owned_by_workspace(foreign_pid, ws),
          f"pid={foreign_pid}")

    check("系统进程（PID 4）稳定判为外来",
          win_process_table() and not owned_by_workspace(4, ws), "pid=4")

    free_owned_ports([port_ours, port_foreign], ws)
    check("free_owned_ports 清掉了自己的那个", not port_open(port_ours), f"port={port_ours}")
    check("free_owned_ports 没碰外来的那个", port_open(port_foreign), f"port={port_foreign}")
    if foreign_pid:
        win_kill_tree(foreign_pid)


def case_table() -> None:
    """进程表本身要拿得到 —— 拿不到就什么都别谈。"""
    table = win_process_table()
    check("win_process_table 拿得到进程表", len(table) > 5, f"{len(table)} 个进程")
    check("表里有我们自己的 pid", any(row[0] == os.getpid() for row in table), f"pid={os.getpid()}")


def main() -> int:
    print("local process/port cleanup  (no network, no tokens)")
    print(f"  platform={sys.platform} os.name={os.name} "
          f"node={'yes' if subprocess.run(['node', '--version'], capture_output=True).returncode == 0 else 'NO'}")
    print()
    if sys.platform != "win32":
        print("  这个脚本验的是 Windows 口径；非 win32 上跑只能算冒烟。")
        print()
    for name, case in (("table", case_table), ("free_port", case_free_port),
                       ("tree kill", case_tree_kill), ("ownership", case_ownership)):
        print(f"{name}:")
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            check(f"{name} 抛异常", False, repr(exc))
        print()
    for proc in SPAWNED:
        try:
            win_kill_tree(proc.pid)
        except Exception:  # noqa: BLE001
            pass
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  !! {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
