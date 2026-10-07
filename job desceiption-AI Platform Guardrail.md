# Associate Director, Software Engineering (Guardrail Platform AI Safety Track)
> HSBC Job ID: 53435
> Location: Guangzhou or Xi'an, China
> Original application deadline shown: September 30, 2026
> > **Application-status check:** The captured deadline has passed as of October 5, 2026. Confirm that the role is still open before applying.

## Listing Details
| Field | Information |
| --- | --- |
| Employer | HSBC |
| Business | CTO Platform, AI Platforms |
| Job ID | 53435 |
| Locations | Guangzhou or Xi'an, China |
| Application deadline shown in the capture | September 30, 2026 |
| Listing URL | Not included in the supplied capture |
| Other listing metadata | Not included in the supplied capture |

## Role Overview
HSBC's Group AI Platform team builds shared AI capabilities for use across the Bank. This role focuses on a centralized guardrail platform: turning AI safety checks into reusable services.

The platform scope includes:
- AI safety and policy enforcement.
- Prompt and response validation.
- Synchronous, asynchronous, and streaming checks.
- Multi-tenant configuration.
- Evaluation, observability, and operational tooling.
- Human review workflows.
- Model and vendor integrations.
- Runtime orchestration and enterprise APIs and controls.

## Role Responsibilities
1. Design and productionize detectors for:
    - Prompt injection and jailbreaks.
    - PII and other sensitive data, including data leakage.
    - Harmful content and policy violations.
    - System-prompt exposure.
    - Risky agent tool behavior.
2. Build detector pipelines that combine rules and patterns, NLP or ML classifiers, embedding-based approaches, LLM-as-a-judge evaluation, and hybrid strategies.
3. Define detector scoring, confidence scoring, and policy-driven actions.
4. Develop safety-model evaluation workflows, including test-set creation, labeling, fine-tuning, validation, threshold calibration, precision/recall optimization, regression testing.
5. Evaluate and integrate open-source or third-party safety tools. Benchmark quality, latency, cost, and operational fit, and make build-versus-buy recommendations.
6. Design protections across RAG, coding assistants, conversational systems, and agents, covering input, retrieval, generation, output, logging, and tool execution.
7. Build safety observability and governance, including telemetry, tracing, dashboards, evaluation metrics, audit trails, feedback loops, and operational controls.
8. Work with platform, infrastructure, security, and application teams on performance, scalability, detector lifecycle, and reliable rollout.

## Required Experience and Skills
The job listing emphasized experience with:
- First-party safety detector development.
- Fine-tuning pipelines for safety-oriented LLMs or SLMs.
- Integrating and operating open-source or third-party detectors.
- Build-versus-buy evaluation for safety capabilities.
- AI evaluation or observability tools, such as Langfuse, LangSmith, Arize, or Phoenix.
- DLP, secret scanning, document classification, or enterprise information protection.
- Adversarial testing, red teaming, or continuous attack simulation.

## Technologies and Keywords Reference
| Area | Items mentioned in the capture |
| --- | --- |
| Threat detection | Prompt injection, jailbreaks, PII, sensitive data, data leakage, harmful content, policy violations, system-prompt exposure, risky tool behavior |
| Detection models | Rules and patterns, NLP/ML classifiers, embeddings, LLM-as-a-judge, hybrid pipelines |
| Evaluation and tuning | Test sets, labeling, fine-tuning, validation, threshold calibration, precision, recall, regression tests, production feedback |
| Safety tools | Open-source and third-party detectors; build-versus-buy assessment |
| Observability | Langfuse, LangSmith, Arize, Phoenix, telemetry, traces, dashboards, audit trails |
| Enterprise controls | DLP, secret scanning, document classification, information protection |
| Protected applications | RAG, coding assistants, conversational systems, agents, human review, multi-tenancy |

# Part 2: AI Guardrail Platform Interview Preparation
**Target role:** Associate Director, Software Engineering (Guardrail Platform AI Safety Track)
**Suggested pace:** 5 days, about 3 hours per day
**Starting point:** Prompt engineering, RAG, model tuning, and AI metrics

## Interview Core Concepts
> This is a safety-platform role rather than a single-detector or model-training role. Prepare to explain how detector signals become consistent, auditable decisions across multiple AI applications.

