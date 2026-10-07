# 面试准备任务计划：本地 Agent 实践项目主线

> **目标职位：** HSBC Associate Director, Software Engineering（Guardrail Platform AI Safety Track）\| Job ID 53435
> **用户起点：** 已熟悉 prompt engineering、RAG、模型微调、AI 指标；有 MiniMax API 访问权限
> **项目主线：** 构建一个本地运行的 AI Agent（MiniMax 后端），集成全链路指标监控与安全防护框架
> **建议周期：** 7–10 天（可根据节奏压缩或延展）
> 
> 

**核心定位**：这不是一个"学习清单"，而是一个**可运行的项目 \+ 面试素材库**。每个阶段的产出都是面试中可以直接讲述的实证。

平台化视角贯穿始终：你的叙事焦点是"如何为多个 AI 应用提供一致、可审计的安全决策"，而非"我调了一个检测器"。

---

## 一图看懂 · 总览

核心心智模型：请求沿管道向下（顺序数据流），可观测性与治理贯穿每一层（横切面）。

```
┌────────────────────────────────────────────────────────────┐
│              用户交互层   CLI / Web UI / API               │
└───────────────────────────┬────────────────────────────────┘
                            ▼
┌────────────────────────────────────────────────────────────┐
│ Agent 编排层   Planner | Memory | Tool Router | ReAct Loop │
│                 全链路 span 埋点均在本进程                 │
└───────────────────────────┬────────────────────────────────┘
                            ▼
┌────────────────────────────────────────────────────────────┐
│          前置检测 Input Guard  ← 环绕拦截（同步）          │
│        L1 规则 → L2 分类器 → L3 嵌入 → L4 LLM Judge        │
└───────────────────────────┬────────────────────────────────┘
                            ▼
┌────────────────────────────────────────────────────────────┐
│                 检索增强 RAG  +  工具执行                  │
│             文档注入检测 / 权限过滤 / 参数校验             │
└───────────────────────────┬────────────────────────────────┘
                            ▼
╔════════════════════════════════════════════════════════════╗
║     大模型后端  MiniMax  —— 外部资产，位于信任边界之外     ║
║          无法在其服务进程内插桩；遥测在调用侧闭环          ║
╚═══════════════════════════════╤════════════════════════════╝
                            ▼
┌────────────────────────────────────────────────────────────┐
│    后置检测 Output Guard  ← 环绕拦截（终检 / PII 脱敏）    │
│               拦截必须发生在响应返回用户之前               │
└───────────────────────────┬────────────────────────────────┘
                            ▼
           返回用户响应（已按 Policy Action 处理）

════════════════════════════════════════════════════════════
╔════════════════════════════════════════════════════════════╗
║            横切面：可观测性与治理（贯穿每一层）            ║
║        埋点层   OTel + OpenInference（client span）        ║
║     审计层   Detector Contract → 决策记录 append-only      ║
║  存储层   Phoenix / Langfuse + 本地文件（脱敏 + 保留期）   ║
╚════════════════════════════════════════════════════════════╝
════════════════════════════════════════════════════════════
```

三个必须记住的位置结论：

1. **大模型后端是外部资产**。MiniMax 以 SaaS 形式提供，我方无法在其服务进程内插桩，GPU 利用率、服务侧日志等内部指标不可得。

2. **遥测在调用侧闭环**。延迟、token usage、成本、错误码全部由本进程包装 HTTP client 采集（`llm_call` span）。跨网络边界的调用本来就是调用方埋点，这是可观测性的标准做法，与后端是否自研无关。

3. **安全检测是环绕拦截（wrapping），不是管道下游**。Input Guard 在调用前、Output Guard 在响应返回用户前，两者同步执行，检测结论会改变数据流本身；而审计留痕不影响数据流，属于横切面。

> 详细架构见《本地 Agent 技术架构方案》§1.1 与 §1.2（其中 §1.2 表格的「执行位置」列逐行标注了每一环的归属）。

---

## 项目定位与面试叙事

### 为什么选这个主线？

JD 的核心是 **Guardrail Platform** —— 把 AI 安全检查变成可复用的平台服务。用户已有的 prompt/RAG/微调/指标经验，正好可以延伸为平台工程视角：

- **Prompt 经验** → 理解输入侧威胁和检测策略

- **RAG 经验** → 理解检索侧注入和权限控制

- **微调经验** → 理解 safety model 的数据准备、验证和生命周期

- **指标经验** → 理解 observability、trace、dashboard 和反馈闭环

### 面试开场定位陈述

> "我有 prompt engineering、RAG、模型微调和 AI 指标的经验。我正在把这些延伸为平台工程能力，把安全看作贯穿 AI 全工作流的控制体系——包含显式策略决策、可量化的检测器质量，以及生产运营实践。"
> 
> 

