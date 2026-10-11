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
│  ├─ detector_ml/       C5  L2 PII(Presidio) + L3 嵌入相似度 + L4 LLM-as-Judge
│  ├─ agent_tools/       C6  工具注册表 + Schema 校验 + 权限边界 + 高危确认
│  ├─ orchestrator/      C7  LangGraph 状态图：plan→input_guard→retrieve→tool_loop→output_guard→respond
│  ├─ audit_ledger/      C8  审计账本：JSONL append-only + 落盘前脱敏 + 保留期 + 检索
│  ├─ telemetry/         C9  可观测性：span 埋点门面 + 属性脱敏 + OTel/NoOp 实现
│  ├─ llm_minimax/       MiniMax 客户端（OpenAI 兼容，传输层可注入）
│  ├─ agent_service/     C10 FastAPI 服务：/healthz /chat /guard/inspect
│  └─ eval_harness/      C11 评估回归：数据集加载器 + P/R/F1 报告 + 阈值扫描
├─ tests/
│  ├─ fakes.py                 确定性替身（FakeLLM / StubDetector / FrozenClock ...）
│  ├─ unit/                    L0 纯函数单元测试
│  ├─ integration/             L1 集成测试（Fake 依赖，仍完全离线）
│  ├─ smoke/                   L2 冒烟（target：对已部署服务）
│  └─ functional/              L3 功能（target：12 个业务场景）
├─ infra/                      基础设施（Docker Compose，独立版本，见 infra/README.md）
├─ deploy/                     部署脚本 + systemd unit 模板 + 配置模板
├─ docs/                       文档（metrics.md = 指标口径唯一权威）
├─ scripts/
│  ├─ tasks.py                 跨平台任务入口
│  └─ phoenix_metrics.py       指标汇总视图：直查 Phoenix GraphQL，输出四组指标表
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

`user@192.168.31.163`（主机名 `gmk0`，Ubuntu 24.04.4）

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

## 可观测性：C8 审计账本 + C9 埋点

> **指标口径的唯一权威在 [`docs/metrics.md`](docs/metrics.md)** —— 18 项指标的
> 字段、定义、数据来源、聚合方式，以及**已知盲区**。它与
> `scripts/phoenix_metrics.py` 必须永远一致（脚本是文档的可执行版本）。
>
> ```bash
> # 一条命令回答："过去 10 次运行平均延迟多少？工具失败率多少？"
> python scripts/phoenix_metrics.py --endpoint http://127.0.0.1:6006 --last 10
> ```
>
> 端到端验收由 `tests/smoke/test_l2_trace_acceptance.py` 在**每次部署时自动断言**：
> trace 六层齐全、成本非零、工具 span 在位。

银行场景对这两样东西的要求完全不同，因此刻意做成两个独立组件：

| | C8 `audit_ledger` | C9 `telemetry` |
|---|---|---|
| 回答的问题 | **谁**、什么时候、按哪条策略、依据什么证据判的 | 这次请求**慢在哪**、花了多少钱 |
| 写入语义 | append-only，只追加不修改 | 异步批量导出 |
| 保留期 | 365 天（合规留痕） | 90 天（观测数据） |
| 脱敏 | **落盘前**强制脱敏，没脱敏器就拒绝构造 | 属性出口拦截 |
| 默认 | 开启 | 关闭（收集端 Phoenix 部署完成后按需开启，见下） |

### 审计账本

`AuditRecord.from_decision` **强制要求**传入脱敏器，没有就拒绝构造。
理由是 FT-12 的"只追加不修改"：一旦明文写进 JSONL，就再也删不干净了。
这条与 `to_audit_record` 的"默认不脱敏"是刻意分工 —— 后者服务于临时排查
（不落盘），前者服务于落盘。

`JsonlAuditSink` 压根不提供任何改写入口，append-only 不靠调用方自觉。