1. **Threats and trust boundaries**
    - Direct prompt injection and jailbreaks; indirect injection in retrieved documents and tool results.
    - PII, credentials, confidential data, harmful content, policy violations, and system-prompt exposure.
    - Risky agent behavior, excessive permissions, unsafe arguments, and consequential tool calls.
    - Treat user input, retrieved content, model output, and tool output as separate trust boundaries. A model saying something is safe is not itself a security control.

2. **Layered detection**
    - Rules and patterns are fast and explainable, but can be brittle.
    - Specialized classifiers and PII detectors offer targeted signals, but need evaluation across languages and domains.
    - Embeddings can help with semantic similarity and known attack patterns, but are not a complete security boundary.
    - LLM-as-a-judge can assess nuanced cases, but adds latency and cost and may be inconsistent.
    - Hybrid pipelines combine signals; no single detector should be treated as universally reliable.

3. **Detector contracts and policy decisions**
    - A detector should return a typed result such as label, score, confidence, evidence, version, and latency.
    - The policy layer maps results and context to actions: allow, redact, block, rewrite, require approval, or escalate.
    - Thresholds are risk and product decisions. Discuss false positives, false negatives, user impact, and the intended use case.

4. **Controls throughout the AI workflow**
    - **Input:** validate requests; inspect for injection, sensitive data, and disallowed intent.
    - **Retrieval:** enforce document permissions; inspect retrieved passages for injection and sensitive data.
    - **Generation and output:** apply policy and inspect the response before release.
    - **Tools:** authorize each action, restrict available tools, validate argument schemas, and require confirmation for high-impact actions.

5. **Evaluation and operations**
    - Build labeled, adversarial, and regression test sets; measure precision, recall, false-positive and false-negative rates; calibrate thresholds.
    - Track platform outcomes such as block, redact, escalation, and override rates, alongside p95 latency, availability, throughput, and cost.
    - Version detectors and policies. Record enough trace and decision evidence for investigation while minimizing sensitive data in logs.
    - Plan for timeouts, retries, detector outages, streaming, human review, and explicit fail-open, fail-closed, or fallback behavior.
    - Enforce tenant-specific configuration, access control, and audit boundaries.

## Frameworks and Tools to Prioritize
| Priority | Framework or tool | What to learn | Interview use |
| --- | --- | --- | --- |
| Must know | OWASP Top 10 for LLM Applications | Common LLM application risks, including prompt injection, sensitive information exposure, and excessive agency | Structure threat modeling and explain coverage gaps |
| Must know | NIST AI Risk Management Framework and Generative AI Profile | Governance, risk ownership, measurement, and ongoing management | Connect engineering controls to enterprise risk practice |
| Must know | One of Langfuse or Arize Phoenix | Traces, evaluations, feedback, and version-aware observability | Describe how you would investigate and improve a production detector |
| Useful | MITRE ATLAS | Adversarial tactics and techniques relevant to AI systems | Organize red-team scenarios and test coverage |
| Useful | Microsoft Presidio | PII detection and anonymization patterns | Discuss one detector's strengths and limits; do not present it as complete DLP |
| Compare | Guardrails AI and NVIDIA NeMo Guardrails | How checks and policies are composed, and their integration and operational trade-offs | Make a reasoned build-versus-buy or tool-selection recommendation |
| Optional | Open Policy Agent (OPA) | Policy-as-code concepts and centralized decisions | Explain how policy could be separated from detector implementation |
| Lower priority | Hugging Face Transformers, PEFT, and TRL | Safety-model fine-tuning workflow | Relate existing tuning knowledge to data quality, validation, and lifecycle controls |

> You do not need to master every tool in five days. Learn OWASP and NIST at a conceptual level, then get hands-on familiarity with one observability tool and compare the guardrail frameworks at an architectural level.

## Five-Day Learning Plan
Assume roughly three hours each day. Reserve the final 15 minutes to explain the day's topic aloud without notes.