---

## 五阶段执行计划

---

### 阶段 1：最小可运行 Agent（MiniMax 后端）搭建

**建议投入：** 1–2 天

**前置条件：** 已有 MiniMax API Key；本地 Python 环境就绪

#### 目标

搭建一个能本地运行的 AI Agent，以 MiniMax 为 LLM 后端，支持 ReAct 或类似推理循环，能调用至少 2 个外部工具。这是后续所有监控和安全工作的运行基础。

#### 任务清单

1. **选择并安装 Agent 框架**

    - 推荐方案（三选一）：

        - **LangChain**（生态最成熟，文档丰富，面试讨论度高）

        - **LlamaIndex**（RAG 原生支持强，如果你已有 RAG 经验）

        - **自研轻量框架**（如果你想展示对 Agent 循环的底层理解）

    - 安装依赖，配置 MiniMax API 接入

2. **实现基础 ReAct / Tool\-use 循环**

    - 实现：Observation → Thought → Action → 调用 Tool → 观察结果的循环

    - 至少集成 2 个工具（示例：Web 搜索、代码执行、数据库查询、API 调用）

    - 确保 Agent 能在本地终端连续交互运行

3. **定义 Agent 输入输出契约**

    - 明确：用户输入格式、工具参数 schema、最终响应结构

    - 为后续监控和安全检测预留 hook 点（输入前、工具调用前、输出前）

4. **（可选）接入一个简单 RAG 流程**

    - 如果你选择 LlamaIndex 或想在 Agent 中加检索能力

    - 加载少量本地文档，实现 retrieve\-then\-generate 流程

#### 产出物

* [ ] 本地可运行的 Agent 脚本/项目

* [ ] 支持至少 2 个工具调用

* [ ] 一次完整运行的输入输出示例（保存为日志）

* [ ] Agent 架构简图（手绘或文字描述即可，阶段 5 会精修）

#### 对应 JD 职责与技能

|JD 条目|对应关系|
|---|---|
|职责 6|在 RAG、coding assistants、conversational systems 和 agents 上设计 protections|
|职责 8|与 platform、infrastructure、security、application 团队协作|
|技能|有 AI evaluation/observability 工具经验（为阶段 2 铺垫）|

#### 面试中如何讲

- **系统设计中**："我先搭了一个最小可运行的 Agent，支持 ReAct 循环和两个工具。这帮助我理解 Agent 在运行时会产生哪些信任边界——用户输入、检索内容、模型输出、工具输出——每个边界都需要独立检查。"

- **行为面试**："我选择 LangChain / 自研框架的原因是……"

#### 验收标准

* [ ] 运行 `python agent.py`（或等效命令），Agent 能响应用户问题并调用工具

* [ ] MiniMax API 调用成功，返回合理结果

* [ ] 至少完成 3 轮不同场景的完整对话测试并保存日志

* [ ] 代码结构清晰，预留了输入前、工具调用前、输出前的 hook 接口

---

### 阶段 2：接入全链路监控（Trace \+ 指标）

**建议投入：** 1\.5–2 天
**依赖阶段：** 阶段 1（需有可运行 Agent）

#### 目标

为 Agent 建立全链路可观测性：每一次运行都有完整的 trace、延迟、token 用量、工具成功率等指标可被记录和查询。

#### 任务清单

1. **选择并部署 observability 工具**

    - 推荐：**Langfuse**（开源、自托管友好、trace 能力完整）或 **Arize Phoenix**（对 evaluation 支持强）

    - 本地 Docker 快速启动，或接入云服务

    - 配置项目、环境、trace 命名规范

2. **集成 trace 到 Agent 全链路**

    - 在用户输入层打 trace：记录原始输入、预处理后的输入

    - 在模型调用层打 trace：记录 prompt、response、latency、token usage

    - 在工具调用层打 trace：记录工具名、参数、返回值、耗时、成败

    - 在输出层打 trace：记录最终输出、输出前是否经过安全检测

    - **明确可观测性边界**：MiniMax 是外部 SaaS，无法在其服务进程内插桩。模型调用层的 latency / token usage / 成本 / 错误码 / 重试次数全部由本进程包装 HTTP client 采集，形成 `llm_call` span。这是调用方埋点，是跨网络边界调用的标准做法

    - 把"拿不到的"显式列出来：GPU 利用率、KV cache 命中率、服务侧日志等模型服务内部指标不纳入观测范围，写进指标文档的"已知盲区"章节

3. **定义并采集关键指标**

    - **质量类**：请求成功率、工具调用成功率、模型响应相关性（可先用简单启发式）

    - **性能类**：端到端延迟、p50/p95/p99 latency、各阶段耗时分解

    - **成本类**：token 用量、API 调用次数

    - **安全类**：（为阶段 3 预留）检测触发次数、拦截率、误报率