**账本根目录**按 `显式参数 > audit.root > cwd` 解析。生产上 systemd 注入
`MINIMAX_AGENT_AUDIT__ROOT=%h/shared` —— 必须落在跨版本共享目录：
若解析成 cwd（= release 目录），版本更新切 symlink 后新版本会在新目录
从零写账本，旧记录"消失"，回退时账本跳变，release 清理时历史直接被删。
审计历史不该是部署的副作用。

账本文件显式设为 `0600`，保留期清理的整文件重写也会**保留原权限** ——
清理是破坏性操作，顺手把权限放宽回 umask 就等于悄悄放宽了访问控制。

### span 树

埋点只走 `Instrumentation` 这一个入口，span 名字与属性口径由它统一维护：

```
agent.request              request.id
├── input_guard            guard.action / guard.rule / guard.latency_ms
├── llm_call               llm.model_name / openinference.span.kind
│                          llm.token_count.prompt / completion / total
│                          llm.token_count.prompt_details.cache_read
│                          llm.cost.total / llm.cost.prompt / llm.cost.completion
│                          llm.cost.prompt_details.cache_read
│                          llm.token_price.input / cache_read / output
├── tool_guard             guard.stage=tool —— 回答"要不要执行"
├── tool_call              tool.name / tool.arguments / tool.status / tool.duration_ms
│                          —— 回答"执行成没成"
└── output_guard           guard.action / guard.matched
```

**`tool_guard` 与 `tool_call` 必须是两个 span。** 只有前者时，看到一条工具相关
记录**分不清是被策略拦下还是执行失败**，工具失败率也无从算起。补上 `tool_call`
之后"trace 包含工具调用（参数/返回值/耗时）"这条验收标准才真正成立 ——
此前 `executor.execute()` 完全没有被埋点。

**检测点 span 内部包住检测本身。** 跑完检测再补开一个 span 记录结果，
在结构上完全说得通、断言也过得去，但 span 里根本没有检测工作：时长恒等于零，
出问题时看不出是检测慢还是别处慢。

**成本口径（FT-11 落点）**：`llm.cost.total` 是业务侧按**人民币优惠单价**
自算的精确口径（输入 4.2 / 输出 16.8 / 缓存命中 0.84 元·百万 token，2026-10
官方五折价，见 `agent_core/config.py`）。缓存命中用量取自 MiniMax 响应的
`usage.prompt_tokens_details.cached_tokens`（实测存在，如 181 输入中 128 命中），
计费公式 `(prompt−cached)×输入价 + cached×缓存价 + completion×输出价`。

注意：Phoenix 的 `span_costs` 表由 daemon 用**内置模型定价目录**（litellm
美元价，`minimax/MiniMax-M3` 已在清单中）从 token_count 另算，**不读**
`llm.cost.total`——两个口径并存：`span_costs` 是 Phoenix 美元价，`llm.cost.*`
是人民币价。`openinference.span.kind=LLM` 与 `llm.model_name` 是
`should_calculate_span_cost` 的必需前置（缺一则 span_costs 恒空，FT-11 曾实测踩到）。

**Phoenix 大屏缓存命中率（SQL，UI 查询）**：

```sql
SELECT name,
       COUNT(*)                                                  AS span_count,
       SUM(json_extract(attributes, '$.llm.token_count.prompt')) AS prompt_tokens,
       SUM(json_extract(attributes, '$.llm.token_count.prompt_details.cache_read')) AS cache_read_tokens,
       ROUND(100.0 * SUM(json_extract(attributes, '$.llm.token_count.prompt_details.cache_read'))
             / NULLIF(SUM(json_extract(attributes, '$.llm.token_count.prompt')), 0), 2)
       AS cache_hit_rate_pct
FROM spans
WHERE name = 'llm_call'
GROUP BY name;
```

两个刻意的设计：

1. **检测发生在 span 内部**，不是跑完再补开一个 span 记录结果。
   反过来写在结构上完全说得通、断言也过得去，但 span 里根本没有检测工作，
   时长恒等于零，出问题时看不出是检测慢还是别处慢。
