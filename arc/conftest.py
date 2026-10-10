"""让 `pytest` 从任意工作目录都能 import 到 `octos`。

存在的唯一理由：`tests/` 下没有 `__init__.py`，pytest 默认的 prepend 导入模式会把
`tests/` 自己（而不是 `arc/`）放进 sys.path，于是 `from octos.core... import ...` 找不到。
这个文件位于 `arc/` 根，pytest 会把它所在的目录插进 sys.path，问题就没了。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