4. **搭建基础 Dashboard**

    - 在 Langfuse/Phoenix 中配置关键视图

    - 或导出到 Grafana/Prometheus（如果你更熟悉这套栈）

#### 产出物

* [ ] Agent 每次运行都有完整 trace 记录

* [ ] 至少 5 个关键指标可被查看

* [ ] 一个基础 dashboard 或指标汇总视图

* [ ] 指标定义文档（字段、口径、采集方式）

#### 对应 JD 职责与技能

|JD 条目|对应关系|
|---|---|
|职责 7|Build safety observability and governance: telemetry, tracing, dashboards, evaluation metrics, audit trails, feedback loops|
|技能|AI evaluation or observability tools, such as Langfuse, LangSmith, Arize, or Phoenix|

#### 面试中如何讲

- **系统设计中**：" observability 不是事后加的。我在 Agent 的每个信任边界都预埋了 trace 点——输入前、模型调用、工具调用、输出前。这样当安全检测器触发时，我能完整回溯上下文，判断是误报还是真实威胁。"

- **技术判断**："我选择 Langfuse 而不是自建是因为……它的 trace 模型正好匹配 LLM 应用的 span 结构，自托管也符合数据安全要求。"

#### 验收标准

* [ ] 任意一次 Agent 运行后，能在 observability 工具中看到完整 trace

* [ ] trace 包含：输入、模型调用（latency/token）、工具调用（参数/返回值/耗时）、最终输出

* [ ] 至少能回答："过去 10 次运行中，平均延迟多少？工具失败率多少？"

* [ ] 指标定义文档已撰写，口径清晰

---

### 阶段 3：接入安全防护框架与检测器

**建议投入：** 2–3 天
**依赖阶段：** 阶段 1、2（需有可运行 Agent \+ 监控系统）

#### 目标

为 Agent 建立多层安全检测体系：从快速规则到语义检测，覆盖输入、检索、生成、输出、工具执行各环节。定义 detector contract 和 policy engine，使安全决策可审计、可配置。

#### 任务清单

1. **设计 detector contract**

    - 每个检测器返回结构化结果：

        - `label`：威胁类别（如 prompt\_injection、PII、harmful\_content）

        - `score`：风险分数（0–1 连续值）

        - `confidence`：检测器对自身判断的信心

        - `evidence`：判断依据（匹配到的规则、相关文本片段）

        - `version`：检测器版本号

        - `latency_ms`：检测耗时

    - 用 Pydantic model 或 dataclass 定义，确保类型安全

2. **实现第一层：规则/模式检测器（快速、可解释）**

    - Prompt injection 关键词/模式匹配（如 "ignore previous instructions"、DAN 变体）

    - PII 正则匹配（手机号、身份证号、银行卡号、邮箱等）

    - 敏感指令检测（如 "delete all"、"drop table" 等工具相关）

    - 特点：延迟 \< 10ms，可解释，但容易被绕过

3. **实现第二层：Embedding / 语义相似度检测器**

    - 收集已知攻击 prompt 的 embedding 库（可用公开数据集如 PromptInject、HuggingFace 上的 adversarial prompts）

    - 对输入计算 embedding，与攻击库做相似度匹配

    - 适用于：已知攻击变体、语义等价的注入尝试

4. **实现第三层：LLM\-as\-a\-Judge 检测器**

    - 用 MiniMax（或更小的模型）判断输入/输出是否包含：

        - 越狱尝试

        - 有害内容

        - 系统提示泄露风险

    - 设计结构化 prompt，要求模型输出 JSON 格式的判断结果

    - 特点：覆盖 nuanced cases，但增加延迟和成本

5. **构建 Policy Engine**

    - 接收多个检测器的输出，按策略映射到行动：

        - `ALLOW`：通过

        - `REDACT`：脱敏后放行（如 PII 替换为 \[REDACTED\]）

        - `BLOCK`：阻断，返回预设响应

        - `REWRITE`：要求模型重新生成

        - `REQUIRE_APPROVAL`：转人工审核

        - `ESCALATE`：升级告警

    - 策略规则示例：

        - 规则检测器 score \> 0\.8 → BLOCK

        - Embedding 相似度 \> 0\.9 → REQUIRE\_APPROVAL

        - LLM Judge 判断为 harmful → BLOCK

        - PII 检测命中 → REDACT

    - 支持按 use case / tenant 配置不同策略（多租户雏形）

6. **在全链路关键节点嵌入检测**

    - **输入侧**：用户输入 → 规则检测 → Embedding 检测 → LLM Judge → Policy 决策

    - **检索侧**（如有 RAG）：检索到的文档 → 注入检测 → PII 检测

    - **输出侧**：模型生成输出 → 有害内容检测 → PII 检测 → Policy 决策

    - **工具侧**：工具调用参数 → schema 校验 → 敏感指令检测 → 高影响操作确认

