"""临时目录：建的人负责删，但「建的人被硬杀」没人负责。

私有 Playwright 一份 767 MB —— 2026-09-29 实测攒了 5 份、3.0 GB，磁盘告急。
两条路都要通，缺一条就漏：

    _temp_dir + atexit    进程能走到退出 → 自己删
    _sweep_temp           进程没走到退出（关窗口 / 被硬杀 / 装 Playwright 时被
                          打断）→ 下一次启动扫掉

判据只有一条：**不能误删另一个正在跑的任务的目录**（本地 --port 43300 并行跑
两题是常规操作）。所以目录名里带属主 pid，扫的时候先探活。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import main as M


def _dead_pid() -> int:
    """一个**确定已经退出**的 pid —— 起一个进程，等它结束，拿它的号。"""
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True)
    return int(proc.stdout.strip())


def _leftover(prefix: str, pid: int) -> Path:
    """在真的临时目录里造一份「上一轮的残骸」。"""
    path = Path(tempfile.mkdtemp(prefix=f"{prefix}{pid}-"))
    (path / "payload").write_text("x", encoding="utf-8")
    return path


# ------------------------------------------------------------ 登记 + 退出时删

def test_a_registered_dir_is_removed_on_exit():
    path = M._temp_dir("octos-data-")
    assert path.is_dir()
    assert path.name.startswith(f"octos-data-{os.getpid()}-"), \
        "名字里必须带属主 pid —— _sweep_temp 靠它分辨残骸和活人"
    assert path in M._TEMP_DIRS
    M._drop_temp_dirs()
    assert not path.exists()
    assert path not in M._TEMP_DIRS


def test_dropping_an_empty_registry_is_not_an_error():
    M._TEMP_DIRS.clear()
    M._drop_temp_dirs()  # 什么都不该发生，也不该抛


# ------------------------------------------------------------ 探活

def test_a_live_pid_reads_as_alive():
    assert M._pid_alive(os.getpid()) is True


def test_a_finished_pid_reads_as_dead():
    assert M._pid_alive(_dead_pid()) is False


def test_pid_zero_is_not_alive():
    assert M._pid_alive(0) is False


# ------------------------------------------------------------ 启动时扫残骸

def test_a_dir_left_by_a_dead_run_is_swept():
    path = _leftover("octos-arc-playwright-", _dead_pid())
    M._sweep_temp()
    assert not path.exists()


def test_a_dir_owned_by_a_live_process_is_left_alone():
    """**这条是全部意义所在**：并行跑的另一个任务不能被误伤。"""
    path = _leftover("octos-arc-playwright-", os.getpid())
    try:
        M._sweep_temp()
        assert path.is_dir(), "属主还活着，扫的时候必须放过"
    finally:
        M._drop_temp_dirs()
        import shutil
        shutil.rmtree(path, ignore_errors=True)


def test_a_dir_without_a_pid_is_left_alone():
    """旧格式（这次改动之前建的）不带 pid，认不出属主就不动它。"""
    path = Path(tempfile.mkdtemp(prefix="octos-data-"))
    try:
        M._sweep_temp()
        assert path.is_dir()
    finally:
        import shutil
        shutil.rmtree(path, ignore_errors=True)


def test_the_sweep_touches_nothing_else():
    """只认自己那四种前缀。别人的临时目录一个字节都不碰。"""
    other = Path(tempfile.mkdtemp(prefix="somebody-elses-"))
    try:
        M._sweep_temp()
        assert other.is_dir()
    finally:
        import shutil
        shutil.rmtree(other, ignore_errors=True)
