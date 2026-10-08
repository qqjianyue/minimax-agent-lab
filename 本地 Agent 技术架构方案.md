# 本地 Agent 技术架构方案

> **目标**：设计一个可在本地机器轻量运行的 AI Agent，以 MiniMax 为大模型后端，集成全链路指标监控与安全监控防护框架。本方案同时服务于 ABG Bank Guardrail Platform（Job ID 53435）面试准备。
> 
> 

---

## 1\. 整体架构与数据流

### 1\.1 架构概览

> 架构包含两类组件：**顺序数据流**（请求沿管道向下）与**横切面**（可观测性与治理，贯穿每一层）。
>
> 安全检测属于**环绕式拦截（wrapping）**而非下游节点——它在数据流的前后同步执行，检测结论会改变数据流本身。

```
┌──────────────────────────────────────────────────────────────┐
│                          用户交互层                          │
│                   CLI / Web UI / API 入口                    │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                         Agent 编排层                         │
│      Planner  |  Memory  |  Tool Router  |  ReAct Loop       │
│                全链路 span 埋点全部落在本进程                │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│      前置安全检测  Input Guard   ← 环绕拦截（同步执行）      │
│     L1 规则 → L2 分类器 → L3 嵌入检索 → L4 LLM-as-Judge      │
│  Policy Action: allow / redact / block / escalate / rewrite  │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                  检索增强 RAG  +  工具执行                   │
│        文档注入检测 / 权限过滤 / 参数校验 / 人工确认         │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
╔══════════════════════════════════════════════════════════════╗
║         大模型后端  MiniMax（OpenAI-compatible API）         ║
║         ─── 外部资产 · 信任边界之外 · 非我方控制 ───         ║
║     服务端内部指标不可得 → 遥测闭环于调用侧 client span      ║
╚═══════════════════════════════╤══════════════════════════════╝
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  后置安全检测  Output Guard   ← 环绕拦截（终检 + PII 脱敏）  │
│        拦截必须发生在响应返回用户之前，此位置不可后移        │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
            返回用户响应（已按 Policy Action 处理）

════════════════════════════════════════════════════════════════
╔══════════════════════════════════════════════════════════════╗
║    横切面：可观测性与治理 —— 贯穿每一层，非任何一层的下游    ║
║  埋点层   OTel SDK + OpenInference 语义约定（100% 本进程）   ║
║   审计层   Detector Contract → 决策记录 → append-only 写入   ║
║  存储层   Phoenix (SQLite/Postgres) 或 Langfuse + 本地文件   ║
║    落盘前必须脱敏 + 设定保留期 + 访问控制（银行场景强制）    ║
╚══════════════════════════════════════════════════════════════╝
════════════════════════════════════════════════════════════════
```

**归属与可观测性边界**

- **大模型后端属于外部资产**：MiniMax 以 SaaS 形式提供，我方无法在其服务进程内插桩，GPU 利用率、KV cache 命中率、服务侧日志等内部指标不可得。

- **遥测闭环在调用侧完成**：延迟、token usage、成本、错误码、重试次数全部由本进程包装 HTTP client 采集（`llm_call` span）。跨网络边界的调用本就是**调用方埋点**，这是可观测性的标准做法，与后端是否自研无关。

- **业务链路 trace 完全可控**：trace 由 Agent 侧发起，Planner、Guard、工具调用、每一轮 generation 均为本进程 span，无需第三方配合。

- **两类治理动作不可混淆**：**拦截**（block / redact / escalate / rewrite）必须同步发生在数据流上，因此位于管道的前后两侧；**审计**只记录已发生的决策与证据，不改变数据流，因此以横切方式写入日志存储。

- **私有部署的增量价值**：若模型后端私有化部署，可在推理服务侧补充 TTFT / TPOT 分解、KV cache 命中率、调度与 GPU 指标，并保证客户数据不出网；但 Agent 侧仍应保留同一套 OTel 语义约定、共享同一观测后端，避免维护两套 trace 体系。

### 1\.2 数据流与检测点