7. **集成到监控**

    - 每个检测器的调用都记录到 trace 中

    - 记录检测器结果、延迟、决策 action

    - 在 dashboard 上增加：检测触发率、各检测器 latency、拦截/放行/升级比例

#### 产出物

* [ ] Detector Contract 定义（代码 \+ 文档）

* [ ] 至少 3 类检测器实现（规则、Embedding、LLM Judge）

    * [ ] Prompt injection / jailbreak 检测

    * [ ] PII / 敏感数据检测

    * [ ] 有害内容 / 策略违规检测

* [ ] Policy Engine（支持至少 4 种 action）

* [ ] 安全检测集成到 Agent 全链路

* [ ] 安全相关指标接入 dashboard

#### 对应 JD 职责与技能

|JD 条目|对应关系|
|---|---|
|职责 1|Design and productionize detectors for prompt injection, PII, harmful content, system\-prompt exposure, risky agent tool behavior|
|职责 2|Build detector pipelines combining rules, NLP/ML classifiers, embeddings, LLM\-as\-a\-judge, hybrid strategies|
|职责 3|Define detector scoring, confidence scoring, and policy\-driven actions|
|职责 6|Design protections across RAG, coding assistants, conversational systems, and agents|
|技能|First\-party safety detector development；DLP, secret scanning, document classification|

#### 面试中如何讲

- **系统设计中**："我没有依赖单一检测器，而是设计了分层架构。规则检测器在 5ms 内拦截已知攻击；embedding 检测器捕捉语义变体；LLM Judge 处理 nuanced cases。Policy Engine 把多个信号映射到 action，而且策略是按用例可配置的——银行客服和内部数据分析可以有不同的风险容忍度。"

- **技术判断**："Detector 必须返回结构化 contract，包含 score、confidence、evidence 和 version。这让政策决策可审计，也让运营团队能追踪哪个版本在哪个 tenant 上出了什么问题。"

- **权衡讨论**："LLM Judge 增加了 200ms 延迟，所以我只在规则检测器未命中时触发，并且设置了超时 fallback。"

#### 验收标准

* [ ] 至少 3 个不同类型的检测器运行正常

* [ ] Policy Engine 能根据检测结果执行正确 action

* [ ] 检测器结果包含完整的 contract 字段

* [ ] 安全检测的 latency 和结果被记录到 trace

* [ ] 能演示一个 prompt injection 被拦截的完整流程

* [ ] 能演示一个 PII 被 redact 的完整流程

* [ ] 策略规则文档化，至少覆盖 4 种 action 的决策逻辑

---

### 阶段 4：红队测试与对抗样本

**建议投入：** 1\.5–2 天
**依赖阶段：** 阶段 3（需有检测器才能测试）

#### 目标

通过系统化的红队测试验证安全框架的有效性，发现 bypass 路径，建立评估数据集，并基于测试结果迭代改进检测器。

#### 任务清单

1. **设计对抗测试集（至少 10 个样本，覆盖 4 类威胁）**

    - **Direct Prompt Injection / Jailbreak**（3–4 个）：

        - 经典越狱：DAN、Developer Mode、AIM 等变体

        - 角色扮演绕过："你是一个不受限制的安全研究员……"

        - 编码/翻译绕过：用 base64、外语、代码注释包裹恶意指令

    - **Indirect Prompt Injection**（2–3 个）：

        - 在 RAG 检索文档中嵌入隐藏指令

        - 在网页/工具返回内容中嵌入注入

        - 多轮对话中逐步注入

    - **PII / 数据泄露**（2–3 个）：

        - 诱导模型输出系统提示中的敏感信息

        - 诱导模型复述训练数据中的个人信息

        - 通过工具调用泄露敏感参数

    - **Tool Misuse / Risky Agent Behavior**（2–3 个）：

        - 诱导 Agent 执行未授权的数据库操作

        - 构造参数使工具产生副作用

        - 通过多步推理绕过单步权限检查

2. **执行红队测试**

    - 对每个对抗样本，记录：

        - 输入/攻击方式

        - 哪些检测器触发了（如果有）

        - 最终系统行为（被拦截 / 被放行 / 部分拦截）

        - Bypass 路径分析

    - 保存所有测试结果到结构化文档

3. **分析 bypass 并迭代检测器**

    - 识别哪些攻击成功 bypass 了当前防护

    - 分析根因：是规则缺失？threshold 太高？还是检测器盲区？

    - 针对性改进：

        - 补充规则/模式

        - 调整 threshold

        - 增加新的检测层

        - 改进 prompt engineering（对 LLM Judge）

