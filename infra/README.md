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
└─ <component>/                新组件按同样结构新增
```

**每个组件目录必须包含 `setup.sh`** —— 目标机上一切变更都通过脚本执行，
不手工敲命令。手工操作无法复现，回退时也无从确认"改动过什么"。

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
