"""模型交回来的文本在落盘前的那几道修补。

**这不是内核的一部分。** 内核从不这样做 —— 它是 harness 侧的补丁，只作用在
驱动层（`tool_driver.py` 的 `write_file` / `edit_file` 里），所以它单独成一个模块，
不混进任何「内核」的目录。三件事：

  - `ensure_charset`      页面缺 charset 声明 → Chromium 按 Latin-1 解，中文全变乱码
  - `repair_flattened_js` 换行被转义成字面 `\\n` 的一行 → 启动时 `node --check` 就挂
  - `_find_span`          模型重排了缩进 → 按内容定位，别因为空白丢掉一次正确的编辑

三条都来自实测事故，不是预防性防御。共同点：**模型是对的，是文本在传输里坏了**，
所以修的是文本，不是让它重做一遍。
"""

from __future__ import annotations

import re
from pathlib import Path

CHARSET_META = '<meta charset="utf-8">'


def ensure_charset(text: str) -> str:
    """Pages without a charset declaration were decoded as Latin-1 by Chromium
    (the servers send `text/html` without charset), so every Chinese string the
    specs look for turned into mojibake (local s5/s10: 0/6). Inject the meta tag."""
    if re.search(r"<meta[^>]+charset", text, re.IGNORECASE):
        return text
    m = re.search(r"<head[^>]*>", text, re.IGNORECASE)
    if m:
        return text[:m.end()] + CHARSET_META + text[m.end():]
    m = re.search(r"<html[^>]*>", text, re.IGNORECASE)
    if m:
        return text[:m.end()] + "<head>" + CHARSET_META + "</head>" + text[m.end():]
    return CHARSET_META + "\n" + text


def js_parses(path: Path) -> bool | None:
    """`node --check`; None when node is unavailable."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        return None
    try:
        return subprocess.run([node, "--check", str(path)], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def repair_flattened_js(path: Path) -> bool:
    """Partially flattened blocks (some lines carry literal \\n between statements;
    local s12 crashed at startup) are only rewritten when the unescaped version
    parses and the original does not."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if "\\n" not in text or js_parses(path) is not False:
        return False
    fixed = "\n".join(line.replace("\\n", "\n") if line.count("\\n") >= 2 and not line.lstrip().startswith(("res.", "return"))
                      else line for line in text.split("\n"))
    if fixed == text:
        return False
    backup = path.read_bytes()
    path.write_text(fixed, encoding="utf-8")
    if js_parses(path):
        return True
    path.write_bytes(backup)
    return False


def _find_span(text: str, find: str) -> tuple[int, int] | None:
    """Where `find` sits in `text`: exact first, then ignoring indentation.

    Models re-indent the source they quote -- a block lifted into a nested context
    comes back with its inner lines shifted -- and rejecting that would throw away a
    correct edit over whitespace. The tolerant pass strips every line on both sides and
    maps the match back to a real character span, so the file keeps its own
    indentation everywhere the edit does not reach.
    """
    if not find:
        return None
    at = text.find(find)
    if at >= 0:
        return at, at + len(find)

    lines = text.split("\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    norm_starts, norm_parts, parts = [], [], 0
    for ln in lines:
        norm_starts.append(parts)
        s = ln.strip()
        norm_parts.append(s)
        parts += len(s) + 1
    norm = "\n".join(norm_parts)
    want = "\n".join(ln.strip() for ln in find.strip("\n").split("\n"))
    if not want:
        return None
    j = norm.find(want)
    if j < 0:
        return None

    def to_text(off: int) -> int:
        i = 0
        while i + 1 < len(norm_starts) and norm_starts[i + 1] <= off:
            i += 1
        ln = lines[i]
        lead = len(ln) - len(ln.lstrip(" \t"))
        return starts[i] + lead + (off - norm_starts[i])

    return to_text(j), to_text(j + len(want))