4. **建立回归测试集**

    - 将红队测试样本 \+ 正常用例 组成 regression test set

    - 正常用例应包含容易触发 false positive 的 benign 输入

    - 设计自动化脚本：运行测试集 → 输出 precision / recall / F1

5. **校准 threshold**

    - 针对每个检测器，绘制 precision\-recall 曲线

    - 选择 threshold：false positive 不能太高（影响用户体验），false negative 必须可控（安全底线）

    - 记录 calibration 决策理由

#### 产出物

* [ ] 对抗样本集（≥10 个，覆盖 4 类威胁）

* [ ] 红队测试报告（含 bypass 分析）

* [ ] 检测器改进记录（改了什么、为什么、效果如何）

* [ ] 回归测试集 \+ 自动化评估脚本

* [ ] Threshold calibration 文档

#### 对应 JD 职责与技能

|JD 条目|对应关系|
|---|---|
|职责 4|Develop safety\-model evaluation workflows: test\-set creation, labeling, validation, threshold calibration, precision/recall optimization, regression testing|
|职责 5|Evaluate and integrate open\-source or third\-party safety tools; benchmark quality, latency, cost|
|技能|Adversarial testing, red teaming, or continuous attack simulation|

#### 面试中如何讲

- **系统设计中**："评估不是一次性活动。我建立了一个持续的红队测试流程：每次检测器更新后，自动运行回归测试集，确保没有 regression。我的测试集包含直接注入、间接注入、数据泄露和工具滥用四类，而且我特意加入了 benign 但容易误触发的用例来监控 false positive。"

- **技术判断**："Threshold 校准是一个风险决策，不是纯技术问题。对于银行客服场景，false positive 影响用户体验；对于访问核心数据的 Agent，false negative 可能导致合规事故。我选择 threshold 时会同时考虑 precision、recall 和业务风险。"

- **经验故事**："在一次红队测试中，我发现编码绕过能 bypass 规则检测器。我的改进是：在规则层之前加一个解码/标准化层，同时让 LLM Judge 的 prompt 明确要求检测编码后的恶意意图。"

#### 验收标准

* [ ] 至少 10 个对抗样本，覆盖 4 类威胁

* [ ] 每个样本有完整的测试结果记录

* [ ] 至少识别 2 个 bypass 路径并实施改进

* [ ] 回归测试脚本能自动运行并输出 precision/recall

* [ ] Threshold calibration 文档说明每个检测器的阈值选择理由

* [ ] 改进后的检测器对原 bypass 样本能正确拦截

---

### 阶段 5：复盘与面试叙事打磨

**建议投入：** 1–2 天
**依赖阶段：** 阶段 1–4（需要完整项目经验）

#### 目标

将项目经验转化为结构化面试素材：系统设计方案、经验故事、职责映射、快查卡。确保能在面试中清晰、自信地讲述。

#### 任务清单

1. **绘制项目架构图**

    - 一张图展示：Agent 架构 \+ 安全检测层 \+ Policy Engine，外加贯穿全链路的**可观测性横切面**

    - 标注每个组件的输入输出、信任边界、检测点

    - 在大模型后端上标出"外部资产 / 信任边界之外"——这是面试官最容易追问的点

    - 标注技术选型及理由

2. **编写 3–5 个经验故事（STAR 格式）**

    - 从项目中提取真实决策和挑战：

        - **故事 A**：为什么选择分层检测而不是单一 LLM Judge？（技术判断 \+ 权衡）

        - **故事 B**：如何处理一个 bypass 的发现和修复？（问题解决 \+ 迭代）

        - **故事 C**：如何设计 detector contract 让运营团队能审计决策？（平台思维 \+ 协作）

        - **故事 D**：threshold 校准中的 false positive / false negative 权衡（风险决策）

        - **故事 E**：如何与"业务团队希望放宽检测"和"安全团队希望收紧"之间平衡？（跨团队协作）

    - 每个故事控制在 90 秒内讲完

3. **逐条映射 JD 8 条职责**

    - 对每条职责，指出项目中哪部分覆盖了它

    - 准备 1–2 句话的"如果面试官问这条职责，我怎么答"

4. **模拟系统设计题**

    - 题目："Build a guardrail platform for a bank\-wide RAG and agent service"

    - 按材料中的 6 步结构准备完整答案：

        1. Clarify use cases

        2. Map threats and trust boundaries

        3. Propose layered detectors and policy

        4. Explain evaluation before rollout

        5. Cover production behavior

        6. Define success and feedback

    - 准备 3 个 trade\-off 讨论和可能的 follow\-up

5. **准备向面试官提的问题**

    - 从材料中选出 3–5 个最有价值的问题

    - 每个问题准备"我为什么关心这个"的背景

