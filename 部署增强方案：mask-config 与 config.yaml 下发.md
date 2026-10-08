# 部署脚本增强方案：mask-config 与 config.yaml 的下发

> **状态**：待审查，未应用任何改动。
> **审查人**：李剑月　**编写时间**：2026-10-08
> **涉及文件**：`deploy/config.yaml`、`deploy/lib/`、`install.sh`、`update.sh`、`status.sh`、`preflight.sh`

---

## 一、现状核查（已确认的事实）

| 检查项 | 实际结果 |
|---|---|
| `deploy/config.yaml` | 存在，2 个键：`build.config` / `runtime.config` |
| `C:\workspace\mask-config.yaml` | **存在**，155 字节 / 1 行，含且仅含键 `MINIMAX_AGENT_LLM__API_KEY` |
| `C:\workspace\minimax-agent-lab\config.yaml` | 存在，12 字节，内容为 `#placeholder` |
| **本地** `/workspace/config.yaml`（= `C:\workspace\config.yaml`） | ❌ **不存在** |
| **目标机** `/workspace/` 目录 | ❌ **不存在**（目标机只有 `/data/workspace`） |
| 已确立的 `AGENT_HOME` | `/data/workspace/minimax-agent` |

> **关于私密信息**：本次核查**只读取了文件元信息与顶层键名**，
> 没有读取 `mask-config.yaml` 的任何值，也不会在任何脚本输出、日志、
> 审计记录或本方案中打印它。后续脚本同样遵守这条纪律。

### 两个必须先解决的路径问题

**问题 1：`build.config: /workspace/config.yaml` 在本地也不存在。**
该项目配置 placeholder 实际在**仓库根目录** `minimax-agent-lab/config.yaml`。
当前配置指向了一个空路径。

**问题 2：`runtime.config: /workspace/minimax-agent/config.yaml` 在目标机无对应目录。**
目标机没有 `/workspace/`，而已确立的部署根是 `/data/workspace/minimax-agent`。
两套路径并存会让"东西到底在哪"变得无法回答 —— 这正是回退排障时最需要确定的信息。

### 第三个发现：mask-config 与 config.yaml 性质完全不同

| | `mask-config.yaml` | `config.yaml` |
|---|---|---|
| 内容 | `MINIMAX_AGENT_LLM__API_KEY=…` | `#placeholder` |
| 格式 | **dotenv（KEY=VALUE）**，非 YAML | 注释占位 |
| 性质 | **私密**（含凭据） | 公开项目配置 |
| 是否进版本库 | 否 | 是 |
| 目标机权限 | `600` | `644` |
| 变化频率 | 极低（换密钥才变） | 随版本 |

因此二者**必须走两条独立的部署路径**，不能用同一套逻辑：
一个跨版本共享、权限收紧、只在显式请求时更新；一个属于 release 产物、跟着版本走。

---

## 二、`deploy/config.yaml` 扩展设计

```yaml
# deploy/config.yaml —— 部署脚本自身的配置源（提交进仓库，不含任何凭据）

build:
  # 项目配置来源。相对于**仓库根**解析，避免写死本机绝对路径。
  config: config.yaml

mask:
  # 私密配置在本机的位置。**故意不写进本文件**（各机不同，且路径本身也可能是敏感的），
  # 由 --mask-source 参数或 MASK_SOURCE 环境变量提供。
  # 默认探测：<repo>/../mask-config.yaml
  default_source: ../mask-config.yaml

runtime:
  # 目标机落地位置。与 AGENT_HOME 对齐（单一事实来源）。
  config: shared/config.yaml    # 相对 AGENT_HOME
  mask:   shared/.env           # 相对 AGENT_HOME；与现有 shared/.env 是同一个文件
```

**关键点**：`runtime.*` 改为**相对 `AGENT_HOME` 的相对路径**。
这样目标机部署根变了（现在是 `/data/workspace/minimax-agent`），配置文件不用跟着改，
也不会出现两套路径并存。

---

## 三、新增：`deploy/lib/deployconfig.py`

目标机**没有 `jq`**，且首次安装时 venv 还不存在（`PyYAML` 装不进来）。
因此不能依赖 PyYAML 解析。

