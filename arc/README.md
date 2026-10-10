# arc/ — 在这一个仓库里完成 ARC-Bench 的改、测、交

这个目录是 Octos 参加 ARC-Bench 的全部外围：平台适配包、公开验收测试、本地做题和打分脚本。
学员只需要这一个仓库。内核源码在上一级（`crates/`），适配包在这里。

## 四步

```sh
# 0. 准备（一次）
pip install -r arc/requirements.txt          # pyyaml、arcbench-runtime
export ARCBENCH_API_KEY=ak_...               # arc-bench.com 个人页的 API key
export NODE_BIN=/opt/homebrew/opt/node@24/bin # 你的 node 目录（Linux 一般不用设）

# 1. 本机做题（约 5 分钟，不到一分钱）
#    **做题入口在上一层**（`run-local.py`）—— 本目录里没有做题脚本。
python3 run-local.py --task arc/tasks/smoke--counter --tests arc/public-tests/smoke--counter \
    --out arc/arc-output/try1 --env <凭据文件>
#    凭据文件不在本目录里：它**绝不能进 zip**，所以放在工作区外，用 --env 指过去
#    --port 43300 换端口可并行跑多题；--template <已有产物目录> 进入 evolution 模式
#    （先跑 smoke--counter，再以它的产物为模板跑 smoke-evolution--counter）
#    跑大题显式传 --time-budget：默认 5400 秒（90 分钟），不够 keep 那种 32 节点的题

# 2. 用平台原版 Playwright 测试打分（首次会自动装 Playwright）
python3 arc/grade-local.py arc/arc-output/try1 smoke--counter
python3 arc/metrics.py arc/arc-output/try1        # 轮数 / Token / 费用 / 耗时 / 节点状态 / 打分，一行表格
python3 -m pytest arc/tests -q \
    --ignore=arc/tests/test_harness_errors.py --ignore=arc/tests/test_scope.py \
    --ignore=arc/tests/test_truncate.py           # 编排器纯函数的单元测试
# 三个 --ignore 的文件属于「把 octos 内核本地化成纯 Python」那条工作流 —— **已放弃**
# （见下方「已放弃：把内核本地化成 Python」），留着是那条线将来真要捡起来时的对位规范。

# 3. 改一处：orchestrator.py / layers.py / contract.py 的编排与契约，main.py 的
#    提示词 / 环境变量，tool_driver.py 的工具实现，或 crates/ 里的内核
#    改完回到第 1、2 步，改前改后各跑一次，比数字

# 4. 打包上传
sh arc/pack.sh                               # 得到 octos-arc-bundle.zip
# 到 arc-bench.com 对应比赛页 New submission 上传，模型填 deepseek-v4-flash，
# Base URL 填 https://api.arc-bench.com/v1，然后选题、Run
```

> **已放弃：把内核本地化成 Python。** 那条线（`octos/` 包：`core/` 的
> `truncate.py` / `scope.py`、`agent/harness_errors.py` …）的动因是「判分机上没有
> octos 内核二进制」。现在不成立了：层级编排的默认驱动是包里的
> `vendor/kernel-dist/serve.mjs`，内核是**随 zip 一起交上去的构建产物**。
> 一行代码没写过，`tests/` 下那三个对位测试留作规范存档，不进当前的测试套件。

## 改了内核怎么让平台用上

**层级编排这条路压根不起内核二进制**：默认驱动 `ccb_driver` 直接跑包里的
`vendor/kernel-dist/serve.mjs`（单文件内核，构建产物）。所以内核源码
（`crates/octos-agent/` 等）改完之后要**重新构建 serve.mjs 并放进 `vendor/`**，
再 `pack.sh` —— 内核的能力是走这个文件进去的，不是走下载。

**只有这一条引擎路径了。** 旧 Rust octos 内核那条（`OCTOS_ARC_ENGINE=rust` →
`octos arc run --policy arc-policy.toml`）连同它的 `rust_engine.py` / `arc-policy.toml` /
`prompts/*.md` 和 `main.py` 里 `OCTOS_RELEASE_URL` + `find_octos()` 的下载兜底一起删掉了
—— 全仓库没有任何地方设那个开关，它从来不会被执行。

> ⚠️ `octos-arc/arc/prompts/` 是**另一个目录**，Rust 仓库靠
> `include_str!("../../../arc/prompts/…")` 把那些模板编译进二进制。删的是本仓库的副本，
> 那边不受影响，也**不要去动那边**。

## 目录

### 编排：层级式

需求树不是任务清单，是**精度层级**——每一层都是一个完整 app，只是越来越细。
跑在架构里的只有三句话（详见 `../ORCHESTRATION.md`，落地清单见 `../MIGRATION.md`）：

> **① 实现从大到小** —— 正被实现的节点，全部祖先契约都已存在。
> **② 验收从小到大** —— 节点 spec 通过 ⟹ 整棵子树都通过；父节点等子节点全部完成才验收。
> **③ 单调性** —— 通过的只能增加。破坏基线就立刻回滚，并告诉模型它破坏了谁。

