# 指标定义文档（阶段二 · 可观测性）

本文件是 **span 字段 → 指标 → 口径** 的唯一权威来源。

它存在的理由很直接：指标口径写在代码注释里，会随重构漂移；写在聊天记录里，
三个月后没人找得到。面试官问"你 p95 延迟是怎么算的"时，能当场打开这个文件
指出字段来源——这比任何口头描述都有说服力。

> **文档与实现不一致时，以实现为准，并立刻修本文档。**
> 属性名的唯一来源是 `src/telemetry/tracing.py`（`Instrumentation` 门面）。
> 任何新增埋点都必须同时更新本文件，否则视为未完成。

---

## 0. 采集链路

```
业务代码
  └─ Instrumentation.guard / .llm_call / .tool_call / .trace
       └─ TelemetryPort（OtelTelemetry / NoOpTelemetry）
            └─ OTel SDK BatchSpanProcessor（后台线程批量发送）
                 └─ OTLP/HTTP → {telemetry.phoenix_endpoint}/v1/traces
                      └─ Phoenix（自托管，D4 部署）
```

| 环节 | 配置项 | 目标机取值 |
|---|---|---|
| 开关 | `MINIMAX_AGENT_TELEMETRY__ENABLED` | `true` |
| 收集端 | `MINIMAX_AGENT_TELEMETRY__PHOENIX_ENDPOINT` | `http://127.0.0.1:6006` |
| 原文开关 | `MINIMAX_AGENT_TELEMETRY__CAPTURE_PROMPTS` | `false`（银行场景默认） |
| 服务名 | `MINIMAX_AGENT_TELEMETRY__SERVICE_NAME` | `minimax-agent`（OTel resource） |

**遥测在调用侧闭环。** MiniMax 是外部 SaaS，我方无法在其服务进程内插桩；
所有模型侧指标都由本进程包装 HTTP client 采集。这是跨网络边界调用的标准做法。

**关闭路径不是可选的礼貌方法。** `BatchSpanProcessor` 把 span 攒在内存队列里
异步发送，不显式 `shutdown()` 的话，进程退出时队列里的最后几条（往往正是故障
现场那几条）会随进程一起消失。`TelemetryPort.shutdown()` 由应用 lifespan 的
`finally` 调用。

---

## 1. Span 树与字段

一次 `/chat` 的完整形状（工具循环会产生多个 `llm_call` 与 `tool_call`）：

```
agent.request                 ← 一次用户请求的根
├── input_guard               ← INPUT 环绕拦截
├── llm_call                  ← 规划调用
├── tool_guard                ← TOOL 阶段策略判定（要不要执行）
├── tool_call                 ← 工具实际执行（执行成没成）
├── llm_call                  ← 带工具结果的下一轮
└── output_guard              ← OUTPUT 环绕拦截（返回用户前）
```

> `tool_guard` 与 `tool_call` 必须是**两个** span。只有前者时，看到一条
> `tool_guard` 记录无法区分"被策略拦下"和"执行了但失败"，工具失败率也无从算起。

### 1.1 `agent.request`

| 字段 | 类型 | 口径 |
|---|---|---|
| `request.id` | str | 贯穿一次请求的关联键，与审计账本的 `request_id` **同值** |
| `chat.has_tools` | bool | 本次编排是否装配了工具 |

### 1.2 检测点 span：`input_guard` / `output_guard` / `tool_guard` / `guard_inspect`

| 字段 | 类型 | 口径 |
|---|---|---|
| `guard.stage` | str | `input` / `output` / `retrieval` / `tool` |
| `guard.action` | str | 策略动作：`allow` / `block` / `redact` / `rewrite` / `escalate` / `require_approval` |
| `guard.rule` | str | 命中的规则名；未命中任何规则时为 `default_action` |
| `guard.matched` | bool | 是否命中规则（`default_action` 也算命中，因此**判断拦截要看 `guard.action`**） |
| `guard.policy_version` | str | 求值时的策略集版本 |
| `guard.latency_ms` | float | **检测本身**的耗时（不含 span 建立开销，3 位小数） |
| `guard.detectors` | str | **实际运行过**的检测器，逗号分隔 |
| `guard.hit_labels` | str | **报出风险**的标签，逗号分隔；无则 `<none>` |
| `guard.detector_result_count` | int | 报出风险的检测结果条数 |
| `guard.fail_mode` | str | 仅降级路径出现：`open` / `closed` / `degraded` |
| `guard.failed_detectors` | str | 仅失败路径出现，未能给出结论的检测器 |