2. **`/chat` 全程共用一个 `request_id`**。它是 trace 与账本的关联键 ——
   各自生成的话，账本里一次对话会散成两条互不相干的记录，按 id 查只能
   捞到一半，而"输入放行、输出拦截"恰恰是最需要一次查全的场景。

### 两个默认值，以及为什么

| | 默认 | 理由 |
|---|---|---|
| `telemetry.capture_prompts` | `False` | trace 是**可被调阅**的观测库。宁可少一点上下文，也不让 prompt 原文与 PII 进去 |
| `telemetry.enabled` | `False` | 可观测性依赖收集端真实存在。Phoenix 未部署时若默认开启，服务会起一个后台线程不断重连一个没人监听的端点，失败日志能把真正的告警淹掉 —— 观测设施不可用反过来损害了可观测性。**Phoenix 已部署后**（D4，v20.20.0，见 `infra/phoenix/` 与 BOM），在 `shared/.env` 设 `MINIMAX_AGENT_TELEMETRY__ENABLED=true` 打开 |

`telemetry.enabled` 打开后还需要一个**显式的关闭路径**：批量导出是异步的，
不 flush 的话进程退出时内存队列直接丢掉，丢的往往正是故障现场那几条。
所以 `TelemetryPort` 带 `shutdown()`，由应用 lifespan 在 `finally` 里调用
（uvicorn 收到 SIGTERM 后正常退出也走这里）。

### 属性脱敏

脱敏做在**出口**而不是做成调用约定：`set_attribute("prompt", text)` 在业务代码里
照常写，由适配层在写进 span 前统一拦截。属性名由埋点代码决定，只要某处顺手写了
一个 `prompt` 属性，明文就已经静默出去了；靠约定迟早会漏。

`FORBIDDEN_ATTRIBUTES`（凭据类）无论 `capture_prompts` 如何设置都直接丢弃，
连占位符都不给 —— 免得有人去猜是不是真的配了。

过滤**递归**到嵌套的 dict / list，且**顶层与嵌套共用同一套键名规则**。
这不是洁癖：`set_attribute("llm.cfg", {"api_key": "sk-live-..."})` 会把凭据
原样送进观测库，而 redactor 兜不住 —— 它只认 PII 模式（身份证/手机号/银行卡），
对 `sk-` 开头的密钥一无所知。键名才是判断依据，与它出现在第几层无关。

> **这一节的教训**：最早 `OtelSpan.__enter__` 没有把 span 挂进 OTel 的 contextvar，
> 导致**生产环境父子 span 关系是平的**（所有 span 都成了 root），而且**不报任何错**。
> 单测之所以一直是绿的，是因为 `InMemoryTelemetry` fake 用自己的栈维护父子关系 ——
> fake 和代码犯了同一个方向的错，互相印证了错误。
> 后来用真实 OTel SDK + 内存 exporter 补了一条集成测试才暴露出来，
> 并做了变异验证（拿掉 attach → 该用例立刻失败）。

### B4 上目标机时暴露的部署缺陷

补了一条静态不变式测试后才发现：`install_systemd_unit` 原本只定义在 `install.sh` 里，
**`update.sh` 和 `rollback.sh` 都不重新渲染 unit**。

后果正是上面说的那种"以为生效了其实没有"：模板里新增的
`MINIMAX_AGENT_AUDIT__ROOT` 到不了目标机，跑的还是安装时留下的旧 unit ——
服务照常健康、接口照常正常，只是账本仍旧写进 release 目录，每次版本更新消失一次。
**本地跑一万遍也发现不了**，因为本地根本不经过 systemd。

已把该函数提到 `lib/common.sh`，装/更/退三条路径都调用，并加不变式测试
（切换 release 的脚本必须调用它，且必须排在 `switch_release` 之前）。