流程形状：`L0 蓝图（契约）→ L1 骨架 → 逐层展开契约 → 叶子实现 → 聚合验收`。
**没有 codegen 模式**，每一轮都通过工具作用在真实 app 上；harness 只做两件事——**给足信息、验全局**。

| 文件 | 作用 |
|---|---|
| `main.py` | 平台入口 + 流程外壳：环境、驱动选择、事件上报、`Flow` 上编排要用到的那一面 |
| `orchestrator.py` | **编排本体**：L0/L1/展开/叶子/聚合，三个不变式的执行点 |
| `layers.py` | 需求树 → 层级结构（纯函数，无 I/O、无 LLM）：`layerize` / `forward_deps` / `pending_children` / `aggregate_order` |
| `contract.py` | 契约的读写与校验：只有**跨节点可见**的东西进契约（数据模型、路由命名、共享文件归属、UI 骨架） |
| `requirement_order.py` | 剥到只剩 `node_fingerprint`（evolution 的差异判据）。原先的 `flatten_atomic` / `topo_order` / `ancestors_of` 是 DFS 编排的地基，已删 |
| `acceptance.py` | 本地 Playwright 验收：spec↔节点映射、起服务、跑 spec、四字段失败摘要 |
| `tool_driver.py` | 工具模式驱动（`read_file`/`write_file`/`edit_file`/`glob`/`grep`/`shell`…）——**层级编排唯一走的那条路** |
| `text_repair.py` | 驱动层的三处文本补丁（charset / 扁平化 JS 还原）。**不是内核的一部分**，内核从不这样做 |
| `local_driver.py` | 一次请求的无状态驱动（历史退路，`OCTOS_ARC_DRIVER=local`） |
| `ccb_driver.py` | 默认驱动：起 `vendor/kernel-dist/serve.mjs` 走 JSON-RPC（`OCTOS_ARC_DRIVER=ccb`） |
| `guard.py` | 守护规则：未验证就宣称完成、连续同一错误、改保护路径 |
| `metrics.py` | 从事件流读 Token / 费用 / 耗时 / 节点状态 |
| `CHANGELOG.md` | 每条改动的改前改后数据 |
| `public-tests/<题目>/` | 平台公开的 Playwright 验收测试（会自动喂给模型） |
| `tasks/<题目>/` | 各题需求文件的离线副本 |
| `run-local.py`（在上层目录）/ `grade-local.py` / `pack.sh` | 本机做题、打分、打包 |
| ~~`rust_engine.py` / `arc-policy.toml` / `prompts/`~~ | **已删** —— Rust 内核路径的整套东西 |
| ~~`run-task-local.py`~~ | **已删** —— 同一套的东西：Rust 时代的旧本地脚本，要一个本机不可能有的 `octos` 二进制（`OCTOS_BIN`）。已在上一层被 `run-local.py` 取代 |

已知平台细节：容器里 `/workspace/tests` 有验收测试；订票题的测试默认连 3301 端口而平台起在 3000，`main.py` 会要求后端两个端口都监听；容器到 npmjs 很慢，提示词要求零依赖并走 npmmirror。

## 编排器开关（环境变量）

| 变量 | 默认 | 作用 |
|---|---|---|
| `OCTOS_ARC_DRIVER` | `ccb` | 驱动：`ccb`（内核，默认）/ `tool`（纯 Python 工具档）/ `local`（一次请求，历史退路）。**层级编排是工具模式，`local` 起不来** |
| `OCTOS_TIME_BUDGET` / `OCTOS_NODE_TIME_BUDGET` | max(3600, 1500×节点数) / 1500 s | 整体与单节点（含修复轮）的墙钟预算；单节点预算按剩余时间/剩余节点数自适应 |
| `OCTOS_NODE_TIMEOUT` | 1200 s | 单轮上限。L0 蓝图 / L1 骨架 / 每个叶子的实现与修复都用它 |
| `OCTOS_REPAIR_ROUNDS` / `OCTOS_MIN_REPAIR_SECONDS` | 5 / 300 | 每节点验收修复轮数 K；剩余不足 300 s 不再开修复轮 |
| `OCTOS_IMPLEMENT_FRACTION` | 0.6 | 实现轮最多占节点预算的比例，留时间给修复轮 |
| `OCTOS_SESSION_SCOPE` | turn | 新 session 的粒度：`turn`（每轮新，spec 已内嵌所以修复轮自足）、`node`、`run`（全程一个） |
| `OCTOS_ARC_MAX_TOTAL_TOKENS` / `OCTOS_ARC_MAX_TURNS` | −1（按树推导） | 全程成本闸门：超出后不再开修复轮，剩余节点各给一次实现轮，末轮只跑一次全套 |
| `OCTOS_ARC_INLINE_SPECS` / `OCTOS_ARC_INLINE_SPEC_CHARS` | 1 / 24000 | 把该节点的 spec 原文嵌进提示词 |
| `OCTOS_ARC_INLINE_SOURCE_CHARS` | 90000 | 修复/重写轮引用的源码字符上限；0 = 关 |
| `OCTOS_PERF_CONTRACT` / `OCTOS_GUARD` | 1 / 1 | 0 = 关闭性能规则 / 守护注入 |
| `OCTOS_ARC_ALIAS_SPEC_IDS` | 1 | 0 = 不把节点状态镜像到 spec 侧 id |
| `OCTOS_ARC_INSTALL_PLAYWRIGHT` / `OCTOS_ARC_PLAYWRIGHT_ROOT` | 1 / 自动 | 找不到 Playwright 时是否自行安装 / 指定含 `node_modules/@playwright/test` 的目录 |
| `OCTOS_ARC_FULLY_PARALLEL` | 0 | 1 = 本地验收让同一文件内的测试也并行（比平台更严的压力测试） |
| `OCTOS_ARC_TEST_TIMEOUT_MS` / `OCTOS_ARC_SLOW_MS` | 10000 / 3000 | 本地验收单测试超时；超过 SLOW 阈值即提醒模型 |
| `OCTOS_ARC_DRYRUN` | 0 | 不调模型的空走：每轮返回固定回复，走完整流程做结构自检 |