6. **整合面试快查卡**

    - 将本文末尾的 10 个问题答案框架熟记

    - 用自己的项目经验替换示例中的 generic 回答

#### 产出物

* [ ] 项目架构图（可手绘/用工具生成）

* [ ] 3–5 个经验故事（STAR 格式，每篇 150–250 字）

* [ ] JD 职责映射表

* [ ] 系统设计题完整答案（书面版）

* [ ] 向面试官提问清单

* [ ] 熟读面试快查卡

#### 对应 JD 职责与技能

|JD 条目|对应关系|
|---|---|
|全部 8 条|通过项目经验和叙事准备，确保面试中能覆盖每条职责|
|技能|Build\-versus\-buy evaluation；cross\-team delivery；operational ownership|

#### 面试中如何讲

- **开场**：使用定位陈述（见前文）

- **项目介绍**："我为了准备这个方向，动手做了一个端到端的 Guardrail Agent 项目。让我从架构讲起……"

- **深入**：引导面试官到你准备的故事和 trade\-off 讨论上

#### 验收标准

* [ ] 能在 3 分钟内完整介绍项目架构

* [ ] 每个经验故事能在 90 秒内讲完

* [ ] 对 JD 8 条职责，每条都能用项目中的具体工作回应

* [ ] 系统设计题答案能在 8–10 分钟内讲完

* [ ] 能提出 3 个有深度的问题

---

## 阶段依赖与建议时间线

```
阶段1 (1-2天) ──→ 阶段2 (1.5-2天) ──→ 阶段3 (2-3天) ──→ 阶段4 (1.5-2天) ──→ 阶段5 (1-2天)
   搭建Agent          全链路监控           安全防护框架          红队测试            复盘叙事
```

**总建议周期：7–10 天**

|阶段|建议天数|前置依赖|可并行项|
|---|---|---|---|
|1\. 最小可运行 Agent|1–2|无|无|
|2\. 全链路监控|1\.5–2|阶段 1|无|
|3\. 安全防护框架|2–3|阶段 1、2|阶段 2 后期可开始设计 detector contract|
|4\. 红队测试|1\.5–2|阶段 3|无|
|5\. 复盘叙事|1–2|阶段 1–4|可与阶段 4 后半段重叠|

**加速建议：** 如果每天有 4\+ 小时投入，可将阶段 1\+2 合并为 2 天，阶段 3 压缩为 2 天，总计 5–6 天完成。

---

## JD 8 条职责全覆盖映射

|JD 职责|覆盖阶段|项目中的具体体现|
|---|---|---|
|1\. 设计并实现检测器（prompt injection、PII、harmful content、system\-prompt exposure、risky tool behavior）|阶段 3|实现了 3\+ 类检测器，覆盖输入/检索/输出/工具各环节|
|2\. 构建检测器 pipeline（规则 \+ NLP/ML \+ Embedding \+ LLM Judge \+ 混合策略）|阶段 3|分层检测架构：规则层 → Embedding 层 → LLM Judge 层|
|3\. 定义检测器评分、置信度和策略驱动 action|阶段 3|Detector Contract \+ Policy Engine（ALLOW/REDACT/BLOCK/REWRITE/APPROVAL/ESCALATE）|
|4\. 开发安全模型评估工作流（测试集、标注、验证、阈值校准、回归测试）|阶段 4|红队测试集 \+ 回归测试脚本 \+ threshold calibration 文档|
|5\. 评估并集成开源/第三方安全工具，做 build\-vs\-buy 推荐|阶段 3、5|面试中可对比 Guardrails AI、NeMo Guardrails、Presidio 等|
|6\. 在 RAG、coding assistants、conversational systems、agents 上设计 protections|阶段 1、3|Agent 项目本身就是 agent 场景；覆盖输入/检索/生成/输出/工具执行|
|7\. 构建安全可观测性和治理（telemetry、trace、dashboard、指标、审计、反馈）|阶段 2、3|Langfuse/Phoenix trace \+ 安全指标 dashboard \+ 审计日志|
|8\. 与平台、基础设施、安全、应用团队协作|阶段 1–5|Detector Contract 设计便于跨团队协作；经验故事 E 专门准备跨团队场景|

---

## 面试快查卡

> 以下 10 个问题来自你的原始准备材料。答案框架强调**平台化视角**——你的角色是构建让多个 AI 应用共享的安全决策平台，而不是只调优一个检测器。
> 
> 

---

### Q1：Design a guardrail platform that protects a bank\-wide RAG assistant and agent workflow\. What belongs in the shared platform?

**要点框架（3–5 句）：**

1. 先澄清 protected applications、风险容忍度、延迟预算、租户需求——不同场景（客服 vs 核心数据访问）需要不同策略。