同一轮部署还由目标机 L2 抓出第二个真缺陷：账本**文件**是 600，但**目录**是 775 ——
同组用户读不了文件内容，却能删除或替换整本账本。已改为创建时显式 `chmod 700`。

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
| B4 | C8 `audit_ledger` + C9 `telemetry` | ✅ **目标机已实跑验证** |
| B5 | C5 `detector_ml` + C6 `agent_tools` | ✅ **目标机 v0.1.6 实跑验证** |
| B6 | C7 `orchestrator` | ✅ **目标机 v0.1.9 实跑验证**；L2 冒烟账本断言放宽（编排下工具循环产生 TOOL 记录，`>=2`）→ v0.1.10 |
| B7 | C11 `eval_harness` | ✅ **目标机 v0.1.11 已部署 + 真实红队跑分**（整体 P/R/F1=0.900，FP=bg-05 / FN=di-04） |
| B8 | RAG 检索组件（间接注入） | 待开始（决策 Q4：进 v1，优先级最低） |

测试规模：L0 **577**（+10：B6 新增 orchestrator 全分支用例；B7 另增 eval 5 组 unit）+ L1 **139**（+5：/chat 编排端到端）= **716**（本地离线，全绿）；
L2 冒烟 **25** + L3 功能 **34**（FT-13/14 随 B6 解锁）= **59**（目标机执行：FT-09 需 B8、FT-11 需 D4，仍显式 skip）；
L4 评估层 **tests/eval**（B7，Fake 依赖离线）：数据集 / 指标 / 阈值校准 / 报告端到端。
L0+L1 合并覆盖率保持 **94%** 以上（门槛 89.9）。

### B7 变更要点（v0.1.11）

- **C11 `eval_harness`（评估回归 + 阈值校准，开发工具非常驻）**：
  - 数据集加载器：`EvalCase`/`EvalDataset`/`load_dataset`，内置红队集
    `data/redteam.json` —— 直接注入 ×4 / 数据泄露 ×3 / 工具滥用 ×3 /
    良性 ×6（含"带『忽略』字眼的正常改口"FP 哨兵）/ 间接注入 ×2
    **disabled**（B8 前不计分，报告单列）—— 测试集覆盖四类攻击 + FP 监控。
  - 指标计算：`ConfusionMatrix` + P/R/F1/Acc（攻击=正类，拦截=预测正类；
    **redact/rewrite/require_approval/escalate 均算拦截**，与策略引擎
    dataflow 语义对齐；分母 0 → 0.0 而非抛异常）。
  - 阈值扫描：`scan_thresholds` / `calibrate_threshold`（无约束最大化 F1；
    `min_recall` 业务约束先过滤再选 —— 漏检不可接受的合规场景）。
  - 评估运行器：`EvalRunner` 双模式 —— **detector 级**（单检测器 score +
    阈值校准）与 **pipeline 级**（GuardPipeline 决策 action → 拦截判定，
    回归跑分）；产出统一 `CaseVerdict`。
  - 报告：`build_report`/`render_report`/`save_report` —— 整体 + 分类指标
    + FP/FN 逐条明细 + disabled 单列 + 阈值扫描表；JSON 落盘供历史对比。
  - CLI：`python -m eval_harness list|metrics|calibrate`（离线；真实跑分
    走 `deploy/eval.sh`，不把服务装配拖进入口）。
- **`deploy/eval.sh`**：目标机真实红队跑分 —— 复刻 systemd 服务环境
  （shared/.env + mask-config + current/src），`build_container()` 组装
  真实 pipeline（short_circuit 分层），跑分报告落
  `shared/data/reports/eval-<version>-<ts>.json`。不随 update.sh 自动执行
  （评估打真实模型有成本，按需运行）。
- **L4 评估层**：`tests/eval/`（marker `eval`）+ `tasks.py test-eval` +
  `make test-eval` —— 离线 Fake 跑分端到端（含"良性误伤监控"断言）。
- **评估方法论口径（面试）**：threshold 校准是**风险决策** —— FP（误杀）
  损失用户体验、FN（漏检）可能造成合规事故，`min_recall` 约束即
  "访问核心数据的 Agent 漏检不可接受"的工程化表达。