> **`guard.detectors` 与 `guard.hit_labels` 不是一回事。** 前者是"跑了哪些"，
> 后者是"谁报了警"。混用会让"检测器全挂了"被误读成"什么都没跑"——
> 所以检测器失败率必须看 `guard.failed_detectors`。

### 1.3 `llm_call`

属性名对齐 **OpenInference 语义约定**，Phoenix 据此识别 LLM span 并计算成本。

| 字段 | 类型 | 口径 |
|---|---|---|
| `openinference.span.kind` | str | 恒为 `LLM`，Phoenix 识别 LLM span 的必要条件 |
| `llm.model` / `llm.model_name` | str | 模型名。`llm.model_name` 用于匹配 Phoenix 内置定价目录 |
| `llm.token_usage_present` | bool | 本次响应是否带 usage。`false` 时其余 token 字段缺失 |
| `llm.token_count.prompt` | int | 输入 token（含缓存命中部分） |
| `llm.token_count.completion` | int | 输出 token |
| `llm.token_count.total` | int | 上两者之和 |
| `llm.token_count.prompt_details.cache_read` | int | **命中 prompt 缓存**的输入 token |
| `llm.token_price.input` | float | 每百万输入 token 单价（元） |
| `llm.token_price.output` | float | 每百万输出 token 单价（元） |
| `llm.token_price.cache_read` | float | 每百万缓存命中 token 单价（元） |
| `llm.cost.total` | float | 本次调用总成本（**人民币**，见下方公式） |
| `llm.cost.prompt` | float | 未命中缓存部分的成本 |
| `llm.cost.prompt_details.cache_read` | float | 缓存命中部分的成本 |
| `llm.cost.completion` | float | 输出成本 |

**成本公式**（`telemetry.tracing.estimate_cost`）：

```
cost = (prompt − cache_read) × 输入单价
     + cache_read     × 缓存命中单价
     + completion     × 输出单价
```

单价为 0 时 `llm.cost.total = 0.0`，语义是**"未配置单价"而不是"这次调用免费"**。
两者在排障时含义完全不同，所以宁可返回 0 也不编一个看起来合理的数字。

> **两套成本，别混用。**
> `llm.cost.*` 是**本进程按人民币单价自算**的，用于 UI 展示与自定义公式。
> Phoenix 的 `span_costs` 表由后端用**内置模型定价目录（美元价）**从
> `token_count` 另算。两套口径的绝对值不同，本文档与本项目的成本口径一律指
> `llm.cost.total`。

### 1.4 `tool_call`

| 字段 | 类型 | 口径 |
|---|---|---|
| `tool.name` | str | 工具名 |
| `tool.arguments` | str | JSON 序列化的入参。**受 `capture_prompts` 管辖**，默认只留键名 |
| `tool.status` | str | `ok` / `not_found` / `invalid_arguments` / `permission_denied` / `requires_approval` / `handler_error` |
| `tool.duration_ms` | float | 执行耗时 |

> `tool.status` **失败时也必须写**。没有 status 的 span 会让失败率被系统性低估。

### 1.5 脱敏规则

属性出口统一拦截（`telemetry.redaction`），**不依赖调用方自觉**：

| 类别 | 键 | `capture_prompts=false`（默认） |
|---|---|---|
| 凭据 | `api_key` / `authorization` / `password` / `secret` / `token` / `llm.api_key` | **直接丢弃**，连键名都不留 |
| 原文 | `prompt` / `input.text` / `output.text` / `messages` / `system_prompt` / `user_message` / `retrieved_documents` / `tool.arguments` / `tool.output` | 保留键名，内容替换为占位符 |
| PII | 任意字符串值 | 经 `RegexRedactor` 脱敏（身份证 / 手机号 / 银行卡；身份证过校验位、银行卡过 Luhn、手机号过位数与号段形态判定，三者都容忍 `-` / 空格 / `_` 分段写法） |