2. 平台核心是可复用的检测器服务和 Policy Engine，而不是每个应用自建检测。

3. 共享层包括：统一 detector pipeline（规则/Embedding/LLM Judge）、集中式 policy 配置、全链路 trace 与审计、tenant 隔离的阈值和策略管理。

4. 应用层只负责在正确的信任边界调用平台 API：输入前、检索后、输出前、工具调用前。

5. 平台还要提供 evaluation 基础设施：回归测试集、指标 dashboard、feedback loop，让运营团队能持续改进策略而不碰代码。

---

### Q2：How would you combine a fast rules\-based detector with a classifier or LLM judge without making the system too slow or opaque?

**要点框架（3–5 句）：**

1. 采用分层短路策略：规则检测器先跑，latency \< 5ms，命中直接触发 policy，不触发下游检测器。

2. 规则未命中时才启动 Embedding 检测器（\~50ms），再未命中才启动 LLM Judge（\~200ms），平均 latency 受规则命中率控制。

3. 每个检测器返回结构化 contract（label、score、confidence、evidence、version、latency），policy 决策透明可审计，不是黑箱。

4. 对 latency 敏感的场景（如实时客服），可配置跳过 LLM Judge；对安全敏感的场景（如数据访问），强制全量检测。

5. 超时设置 fallback：如果某层检测器超时，按配置选择 fail\-open（放行\+告警）或 fail\-closed（阻断\+人工审核）。

---

### Q3：How would you design a detector contract and map its output to a policy action?

**要点框架（3–5 句）：**

1. Detector Contract 返回：label（威胁类别）、score（0–1）、confidence（高/中/低）、evidence（匹配片段）、version、latency\_ms。

2. Policy Engine 接收多个 detector 的输出，按优先级规则映射到 action：score \> 0\.9 \+ confidence = high → BLOCK；PII 命中 → REDACT；模糊情况 → REQUIRE\_APPROVAL。

3. Policy 规则是 tenant 可配置的 JSON/YAML，运营团队可以调整阈值和 action 而不改代码，实现 policy\-as\-code。

4. 每个决策都记录到审计日志：输入内容（按需脱敏）、所有 detector 结果、最终 action、决策理由、tenant ID、时间戳。

5. Version 字段让平台能追踪"tenant A 在上周还在用 detector v1\.2，本周升级到 v1\.3 后 false positive 下降了 15%"，支持 A/B 和 rollback。

---

### Q4：How would you choose thresholds when false positives disrupt users but false negatives can expose sensitive data?

**要点框架（3–5 句）：**

1. Threshold 不是纯技术参数，是风险决策：需要安全、产品、运营三方参与，明确各自场景的 false positive/false negative 容忍度。

2. 按 use case 差异化：银行客服可以容忍稍高的 false positive（人工复核成本低），但核心数据访问 Agent 必须优先控制 false negative（安全底线）。

3. 用 precision\-recall 曲线辅助决策，但不止看 F1——还要模拟 false positive 对用户旅程的影响（如"每 100 次客服对话被误拦截 3 次"是否可接受）。

4. 设置双层阈值：低阈值触发 soft action（如标记\+人工审核），高阈值触发 hard action（如阻断），避免"一刀切"。

5. Threshold 是持续校准的：生产数据反馈 → 定期评估 → 渐进调整，重大变更走 staged rollout。

---

### Q5：How would you evaluate prompt\-injection defenses against attacks hidden in retrieved documents or tool outputs?

**要点框架（3–5 句）：**

1. 将间接注入视为独立威胁类别，建立专门的测试集：在 RAG 文档、网页内容、工具返回中嵌入隐藏指令，测试 Agent 是否会执行。

2. 检测点不能只在用户输入层，必须在检索后和工具输出后各加一道检测——这是 trust boundary 原则。

3. 对检索内容，在送入模型前做注入检测和敏感信息扫描；对工具输出，校验返回内容是否包含指令覆盖或异常格式。

4. 红队测试要覆盖渐进式注入：第一轮 benign，第二轮 subtly malicious，第三轮 explicit——测试多轮对话中的上下文污染。

5. 评估指标不止"是否拦截"，还要追踪 Agent 实际行为：是否调用了未授权工具？是否输出了敏感信息？行为指标比检测指标更接近真实风险。

---

### Q6：What should happen if an inline safety detector times out or becomes unavailable? How does the answer vary by use case?

**要点框架（3–5 句）：**

1. 必须显式配置 fallback 行为，不能默默跳过或系统崩溃——这是平台可靠性的核心。

2. **Fail\-closed（阻断\+告警）**：适用于高安全场景（核心数据访问、资金操作），宁可误杀不能漏检，同时触发人工审核队列。

3. **Fail\-open（放行\+告警\+延迟审计）**：适用于低风险信息查询，用户体验优先，但事后必须审计并补检。