方案：一个 **仅用标准库**的小解析器，处理 `section.key = value` 这种两层扁平结构。
系统 `python3` 即可运行（preflight 已校验 `>= 3.12`），无任何依赖。

```
$ deploy/lib/deployconfig.py build.config
config.yaml
$ deploy/lib/deployconfig.py runtime.mask
shared/.env
```

- 缺失键 → 非零退出并给出明确报错（**不静默用默认值**，路径错了必须炸）
- 只输出值，**不输出任何来自 mask 文件的内容**（它只读 deploy/config.yaml，不碰 mask）

---

## 四、脚本改动

### 4.1 `install.sh`

新增参数：

| 参数 | 作用 |
|---|---|
| `--with-mask [PATH]` | 部署时下发 mask-config（默认探测 `../mask-config.yaml`） |
| `--mask-only` | 只刷新 mask，不做完整安装（用于换密钥后单独刷新） |

`--with-mask` 执行的步骤（插在现有第 4 步"准备共享配置"之后）：

```
4a. 解析 deploy/config.yaml → runtime.mask = shared/.env
4b. 探测本机 mask 源文件；不存在 → 明确报错并给出 --mask-source 用法
4c. 备份现有 shared/.env → shared/.env.bak（仅当已存在）
4d. 渲染：以 deploy/env.template 为底，mask 文件内容**追加在末尾**
    （systemd EnvironmentFile 后者覆盖前者 → 密钥优先级最高）
4e. chmod 600
4f. 校验（只看元信息，不打印值）：
      - 文件存在且非空
      - 权限 == 600
      - 必需键 MINIMAX_AGENT_LLM__API_KEY 存在（grep -q 键名，不打印匹配行）
4g. 记录 sha256 前 12 位到 state/（用于 status 判断"与本机是否一致"，不是内容指纹用于比对）
```

**为什么用"模板 + mask 追加"而不是两份文件**：服务只读一个 `EnvironmentFile`，
两处配置（比如 systemd unit 里再写一份 key）迟早会漂移。单一文件是唯一可靠做法。

### 4.2 `update.sh`

| 参数 | 作用 |
|---|---|
| `--sync-config` | 把仓库的 `config.yaml` 同步到 `shared/config.yaml`（随版本） |
| `--sync-mask` | 重新下发 mask（换密钥时用；**默认不同步**） |
| `--no-auto-rollback` | 已有 |

行为约定：

1. **mask 默认不随版本更新**。它跨版本不变，每次发布都覆盖只会增加出错面。
   只有显式 `--sync-mask` 才刷新。
2. **config.yaml 默认也不覆盖 runtime 副本**。理由：一旦允许覆盖，
   运维在目标机上直接改的配置会在下次发布时静默丢失。`--sync-config` 才覆盖，
   覆盖前同样先备份。
3. mask 同步失败 → 恢复 `.bak`，**不进入回退流程**（它不是 release 的一部分，
   服务仍能跑上一个有效配置）。config 同步失败 → 归入 release 失败路径，走自动回退。

### 4.3 `preflight.sh`

新增检查（都只查元信息）：

- mask 源文件存在且非空（仅当本次要下发时检查）
- 目标 `shared/.env` 存在时，权限必须是 600，否则警告
- `deploy/config.yaml` 存在且能被 `deployconfig.py` 解析
- 打印**路径**（便于排查），不打印文件内容

### 4.4 `status.sh`

新增两个区块：

```
── 私密配置 ─────────────────────────
  状态:      已部署
  目标:      /data/workspace/minimax-agent/shared/.env
  权限:      600
  指纹:      a1b2c3d4e5f6        （sha256 前 12 位，仅用于判断是否变化）
  必需键:    MINIMAX_AGENT_LLM__API_KEY 存在

── 项目配置 ─────────────────────────
  目标:      /data/workspace/minimax-agent/shared/config.yaml
  版本:      与 release 0.1.0+gabc 一致 / 本地已修改（需 --sync-config）
```

同样不打印任何值。

---

## 五、安全纪律（写进脚本注释，作为验收项）