- **目标机实跑结果（v0.1.11，`deploy/eval.sh`，报告
  `shared/data/reports/eval-0.1.11-*.json`）**：整体 P=0.900 R=0.900
  F1=0.900 Acc=0.875（TP=9 FP=1 TN=5 FN=1）；data_leak / tool_abuse
  P=R=1.000，direct_injection R=0.750；延迟 mean=1314ms p95=4089ms。
  FP=bg-05（"银行营业时间/网点地址"被 presidio 中文 NER 误判 PII →
  redact，**已知中文 NER 误报方向，B4 已修** —— 见下方 B4 变更要点；
  这条 FP 是 v0.1.11 的历史跑分，重跑后应消失）；FN=di-04（base64 编码注入，
  L1 规则不命中且无解码/LLM-judge 兜底 —— 数据集设计时已标注此绕过面，
  留 B8+ 决策）。

### B6 变更要点（v0.1.7）

- **C7 `orchestrator`（LangGraph StateGraph）**：`/chat` 中间段替换为状态图
  `input_guard → llm_plan（带工具声明）→ tool_loop（TOOL guard + executor +
  结果回填）→ output_guard → 响应`；`retrieve` 节点为 B8 RAG 占位（B6 不接
  检索路径，FT-09 保持 skip）。
- **多轮工具循环**：模型返回 `tool_calls` → 逐个过 TOOL guard（guard 非 ALLOW
  即不执行）→ 执行 → 以 `role=tool` + `tool_call_id` 回填 → 再规划；
  `max_steps`（`settings.app.max_steps`，默认 10）硬兜底，不无限烧额度。
- **HITL 语义**：requires_approval / 被拦工具 → `interrupted=True` +
  `requires_human=True`；完整"暂停-恢复"（LangGraph interrupt + checkpointer
  持久化）留 B7+。
- **LLM 埋点不丢失**：编排内每次模型调用仍走 `llm_call` span（token/成本口径，
  FT-11 的载体）；guard 检测点继续写审计 + guard span。
- **响应契约扩展**：`ChatResponse` 新增 `tools_called`（执行的工具清单）/
  `steps`（规划轮数）/ `interrupted`（HITL 标记）；usage 为多轮累计。
- **L3 解锁**：FT-13（编排响应结构）、FT-14（编排输入拦截：block 后不进模型/工具）。
- **部署排障（v0.1.7 失败，已定位根因）**：工具循环对 MiniMax 返回
  `400 invalid params, tool result's tool id(call_...) not found (2013)` ——
  B6 编排最初只把 `tool_calls` 放状态队列、assistant 消息不携带声明，而
  OpenAI/MiniMax 协议要求**后续 `role=tool` 结果必须能在 assistant 消息的
  `tool_calls` 声明里找到匹配 id**。修复：`LLMMessage` 增加 `tool_calls` 字段、
  `build_payload` 序列化输出、`llm_plan_node` 落盘声明（v0.1.8 待部署验证）。
- **L2 账本断言适配（v0.1.10）**：`test_chat_appends_input_and_output_records`
  原断言"一次对话恰好 2 条审计记录"（input+output）；B6 编排下模型触发工具
  循环时还会写 TOOL 阶段 guard 记录（0.1.9 部署 L2 第 1 次即因此失败，靠
  重试通过）。改为"**至少 2 条且必含 input/output 阶段**"——工具循环是编排
  的合法行为，不该被判成账本异常。

### B5 变更要点（v0.1.6）

- **C5 `detector_ml`**：L2 Presidio PII（`pii.presidio`，evidence 掩码不落明文）、
  L3 嵌入相似度（`injection.embedding`，bge-base-zh-v1.5，D1）、
  L4 LLM-as-Judge（`llm.judge`，judge JSON 契约 + 证据结构化摘要）、
  真实墙钟超时执行（daemon 线程 + 结果队列，B2 承诺落地）、
  分层注册表（后层只在前层未命中时触发，`GuardPipeline.short_circuit`）。