4. **Degraded mode（降级检测）**：用更轻量的备用检测器（如只跑规则层）替代失效的复杂检测器，保持基础防护。

5. 用 use case 标签自动路由 fallback 策略，并在 trace 中显式标记"detector X timeout, fallback to fail\-closed"，确保可审计。

---

### Q7：How would you inspect streaming model output before releasing it to the user?

**要点框架（3–5 句）：**

1. Streaming 的安全检查核心问题是：在释放多少 token 之前，必须有足够上下文做出安全决策？

2. 策略一：缓冲窗口——累积前 N 个 token 或完整句子后，一次性做安全检测，通过后再释放；适合延迟容忍度中等的场景。

3. 策略二：渐进释放\+实时检测——每生成一个片段就检测，发现风险立即截断并替换为预设响应；需要检测器支持流式输入。

4. 策略三：预生成安全边界——在 prompt 层约束模型不生成某些类别内容，减少运行时检测压力（辅助手段，不能替代检测）。

5. 平台应支持按场景配置策略：低延迟客服用策略二，高安全报告生成用策略一；所有策略都记录释放决策到 trace。

---

### Q8：How would you build a privacy\-conscious trace and audit trail for safety decisions?

**要点框架（3–5 句）：**

1. 最小必要原则：trace 只记录做安全决策必需的信息，原始用户输入和模型输出按需脱敏或哈希化存储。

2. 分层存储：热数据（最近 7 天完整 trace）存高性能存储；温数据（7–90 天）脱敏后存；冷数据（\>90 天）只保留聚合统计和审计摘要。

3. PII 检测器在记录前就运行：如果输入包含 PII，trace 中存的是脱敏版本，原始内容不进入日志系统。

4. 访问控制：trace 按 tenant 隔离，安全团队可调阅，应用团队只能看到聚合指标，个人不能查看原始对话。

5. 保留期与合规：明确数据保留期限（如 90 天），到期自动清理，支持 GDPR/等保要求的删除和导出。

---

### Q9：How would you compare an open\-source detector with a third\-party service and a first\-party model?

**要点框架（3–5 句）：**

1. 评估维度统一用 QLOC：Quality（precision/recall/语言覆盖）、Latency（p50/p99）、Operational cost（运维复杂度）、Cost（调用费用/自研人力）。

2. **开源方案**（如 Presidio、Guardrails AI）：优势是可控、可定制、无外部依赖；劣势是需自运维、语言/领域覆盖可能不足、社区支持不稳定。

3. **第三方服务**：优势是快速上线、持续更新、多语言支持；劣势是数据出境风险、供应商锁定、费用随用量线性增长、黑箱难以调试。

4. **First\-party 模型**：优势是贴合银行业务场景、数据不出境、可深度定制；劣势是开发周期长、需要标注数据和 ML 基础设施、持续迭代成本高。

5. 平台化视角：不是三选一，而是建一个抽象层让三种 detector 可以并存，按场景动态选择或组合，随时替换而不影响上层 policy。

---

### Q10：How would production feedback be used to improve detectors without contaminating evaluation data or weakening controls?

**要点框架（3–5 句）：**

1. 严格分离三类数据：训练数据、评估数据、生产反馈数据——评估数据一旦用于调优就不再是评估数据，必须重建。

2. 生产反馈走独立通道：用户举报、运营标注、自动异常检测发现的 case，先进入"待审核队列"，经人工确认后才加入训练集。

3. 建立时间隔离：用 T\-30 天前的数据做训练，T\-7 到 T\-1 的数据做评估，T 当天的数据只用于监控 drift 不用于训练。

4. 检测器更新必须过回归测试：新模型在 hold\-out 测试集上的指标必须不劣于旧模型，才能进入 canary 部署。

5. Feedback loop 要可观测：记录"production case X → 人工标注 → 加入训练 → v1\.4 模型 → 该类别检测率提升 12%"的完整链路，用于审计和效果追踪。

---

## 附录：快速启动检查清单

开始阶段 1 前，确认以下事项：

* [ ] MiniMax API Key 可用，已测试能正常调用

* [ ] 本地 Python 3\.9\+ 环境就绪

* [ ] Docker 已安装（用于部署 Langfuse/Phoenix）

* [ ] 已阅读本计划的全部 5 个阶段

* [ ] 已明确每天可投入的时间（建议至少 2 小时）

---

> **最后提醒：** 这个计划的真正价值不在于完成所有任务，而在于你能在面试中清晰、自信地讲述"我做了什么、为什么这么做、遇到了什么问题、怎么解决的"。每个阶段的产出都是你的面试弹药。祝准备顺利！
> 
> 

> （注：部分内容由豆包工作 AI 生成）
