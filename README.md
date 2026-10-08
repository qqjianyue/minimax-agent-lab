# minimax-agent

以 MiniMax 为大模型后端、带分层 Guardrail 与全链路可观测性的本地 Agent。

> 项目目标与面试叙事见《本地 Agent 技术架构方案》与《面试准备任务计划》；
> 组件拆分、测试分层与交付流程见《开发方案：组件拆分与 DevOps 交付流程》。

---

## 仓库结构

单仓多包（决策 Q1）。所有包共享同一版本号（决策 Q7：应用本体统一版本管理），
因为部署与回退都是**整体动作**，不做组件级独立发布。

```
minimax-agent-lab/
├─ src/
│  ├─ agent_core/        C1  基础层：配置 / 端口协议 / 错误 / 重试 / 版本
│  ├─ guard_contract/    C2  契约层：DetectorResult / 枚举 / policy schema / 检测器端口
│  ├─ policy_engine/     C3  策略求值：Decision / PolicyEngine / GuardPipeline / 动作执行 / 脱敏端口
│  ├─ detector_rules/    C4  L1 规则检测器 + 脱敏器
│  ├─ llm_minimax/       MiniMax 客户端（OpenAI 兼容，传输层可注入）
│  └─ agent_service/     C10 FastAPI 服务：/healthz /chat /guard/inspect
├─ tests/
│  ├─ fakes.py                 确定性替身（FakeLLM / StubDetector / FrozenClock ...）
│  ├─ unit/                    L0 纯函数单元测试
│  ├─ integration/             L1 集成测试（Fake 依赖，仍完全离线）
│  ├─ smoke/                   L2 冒烟（target：对已部署服务）
│  └─ functional/              L3 功能（target：12 个业务场景）
├─ infra/                      基础设施（Docker Compose，独立版本，见 infra/README.md）
├─ deploy/                     部署脚本 + systemd unit 模板 + 配置模板
├─ scripts/tasks.py            跨平台任务入口
└─ .github/workflows/ci.yml    CI
```

依赖方向严格单向，无循环：

```
agent_core ← guard_contract ← policy_engine ← agent_service
     ↑                              ↑              ↑
     ├──────── detector_rules ──────┘              │
     └──────── llm_minimax ────────────────────────┘
```

---

## 快速开始

```bash
# 首次
uv sync --all-groups

# 本地闸门：lint + L0 + L1，全离线，不消耗 API 额度
uv run python scripts/tasks.py check
```

Windows 本地没有 `make`，统一用 `scripts/tasks.py`；Linux 侧 `make check` 是同一入口的薄封装。

### 任务清单

| 命令 | 作用 | 需要网络 | 闸门 |
|---|---|---|---|
| `tasks.py setup` | 安装依赖 | ✅ | — |
| `tasks.py lint` | ruff 静态检查 | ❌ | 提交前 |
| `tasks.py test-unit` | L0 单元测试 | ❌ | 阻塞提交、阻塞部署 |
| `tasks.py test-int` | L1 集成测试 | ❌ | 阻塞部署 |
| `tasks.py test-offline` | L0 + L1 | ❌ | 本地默认 |
| `tasks.py test-target` | target 标记用例 | ✅ | 仅目标机 |
| `tasks.py check` | lint + L0 + L1 + 覆盖率门槛 | ❌ | **部署前必过** |

覆盖率门槛 `fail_under = 89.9` 定义在 `pyproject.toml`，是 **L0+L1 合并口径**的下限。
单层命令**不强制**它 —— L1 单跑只有 68%（大量代码只被单元测试触达），
拿单层数字当闸门只会制造无意义的红灯。真正执行门槛的是 `check` 与 `test-offline`。

当前水位 **95%**，余量约 5 个百分点。

---

## 测试分层

