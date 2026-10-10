"""CCB 内核驱动 —— 把 v4 的 TS 内核接到 ARC 适配器上。

内核对 `octos_stdio.py` 是**完全不透明**的：它只知道
`[octos_bin, "serve", "--stdio", "--solo", "--data-dir", X]` 这一串 argv
加一套 NDJSON JSON-RPC。所以这里做的全部事情就是**把 octos_bin 指对**：

    octos_bin  →  运行期 materialize 的 shim  →  node <bundle>/vendor/kernel-dist/serve.mjs

驱动本体（回合循环、会话作用域、心跳包装、重试）直接复用 `main.OctosDriver`
—— 那个类在 v3 里是死代码，但实现是完整的，且已经实现了 `run` /
`without_tools` / `end_scope` / `close` 四件套。**不重写它。**

为什么要有这个文件而不是直接在 main.py 里算路径：
    1. 内核查找 + shim materialize 是**运行期行为**（要落盘、要 chmod），
       放进 main.py 会让那个已经 24 万行的文件再多一段不可单测的逻辑；
    2. 这里的两个函数都能脱离主流程单测（`python -m ccb_driver --selftest`）。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# main.py 就在同一个目录里（zip 解出来后也是）
BUNDLE_DIR = Path(__file__).resolve().parent

# 内核产物的候选相对路径（相对 BUNDLE_DIR）。第一条是正式位置。
_KERNEL_RELPATHS = (
    Path("vendor") / "kernel-dist" / "serve.mjs",
    Path("vendor") / "kernel-dist" / "serve.js",
)

_NODE_MIN_MAJOR = 18


class KernelNotFound(RuntimeError):
    pass


def find_node() -> str:
    """判分机上 node 是确定存在的（整个 harness 就是 npm run build / npm start），
    但还是显式找一次 —— 找不到时的报错要能一眼看出是环境问题而不是内核问题。"""
    for key in ("CCB_NODE", "NODE_BIN"):
        p = os.environ.get(key)
        if p and Path(p).is_file():
            return p
    found = shutil.which("node")
    if found:
        return found
    raise KernelNotFound(
        "找不到 node。v4 的内核是 JS 产物（serve.mjs），需要 node >= "
        f"{_NODE_MIN_MAJOR} 才能跑。设 CCB_NODE=<path> 可以显式指定。")


def find_kernel() -> Path:
    """内核入口（serve.mjs）的五级查找。

    结构照抄的是旧 main.find_octos()（那一套连同 `OCTOS_RELEASE_URL` 的下载兜底
    已随 Rust 内核路径一起删除），但**从来就没有下载兜底** —— 内核是我们自己
    构建的，没有 release 可以下。找不到就是打包漏了文件。
    """
    # 1. 环境变量直接指定
    env_bin = os.environ.get("CCB_BIN")
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)

    # 2. zip 里自带的（正常路径）
    for rel in _KERNEL_RELPATHS:
        p = BUNDLE_DIR / rel
        if p.is_file():
            return p

    # 3. 缓存目录（调试用：把同一个产物放到别处复用）
    cache = Path(os.environ.get("CCB_CACHE_DIR", "/tmp/ccb-kernel"))
    for rel in _KERNEL_RELPATHS:
        p = cache / rel.name
        if p.is_file():
            return p

    # 4. 开发期兜底：从 arc/ 往上找到 v4 根，再看 kernel 的构建源
    for parent in (BUNDLE_DIR, *BUNDLE_DIR.parents[:3]):
        for rel in _KERNEL_RELPATHS:
            p = parent / rel
            if p.is_file():
                return p

    searched = "\n  ".join(str(BUNDLE_DIR / rel) for rel in _KERNEL_RELPATHS)
    raise KernelNotFound(
        "找不到内核产物 serve.mjs。查过：\n  " + searched +
        f"\n  CCB_BIN={env_bin or '(未设)'}"
        "\n多半是 pack.sh 漏了 vendor/ —— 漏文件在云机上是第一行 ImportError、"
        "整轮 0 分、日志里什么都看不到。")


@dataclass(frozen=True)
class Shim:
    """内核的启动器。

    **Windows 上它是「一个文件 + 两个环境变量」**，不是单个文件 —— 真正的路径
    靠 `CCB_NODE` / `CCB_SERVE` 传进去（理由见 `_ensure_shim`）。所以让两者
    只能一起拿到：调用方写 `shim.merged_env(env)`，而不是自己拼路径 + 自己记得
    补环境变量。忘了补的症状是 node 报 `Cannot find module '...\\%CCB_SERVE%'`。
    """

    path: Path
    env: dict = field(default_factory=dict)

    def merged_env(self, base: dict) -> dict:
        out = dict(base)
        out.update(self.env)
        return out

    def __str__(self) -> str:
        # `str(shim)` 只可能是「把 Shim 当成路径用了」，而它的后果是
        # `FileNotFoundError` 加一坨 200 字符的 dataclass repr —— 完全看不出
        # 该改哪儿。这个类型就是为此存在的，所以这里直接点名。
        # （实测：改成 Shim 之后有 4 个调用点，其中 2 个就是这么漏的。）
        raise TypeError(
            "Shim 不是路径。用 shim.path 当路径、用 shim.merged_env(env) 拿环境变量"
            "（两者必须一起用，见 _ensure_shim）。"
        )


# Windows shim 的内容。**必须逐字节是纯 ASCII**，一个非 ASCII 都不能有 ——
# 这不是「省事」，是实测出来的（见 `_ensure_shim` 的长注释）。
#
#   1. `if not defined ...` 那段是给人看的：调用方忘了合 `Shim.env` 时立刻点名，
#      而不是让 node 去报 `Cannot find module 'C:\...\%CCB_SERVE%'`。
#      文本里刻意不放圆括号/感叹号，免得在 () 块里被 cmd 当成语法。
#   2. 换行只能有一个 \r\n —— 用 write_text 写 '\r\n' 会被文本模式再翻一次成
#      '\r\r\n'。所以这里是 bytes 字面量。
_WIN_SHIM = (
    b"@echo off\r\n"
    b"if not defined CCB_SERVE (\r\n"
    b"  echo [octos-kernel] CCB_NODE or CCB_SERVE not set"
    b" - caller forgot to merge Shim.env, see ccb_driver._ensure_shim 1>&2\r\n"
    b"  exit /b 9009\r\n"
    b")\r\n"
    b'"%CCB_NODE%" "%CCB_SERVE%" %*\r\n'
)


def _ensure_shim(serve_js: Path, data_dir: Path) -> Shim:
    """写一个可执行 shim，让 [octos_bin, "serve", ...] 展开成 node serve.mjs ...

    **为什么不在 zip 里带现成的 shim**：zip 不保存可执行位，Linux 上解压出来
    是 644，Popen 直接 Permission denied；Windows 上则是根本没有可执行位这个
    概念、要靠扩展名。所以只能在运行期按平台现写一个。

    Linux 判分机走的是第二条分支（`#!/bin/sh` + 755）。

    ## Windows 那条分支为什么写成「纯 ASCII + 环境变量」

    之前的写法是「把路径直接写进 .cmd，按 OEM 代码页（cp936）编码」—— 那是个
    **错的假设**，实测炸过：

        Cannot find module 'C:\\Users\\evena\\Desktop\\<乱码>\\arc\\vendor\\...'

    cmd.exe 解释 .cmd **文件内容**用的不是「这个文件是什么编码」，而是**当前控制台
    的输出代码页**（`GetConsoleOutputCP`）。而控制台是**整个进程树共享**的，
    任何东西都能改它而且**改完不还原**。实测：跑一次

        powershell -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; ..."

    `GetConsoleOutputCP()` 就从 936 变成 65001，之后**一直是 65001**。那一刻起
    所有按 cp936 写出来的 .cmd 全部被当成 UTF-8 解 —— 路径里只要有一个非 ASCII
    字符（`黑客松` 就是），node 拿到的就是乱码路径。

    这个坑的恶劣之处在于**触发者跟内核毫无关系**：我们只是在自己的测试脚本里调了
    一次 powershell 去数进程，内核就起不来了；而报错信息里只有一串乱码路径。
    判分机是 Linux（走 sh 那条分支、`exec` 直传 argv、没有代码页这回事），所以
    这是**本机开发才踩得到**的坑 —— 但本机正是我们验证一切的地方。

    所以现在 .cmd 里只留 ASCII：路径经 `CCB_NODE` / `CCB_SERVE` 两个环境变量传。
    环境变量走 `CreateProcessW` 的 UTF-16 环境块，**根本不经过代码页**，
    控制台被人改成什么都无所谓。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    node = find_node()

    if os.name == "nt":
        # Windows：.cmd 由 CreateProcess 经 %COMSPEC% 解释，扩展名必须是 .cmd/.bat。
        shim = data_dir / "octos-kernel.cmd"
        shim.write_bytes(_WIN_SHIM)
        return Shim(shim, {"CCB_NODE": node, "CCB_SERVE": str(serve_js)})

    # ⚠️ `exec` 不是装饰：有了它 sh 把 PID 让给 node，`[octos_bin, ...]` 的 PID
    #    就等于内核本身；没有它 sh 会留着当父进程，收尸时就得连树一起杀
    #    （见 octos_stdio.close()）。两行不带 exec 的 shim 会让判分机上出现
    #    和 Windows 一样的进程泄漏。
    shim = data_dir / "octos-kernel"
    shim.write_text(f'#!/bin/sh\nexec "{node}" "{serve_js}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return Shim(shim, {})


# node 的 `process.arch` 名字 ≠ Python 的 `platform.machine()`。
# `ripgrep.ts:70-73` 拼的是 `vendor/ripgrep/<process.arch>-<process.platform>/rg[.exe]`，
# 所以这里必须翻成 **node 的叫法** —— 翻错了的后果是「目录不在」，而报错文本里
# 只有那个目录名，看不出是名字翻译错了。
_NODE_ARCH = {
    "x86_64": "x64", "amd64": "x64",
    "aarch64": "arm64", "arm64": "arm64",
    "i386": "ia32", "i486": "ia32", "i586": "ia32", "i686": "ia32",
    "armv7l": "arm", "armv6l": "arm",
    "ppc64le": "ppc64", "s390x": "s390x",
}


def bundled_rg(kernel_path: Path, plat: str | None = None,
               machine: str | None = None) -> Path | None:
    """随包的 ripgrep 在哪；没有就 None。

    平台/架构可注入，所以**在 Windows 上也能测 Linux 那一支**（`is_file()` 不看
    文件内容），这正是它的意义：判分机是 Linux，本机是 Windows，不注入就没法
    验证判分机上会走哪个分支。
    """
    plat = plat or ("win32" if os.name == "nt" else "linux")
    arch = _NODE_ARCH.get((machine or platform.machine()).lower())
    if arch is None:
        return None
    rg = (kernel_path.parent / "vendor" / "ripgrep" / f"{arch}-{plat}"
          / ("rg.exe" if plat == "win32" else "rg"))
    return rg if rg.is_file() else None


def prepare_ripgrep(kernel_path: Path) -> tuple[str, str | None]:
    """让随包的 ripgrep 在这个平台上**真的能跑**。返回 `(状态描述, rg 所在目录)`。

    `Glob` 和 `Grep` 两个工具**完全依赖它**（`GrepTool.ts:18` 和
    `glob.ts:119` 都走 `ripGrep()`），六个暴露工具里占了两个。

    两件事，都不是可选的：

    **① 补可执行位。** zip **不保证**保留 Unix 权限位 —— 判分机用 `unzip` 会还原，
    用 Python 的 `zipfile.extractall` 则一律 644。644 的后果是 `execFile` 报
    EACCES，而那时的表现是「Grep/Glob 永远失败」，从现象看不出跟权限有关。
    所以不赌，运行期补一次（同 `_ensure_shim` 的理由）。

    **② 把 rg 所在目录挂上 PATH。** 这是给 `ripgrep.ts` 的第二条命：它用
    `distRoot` 拼绝对路径去找内置 rg，而 `distRoot`（`distRoot.ts`）是**按路径里
    有没有 `dist` / `src` 这两个目录名**倒推的 —— 找得到就叫那个前缀，找不到才
    用文件自己所在目录。我们的产物在 `vendor/kernel-dist/`，`kernel-dist` ≠ `dist`，
    所以本机是对的；但**判分机把 zip 解到哪儿我们控制不了**，只要解压路径里出现
    一个叫 `dist` 或 `src` 的目录，`distRoot` 就会算歪、内置 rg 就找不到。那时
    `ripgrep.ts` 退到「系统 rg」，而系统上有没有 rg 是听天由命。

    挂上 PATH 之后那个兜底总是命中：`getRipgrepConfig()` 在系统分支里**故意
    返回裸的 `'rg'`**（不返回绝对路径，防 PATH 劫持），所以它最终是按 PATH 找的。

    代价：PATH 上只多出这一个目录，而里面只有 `rg` 一个文件，对 `Bash` 工具无害。
    """
    rg = bundled_rg(kernel_path)
    if rg is None:
        return (f"!! 没有随包的 ripgrep（{platform.machine()}/"
                f"{'win32' if os.name == 'nt' else 'linux'}）—— "
                "Glob/Grep 会退到系统 rg，判分机上没有就全废", None)
    if os.name != "nt":
        try:
            mode = rg.stat().st_mode
            if not mode & 0o111:
                rg.chmod(0o755)
                mode = rg.stat().st_mode
        except OSError as exc:
            return (f"!! chmod {rg} 失败：{exc}（execFile 会 EACCES）", None)
        return (f"{rg}  mode={oct(mode)[-3:]}", str(rg.parent))
    return (f"{rg}", str(rg.parent))


def selftest() -> int:
    """不碰网络、不起进程的静态自检。CI / 打包后冒烟用。"""
    print(f"platform      : {sys.platform} (os.name={os.name})")
    print(f"python        : {sys.version.split()[0]}")
    print(f"BUNDLE_DIR    : {BUNDLE_DIR}")
    try:
        node = find_node()
        ver = subprocess.run([node, "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=20).stdout.strip()
        print(f"node          : {node}  ({ver})")
        major = int(ver.lstrip("v").split(".")[0])
        if major < _NODE_MIN_MAJOR:
            print(f"  !! node 主版本 {major} < {_NODE_MIN_MAJOR}")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"node          : !! {exc}")
        return 1

    try:
        kernel = find_kernel()
        print(f"kernel        : {kernel}  ({kernel.stat().st_size} bytes)")
    except KernelNotFound as exc:
        print(f"kernel        : !! {exc}")
        return 1

    # ripgrep：Glob/Grep 两个工具的全部依赖。这里把**两个平台**都验一遍 ——
    # 本机是 Windows 而判分机是 Linux，只验本机等于没验判分机（`is_file()`
    # 不看文件内容，所以 Linux 那两个 ELF 在 Windows 上照样能查在不在）。
    bad_rg = 0
    for plat, machine in (("win32", "AMD64"), ("linux", "x86_64"), ("linux", "aarch64")):
        rg = bundled_rg(kernel, plat, machine)
        mark = "ok " if rg else "!! "
        size = f"{rg.stat().st_size:,} B" if rg else "缺失"
        print(f"ripgrep {plat:5s}/{machine:7s}: {mark}{size}")
        bad_rg += 0 if rg else 1
    note, rg_dir = prepare_ripgrep(kernel)
    print(f"ripgrep 本机  : {note}")
    if rg_dir is None:
        bad_rg += 1
    if bad_rg:
        print("  !! 有缺口：Glob/Grep 会退到系统 rg，判分机上大概率没有")
        return 1

    import tempfile
    with tempfile.TemporaryDirectory(prefix="ccb-selftest-") as td:
        shim = _ensure_shim(kernel, Path(td))
        mode = oct(shim.path.stat().st_mode)[-3:] if os.name != "nt" else "n/a (nt)"
        print(f"shim          : {shim.path.name}  mode={mode}")
        if shim.env:
            # 纯 ASCII 的保证就在这一行：shim 文件里一个非 ASCII 都不该有，
            # 路径全部走环境变量。见 _ensure_shim。
            raw = shim.path.read_bytes()
            print(f"shim 内容     : {raw!r}")
            print(f"              : 纯 ASCII = {all(b < 128 for b in raw)}"
                  f"（必须 True，否则控制台代码页一变路径就乱）")
        # 真起一次：只发一个必然报错的 method，确认进程活着且会说协议
        try:
            # `encoding="utf-8"` 同 `octos_stdio.py`：不给就落到
            # `locale.getpreferredencoding()`，中文 Windows 上是 cp936，
            # 而内核一律吐 UTF-8。这里目前只读一行 ASCII 的协议错误，
            # 但**协议本身可能带中文**（tool 输出、模型正文），
            # 不该靠「现在恰好是 ASCII」侥幸。
            proc = subprocess.Popen(
                [str(shim.path), "serve", "--stdio", "--solo", "--data-dir", td],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=shim.merged_env(os.environ))
            proc.stdin.write('{"jsonrpc":"2.0","id":"st-1","method":"__selftest__","params":{}}\n')
            proc.stdin.flush()
            line = proc.stdout.readline().strip()
            proc.stdin.close()
            proc.wait(timeout=10)
            print(f"shim 应答      : {line}")
            if '"id":"st-1"' not in line.replace(" ", ""):
                print("  !! shim 没有按协议应答")
                return 1
        except Exception as exc:  # noqa: BLE001
            print(f"shim 起不来    : !! {exc}")
            return 1

    print("SELFTEST_OK")
    return 0


def make_driver(octos_driver_cls):
    """造出 CcbDriver 类，基类是传进来的 OctosDriver。

    **为什么不在这里 `from main import OctosDriver`**：main.py 是以 `__main__`
    的身份跑起来的，`main` 这个名字**不在 sys.modules 里**。在 ccb_driver 里
    import main 会让 Python 把那个 24 万行的文件**整个再执行一遍**（经典的
    `__main__` 双导入陷阱），模块级副作用跑两次。

    所以由调用方（main.py，它手上就有 OctosDriver）把基类递进来。调用点：

        from ccb_driver import make_driver
        driver_class = make_driver(OctosDriver)
    """

    class CcbDriver(octos_driver_cls):
        """CCB 内核驱动。除了把 octos_bin 指到 shim 上，**什么都不改**。

        `hooks` / `tools_disabled` / `session_scope` / 重试 / 心跳全部由
        OctosDriver 原样处理 —— 它们走的是 profile 与协议，而桥两者都说。
        """

        def __init__(self, cwd: Path, env: dict, data_dir: Path,
                     max_iterations: int, events_log: Path) -> None:
            kernel = find_kernel()
            shim = _ensure_shim(kernel, data_dir)
            # env 在这里合一次就够了：OctosDriver 把它原样存进 self.env，
            # 之后 _get_session() / run_octos 都从那儿取（main.py:900 / :999）。
            env = shim.merged_env(env)
            self.rg_note, rg_dir = prepare_ripgrep(kernel)
            if rg_dir:
                # 滤掉空条目：POSIX 的 PATH 里一个空的字段意思是「当前目录」，
                # 而 `Bash` 工具跑的就是这个 env —— 不要凭白给它加上这个。
                env["PATH"] = os.pathsep.join(
                    p for p in [rg_dir, *(env.get("PATH") or "").split(os.pathsep)] if p)
            super().__init__(str(shim.path), cwd, env, data_dir, max_iterations, events_log)
            self.kernel_path = kernel
            # 受保护目录交给桥自己去 profile 文件里读（和 tool_driver._protected_roots
            # 读的是同一份 hooks），所以这里不需要 protected_dirs 做判定 —— 但它是
            # **下面那个 tripwire 的输入**，main.py 也认这个属性（见 :3843）。
            self.protected_dirs: list = []
            self._protection_checked = False

        # ── 保护目录的失效报警 ───────────────────────────────────────────
        #
        # 为什么需要它：桥的受保护目录**只有 hooks 一个来源**（profile 文件里
        # 只有 `hooks`）。而 `protected_hooks()`（main.py:637）在
        # `deny_protected.py` 不存在时返回 `[]` —— 那一刻 v4 **完全不设防**，
        # 而且没有任何报错，症状只是「模型写进了 public-tests 而没人拦」。
        #
        # v3 不会这样：它除了 hooks 还有 `self.protected_dirs` 这条第二来源
        # （tool_driver.py:790）。v4 没有第二来源，所以**必须有这个报警**补上。
        #
        # ⚠️ 报警而**不抛异常**是刻意的：兜底是 main.py 每回合末恢复受保护树
        #    （那一条不依赖 hooks），所以「没设防」是**可存活**的；抛异常把整轮
        #    打成 0 分反而是更坏的结局。
        #
        # 同一个方法里还报 ripgrep —— 两者是同一类东西：**只会以「工具莫名其妙
        # 不好使」的形式露面的启动期事实**。跑真回合之前先在 stderr 说清楚，
        # 免得判分日志里只剩一个「Grep 老是失败」。
        def _warn_if_protection_dead(self) -> None:
            if self._protection_checked:
                return
            self._protection_checked = True
            sys.stderr.write(f"[ccb-driver] ripgrep: {self.rg_note}\n")
            sys.stderr.flush()
            if self.protected_dirs and not self.hooks:
                sys.stderr.write(
                    "[ccb-driver] !! 保护目录失效：main.py 交了 "
                    f"{len(self.protected_dirs)} 个受保护目录，但 protected_hooks() 返回空 —— "
                    "多半是 zip 里漏了 hooks/deny_protected.py。\n"
                    "[ccb-driver] !! 桥只能从 profile 的 hooks 读受保护目录，没有第二来源，"
                    "所以这一轮**不会拦截**对官方测试/需求目录的写入。\n"
                    "[ccb-driver] !! 剩下唯一的防线是 main.py 回合末恢复受保护树。\n")
                sys.stderr.flush()

        def run(self, prompt: str, timeout: int, monitor=None):
            self._warn_if_protection_dead()
            return super().run(prompt, timeout, monitor)

    CcbDriver.__name__ = "CcbDriver"
    CcbDriver.__qualname__ = "CcbDriver"
    return CcbDriver


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(__doc__)
    raise SystemExit(0)