|阶段|检测点|执行位置|监控埋点|安全动作|
|---|---|---|---|---|
|**输入**|用户 Query 进入|本进程（环绕拦截）|trace\_start, span: input|prompt injection, PII, 敏感词|
|**检索**|RAG 召回文档|本进程（环绕拦截）|span: retrieval|文档注入检测, 权限过滤|
|**规划**|Planner 生成计划|本进程|span: planning|危险意图识别|
|**工具**|Function Calling|本进程（环绕拦截）|span: tool\_call, span: tool\_result|参数校验, 权限边界, 人工确认|
|**生成**|调用 MiniMax 推理|外部边界内不可观测，调用侧闭环|span: generation, span: llm\_call（token\_usage / latency / cost / retry）|请求级策略由模型侧承担，响应内容由 Output Guard 承担|
|**输出**|返回用户之前|本进程（环绕拦截）|span: output, latency|终检过滤, PII 脱敏, system prompt 泄露检测|
|**审计**|全链路决策记录|本进程产生，横切写入|trace\_end, cost|决策留痕（who / when / detector\_version / verdict / evidence / action），append-only|

> **读表提示**：「执行位置」列区分了两类动作——本进程环绕拦截会改变数据流；外部模型服务不可观测，其内部指标不纳入本方案的观测范围，相关遥测全部由调用侧 `llm_call` span 承接。
>
> **日志存储的额外约束**：trace 会落盘 prompt 与 response 原文，落盘前必须脱敏，并设定保留期与访问控制（见 6\.4 已知风险）。

### 1\.3 信任边界

- **用户输入边界**：不可信，需输入检测

- **检索内容边界**：半可信，需注入检测和权限过滤

- **模型输出边界**：不可直接放行，需输出检测

- **工具输出边界**：不可直接信任，需结果校验

- **系统提示边界**：需防止泄露和篡改

---

## 2\. MiniMax 作为大模型后端

### 2\.1 API 现状（已核实）

> **来源**：MiniMax 开放平台官方文档 [https://platform\.minimaxi\.com/docs/guides/text\-generation](https://platform.minimaxi.com/docs/guides/text-generation)（2026\-10 核实）
> 
> 

|项目|详情|
|---|---|
|**OpenAI 兼容端点**|`https://api.minimax.cn/v1/chat/completions`|
|**Anthropic 兼容端点（推荐）**|`https://api.minimax.cn/anthropic/v1/messages`|
|**认证方式**|`Authorization: Bearer <MINIMAX_API_KEY>`|
|**SDK**|可直接复用 OpenAI SDK \(`pip install openai`\) 或 Anthropic SDK|

### 2\.2 可用模型列表

|模型|上下文窗口|特点|推荐场景|
|---|---|---|---|
|**MiniMax\-M3**|1,000,000 tokens|原生多模态、Agent 工作流优化、100\+ TPS|**首选**：复杂 Agent 推理、工具调用、长上下文|
|**MiniMax\-M3\.1\-Flash\-Preview**|1,000,000 tokens|可调思考深度 \(reasoning\_effort\)、多模态|需要深度推理的编码/分析任务|
|**MiniMax\-M2**|204,800 tokens|Agentic capabilities、Advanced reasoning|轻量 Agent、快速响应|
|**MiniMax\-M2\.5**|204,800 tokens|性价比之选、60 TPS|常规对话和简单工具调用|

### 2\.3 Function Calling / Tool Use 支持

- **支持**：`tools` 参数（OpenAI 兼容格式）

- **不支持**：已废弃的 `function_call` 参数（官方明确说明使用 `tools`）

- **多模态输入**：M3 / M3\.1 支持 `image_url` 和 `video_url` 内容部分

- **流式输出**：支持 `stream=true`，可返回 `reasoning_content` 和 `content`

### 2\.4 接入代码示例

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.minimax.cn/v1",
    api_key="<MINIMAX_API_KEY>"
)