| 层 | 位置 | 真实模型 | 失败后果 |
|---|---|---|---|
| L0 单元 | 本地 + 目标机 | ❌ | 阻塞部署 |
| L1 集成 | 本地 + 目标机 | ❌ | 阻塞部署 |
| L2 冒烟 | 目标机 | ✅ | **自动回退** |
| L3 功能 | 目标机 | ✅ | **自动回退** |
| L4 评估回归 | 目标机 | ✅ | 产出报告 |

L0/L1 的离线不是靠约定，而是靠 `conftest.py` 默认禁用 socket：任何测试
若意外发起真实网络请求会**立即失败**，而不是让结果依赖外网状态。
需要联网时显式加 `--allow-network`。

无法在本地运行的用例（嵌入模型下载、spacy 大模型、真实 MiniMax 调用、
Phoenix 连通性等）用 `@pytest.mark.target` 显式标记 —— **跳过是可见的，不静默**。

---

## 配置与密钥

密钥**不进仓库、不进环境变量明文**。配置分五层，从高到低：

```
环境变量  >  私密 YAML (mask)  >  项目 YAML  >  .env  >  模型默认值
```

### 两份 YAML

| 层 | 文件 | 位置 | 谁指定 | 随版本走？ |
|---|---|---|---|---|
| 私密 | `mask-config.yaml` | `MINIMAX_AGENT_MASK_CONFIG_FILE` | `deploy/config.yaml` → `sync-mask.sh` 下发 | ❌ 独立存放 |
| 项目 | `config.yaml` | `MINIMAX_AGENT_PROJECT_CONFIG_FILE`，默认 `./config.yaml` | 方案 A：固定从工作目录读 | ✅ 随 release |

项目配置走**方案 A**是有意的：它是代码的一部分，随 release 走天然保证"跑着的版本
和它的配置是同一份"，不需要额外下发，也不会出现配置滞后于代码。

### 为什么由 app 读 YAML，而不是 systemd

systemd 的 `EnvironmentFile=` **只解析 `KEY=VALUE`**。把
`MINIMAX_AGENT_LLM__API_KEY: sk-xxx` 这种 YAML 喂给它，整行会被**静默跳过**：
服务照常启动、`/healthz` 返回 200、看起来一切正常，但 `/chat` 全部 503。

这是最糟的失败模式 —— 延迟到真正发消息时才暴露，且没有任何报错。
所以 `agent_core/yaml_source.py` 让 app 直接解析，unit 模板只用一个环境变量
声明**文件在哪**。

```bash
# 本机：读本机那份
export MINIMAX_AGENT_MASK_CONFIG_FILE=/workspace/mask-config.yaml

# 目标机：unit 模板只声明"文件在哪"，不写值
Environment=MINIMAX_AGENT_MASK_CONFIG_FILE=%h/mask-config.yaml
```

> 模板里的 `%h` 不是家目录：`install.sh` 渲染时替换为
> `$HOME/.minimax-agent-home` —— 一个指向 `AGENT_HOME`（`/data/workspace/minimax-agent`）
> 的软链接。这样 unit 文件不依赖部署根的具体取值，而密钥文件本身**不会落进
> 只剩 2.9G 空间的 `~` 分区**。

两种键名写法都支持 —— 环境变量式（mask 文件当前的形式）与原生嵌套式：

```yaml
MINIMAX_AGENT_LLM__API_KEY: sk-xxx     # 环境变量式
llm:
  model: MiniMax-M2                    # 原生嵌套式
```

### 三个刻意的行为

1. **显式指定但不存在的配置文件 → 启动即报错**。与"根本没配置"（用默认值）
   严格区分。前者是配置事故，静默忽略会让服务带着缺失的密钥正常启动。
2. **未知配置键按名字 WARNING**。`api_key` 拼成 `apikey` 会被 pydantic 静默忽略，
   服务照常启动但配置没生效。这里把静默失效变成明确告警 —— 报的是**键名**，
   不是值，键名不是机密。
3. **私密 YAML 高于项目 YAML**。项目配置随 release 走、可能滞后；
   密钥文件是独立更新的。

