# arc-bench-harness

**「智能体软件工厂」黑客松 · 决赛（演进赛）参赛作品 —— 决赛第 15 名**

面向 ARC-Bench 平台的**层级式编排 harness**：把需求树当作精度层级 —— 每一层都是一个完整 app，只是越来越细 —— 驱动随包交付的单文件内核，自动完成「蓝图（L0）→ 骨架（L1）→ 逐层展开契约 → 叶子实现 → 聚合验收」的全流程。

编排只有三条不变式（设计见 [ORCHESTRATION.md](ORCHESTRATION.md)）：

> ① **实现从大到小** —— 正被实现的节点，全部祖先契约都已存在。
> ② **验收从小到大** —— 节点 spec 通过 ⟹ 整棵子树都通过；父节点等子节点全部完成才验收。
> ③ **单调性** —— 通过的只能增加。破坏基线就立刻回滚，并告诉模型它破坏了谁。

harness 只做两件事：**给足信息、验全局**。没有 codegen 模式，每一轮都通过工具作用在真实 app 上。

## 与决赛提交包的对应关系

| | |
|---|---|
| 决赛提交包 | `arc-final-ff872e72.zip` |
| SHA-256 | `FF872E72133BBD7E01368043F1AAF3914A5AEFE4DFE4E889DBB385D8D0851608` |
| 关系 | 提交包内 29 个文件与 [`arc/`](arc/) 目录**逐字节一致**（0 差异）；提交包由 `sh arc/pack.sh` 打包得到 |

## 目录

| 路径 | 内容 |
|---|---|
| [`arc/`](arc/) | 全部源码与工程文档（完整说明见 [arc/README.md](arc/README.md)）。编排器 `orchestrator.py` / `layers.py` / `contract.py`；平台入口 `main.py`；驱动 `tool_driver.py`（工具模式）与 `ccb_driver.py`（内核模式）；本地验收 `acceptance.py`；测试 `tests/`；打包 `pack.sh`；改动日志 [arc/CHANGELOG.md](arc/CHANGELOG.md)（每条改动的改前/改后数据） |
| [`arc/vendor/kernel-dist/`](arc/vendor/kernel-dist/) | 随提交包一起交付的运行时：单文件内核 `serve.mjs`（构建产物）与三个平台的 `ripgrep` 二进制 |
| [ORCHESTRATION.md](ORCHESTRATION.md) | 层级编排设计文档（三个不变式的来源） |
| [MIGRATION.md](MIGRATION.md) | 从旧 DFS/codegen 架构到层级编排的拆除与落地清单 |
| [run-local.py](run-local.py) | 本机做题入口（跑完用平台原版 Playwright 测试打分） |

## 快速开始

```sh
pip install -r arc/requirements.txt        # pyyaml、arcbench-runtime
export ARCBENCH_API_KEY=ak_...             # arc-bench.com 个人页的 API key（凭据绝不入库）

# 本机做题（tasks/ 与 public-tests/ 是平台的公开件，本仓库未携带，用法见 arc/README.md）
python3 run-local.py --task arc/tasks/<题目> --tests arc/public-tests/<题目> --out arc/arc-output/try1

# 编排器纯函数单元测试
python3 -m pytest arc/tests -q

# 打包（重新产生提交包）
sh arc/pack.sh
```

## 说明

- 平台的设计是「判分机不自带内核」：**内核随提交包一起交付**。`arc/vendor/kernel-dist/serve.mjs` 即该内核的单文件构建产物（由 [CCB / claude-code-best](https://github.com/claude-code-best/claude-code) 上游构建，本项目的工作全部在编排层）。
- `arc/vendor` 中三份 `ripgrep` 二进制取自 [BurntSushi/ripgrep](https://github.com/BurntSushi/ripgrep) 15.2.0（Linux 为 musl 静态版），供内核的 Glob/Grep 工具使用。
- 本仓库不含任何 API key 或凭据；密钥在运行时经环境变量或外部凭据文件注入。