response = client.chat.completions.create(
    model="MiniMax-M3",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Search for ABG Bank stock price."}
    ],
    tools=[{
        "type": "function",
        "function": {
            "name": "search_stock",
            "description": "Search stock price",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"}
                },
                "required": ["symbol"]
            }
        }
    }]
)
```

### 2\.5 注意事项

- `temperature` 范围 `[0, 2]`，默认值 `1`

- `n` 参数仅支持值 `1`

- `presence_penalty`、`frequency_penalty`、`logit_bias` 等 OpenAI 参数会被忽略

- M3\.1\-Flash\-Preview 的 thinking 无法关闭（传 `disabled` 返回 400）

- **未验证**：具体定价、Rate Limit、企业级 SLA

---

## 3\. Agent 框架选型

### 3\.1 候选框架对比

|维度|LangGraph|LangChain|OpenAI Agents SDK|轻量自研循环|
|---|---|---|---|---|
|**学习曲线**|中等（需理解图结构）|低（生态成熟）|低（API 简洁）|低（纯 Python）|
|**本地运行**|✅ 纯 Python|✅ 纯 Python|✅ 纯 Python|✅ 纯 Python|
|**依赖重量**|中等（依赖 LangChain）|较重|轻量|极轻|
|**可视化调试**|✅ 内置图可视化|❌ 无|❌ 无|❌ 需自建|
|**工具调用抽象**|✅ 原生支持|✅ 原生支持|✅ 原生支持|需手动封装|
|**状态管理**|✅ 内置 StateGraph|需手动管理|轻量|需手动管理|
|**\-human\-in\-the\-loop**|✅ 内置中断点|需自建|基础支持|需自建|
|**监控集成**|✅ Langfuse/Phoenix 原生|✅ 良好|✅ 基础|需手动埋点|

### 3\.2 推荐：LangGraph（主选）\+ 轻量自研（备选）

**推荐 LangGraph 的理由：**

1. **状态机清晰**：Agent 的每一步（规划→工具→观察→再规划）天然适合图结构建模，面试中可清晰画出状态流转

2. **Human\-in\-the\-loop**：内置 `interrupt` 机制，可在关键决策点暂停等待人工确认（契合银行级安全需求）

3. **工具调用编排**：通过 `ToolNode` 和 `ConditionalEdge` 实现条件分支，比手写循环更可靠

4. **监控友好**：每个节点自动成为 trace 中的一个 span，与 Langfuse/Phoenix 天然对齐

5. **本地轻量**：纯 Python 依赖，无需外部服务

**备选：轻量自研循环**

如果追求极简（单文件 \<200 行），可以手写 ReAct 循环：

```python
class SimpleAgent:
    def __init__(self, llm, tools, max_steps=10):
        self.llm = llm
        self.tools = {t.name: t for t in tools}
        self.max_steps = max_steps

    def run(self, query: str) -> str:
        memory = [{"role": "user", "content": query}]
        for step in range(self.max_steps):
            response = self.llm.chat(memory, tools=list(self.tools.values()))
            if response.tool_calls:
                memory.append(response.message)
                for tc in response.tool_calls:
                    result = self.tools[tc.function.name](**tc.function.arguments)
                    memory.append({"role": "tool", "content": str(result)})
            else:
                return response.content
        return "[Max steps exceeded]"