嵌套配置用双下划线：`MINIMAX_AGENT_LLM__MODEL`、`MINIMAX_AGENT_TELEMETRY__PHOENIX_ENDPOINT`。

`LLMSettings.api_key` 用 `SecretStr` 承载，序列化后为 `**********`；
`api_key_hint` 只暴露末四位供日志使用。

> 银行场景默认 `telemetry.capture_prompts = false` —— trace 不落盘 prompt 原文。

### 下发私密配置

密钥在本机，`install.sh` 跑在目标机，两端路径语义不同，所以单独一个脚本：

```bash
./deploy/sync-mask.sh --target user@host --dry-run   # 本机执行，只演练
./deploy/sync-mask.sh --target user@host
```

路径全部来自 `deploy/config.yaml`，代码里不出现机器相关路径：

```yaml
build:
  mask-config: /workspace/mask-config.yaml                      # 本机（POSIX 写法自动翻译成 Windows 路径）
runtime:
  mask-config: /data/workspace/minimax-agent/mask-config.yaml  # 目标机
```

流程：远端建目录 → 备份 `.bak` → scp 到 `.incoming` → **原子改名** → `chmod 600`
→ 远端复核（存在 / 非空 / 权限 / 必需键 / sha256 一致）→ 不过则自动恢复 `.bak`。
先传临时文件再改名，中断时远端要么是旧的、要么是新的，不存在"存在但内容不完整"的窗口。

全程不打印文件内容：校验只做存在性、非空、必需**键名**和 sha256 指纹。

同步默认**不发生** —— 密钥跨版本不变，只有 `--with-mask` / `--sync-mask` 显式触发；
且 mask 刷新失败**不阻断**代码发布。

---

## 目标环境

`qqjianyue@192.168.31.163`（主机 `gmk0`，Ubuntu 24.04.4）

| 项 | 状态 |
|---|---|
| Docker / Compose | 29.1.4 / v5.0.1 ✅ |
| Python | 3.12.3（venv 可用，系统 pip 被 PEP 668 锁定）✅ |
| 依赖安装 | 走 `uv`，免 sudo ✅ |
| 部署目录 | `/data/workspace/minimax-agent`（**不用 `~`**，其所在分区仅剩 2.9G） |
| sudo | 需要密码 —— 部署流程设计为完全不依赖 sudo |
| 时区 | UTC（日志时间戳注意 8 小时差） |

---

## 安全判定链

C3 + C4 已可在**不调用任何模型**的前提下完成判定：

```python
from detector_rules import RulesL1Detector
from guard_contract.default_policy import load_default_policy
from guard_contract.enums import GuardStage
from policy_engine import GuardPipeline, PolicyEngine

pipeline = GuardPipeline(PolicyEngine(load_default_policy()), [RulesL1Detector()])

pipeline.run("Ignore all previous instructions", stage=GuardStage.INPUT).action
# <PolicyAction.BLOCK: 'block'>

pipeline.run("我的身份证是 11010519491231002X", stage=GuardStage.INPUT).action
# <PolicyAction.REDACT: 'redact'>

pipeline.run("drop table customers", stage=GuardStage.TOOL).action
# <PolicyAction.REQUIRE_APPROVAL: 'require_approval'>
```

三条设计约定：

1. **正向命中优先于 fail_mode**。检测器正常给出的信号不该被"另一个检测器挂了"
   抹掉；fail_mode 只在**判不出来**时生效。详见 `policy_engine/engine.py` 顶部说明。
2. **兜底放行用 `default_action`，不要写成 `min_score=0.0` 的规则**。后者会匹配
   任何检测结果，导致 fail_mode 永远走不到。`PolicySet` 会在加载时拒绝这种配置。
3. **同一段文本在不同检测点风险不同**。`drop table customers` 在 INPUT 阶段是
   正常提问，在 TOOL 阶段才是风险 —— 检测点位置决定风险含义。

---

## 凭据纪律

