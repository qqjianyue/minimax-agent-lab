# 基础设施（infra）

本目录存放**基础设施组件的搭建脚本与配置模板**，与 Python 应用本体同仓管理，
但**版本独立演进**（决策 Q5 / Q7）。

---

## 为什么要独立版本

| | 应用本体 | 基础设施组件 |
|---|---|---|
| 形态 | venv + systemd user service | Docker Compose |
| 发布触发 | 代码变更 | 依赖升级 / 配置调整 |
| 回退单位 | 整体应用版本 | 单个组件版本 |
| 变更频率 | 高（每个批次都可能发） | 低（几周一次） |

把两者绑成同一个版本号会导致两种劣化：应用发版时被迫重建 Phoenix 容器，
或者升级 Phoenix 时被迫回退应用。独立版本后，应用回退只切 `current` 符号链接，
**完全不触碰基础设施** —— 这对"回退要快且副作用小"是决定性的。

版本清单见 [`BILL_OF_MATERIALS.yaml`](BILL_OF_MATERIALS.yaml)。

---

## 目录约定

```
infra/
├─ BILL_OF_MATERIALS.yaml      组件清单 + 各组件版本（唯一版本来源）
├─ phoenix/                    组件：Arize Phoenix（trace 存储与评估 UI）
│  ├─ compose.yaml             编排定义
│  ├─ env.template             配置模板（提交进仓库）
│  └─ setup.sh                 搭建 / 升级脚本
├─ models/                     组件：嵌入模型权重（D1，bge-base-zh-v1.5）
│  ├─ manifest.json            模型声明（repo / revision / 文件清单，入库）
│  ├─ setup.sh                 下载 / 校验 / 状态（install | verify | status）
│  └─ env.template             配置模板（提交进仓库）
├─ spacy/                      组件：spaCy NER 模型（D2，en_core_web_lg）
│  ├─ manifest.json            模型声明（包名 / 精确版本，入库）
│  ├─ setup.sh                 安装 / 校验 / 状态（install | verify | status）
│  └─ env.template             配置模板（提交进仓库）
└─ <component>/                新组件按同样结构新增
```

**每个组件目录必须包含 `setup.sh`** —— 目标机上一切变更都通过脚本执行，
不手工敲命令。手工操作无法复现，回退时也无从确认"改动过什么"。

**为什么模型组件用 JSON 而不是 YAML**：`deploy/lib/deployconfig.py` 的两层
扁平解析器不接受子目录文件路径（`1_Pooling/config.json` 里的 `/`）与列表；
标准库 `json` 是目标机唯一保证存在的解析器 —— 与 deployconfig "只用标准库"
是同一个哲学。

**模型资产的完整性凭证 = manifest.lock.json**：`manifest.json` 是声明（人维护，
入库）；`setup.sh install` 首次成功后生成 `manifest.lock.json`（文件级
sha256 + 体积），**写入部署根 `integrity/models/`（不在 repo 副本 / release
内，rsync --delete 不触及）**，`verify` 只认凭证（fail-closed），并校验
凭证内嵌的模型身份 == manifest 声明。**不跟应用版本号**：模型跨版本共享
（shared/models），凭证只跟模型身份（repo+revision）；模型升级 = 改
`manifest.json` 的 revision + 重跑 install，凭证覆盖更新（不做历史留档）。
历史遗留于组件目录的旧 lock 会在首次 verify 时自动迁移到 integrity/。

---

## 密钥约定

`env.template` 提交进仓库，真实 `env` 文件**只在目标机上生成且不入库**。
模板里不放任何真实凭据，只放变量名和默认值。

各组件的 `setup.sh` 应遵循同一约定：若 `${COMPONENT_DIR}/env` 不存在，
则从 `env.template` 复制并提示用户填写，绝不静默生成含默认密钥的配置。

---

## 当前组件状态

| 组件 | 版本 | 状态 | 备注 |
|---|---|---|---|
| `phoenix` | `20.20.0`（tag 已固定，digest 锚点见 BOM） | 已部署（2026-10-24 复核） | 容器 minimax-agent-phoenix，healthcheck 用 exec 直调 python3.13（镜像无 /bin/sh）；trace 落库已实测（default 项目 traceCount=2036）；组件配置在目标机仓库副本里，**不进 release** |
| `embedding-model` | `bge-base-zh-v1.5@f03589ce` | 已部署（2026-10-10） | 目标机直连下载（HF_ENDPOINT 镜像备用）；完整性凭证在 integrity/models/；实测 12 文件 sha256 全通过 |
| `spacy-model` | `3.8.0` | 已部署（2026-10-10） | 装进 shared/venv；wheel 持久化在 /data/workspace/tmp；实测 en/zh 双模型 `spacy.load` 通过 |

> **组件状态是"实测"出来的，不是"声明"出来的。** 复核方式见
> 《开发方案：组件拆分与 DevOps 交付流程.md》的待跟进任务区：镜像 ID、
> sha256 完整性、`spacy.load` 真加载、容器 healthcheck、trace 落库计数，
> 每项都有对应的可执行校验。

---

## D1 / D2 设计要点

- **D1 下载通道**：目标机**直接下载**（不走本机中转）。目标机网络受限时，
  在 `models/env` 配 `HF_ENDPOINT=https://hf-mirror.com`。模型落在
  `${MODELS_HOME}/bge-base-zh-v1.5/`（即 `shared/models/`，跨版本共享，
  应用回退不丢）。
- **D2 安装目标**：`shared/venv`（与应用大依赖同 venv，部署指纹复用机制
  天然兼容）。校验以 `spacy.load` **真实可加载**为准（走一次真实解析），
  版本精确 pin `3.8.0` —— 800 MiB 资产跟 latest 会让应用回退失去意义。
- **应用接缝（B5 落地）**：⚠️ **未落地** —— 这里原本写着"C5 detector-ml
  启动时校验模型完整性，失败拒绝启动"，但代码里并没有这条路径：
  `embedders.py` 只做 `import sentence_transformers` + 目录探测 + 加载，
  `presidio_pii.py` 只做 import + `spacy.load`，**两者都不调用
  `setup.sh verify`、都不校验 sha256**；而 `container.py` 对检测器构建失败
  的处理是**跳过并记 warning**，与"拒绝启动"正好相反（P0-2 即此）。
  `deploy/status.sh` 里也没有那两行完整性检查。
  后果：D1/D2 的完整性机制目前**只有 setup.sh 自己会调** —— 模型文件被截断
  或污染时，模型照常加载、检测结果悄悄变差，全程无信号。
  跟进任务见《开发方案：组件拆分与 DevOps 交付流程.md》待跟进任务区。
