"""需求树的一个纯函数：内容指纹。

原先这里还有 `flatten_atomic` / `topo_order` / `ancestors_of` —— 把树压平成原子
节点再按依赖排序的那一套。那是 DFS 编排的地基，分层编排（`layers.py`）不需要它：
树的结构本身就是顺序，不该被压平。删掉了，不是留着备而不用。

进化模式用它判断「这个节点和上一轮是不是同一份需求」。
"""

from __future__ import annotations

import hashlib
import json


def node_fingerprint(node: dict) -> str:
    """Stable hash of the requirement content (not of child nodes)."""
    payload = {
        "id": str(node.get("id") or ""),
        "name": str(node.get("name") or ""),
        "description": str(node.get("description") or ""),
        "scenarios": node.get("scenarios") or [],
        "dependencies": [str(d) for d in (node.get("dependencies") or [])],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