规则**递归**到嵌套的 dict / list，顶层与嵌套共用同一套键名规则。

> 银行场景默认 `capture_prompts=false`：trace 是**可被调阅**的观测库，
> 宁可少一点上下文，也不让原文与 PII 进去。这与"日志脱敏"不是同一件事——
> 日志的输出格式我们自己控制，而 trace 的**属性名由埋点代码决定**，
> 只要某处顺手写了 `prompt` 属性，明文就已经静默出去了。

---

## 2. 指标定义

分母口径统一为 **trace 根（`agent.request`）的数量**，而不是 span 总数——
一次请求会产生 6 个以上 span，用 span 数当分母会把"请求成功率"算成另一个东西。

### 2.1 质量类

| # | 指标 | 定义 | 数据来源 |
|---|---|---|---|
| Q1 | **请求成功率** | `agent.request` 中最终返回 HTTP 200 的比例 | trace 是否存在 + `/chat` 状态码 |
| Q2 | **工具调用成功率** | `tool_call` 中 `tool.status == "ok"` 的比例 | `tool_call` span 的 `tool.status` |
| Q3 | **工具被策略拦下率** | `tool_guard` 中 `guard.action != "allow"` 的比例 | `tool_guard` span 的 `guard.action` |
| Q4 | **响应相关性** | 启发式：返回文本非空 且 `usage.total_tokens > 0` 的比例 | `llm_call.token_count.total` + 响应体 |

> Q4 只用"非空 + 有 token"这种弱信号。真正的相关性需要 `eval_harness`（C11，
> B7 已交付）的跑分，属于阶段三。这里**不假装有**。

### 2.2 性能类

| # | 指标 | 定义 | 数据来源 |
|---|---|---|---|
| P1 | **端到端延迟** | `agent.request` 的 `endTime − startTime` | span 原生时间戳 |
| P2 | **p50 / p95 / p99 延迟** | P1 的分位数 | 同上 |
| P3 | **模型调用延迟** | `llm_call` 的 `endTime − startTime` | 同上 |
| P4 | **工具执行延迟** | `tool_call.duration_ms` | `tool.duration_ms` |
| P5 | **检测耗时分解** | 各 guard span 的 `guard.latency_ms` | `guard.latency_ms` |

> P1 是**用户感知**延迟；P3 + P5 之和通常小于 P1，差额是编排、序列化与
> 网络开销。这个差额本身就是有用的信号——它大，说明时间花在埋点之外的地方。

### 2.3 成本类

| # | 指标 | 定义 | 数据来源 |
|---|---|---|---|
| C1 | **token 用量** | `Σ llm.token_count.total`，按 prompt / completion / cache_read 拆分 | `llm.token_count.*` |
| C2 | **缓存命中率** | `cache_read / prompt` | `llm.token_count.prompt_details.cache_read` |
| C3 | **单次调用成本** | `Σ llm.cost.total`（**人民币**） | `llm.cost.total` |
| C4 | **API 调用次数** | `llm_call` span 数（= 模型被调了几次，与用户请求次数不同） | span 计数 |

> C2 是这套里最值得盯的指标：缓存命中与否对成本的影响是**数量级**的，
> 而 `llm.token_price.cache_read` 与 `llm.token_price.input` 的比值直接
> 告诉你调优还有多大空间。

### 2.4 安全类

| # | 指标 | 定义 | 数据来源 |
|---|---|---|---|
| S1 | **检测触发次数** | `guard.matched == true` 的 guard span 数 | `guard.matched` |
| S2 | **拦截率** | `guard.action ∈ {block, require_approval}` 的比例 | `guard.action` |
| S3 | **脱敏率** | `guard.action == "redact"` 的比例 | `guard.action` |
| S4 | **检测器失败数** | 出现 `guard.failed_detectors` 的 span 数 | `guard.failed_detectors` |
| S5 | **降级路径触发数** | 出现 `guard.fail_mode` 的 span 数 | `guard.fail_mode` |

> S4 / S5 是**降级机制是否在工作**的唯一信号。检测器挂了而这两个数一直是 0，
> 说明不是没挂，是根本没跑到——那本身就是缺陷。

---

## 3. 如何查

### 3.1 命令行（推荐，可脚本化）