- **C6 `agent_tools`**：工具注册表 / JSON Schema 参数校验（jsonschema）/
  执行安全链（Schema → 高危拦截 → 权限边界 → handler）/ 银行示例工具
  （`get_product_rate` 安全 / `get_account_balance` 需 `account:read` /
  `delete_customer_records` 高危永不自动执行）；`/tools/execute` 端点
  （TOOL 阶段 guard 前置，guard 非放行即不执行）。
- **依赖策略（决策 D7）**：ML 重依赖进 `ml` extras，本地不装（torch/presidio/spacy），
  目标机 `ensure_venv` 带 `--extra ml` 装全量；本地 L0/L1 用 Fake + skipif 测契约。
- **L3 解锁**：FT-07（危险工具 → require_approval）、FT-08（安全工具执行成功）走真实
  `/tools/execute` 路径；FT-11（成本可观测）随 D4 Phoenix 部署解锁 —— 真实 chat 的
  trace 落库 Phoenix（SQLite shared/data/phoenix）且 span_costs 非空。

> L2 里 11 条是 B4 新增的可观测性冒烟（账本位置 / 权限 / request_id 关联 / 落盘脱敏）。

### B4 变更要点（v0.1.15）：Presidio 双语 + 形态过滤

修掉阶段一遗留的**中文误报**问题：`presidio_pii.py` 的 `_load_analyzer(language)`
收了参数从不使用，`AnalyzerEngine()` 恒定加载 `en_core_web_lg` 跑中文。

**先量再设计** —— 动手前在目标机跑对照实验（12 条真实模型输出 + 6 条构造正样本）：

| 配置 | 负样本误报率 | 正样本召回率 | 最长误报片段 |
|---|---|---|---|
| en（修复前） | 41.7% | 33.3% | **128 字**（整段输出被当成一个 LOCATION） |
| zh_core_web_lg 裸跑 | 66.7% | 100% | 短片段 |
| zh + 形态过滤（关卡 0，18 条语料） | 0% | 100% | — |
| **zh + 形态过滤（部署后真实 `/chat`）** | **33.3%** | 100% | 整句 |

两个反直觉结论：**阈值调优是死的**（误报与真命中分数**同为 0.85**，跨过 0.86 误报归零但召回也崩到 16.7%）；**误报率上升不等于变差**（en 的代价是 128 字整段误判，zh 只是短片段）。

解法是**形态过滤**：不判断"这是不是真 PII"，只判断"这**长得像不像**中文人名地名"。
11 处误报全是三类非中文实体——模型输出里混进的英文（`sking`/`needing`/`banks`）、
emoji（🏦/⏱️）、中文分词碎片（`(大额`/`方App`）。

- **双语引擎**：`NlpEngineProvider` 配 en+zh，**只传 `nlp_engine` + `supported_languages`，
  registry 交给 Presidio 自建**（手工拼 registry 必失败：内置识别器覆盖 20 国却没有中国）
- **语言路由**：`detect_language()` 纯函数，汉字 dominance（汉字 ≥2 且 ≥ 拉丁）
- **形态过滤**：`passes_shape_filter()` 纯函数，四条规则，**只作用于 NER 产出**
  （用 `recognition_metadata.recognizer_name` 区分，认不出来一律放行）——
  否则 `邮箱xxx@yy.com` 会被"中文形态"规则一起杀掉
- **FT-01 断言收回 `allow`**（原 `in {allow, redact}` 放宽后已失去检测该回归的能力）
- `infra/spacy` 改多模型资产 + `spacy-pkuseg>=1.0.1`（0.0.33 是 numpy 1.x ABI 会崩）

### ⚠️ 关卡 0 的 0% 被真实流量证伪（部署后才发现）

上表前两行是关卡 0 的结论，第三行是**部署后走真实 `/chat` 复测**的结果：
形态过滤后仍残留 **4/12 = 33.3%** 误报，与修复前的英文基线基本持平。

根因不是过滤器写错了，是**判定的粒度错了** —— 过滤开关绑在
`detect_language()` 判出的**文档级**语言上，而模型输出常常是英文表格骨架里
嵌中文单元格：`latin >= han` 让整段判成 `en`，形态过滤于是按设计整体关闭，
英文模型转头对中文乱标：