```

**取舍建议**：

- **面试演示用 LangGraph**：展示对 Agent 编排的深入理解，图结构便于讲解

- **快速原型用自研循环**：验证概念时更灵活，无框架依赖

- **LangChain 不推荐单独使用**：其链式抽象对 Agent 场景不如 Graph 直观

- **OpenAI Agents SDK 可备选**：如果团队已深度使用 OpenAI 生态，但功能相对基础

---

## 4\. 全链路监控方案

### 4\.1 需求分析

Agent 比单次 LLM 调用更需要链路追踪，因为：

- 单次请求可能触发 **多轮 LLM 调用 \+ 多次工具调用 \+ 检索步骤**

- 失败可能发生在任意环节，需要定位到具体 span

- 成本按 token 累积，需精确到每个步骤

### 4\.2 Langfuse vs Arize Phoenix 对比

|维度|**Langfuse**|**Arize Phoenix**|
|---|---|---|
|**许可证**|MIT（真正开源）|Elastic License 2\.0（非 OSI 认证开源）|
|**本地部署**|Docker Compose，需 Postgres \+ ClickHouse \+ Redis \+ S3|`pip install arize-phoenix`，单进程，SQLite 默认|
|**启动复杂度**|中等（4 个服务）|极低（pip 安装，1 分钟启动）|
|**核心优势**|生产级追踪、成本追踪、多 Agent 观测、Prompt 版本管理|评估深度、RAG 专用追踪、一键自动埋点|
|**OpenTelemetry**|支持 ingest|原生支持（OpenInference）|
|**Trace 模型**|trace → span → observation|trace → span|
|**层级粒度**|细（支持嵌套 observation）|中等|
|**Prompt 管理**|✅ 版本化资产、发布通道|有 Playground，非核心概念|
|**评估指标**|基础 LLM\-as\-judge|50\+ 内置指标（faithfulness、hallucination 等）|
|**免费自托管限制**|无限制|无限制（单节点）|
|**生产规模**|适合（ClickHouse 支撑）|需 Postgres 14\+，单节点 OSS|

### 4\.3 推荐：Arize Phoenix（本地开发首选）

**推荐 Phoenix 的理由：**

1. **本地轻量**：`pip install arize-phoenix` 即可运行，无需 Docker Compose 编排多个服务

2. **Agent 评估友好**：内置 50\+ 评估指标，特别适合面试中展示评估能力

3. **一键自动埋点**：`from phoenix.trace.langchain import LangChainInstrumentor; LangChainInstrumentor().instrument()` 一行代码完成 LangGraph 全链路追踪

4. **OpenTelemetry 原生**：与 OpenInference 语义约定对齐，未来可平滑迁移

**备选：Langfuse**

如果未来需要：

- 生产级成本追踪（按用户/会话）

- Prompt 版本治理（A/B 测试、发布通道）

- 高并发追踪（ClickHouse 支撑）

则迁移到 Langfuse。两者不互斥，很多团队 **Phoenix 做开发评估，Langfuse 做生产监控**。

### 4\.4 核心指标体系

|指标类别|具体指标|采集方式|
|---|---|---|
|**延迟**|p50/p95/p99 端到端延迟、各 span 延迟|OpenTelemetry span duration|
|**Token**|input/output/total tokens、各步骤 token|LLM API usage 回传|
|**成本**|单次请求成本、累计成本|token × 模型单价|
|**成功率**|请求成功率、工具调用成功率、各检测器成功率|span status|
|**评估得分**|faithfulness、relevance、toxicity、 hallucination|LLM\-as\-judge / 内置评估器|
|**安全指标**|block 率、redact 率、escalate 率、误报率|Guardrails 决策记录|
|**业务指标**|用户满意度、任务完成率|反馈收集|

### 4\.5 Trace/Span 层级设计

```
trace: session_123
├── span: input_guard (输入检测)
│   └── span: prompt_injection_check
│   └── span: pii_check
├── span: retrieval (检索增强)
│   └── span: vector_search
│   └── span: document_rerank
├── span: planning (任务规划)
├── span: generation_step_1 (LLM 调用)
│   └── span: tool_call_search
│   └── span: tool_result_process
├── span: generation_step_2 (LLM 调用)
├── span: output_guard (输出检测)
│   └── span: toxicity_check
│   └── span: pii_redaction
└── span: response_delivery
```

---

## 5\. 安全防护方案

### 5\.1 威胁模型（对应 OWASP LLM Top 10）

|OWASP 风险|覆盖阶段|检测手段|
|---|---|---|
|**LLM01 Prompt Injection**|输入、检索|规则匹配 \+ 分类器 \+ 嵌入相似度|
|**LLM02 Insecure Output Handling**|输出|输出过滤 \+ PII 检测 \+ 内容安全|
|**LLM06 Sensitive Information Disclosure**|输入、输出、日志|PII 检测 \+ 数据脱敏 \+ 审计控制|
|**LLM08 Excessive Agency**|工具调用|工具权限边界 \+ 参数校验 \+ 人工确认|
|**LLM09 Overreliance**|生成|事实性校验 \+ 溯源标记|
|**System Prompt 暴露**|输入、输出|输出过滤检测 system prompt 泄露模式|

### 5\.2 分层检测架构

```
用户输入 → [Layer 1: 规则引擎] → [Layer 2: 分类器] → [Layer 3: 嵌入检索] → [Layer 4: LLM-as-Judge] → 决策
              (极速 <1ms)      (快速 <10ms)        (中等 <50ms)        (慢速 <500ms)