```bash
# 汇总视图：一次给出 Q/P/C/S 全部指标
python scripts/phoenix_metrics.py --endpoint http://127.0.0.1:6006 --last 100

# 只看安全类
python scripts/phoenix_metrics.py --endpoint http://127.0.0.1:6006 --group safety

# 回答验收标准里那句话："过去 10 次运行平均延迟多少？工具失败率多少？"
python scripts/phoenix_metrics.py --endpoint http://127.0.0.1:6006 --last 10
```

脚本直接查 Phoenix GraphQL，不依赖 UI 版本、不需要手动加过滤器，
因此**可以在 CI 里跑**，也可以在面试现场直接演示。

### 3.2 Phoenix UI

| 想看什么 | 怎么做 |
|---|---|
| 某一次请求的完整链路 | Projects → default → Traces → 展开一棵 trace 树 |
| 慢请求 | Traces 按 Latency 排序，取尾部若干条点开看阶段分解 |
| 成本 | Spans 过滤 `openinference.span.kind == "LLM"`，按 cost 排序 |
| 工具失败 | Spans 过滤 `name == "tool_call"`，看 `tool.status` 分布 |

---

## 4. 已知盲区

**这些指标拿不到，且是拿不到而不是没做。** 面试时主动说出来，比被问到再说更有分量。

| 盲区 | 原因 |
|---|---|
| GPU 利用率、显存占用 | MiniMax 是外部 SaaS，我方无其进程内观测权 |
| KV cache 命中率 | 同上，服务端内部指标不暴露 |
| 模型服务侧日志 / 错误码分布 | 同上；我方只能看到 HTTP 状态码与响应体 |
| 模型内部推理步数 / 思考 token | 协议不返回 |
| Phoenix 内置 `span_costs` 的绝对值 | 用的是 Phoenix 内置美元定价目录，与本项目人民币口径不一致；本项目口径一律以 `llm.cost.total` 为准 |

### 另外两个当前的口径缺陷

1. **`llm.token_usage_present == false` 时成本按 0 记。** 极少数不带 usage 的
   响应会拉低 C1/C3。这是"看不见"而不是"免费"，分析时需先排除这些样本。
2. **`guard_inspect` 与 `/chat` 的 span 混在同一 project 里。** 统计 Q1 时
   应按 `chat.has_tools` 或端点 metadata 过滤，否则独立探测接口会稀释请求成功率。

---

## 5. 埋点缺失会导致什么

指标依赖 span，span 依赖埋点。以下是**已确认修过**的坑，留档以防复发：

| 坑 | 后果 |
|---|---|
| 属性名没对齐 OpenInference（用了 `llm.prompt_tokens`） | trace 正常落库，但 Phoenix **关联成本为 0** —— 数据在、钱算不出来 |
| OTel span 没挂进 contextvar | 父子关系全平，**不报任何错** |
| 工具执行没有独立 span | 无法区分"被拦"与"执行失败"，Q2/Q3 都算不出来 |
| 单测用内存 fake 验证 span 树 | fake 与实现同时错同一个方向时，测试是绿的 |

最后一条尤其值得警惕：**任何"靠状态推导"的语义（span 父子、contextvar），
fake 都只能当假设，不能当证据。**

### 5.1 Phoenix GraphQL 查询本身的两个坑

写这批工具时在 Phoenix 上连踩两次，**两次都不报错**，故单列一节：

| 坑 | 表现 | 正解 |
|---|---|---|
| `spans(first: N)` 不带 `timeRange` | 返回的是项目里**最早**的 N 条，不是最近的。实测项目积累到 ~1300 次运行时，无时间窗查询拿到的是几小时前的数据 | 一律带 `timeRange: {start, end}`，按 `trace { id }` 归属而不是拿时间猜 |
| `first` 超过 1000 | 直接报 `first must be less than or equal to 1000` | 单页封顶 1000，用 `after` 游标翻页；**静默截断比报错更糟**——汇总会少算，而输出看着仍然完整 |

另外：`Span` 上**没有** `traceId` 字段，归属要用 `trace { id }`；
`attributes` 返回的是 JSON **字符串**，需要二次解析；
`sort` 参数试过不生效，限定窗口只能靠 `timeRange`。