| Day | Focus | Study / Practice | Deliverable |
| --- | --- | --- | --- |
| 1 | Threats and controls | Review the OWASP LLM risks and NIST GenAI Profile summaries. Map injection, data exposure, unsafe output, and tool misuse to the AI workflow. Draw trust boundaries for a RAG assistant with tools. At each boundary, name a threat, a control, and a failure mode. | One-page threat model and a 60-second answer to "What does a guardrail platform protect?" |
| 2 | Detector design and evaluation | Compare rules, PII detectors, classifiers, embeddings, and LLM-as-a-judge. Review precision, recall, calibration, threshold choice, and labeled data. Design an injection-detection pipeline. Specify its contract, test set, threshold rationale, fallback, and policy actions. Include benign examples likely to trigger false positives. | Detector specification and evaluation plan |
| 3 | Platform architecture | Sketch input, retrieval, generation, output, and tool checks. Add timeout behavior, a degraded mode, and a human-review path. | System-design diagram and a two-minute walkthrough |
| 4 | Red teaming and observability | Review MITRE ATLAS and learn one tool: Langfuse or Arize Phoenix. Connect traces, detector versions, policy decisions, and feedback to evaluation. Write 10 adversarial tests across direct and indirect injection, data leakage, and tool misuse. | Define detector, workflow, and service-level metrics |
| 5 | Small real rehearsal | Revisit the role requirements. Prepare examples of delivery, technical judgment, collaboration, and operational ownership from your own experience. Run a 30-minute mock design: "Build a guardrail platform for a bank-wide RAG and agent service." Practice trade-offs and follow-up questions. | Polished design answer, three evidence-based experience stories, and interviewer questions |

## System-Design Answer Structure
Use this sequence for a platform-design question:
1. **Clarify the use cases:** protected applications, risk tolerance, latency budget, regions, tenant needs, and whether responses or actions can be consequential.
2. **Map threats and trust boundaries:** cover input, retrieval, generation, output, logs, and tool execution.
3. **Propose layered detectors and policy:** explain each signal, its latency and limitations, and how results map to allow, redact, block, approval, or escalation.
4. **Explain evaluation before rollout:** labeled and adversarial sets, regression tests, quality metrics, threshold calibration, and staged deployment.
5. **Cover production behavior:** streaming release strategy, timeouts, retries, outages, fail-open or fail-closed choices, auditability, versioning, and human review.
6. **Define success and feedback:** quality, user impact, service-level metrics, cost, and how production feedback can improve the system without silently degrading safety.

> For streaming, explicitly discuss how much content can be released before there is enough context to make a decision. For outages, choose behavior by use case: a low-risk informational assistant and a high-impact agent need not share the same fallback.

## Experience Stories to Prepare
Choose real examples from your work and use a concise situation, action, result structure. Do not imply that model tuning or prompt design alone is a complete safety platform.

| Story | Evidence to bring |
| --- | --- |
| Prompt or RAG improvement | A concrete quality or reliability problem, how you measured it, and what changed |
| Model tuning | Data preparation, validation approach, trade-offs, and a measurable outcome |
| Metrics-led decision | A metric that changed a technical or product decision, including limitations of the metric |
| Cross-team delivery | How you aligned stakeholders, handled risk or disagreement, and delivered an operable result |

**Positioning statement for interview opening:**
> "I have experience with prompts, RAG, model tuning, and AI metrics. I am extending that into platform engineering by treating safety as controls across the full AI workflow, with explicit policy decisions, measurable detector quality, and production operating practices."

Follow it with a specific example from your experience.

## Practical Interview Questions to Rehearse
- Design a guardrail platform that protects a bank-wide RAG assistant and agent workflow. What belongs in the shared platform?
- How would you combine a fast rules-based detector with a classifier or LLM judge without making the system too slow or opaque?
- How would you design a detector contract and map its output to a policy action?
- How would you choose thresholds when false positives disrupt users but false negatives can expose sensitive data?
- How would you evaluate prompt-injection defenses against attacks hidden in retrieved documents or tool outputs?
- What should happen if an inline safety detector times out or becomes unavailable? How does the answer vary by use case?
- How would you inspect streaming model output before releasing it to the user?
- How would you build a privacy-conscious trace and audit trail for safety decisions?
- How would you compare an open-source detector with a third-party service and a first-party model?
- How would production feedback be used to improve detectors without contaminating evaluation data or weakening controls?

## Questions to Ask the Interviewer
- Which use cases and threat categories are the platform's current priorities?
- How are detector quality, latency, and user impact measured today?
- How are tenant policies and detector versions governed and rolled out?
- What is the current approach to streaming, human review, and detector outages?
- How does the team balance first-party detector development with third-party tools?

## Source Notes
- This combined document is assembled from two capture files: job listing and 5-day interview preparation plan.
- Job ID 53435 and original deadline September 30, 2026; verify both against the live job listing if available.
- The capture does not include a listing URL or complete employment metadata.