| 语料 | 判定语言 | 残留误报片段 |
|---|---|---|
| 外汇兑换 | `en` | `'外币定期、活期等'` |
| 贷款审批 | `en` | `'完成审批\n- 简单小额贷款甚至可以当日放款'` |
| 办理银行卡 | `en` | `'！'` |

同一个 `detect_language()` 在**纯中文**长文本上是正确的（实测 685 字里
han=484 / latin=13，判 zh），计数没写错。错的是**该按片段判语言、
却按文档判**。

### B6 变更要点（v0.1.17）：形态过滤按片段判，并引入姓氏先验

先补两个前置条件，否则后面全是空谈：

- **必须拿到未脱敏的原始输出。** 第一版语料存的是 `/chat` 的响应，
  而误报片段早已被 `[REDACTED:PERSON]` 替换掉了 —— 分析等于分析空气。
  改用 `build_container(detectors=[RulesL1Detector()])` 直采原文，
  编排/工具/系统提示与线上完全一致。
- **语料要落盘。** 存成 `corpus_raw.json` 后反复迭代规则不必再烧模型额度。

在 20 条真实模型输出 + 9 个真实体上量化比较了几种改法，**其中两种被实测否掉**：

| 方案 | 残留误报 | 真实体保留 |
|---|---|---|
| 文档级语言（原实现） | 3 | 9/9 |
| 纯片段级（只看片段含不含汉字） | **10** | 9/9 |
| 上下文窗口内出现业务词即丢弃 | 3 | **8/9** ❌ |
| **片段 OR 文档级 + 姓氏先验 + 空白收尾** | **0** | **9/9** |

1. **"只按片段判语言"是错的** —— 它会在中文文档里把英文碎片放回去
   （`inquiry about` / `typical` / 🏦），残留从 3 涨到 10。正确做法是
   **并集**：文档判中文 **或** 片段含汉字，才启用形态规则。
2. **上下文窗口匹配有害** —— 它把真人名 `李娜` 误杀了（旁边 6 字内有"网点"）。
   **PII 漏报远比误报严重**，所以最后没用它。
3. **姓氏先验才是真解** —— `卡需` / `普卡` 漏网是因为 `term in fragment`
   要求业务词**完全落在片段内**，而 NER 把"银行卡需要"切成多长不受控。
   姓氏**有界**（几百个姓覆盖近乎全部人口）、可枚举、不随业务上新失效：
   实测 58 条业务词里有 29 条在这批语料里根本没出现过。

新增 `looks_like_false_positive()` 作为唯一判定入口，四条规则：纯图标
（与语言无关，排在最前）→ 适用性（文档 zh **或** 片段含汉字）→ 形态
（`passes_shape_filter` 加"不以空白收尾"）→ PERSON 姓氏先验（含复姓）。

`_is_emoji` 的码段也补全了 —— 原来的范围漏掉 `⏱️`（U+23F1 + U+FE0F）
这类高频写法，实测就漏过一次。

### 线上实测（v0.1.18）

用与语料完全相同的 20 条提问打真实 `/chat`：

| 版本 | 正常提问被实际遮字 | 真人名样本 |
|---|---|---|
| v0.1.16 | 58.3% | 3/3 ✅ |
| v0.1.17 | 20.0% | 3/3 ✅ |
| **v0.1.18** | **10.0%** | **3/3 ✅** |

（模型输出逐次不同，绝对值会波动，看量级。）

再采**一批全新语料**（不复用调参那批，避免拿训练集当测试集）用线上代码
复量，残留 4 处，来源很清楚：

| 残留片段 | 文档语言 | 原因 |
|---|---|---|
| `China` ×2 | `en` | 英文模型把国家名判成 LOCATION |
| `📈` | `en` | 图标被判成 LOCATION（已由图标规则修掉） |
| `管资` | `zh` | 形态通过，且是 LOCATION 不适用姓氏先验 |