> `OCTOS_DESIGN_TURN` / `OCTOS_DESIGN_MODE` / `OCTOS_DESIGN_MIN_NODES` / `OCTOS_DESIGN_TIMEOUT`
> / `OCTOS_ARC_REGRESSION_CHECKPOINT` 是**旧 DFS 编排**的开关（设计轮、按节点数定期抽查回归），
> 层级编排里没有对应概念：契约在 L0 一次成型，回归检查是**每次提交**都做的（不变式 ③）。
> 现在没有任何地方读它们（`arc-policy.toml` 已随 Rust 内核路径一起删掉）。

Web 大题（32–138 节点）的建议参数见 `CHANGELOG.md` 末尾「ARC-Bench Web 六题的建议参数」。

### 同题内按步骤选择模型

`OCTOS_ARC_MODEL_ROUTES` 接受有序 JSON 规则。同一题的不同节点、首轮与修复可以使用不同模型；匹配依据只有阶段、完整消息与工具定义的字符数、工具/图片能力，不按题名分支。第一条匹配的规则生效，无匹配则保留原请求模型；不配置时保持原行为。上下文字符数按完整消息与工具定义的紧凑 JSON 计算（Unicode 字符，不是 token）。

规则只有这一套格式，也没有第二种引擎可切：所有请求都过本机转发层（`llm_proxy.py`），实际选中的模型记录在 `.arc/model-routes.jsonl`；分档开启后单模型费用估算为空，费用以 provider 账单为准。

云端提交也可携带同一规则：保存为任意 JSON 文件，然后运行 `sh arc/pack.sh /path/to/routes.json`，包内会增加 `model-routes.json`。打包和运行都会校验规则。环境变量 `OCTOS_ARC_MODEL_ROUTES` 优先于包内配置，显式设置为空可禁用分档；不传配置文件时，打包结果不包含任何默认模型规则。

下面仅是配置示例，不是经过费用/质量验证的默认值。模型 ID 来自 2026-09-15 ARC provider 的 `/v1/models`；该端点没有提供参数量和价格，也没有列出 `qwen3.8-27b`。应以使用时 provider 返回的目录和参数能力为准。

```json
[
  {
    "model": "qwen3.6-flash",
    "phases": ["implement"],
    "max_input_chars": 8000,
    "tools": false,
    "images": false,
    "parameters": {"max_tokens": 4096}
  },
  {
    "model": "qwen3.8-max",
    "phases": ["repair"],
    "tools": true,
    "images": false,
    "parameters": {"max_tokens": 8192}
  }
]
```

- 允许阶段：`implement`、`repair`、`verify`、`design`。修复含重写；阶段由流程设置。小模型失败后，现有验收/修复流程进入 `repair`，使用该阶段的匹配规则。
- `max_input_chars` 是字符预算，不是 token 上下文容量或模型能力的保证；用实际任务对比校准，给输出与协议开销留余量。
- `tools` / `images` 表示能力声明，默认 false；含工具历史也要求工具能力。声明应先验证，路由不会虚构 provider 能力。
- `parameters` 可覆盖 `temperature`、`top_p`、`max_tokens`、`max_completion_tokens`、`thinking`、`reasoning_effort`。切换模型会去掉原请求的供应商推理字段，再应用该规则的参数，避免跨模型照搬。
- 模型偏好由规则顺序明确表达；不根据未验证价格自动排序。不存在的模型或不支持的参数由 provider 报错，不偷偷更换并重复计费。
- 用量 JSONL 记录每次请求选中的 `model`、`phase`、耗时和 provider 返回的 token 用量。模型价格未提供时不编造成本。
- 跨模型分档的实际效果仍待评测。多个模型请求仍遵守同 key 串行的费用计量约束。
