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

**模型资产的版本事实 = manifest.lock.json**：`manifest.json` 是声明（人维护，
入库）；`setup.sh install` 首次成功后生成 `manifest.lock.json`（文件级
sha256 + 体积，入库），`verify` 只认 lock —— 与 deploy 侧
requirements.lock 指纹同一模式。升级模型 = 改 `manifest.json` 的
revision + 重跑 install，生成新 lock。

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
| `phoenix` | 见 BILL_OF_MATERIALS | 未部署 | 首次在 B3 批次部署验证后，把 image tag 固定为具体版本 |
| `embedding-model` | `bge-base-zh-v1.5@f03589ce` | 未部署 | 目标机直连下载（HF_ENDPOINT 镜像备用）；待 B5（C5 detector-ml）接入启动校验 |
| `spacy-model` | `3.8.0` | 未部署 | 装进 shared/venv；无决策依赖，可随时 install |

---

## D1 / D2 设计要点

- **D1 下载通道**：目标机**直接下载**（不走本机中转）。目标机网络受限时，
  在 `models/env` 配 `HF_ENDPOINT=https://hf-mirror.com`。模型落在
  `${MODELS_HOME}/bge-base-zh-v1.5/`（即 `shared/models/`，跨版本共享，
  应用回退不丢）。
- **D2 安装目标**：`shared/venv`（与应用大依赖同 venv，部署指纹复用机制
  天然兼容）。校验以 `spacy.load` **真实可加载**为准（走一次真实解析），
  版本精确 pin `3.8.0` —— 800 MiB 资产跟 latest 会让应用回退失去意义。
- **应用接缝（B5 落地）**：C5 detector-ml 启动时校验模型完整性 ——
  embedding 调 `infra/models/setup.sh verify`、spacy 调
  `infra/spacy/setup.sh verify`，失败拒绝启动并指向对应脚本；
  同时 `deploy/status.sh` 增加两行完整性检查（与 Phoenix 状态行并列）。