⚠️ **残留的 3/4 在英文文档里**，属于英文模型在中文业务回答上对英文的
过激标注，与中文形态过滤无关。**没有为它加规则** —— "国家名不算 PII"
会直接误伤真实的地址型 PII（地址本身就含地名）。宁可留缺口，不拿漏报换误报。

**教训**：18 条语料上的 0% 是**对自己选的测试集的拟合** —— 业务词表又是看着
这 18 条的失败样本写的，两者互相加固，强化的是错觉。**度量必须走真实入口，
且必须拿到未经后处理修饰的原始输出**（第一版语料存的是已脱敏的响应，
误报片段早被 `[REDACTED:PERSON]` 替换掉了，分析等于分析空气）。

### B5 变更要点（v0.1.16）：脱敏跟着决策走

**这个漏洞是部署 v0.1.15 后跑真实 `/chat` 才抓到的**，读代码和跑单测都发现不了：

```
/guard/inspect  张伟 → pii.presidio PERSON 0.85 → action=redact
RegexRedactor.changed=False      apply_action(REDACT) 输出与原文相同：True
```

`policy_engine.actions.apply_action` 的 `REDACT` 分支调 `RegexRedactor`，
而它只加载 C4 那 4 条正则（身份证/手机号/银行卡/邮箱）——
**Presidio 的实体从来不在脱敏器的知识范围里**。实测结果是
`action=redact` 而 `redacted=False`、占位符 0 个，原文一字未改地进了模型、
又原样返回给用户。危害方向和误报相反：误报难看，**真阳性是漏的**。

病根是**决策与执行用了两套检测**：决策看完整检测栈（含 Presidio），执行重新
跑一遍只有 C4 正则的脱敏器。"判了要脱敏"和"真的脱敏了"之间没有任何强制关系，
只要新增一个检测器，这个洞就会再开一次。

修法是让脱敏**跟着决策走**：

- 检测器把命中区间写进 `metadata`（`start`/`end` 是**偏移量不是原文**，
  落盘审计不构成泄露）
- `policy_engine.redactor` 新增 `redactions_from_results()`（取区间）与
  `apply_redactions()`（按区间遮）；`placeholder_for()` 上移到这里作单一来源
- `apply_action(..., results=decision.results)` 把决策里的区间一并应用
- `detector_rules.RegexRedactor` 改为复用同一份区间应用逻辑 —— 正则路径与
  决策驱动路径必须在"重叠取最长、从右往左替换"上完全一致
- 畸形区间（缺端点、倒置、越界、字符串偏移、`bool`）一律跳过：**宁可少遮也
  不能错位**，用错偏移切片会静默截断用户内容

验证：`张伟` / `李娜` 的 `/chat` 从 `redacted=False` 变为 `redacted=True`。

> ⚠️ **副作用，且必须正视**：修复前那些误报的 `redact` 是空操作，用户无感；
> 现在**真的遮字**了。真实流量复测 12 条正常业务提问，
> **7/12（58.3%）会被遮掉片段**，用户看到自己的产品介绍里出现
> `[REDACTED:PERSON]` / `[REDACTED:LOCATION]`。
> **D1 修好了一个漏洞，同时把 D2 的损害从不可见变成可见。** D2 必须紧接着修。
> 它们**直接读目标机文件系统**而不只看 HTTP 响应 —— 有一整类问题只在目标机上
> 才存在：配置到底有没有真的生效。B4 就踩过一次（见下）。

> **覆盖率口径**：`check` 依次跑 L0 和 L1，但**全程只打印一份报告**，且明确标注是合并口径。
> 各层写入自己的数据文件（`.coverage.l0` / `.coverage.l1`）后再 `coverage combine` ——
> 因为 pytest-cov 默认每次运行都会擦除上一次的 `.coverage`，分层直接打印会互相覆盖，
> 最后看到的那份其实只有 L1。单层命令（`test-unit` / `test-int`）则明确标注"仅该层"。
