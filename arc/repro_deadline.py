"""验证「回合守时」——不联网、不花 token。

两个层次：
  A. 真 socket：起一个收下请求就永远不回的 HTTP 服务器，看驱动能不能在
     截止时间掐断它。这是修的那个 bug 的真实形状 —— urllib 的 timeout= 是
     按每次 recv 计的，上游只要不关连接就永远不响。
  B. 假 _chat：直接把会话层换掉，逐个断言 run(timeout=T) 的墙钟。
"""
from __future__ import annotations

import io
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tool_driver import ToolDriver  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str) -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label:52} {detail}")
    if not ok:
        FAILURES.append(label)


# ---------------------------------------------------------------- A. 真 socket

class _StallHeaders(BaseHTTPRequestHandler):
    """收下请求体，连状态行都不发。卡住的是 open_upstream()。"""

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(300)

    def log_message(self, *args) -> None:
        pass


class _StallBody(BaseHTTPRequestHandler):
    """状态行和头都发了，正文永远不发。卡住的是 resp.read()。"""

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100000")
        self.end_headers()
        time.sleep(300)

    def log_message(self, *args) -> None:
        pass


def real_socket_case(label: str, handler, limit: float, expect_abort_msg: str) -> None:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="repro-socket-"))
    driver = ToolDriver(tmp, {"_ARC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
                              "_ARC_MODEL": "stall"}, None, 60, None)

    buf = io.StringIO()
    t0 = time.time()
    with redirect_stdout(buf):
        ok, text = driver.run("hello", limit, None)
    elapsed = time.time() - t0
    out = buf.getvalue()

    check(label, elapsed <= limit + 4.0 and ok is False,
          f"limit={limit:.0f}s actual={elapsed:.1f}s ok={ok} (+{elapsed - limit:.1f}s)")
    check(f"  └ 日志说清了是{expect_abort_msg}",
          expect_abort_msg in out, "")
    check("  └ 返回值是超时", "timed out" in text.lower(), f"text={text[:60]!r}")


# ---------------------------------------------------------------- B. 假 _chat

def fake_chat_case(label: str, block: float, limit: float, expect_ok: bool) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="repro-fake-"))
    driver = ToolDriver(tmp, {}, None, 60, None)

    def blocking_chat(messages, tools, timeout):
        time.sleep(block)
        return True, {"role": "assistant", "content": "late reply", "tool_calls": None}, ""

    driver._chat = blocking_chat
    buf = io.StringIO()
    t0 = time.time()
    with redirect_stdout(buf):
        ok, text = driver.run("prompt", limit, None)
    elapsed = time.time() - t0
    over = elapsed - min(block, limit)
    check(label, over <= 3.0 and ok is expect_ok,
          f"limit={limit:.0f}s actual={elapsed:.1f}s ok={ok} (+{over:.1f}s)")


def multi_turn_case() -> None:
    """连着跑几个回合 —— 那 294 秒的回合是**第二次**调用，形状要对得上。"""
    tmp = Path(tempfile.mkdtemp(prefix="repro-multi-"))
    driver = ToolDriver(tmp, {}, None, 60, None)
    beats = []

    def blocking_chat(messages, tools, timeout):
        time.sleep(12)
        return True, {"role": "assistant", "content": "done", "tool_calls": None}, ""

    driver._chat = blocking_chat
    for n in (1, 2, 3):
        buf = io.StringIO()
        t0 = time.time()
        with redirect_stdout(buf):
            ok, _ = driver.run("prompt", 5, None)
        elapsed = time.time() - t0
        beats.append((n, elapsed, ok, buf.getvalue().count("still running")))
    for n, elapsed, ok, hb in beats:
        check(f"第 {n} 个回合守时", elapsed < 8.0 and ok is False,
              f"actual={elapsed:.1f}s ok={ok} heartbeats={hb}")


def main() -> int:
    print("deadline discipline  (no network, no tokens)")
    print()
    print("A. 真 socket：上游卡住")
    real_socket_case("卡在响应头之前 / 限时 4s -> 准时返回", _StallHeaders, 4, "abandoned")
    real_socket_case("卡在响应正文   / 限时 4s -> 准时返回", _StallBody, 4, "aborting the request in flight")
    print()
    print("B. 假 _chat：会话层")
    fake_chat_case("阻塞 10s / 限时 2s  -> 2s 内返回且失败", 10, 2, False)
    fake_chat_case("阻塞  5s / 限时 8s  -> 正常成功", 5, 8, True)
    fake_chat_case("阻塞 40s / 限时 3s  -> 3s 内返回且失败", 40, 3, False)
    print()
    multi_turn_case()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  !! {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