| # | 纪律 | 实现方式 |
|---|---|---|
| 1 | 脚本永不打印 mask 内容 | 全程无 `cat`/`head`/`echo $(cat …)`；校验只用 `grep -q` 判存在性 |
| 2 | 异常与日志不回显内容 | 不把文件内容放进 `die`/`warn` 消息 |
| 3 | 目标机权限 600 | `install -m 600` 或 `chmod 600`，并在 preflight 复核 |
| 4 | 不进 release 目录 | rsync 显式 `--exclude` mask 相关文件；mask 只落 `shared/` |
| 5 | 不进版本库 | `deploy/config.yaml` 只存**路径**不存内容；`shared/` 已被 gitignore |
| 6 | 状态记录只存指纹 | `state/` 记录 sha256 前 12 位，不记录内容 |
| 7 | 已有专项测试 | L1 增加"响应体不含 `sk-`"断言（已有）；新增"脚本输出不含 key"的静态检查 |
| 8 | 备份而非覆盖 | 覆盖前先 `.bak`，失败自动恢复 |

---

## 六、幂等与回退

| 场景 | 行为 |
|---|---|
| 重复执行 `--with-mask` | 幂等：内容一致时跳过写入（比 sha256），仍强制校验权限 |
| mask 写入失败 | 恢复 `.bak`；服务继续用上一份有效配置；**不动 current** |
| mask 权限被改坏 | preflight 警告；`status.sh` 高亮 |
| config 同步失败 | 归入 release 失败路径 → 自动回退到 previous |
| mask 与本地不一致 | `status.sh` 显示"需 `--sync-mask`"，不自动改 |

**回退不涉及 mask**：应用回退只切 `current` 符号链接，mask 在 `shared/` 里
跨版本共享，回退不会改变它。这与"基础设施版本独立"是同一个理由。

---

## 七、需要你确认的决策点

| # | 决策 | 我的建议 | 备选 |
|---|---|---|---|
| **Q1** | `build.config` 指向哪？ | **仓库根 `config.yaml`**（相对仓库解析） | 保留 `/workspace/config.yaml` 并把 placeholder 移过去 |
| **Q2** | `runtime.config` 落哪？ | **`AGENT_HOME/shared/config.yaml`**（与 AGENT_HOME 对齐） | 在目标机新建 `/workspace/minimax-agent/`，与 AGENT_HOME 并存 |
| **Q3** | mask 与 `env.template` 的关系 | **合并成单个 `shared/.env`**（模板打底，mask 追加在后，密钥覆盖） | 拆成 `shared/.env` + `shared/secret.env` 两个 EnvironmentFile |
| **Q4** | `mask-config.yaml` 的扩展名 | **保持不变**，但在注释里注明它是 dotenv 格式 | 改名为 `mask-config.env`（更准确，但要改你本机的文件） |
| **Q5** | mask 路径是否写进 `deploy/config.yaml`？ | **不写**，用 `--mask-source` / 环境变量传；文件里只写默认探测规则 | 写死路径（提交进仓库会泄露你的本机目录结构） |
| **Q6** | 本批次是否给 app 加 config.yaml 加载器？ | **不加**。placeholder 现在还是 `#placeholder`，加了也无内容可读；留到有真实内容时再实现 | 本批次就实现 YAML 配置加载（会引入新的配置优先级问题） |
| **Q7** | `runtime.config` 目标文件格式 | 保持 YAML，与源一致 | 部署时转成 dotenv 供 EnvironmentFile 用 |

---

## 八、实施顺序（审查通过后）

1. 扩展 `deploy/config.yaml` + 新增 `deploy/lib/deployconfig.py`（含 stdlib 解析器单测）
2. 改 `preflight.sh`（新增 mask/config 元信息检查）
3. 改 `install.sh`（`--with-mask` / `--mask-only`）
4. 改 `update.sh`（`--sync-config` / `--sync-mask`）
5. 改 `status.sh`（新增两个区块）
6. 本地跑完整闸门；`bash -n` 校验全部脚本
7. 更新 README 的部署与凭据章节

**不在本次范围**：目标机部署（等密钥就位后走 B3 收尾）、app 侧 config.yaml 加载器。