```

|层级|技术|优点|缺点|适用场景|
|---|---|---|---|---|
|**L1 规则引擎**|Regex、关键词、 denylist|极速、可解释、零成本|易绕过、误报高|明显恶意输入拦截|
|**L2 分类器**|专用 ML/NLP 模型（如 PII 检测）|准确率高、可微调|需训练数据、领域迁移难|PII、毒性、主题控制|
|**L3 嵌入检索**|向量相似度匹配已知攻击模式|发现变种攻击|无法检测全新攻击|已知 jailbreak 模式匹配|
|**L4 LLM\-as\-Judge**|用 LLM 评估输入/输出安全|覆盖 nuanced 场景|延迟高、成本高、不一致|边界案例、复杂语义判断|

### 5\.3 Guardrails AI vs NVIDIA NeMo Guardrails vs 分层自研

|维度|Guardrails AI|NVIDIA NeMo Guardrails|分层自研|
|---|---|---|---|
|**核心范式**|Pydantic Schema \+ Validator Pipeline|Colang 对话流 \+ 向量相似度|规则→分类器→LLM Judge 管道|
|**输出验证**|✅ 强（结构 \+ 内容双重验证）|中等|需自建|
|**输入检测**|通过 validator 扩展|✅ 内置多种防护|需自建|
|**学习曲线**|低（Python 原生）|高（需学 Colang）|中等|
|**本地运行**|✅ 纯 Python|✅ 纯 Python（可选 GPU 加速）|✅ 纯 Python|
|**延迟**|低（Python 级执行）|高（LLM 调用 \+500ms）|可控（分层决定）|
|**可解释性**|✅ 高（validator 链式报告）|中等|✅ 高（每层独立得分）|
|**银行场景适配**|需扩展|需扩展|完全可控|
|**与框架集成**|LangChain/LlamaIndex|LangChain/LangGraph/LlamaIndex|任意|

### 5\.4 推荐：分层自研（主选）\+ Guardrails AI（辅助结构验证）

**推荐分层自研的理由：**

1. **面试可讲清原理**：每一层的检测逻辑、延迟、局限性都透明可控

2. **银行场景适配**：Guardrail Platform 的核心是"detector signals → policy decisions"，自研最能展示这一设计思想

3. **延迟可控**：L1/L2 在 \<10ms 内完成，只有边界案例才走 L4

4. **契约设计清晰**：每层都返回统一格式的检测结果

**辅助使用 Guardrails AI：**

- 用于**结构化输出验证**（JSON Schema、字段类型、取值范围）

- 其 validator 概念可作为 detector contract 的参考实现

**不推荐 NeMo Guardrails 作为主方案：**

- Colang 学习曲线陡峭，面试中难以快速解释

- 依赖向量相似度匹配意图，对对抗性攻击脆弱（学术论文已证实）

- 延迟较高（LLM\-as\-judge 在关键路径上）

### 5\.5 Detector Contract 设计

每个检测器统一返回以下结构：

```python
@dataclass
class DetectorResult:
    label: str           # 分类标签，如 "safe", "prompt_injection", "pii_leak"
    score: float         # 风险分数 [0.0, 1.0]
    confidence: float    # 模型置信度 [0.0, 1.0]
    evidence: List[str]  # 证据列表，如匹配的文本片段、规则名称
    version: str         # 检测器版本，如 "v1.2.3"
    latency_ms: float    # 检测耗时
