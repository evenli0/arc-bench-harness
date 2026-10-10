"""扫一遍生成的应用，验证「本地尺度不比云上松」的那几条硬不变量。

只看模型真正写出来的文件（跳过 .git/.arc/node_modules/requirements）。
"""
from __future__ import annotations

import pathlib
import sys

SKIP = ("/.git/", "/.arc/", "node_modules/", "/requirements/")
TEXTUAL = {".html", ".htm", ".js", ".cjs", ".mjs", ".css", ".json", ".ts", ".md", ".txt", ".yaml", ".yml"}


def main(root: str) -> int:
    app = pathlib.Path(root)
    violations: list[str] = []
    checked = 0
    for path in sorted(app.rglob("*")):
        if not path.is_file():
            continue
        posix = path.as_posix()
        if any(marker in posix for marker in SKIP):
            continue
        raw = path.read_bytes()
        suffix = path.suffix.lower()
        flags: list[str] = []
        if suffix in TEXTUAL:
            checked += 1
            if b"\r\n" in raw:
                flags.append("CRLF")
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                flags.append("NOT-UTF8")
        if suffix in (".html", ".htm") and b"charset" not in raw.lower():
            flags.append("NO-CHARSET")
        if suffix == ".json" and b"charset" in raw[:200].lower():
            flags.append("CHARSET-IN-JSON")
        if "\\" in path.name:
            flags.append("BACKSLASH-IN-NAME")
        print(f"  {posix:42} {len(raw):>7}b  {','.join(flags) if flags else 'ok'}")
        if flags:
            violations.append(f"{posix}: {','.join(flags)}")

    print(f"\ntextual files checked: {checked}")
    if violations:
        print("VIOLATIONS:")
        for v in violations:
            print(f"  !! {v}")
        return 1
    print("no violations")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