密钥**不进仓库、不进日志、不进响应体**，只从配置读取。

| 场景 | 处理方式 |
|---|---|
| 存储 | `mask-config.yaml`（600，gitignore，本机与目标机各一份），或 `shared/.env`，或 `api_key_file` 指向的密钥文件 |
| 内存 | `SecretStr`，序列化自动变 `**********` |
| 日志 | 启动日志只记录 `llm_configured=true/false`，不记录 key、文件路径或请求体 |
| HTTP 响应 | 契约模型不含任何凭据字段；上游错误一律转成不含内部细节的 502/503 |
| 异常信息 | 错误体截断到 300 字符，且断言不包含 key |
| 配置告警 | 未知键告警只报**键名**，不报值 |
| YAML 解析失败 | 只报文件路径与行号，**不回显出错行内容**（那可能含密钥） |
| 脚本下发 | 全程只做存在性 / 非空 / 键名 / sha256 校验，不 `cat` 不 `echo` 内容 |
| 测试 | 有专项用例断言"任何端点的响应里都不含 `sk-`" |

> 有一个测试用 `@app.on_event` → lifespan 的写法，是因为 `from __future__ import annotations`
> 会把路由注解变成字符串，FastAPI 用 `get_type_hints` 还原时**只查模块全局命名空间**，
> 依赖别名定义在 `create_app` 内部会解析失败、参数被当成 query 参数返回 422。

---

## 部署

```
deploy/
├─ config.yaml                部署路径声明（用户维护，不含凭据）
├─ lib/common.sh              公共库：布局、版本切换、venv 指纹、测试层、状态记录、私密配置校验
├─ lib/deployconfig.py        纯标准库 YAML 解析器（含 Windows 盘符翻译），供 shell 脚本读 config.yaml
├─ preflight.sh               前置检查（只读、免 sudo）
├─ install.sh                 首次安装（--with-mask / --mask-only）
├─ sync-mask.sh               下发私密配置（**在本机执行**）
├─ update.sh                  版本更新 + 冒烟 + 功能测试 + 自动回退（--sync-mask）
├─ rollback.sh                手动回退
├─ status.sh                  状态总览（排查第一入口）
├─ systemd/…service.template  systemd user unit 模板
└─ env.template               共享配置模板（**不含任何密钥行**）
```

部署根 `/data/workspace/minimax-agent` 的布局：

```
├─ releases/<版本>/          每版一份完整代码 + config.yaml
├─ current -> releases/…     原子切换的 symlink
├─ previous -> releases/…    回退目标
├─ shared/                   跨版本共享（.env 等）
├─ state/                    部署状态记录
└─ mask-config.yaml          私密配置（600，独立于 release）
```

### `update.sh` 的流程与关键设计

```
0. 记录旧版本（回退目标）
1. PREFLIGHT     磁盘 / 端口 / 凭据 / 网络    失败 → 退出，current 未动
2. STAGE         暂存 releases/<version>/
3. VENV          依赖指纹比对，未变则复用      （几分钟 → 几十秒）
4. PRE-TESTS     目标机上跑 L0 + L1            失败 → 退出，零影响
5. SWITCH        原子切 symlink + 重启服务
6. L2 冒烟       版本号比对 + 存活 + 最薄端到端  失败 → 自动回退
7. L3 功能       12 个业务场景                  失败 → 自动回退
8. 成功          previous -> 旧版本，清理旧 release
```

两个刻意的安排：

- **L0/L1 放在切换之前**。那时失败可以直接退出，服务仍指着上一个稳定版本，
  不需要任何回退动作。把能离线验证的东西放到切换之后，等于主动制造需要
  回退的场景。
- **冒烟必须比对版本号**。`/healthz` 返回的版本与本次发布不符即判失败 ——
  否则 symlink 切换失败时服务仍在跑旧版本，冒烟全绿、部署被记为成功，
  回退机制就永远不会触发。