```

### 5\.6 Policy Action 映射

|动作|含义|适用场景|
|---|---|---|
|**allow**|直接放行|检测结果安全|
|**redact**|脱敏后放行|检测到 PII 但非恶意|
|**block**|阻断并返回错误|检测到明显攻击或严重违规|
|**escalate**|放行但标记人工复核|置信度低或边界案例|
|**rewrite**|改写后放行|轻微违规可安全改写|

### 5\.7 Fail\-Open vs Fail\-Closed 策略

|场景|策略|理由|
|---|---|---|
|**检测器超时**|fail\-closed（阻断）|银行场景，安全优先|
|**低危查询（信息查询）**|fail\-open（放行）|用户体验优先|
|**高危操作（转账、数据导出）**|fail\-closed（阻断）|风险不可承受|
|**检测器降级**|escalate \+ 日志|保持服务可用性同时记录|

---

## 6\. 组件依赖与本地运行

### 6\.1 依赖清单

```
Python 3.10+
├── openai                    # MiniMax API 调用
├── langgraph                 # Agent 编排（可选）
├── arize-phoenix             # 全链路监控
├── opentelemetry-api/sdk     # 埋点标准
├── presidio                  # PII 检测（微软开源）
├── sentence-transformers     # 嵌入模型（本地运行）
├── numpy, pandas             # 数据处理
└── uvicorn, fastapi          # 可选：本地 API 服务
```

### 6\.2 本地启动方式

```bash
# 1. 安装依赖
pip install openai langgraph arize-phoenix opentelemetry-api presidio

# 2. 启动 Phoenix 监控（本地单进程）
python -m phoenix.server.main serve
# 访问 http://localhost:6006

# 3. 配置环境变量
export MINIMAX_API_KEY="your-key"
export PHOENIX_COLLECTOR_ENDPOINT="http://localhost:6006"

# 4. 运行 Agent
python agent.py
```

### 6\.3 资源估算

|组件|CPU|内存|说明|
|---|---|---|---|
|Agent 本体|\<1 核|\<500 MB|纯 Python，轻量|
|Phoenix 监控|\<1 核|\<1 GB|SQLite 模式，单进程|
|本地嵌入模型|1 核|1\-2 GB|sentence\-transformers 小模型|
|**总计**|**2\-3 核**|**3\-4 GB**|普通笔记本可运行|

### 6\.4 已知风险与缓解

|风险|影响|缓解措施|
|---|---|---|
|MiniMax API 不可用|Agent 无法调用 LLM|本地缓存 \+ 降级模式|
|检测器误报高|用户体验差|阈值校准 \+ 多检测器投票|
|本地嵌入模型性能差|检索质量低|使用轻量模型（all\-MiniLM）|
|日志泄露敏感数据|合规风险|日志脱敏 \+ 访问控制|
|Prompt Injection 绕过|安全失效|分层检测 \+ 持续红队测试|

---

## 7\. 与 JD 要求的对应关系

|JD 要求|本方案覆盖|
|---|---|
|Prompt injection / jailbreak 检测|L1 规则 \+ L2 分类器 \+ L3 嵌入 \+ L4 LLM Judge|
|PII / 敏感数据检测|Presidio \+ 自定义分类器 \+ redact 策略|
|System prompt 暴露防护|输出检测层 \+ system prompt 泄露模式匹配|
|危险工具调用防护|工具权限边界 \+ 参数校验 \+ escalate 策略|
|Langfuse / Phoenix 可观测性|Phoenix 本地部署 \+ OpenTelemetry trace|
|OWASP LLM Top 10|threat model 明确映射到各检测层|
|可观测性与遥测|trace/span 设计 \+ 指标体系|
|分层检测|四层检测架构 \+ 混合管道|
|Detector contract|label/score/confidence/evidence/version|
|Policy action|allow/redact/block/escalate|
|Evaluation / threshold calibration|评估指标 \+ 阈值校准方法论|
|Fail\-open / fail\-closed|按场景明确的策略|

---

> **后续行动建议**：
> 
> 1. 先用 MiniMax\-M3 \+ LangGraph \+ Phoenix 搭建最小可行原型（MVP）
> 
> 2. 实现一个带四层检测的输入 guard（规则 \+ Presidio PII \+ 嵌入 \+ LLM Judge）
> 
> 3. 在 Phoenix 中查看 trace，验证 span 层级是否正确
> 
> 4. 准备面试时，能用此架构回答 "Design a guardrail platform for bank\-wide RAG and agent"
> 
> 

> （注：部分内容由豆包工作 AI 生成）