回退耗时**实测 2 秒**（含 SSH 往返）：切 symlink + 重启 + 健康探测。
失败的 release 目录会保留供离线排查，只是不再被指向。

> 缺 API Key 时 L2/L3 **不会被静默跳过**：脚本会明确警告，并把部署状态
> 记为 `unverified` 而不是 `success`。一次"全绿"但实际没验证过功能的部署，
> 比一次失败更危险。

### L2/L3 的重试：为什么不是"一失败就回退"

L2/L3 打的是**真实模型**，而 `temperature=1.0` 让输出天然不确定。实测出现过一次：
FT-01（"用两句话介绍你们的定期存款产品"，最基础的正常查询）在某次发布里被判成
`escalate`，脚本据此把一个**完全健康的版本自动回退了**；但同一句话随后连问三次
全是 `allow`。

而且**事后查不出来** —— `capture_prompts=false`（银行场景的隐私取舍）意味着失败
的模型输出没有留存，trace 里也没有。不可复现的失败无法归因。

所以在线层失败会**先重试一次**：重试通过就只告警（明确记为"偶发波动"）不回退，
再失败才判定为真回归。权衡理由是误回退的代价明显大于多跑一次测试 —— 好版本被
撤下、排查方向被带偏。偶发不会被藏起来，它会留在部署日志里。

### 部署链路的实测结论

六个脚本全部在目标机真实执行过（非纸面设计）：

| 路径 | 结论 |
|---|---|
| `sync-mask.sh` | 原子传输 + 远端复核（存在/非空/权限/必需键/sha256），失败自动恢复 `.bak` |
| `preflight.sh` | 8 个 section，0 失败；端口被**本服务**占用时识别为正常 |
| `install.sh` | 自举 uv → 建目录 → 暂存 → venv → systemd → 切换前闸门 → 启动 → 在线测试 |
| `update.sh` | 7 步全绿；L2 14/14、L3 零失败；依赖未变时 venv 增量同步 **0.35ms** |
| 自动回退 | L3 失败时正确回到 `OLD_RELEASE`（不是 `previous` 链接）并二次健康检查 |
| `rollback.sh` | **2 秒**回退，且 `current` / 实际运行版本 / `/healthz` 三者一致 |

---

## 当前进度

| 批次 | 内容 | 状态 |
|---|---|---|
| B1 | 脚手架 + C1 `agent_core` + C2 `guard_contract` | ✅ |
| B2 | C3 `policy_engine` + C4 `detector_rules` | ✅ |
| B3 | C10 `agent_service` + `llm_minimax` + 部署脚本 | ✅ **目标机已实跑验证** |
| 部署增强 | `mask-config.yaml` / `config.yaml` 下发 + YAML 配置源 | ✅ |
| B4 | C8 `audit_ledger` + C9 `telemetry` | 待开始 |
| B5 | C5 `detector_ml` + C6 `agent_tools` | 待开始 |
| B6 | C7 `orchestrator` | 待开始 |
| B7 | C11 `eval_harness` | 待开始 |
| B8 | RAG 检索组件（间接注入） | 待开始（决策 Q4：进 v1，优先级最低） |

测试规模：L0 **437**（+4 平台/依赖相关而跳过）+ L1 **88** = **525**（本地离线，全绿）；
L2 冒烟 14 + L3 功能 28 = **42**（目标机执行，**已全绿**：L3 中 3 项因 B5/B8 组件未实现而显式 skip）。
L0+L1 合并覆盖率 **95%**。

> **覆盖率口径**：`check` 依次跑 L0 和 L1，但**全程只打印一份报告**，且明确标注是合并口径。
> 各层写入自己的数据文件（`.coverage.l0` / `.coverage.l1`）后再 `coverage combine` ——
> 因为 pytest-cov 默认每次运行都会擦除上一次的 `.coverage`，分层直接打印会互相覆盖，
> 最后看到的那份其实只有 L1。单层命令（`test-unit` / `test-int`）则明确标注"仅该层"。
