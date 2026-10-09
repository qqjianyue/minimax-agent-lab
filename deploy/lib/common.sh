#!/usr/bin/env bash
# 部署脚本公共库
#
# 由 install.sh / update.sh / rollback.sh / status.sh 共同 source。
# 单独 source 时直接退出，避免被误执行。

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "common.sh 是库文件，请勿直接执行；请使用 install.sh / update.sh / rollback.sh / status.sh" >&2
  exit 64
fi

set -euo pipefail

#: 部署脚本自身所在目录（由本文件位置推导，与调用方无关）
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOYCONFIG="${DEPLOY_DIR}/lib/deployconfig.py"

# ---------------------------------------------------------------------------
# 布局
# ---------------------------------------------------------------------------
AGENT_HOME="${AGENT_HOME:-/data/workspace/minimax-agent}"
RELEASES_DIR="${AGENT_HOME}/releases"
SHARED_DIR="${AGENT_HOME}/shared"
STATE_DIR="${AGENT_HOME}/state"
CURRENT_LINK="${AGENT_HOME}/current"
PREVIOUS_LINK="${AGENT_HOME}/previous"

SHARED_ENV="${SHARED_DIR}/.env"
SHARED_VENV="${SHARED_DIR}/venv"
SHARED_MODELS="${SHARED_DIR}/models"
SHARED_DATA="${SHARED_DIR}/data"
LOG_DIR="${SHARED_DIR}/logs"

STATE_FILE="${STATE_DIR}/deploy-state.json"
SERVICE_NAME="minimax-agent.service"
SERVICE_PORT="${SERVICE_PORT:-8080}"
BASE_URL="${BASE_URL:-http://127.0.0.1:${SERVICE_PORT}}"

#: 保留的历史版本数
KEEP_RELEASES="${KEEP_RELEASES:-5}"

#: 目标机上必须存在的配置键（只校验键名，绝不读取值）
REQUIRED_MASK_KEYS=(
  "MINIMAX_AGENT_LLM__API_KEY"
)

# ---------------------------------------------------------------------------
# 私密配置（mask-config）
# ---------------------------------------------------------------------------

#: 读 deploy/config.yaml。目标机没有 jq，依赖 stdlib-only 的解析器。
dc_get() {
  python3 "$DEPLOYCONFIG" "$@" 2>/dev/null || true
}

#: 目标机上的 mask 文件路径。未配置时返回空串。
mask_path() {
  dc_get resolve runtime.mask-config --as remote
}

#: 目标机上项目配置的位置（方案 A：随 release 走，不单独下发）
project_config_path() {
  printf '%s/config.yaml' "$AGENT_HOME"
}

#: 检查某个文件是否含指定键。**只看键名**，grep -q 抑制输出。
_mask_has_key() {
  grep -qE "^[[:space:]]*${2}[[:space:]]*[:=]" "$1"
}

#: 在目标机侧复核私密配置。返回 0 通过；非 0 表示有具体问题。
#: 刻意逐项打印结论：运维要能一眼看出"是没下发、还是权限坏了、还是缺键"。
verify_mask() {
  local dst="${1:-$(mask_path)}"
  local -a problems=()

  if [[ -z "$dst" ]]; then
    echo "  [SKIP] deploy/config.yaml 未声明 runtime.mask-config"
    return 0
  fi
  if [[ ! -f "$dst" ]]; then
    echo "  [FAIL] 私密配置不存在: $dst"
    problems+=("missing")
  else
    echo "  [ OK ] 存在: $dst"
    if [[ ! -s "$dst" ]]; then
      echo "  [FAIL] 内容为空: $dst"
      problems+=("empty")
    else
      echo "  [ OK ] 非空 ($(wc -c < "$dst") 字节)"
    fi
    local perm; perm="$(stat -c '%a' "$dst" 2>/dev/null || echo '?')"
    if [[ "$perm" == "600" ]]; then
      echo "  [ OK ] 权限 600"
    else
      echo "  [FAIL] 权限为 $perm，应为 600（chmod 600 $dst）"
      problems+=("perm")
    fi
    local key
    for key in "${REQUIRED_MASK_KEYS[@]}"; do
      if _mask_has_key "$dst" "$key"; then
        echo "  [ OK ] 配置项存在: $key"
      else
        echo "  [FAIL] 缺少必需配置项: $key"
        problems+=("key:${key}")
      fi
    done
    # 只记录指纹用于判断"是否变化"，不记录内容
    echo "  [ OK ] 指纹: $(sha256sum "$dst" | cut -c1-12)"
  fi

  ((${#problems[@]} == 0)) || return 1
  return 0
}

#: 判断本机（目标机）mask 是否已就位，用于跳过不必要的下发。
mask_is_ready() {
  local dst; dst="$(mask_path)"
  [[ -n "$dst" && -s "$dst" ]] || return 1
  local key
  for key in "${REQUIRED_MASK_KEYS[@]}"; do
    _mask_has_key "$dst" "$key" || return 1
  done
  return 0
}

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  _C_RESET=$'\033[0m'; _C_OK=$'\033[32m'; _C_WARN=$'\033[33m'; _C_ERR=$'\033[31m'; _C_INFO=$'\033[36m'
else
  _C_RESET=""; _C_OK=""; _C_WARN=""; _C_ERR=""; _C_INFO=""
fi

log()  { printf '%s[deploy]%s %s\n' "$_C_INFO" "$_C_RESET" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$_C_OK" "$_C_RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$_C_WARN" "$_C_RESET" "$*" >&2; }
die()  { printf '%s[FAIL]%s %s\n' "$_C_ERR" "$_C_RESET" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 基础检查
# ---------------------------------------------------------------------------
require_cmd() {
  local missing=()
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  ((${#missing[@]} == 0)) || die "缺少命令: ${missing[*]}"
}

#: 确保 uv 可用；缺失时装到 ~/.local/bin（免 sudo）。
#:
#: 为什么把"装包管理器"写进安装脚本：首次安装的前提就是**什么都没有**。
#: 一个会因为缺 uv 而直接中止的"首次安装"脚本是不完整的 —— 现场复现时
#: 还得先手工补一条 curl 命令，这件事必须自己会做。
#:
#: 刻意**不在 preflight.sh 里装**：preflight 是只读体检工具，改环境是
#: 安装脚本的职责。两者分开，才能既保证 install 自洽，又保持 preflight
#: 随时可以安全地重复运行。
#: 让 uv 缓存落在大分区，而不是 /home（目标机 /home 常被分配得很小）。
#:
#: 为什么部署脚本要自己设置而不是依赖 shell 配置：.bashrc 里的
#: ``export UV_CACHE_DIR=...`` 只在**交互**会话生效 —— 部署脚本经
#: ``ssh host "cmd"`` 非交互执行时 .bashrc 根本不加载，uv 于是回落到
#: ``$HOME/.cache/uv``，在 /home 只有几 GB 的机器上必然写爆（实测：
#: CUDA 版 torch 下载时 ``No space left on device``）。
#:
#: 优先级：env 已显式设置 UV_CACHE_DIR → 用已存在的 /data/workspace/.cache/uv
#: （缓存可复用）→ 默认值兜底。目录不存在则创建。
ensure_uv_cache() {
  local dir="${UV_CACHE_DIR:-/data/workspace/.cache/uv}"
  mkdir -p "$dir"
  export UV_CACHE_DIR="$dir"
  ok "uv 缓存目录: ${UV_CACHE_DIR}"
}

ensure_uv() {
  ensure_uv_cache
  if command -v uv >/dev/null 2>&1; then
    ok "uv 已就绪: $(uv --version 2>/dev/null | head -1)"
    return 0
  fi

  local bin="${HOME}/.local/bin"
  if [[ -x "${bin}/uv" ]]; then
    # 已装但不在 PATH 里（常见于新装的机器）
    export PATH="${bin}:${PATH}"
    ok "uv 已就绪（从 ${bin} 加载）: $(uv --version 2>/dev/null | head -1)"
    return 0
  fi

  require_cmd curl
  log "未检测到 uv，正在安装到 ${bin}（免 sudo，约需数十秒）…"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="${bin}" sh

  export PATH="${bin}:${PATH}"
  command -v uv >/dev/null 2>&1 || die "uv 自动安装失败，请手动执行：
     curl -LsSf https://astral.sh/uv/install.sh | sh"
  ok "uv 安装完成: $(uv --version 2>/dev/null | head -1)"
}

ensure_layout() {
  mkdir -p "$RELEASES_DIR" "$STATE_DIR" "$LOG_DIR" "$SHARED_DATA" "$SHARED_MODELS"
  ok "目录结构就绪: ${AGENT_HOME}"
}

# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

#: 当前激活版本（读 symlink，不是读状态文件 —— 状态文件可能与实际不一致）
current_release() {
  [[ -L "$CURRENT_LINK" ]] || return 1
  basename "$(readlink -f "$CURRENT_LINK")"
}

previous_release() {
  [[ -L "$PREVIOUS_LINK" ]] || return 1
  basename "$(readlink -f "$PREVIOUS_LINK")"
}

release_dir() { printf '%s/%s' "$RELEASES_DIR" "$1"; }

#: 依赖指纹 —— 只覆盖**依赖包**，不覆盖项目自身的版本号。
#:
#: uv.lock 里内嵌了项目自己那一段（``[[package]] name = "minimax-agent"``），
#: 直接哈希整份 lock 的话，每次纯代码发版指纹必变，两个后果：
#:
#: 1. 日志报"重建 venv（约需数分钟）"，实际只花几十毫秒 —— 误导运维
#: 2. venv 复用的快路径形同虚设（它本就是把几分钟压到几十秒的优化）
#:
#: 所以这里只取**依赖包**的 ``name==version`` 排序后哈希，剔除项目自身。
#: 刻意不用 ``uv export``：它的输出头部带一行自动生成的注释，里面含
#: ``--project <路径>``，而 release 目录名本身就带版本号，会把路径差异
#: 重新引入指纹。
venv_fingerprint() {
  local rel="$1"
  local lock="${rel}/uv.lock"
  if [[ ! -f "$lock" ]]; then
    echo "no-lock"
    return 0
  fi

  local self
  self="$(sed -n 's/^name[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    "${rel}/pyproject.toml" 2>/dev/null | head -1)"
  self="${self:-minimax-agent}"

  awk -v self="$self" '
    /^\[\[package\]\]/ { pkg = ""; ver = ""; next }
    /^name[[:space:]]*=/ {
      pkg = $0; sub(/^[^"]*"/, "", pkg); sub(/".*$/, "", pkg); next
    }
    /^version[[:space:]]*=/ {
      ver = $0; sub(/^[^"]*"/, "", ver); sub(/".*$/, "", ver); next
    }
    /^source[[:space:]]*=/ {
      if (pkg != "" && ver != "" && index(pkg, self) == 0) print pkg "==" ver
      next
    }
  ' "$lock" | sort | sha256sum | cut -c1-16
}

#: 从仓库推导版本号：<semver>+g<git-short-sha>（工作区有改动则加 .dirty）
#: 与 agent_core.version.get_version_info() 的格式严格一致 ——
#: 冒烟测试要拿它和 /healthz 返回值做精确比对，格式一旦分叉比对就会永远失败。
derive_version() {
  local repo="$1"
  local semver="0.0.0"
  if [[ -f "${repo}/pyproject.toml" ]]; then
    semver="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${repo}/pyproject.toml" | head -1)"
    semver="${semver:-0.0.0}"
  fi

  local sha="" dirty=""
  if git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    sha="$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || true)"
    if [[ -n "$(git -C "$repo" status --porcelain 2>/dev/null)" ]]; then
      dirty=".dirty"
    fi
  fi

  if [[ -n "$sha" ]]; then
    printf '%s+g%s%s' "$semver" "$sha" "$dirty"
  else
    printf '%s' "$semver"
  fi
}

#: release 的身份串 —— 去掉 ``.dirty`` 后缀。
#:
#: ``.dirty`` 描述的是**源工作区**的状态。一旦内容被 rsync 进
#: ``releases/<version>/``，目录内容就固定了，此后不再随工作区变化。
#:
#: 踩过的坑（会导致冒烟测试**恒定失败**）：``export_release_env`` 故意剥掉
#: ``.dirty``（服务因此自报 ``0.1.0+gabc123``），但调用方仍把**带 .dirty 的**
#: 版本串当作 release 目录名和冒烟的 ``EXPECTED_VERSION``（``0.1.0+gabc123.dirty``）。
#: 两边永远不相等 —— 而冒烟比对版本号正是防止"symlink 切换失败却误报成功"的
#: 机制，等于把最关键的安全网变成了每次都误报的噪声。
release_identity() {
  printf '%s' "${1%.dirty}"
}

#: 部署时注入的版本环境变量。systemd unit 不写死版本，
#: 由服务启动时从这些变量读取，保证 /healthz 报的是实际运行的那份。
export_release_env() {
  local version="$1" rel="$2"
  local sha=""
  # semver 也要写进去：项目本体**不装进 venv**（见 ensure_venv 的说明），
  # dist metadata 取不到版本，get_version_info() 会回退到环境变量。
  # 不写的话 /healthz 会报 0.0.0+unknown，冒烟的版本比对必然失败。
  # 只有版本串里真的带 "+g<sha>" 时才注入 git sha。
  #
  # 踩过的坑：原先无条件写 `MINIMAX_AGENT_GIT_SHA=${version#*+g}`，而
  # **通配不匹配时该展开会原样返回整个字符串**。无 git 提交时版本是纯
  # `0.1.0`，于是 GIT_SHA 变成 "0.1.0"，Python 侧 display 再拼一次
  # "+g" 得到 `0.1.0+g0.1.0`，与冒烟测试的期望版本永远对不上 ——
  # 冒烟会在每次安装时恒定失败。
  if [[ "$version" == *"+g"* ]]; then
    sha="${version#*+g}"
  fi
  cat > "${rel}/.release-env" <<EOF
MINIMAX_AGENT_SEMVER=${version%%+*}
MINIMAX_AGENT_GIT_SHA=${sha}
MINIMAX_AGENT_GIT_DIRTY=0
MINIMAX_AGENT_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  # 清除 dirty 标记：release 目录内容已固定，不再随工作区变化
  sed -i 's/\.dirty$//' "${rel}/.release-env"
}

#: 把 infra 组件的**运行时变量**合并进 shared/.env（幂等，可重复执行）。
#:
#: 背景：infra/*/env 是各组件（D1 models / D2 spacy）自己维护的机器配置，
#: 安装期由组件 setup.sh 消费；但其中一部分变量**服务进程运行时**也要读
#: （systemd unit 的 EnvironmentFile 之一就是 shared/.env）。此前两者没有
#: 打通 —— 目标机上 D1 部署好了、MODELS_HOME 却没进 shared/.env，
#: 服务进程里 SentenceEmbedder 缺 MODELS_HOME，回退到 HF 仓库名联网加载
#: 模型，在 L3 的 5s 检测墙钟内必然超时 → pipeline fail-closed → 全 block。
#:
#: 只同步组件 env 里声明为"运行时必需"的键（见函数体映射），不碰
#: shared/.env 里的其它键（用户手工配置）。部署流程负责同步，组件仍各自
#: 管理自己的 env。
sync_component_env() {
  local rel="$1"
  local target="${SHARED_ENV}"
  if [[ ! -f "$target" ]]; then
    : > "$target"
    chmod 600 "$target"
  fi

  # 组件 env 文件 → 需要同步进 shared/.env 的运行时键
  local -r models_env="${rel}/infra/models/env"
  if [[ -f "$models_env" ]]; then
    local value
    value="$(sed -n 's/^MODELS_HOME=//p' "$models_env" | tail -1 || true)"
    if [[ -n "$value" ]]; then
      if grep -q '^MODELS_HOME=' "$target"; then
        sed -i "s|^MODELS_HOME=.*|MODELS_HOME=${value}|" "$target"
      else
        printf 'MODELS_HOME=%s\n' "$value" >> "$target"
      fi
      ok "已同步组件运行时变量 MODELS_HOME -> ${SHARED_ENV}"
    fi
  fi
}

stored_fingerprint() {
  local marker="${SHARED_VENV}/.fingerprint"
  [[ -f "$marker" ]] && cat "$marker" || echo "none"
}

#: 原子切换 symlink。
#: 先写临时链接再 mv 覆盖：mv -T 是 rename 语义，没有"删旧建新"的中间态，
#: 因此不存在服务在切换瞬间找不到目录的情况。
switch_release() {
  local target="$1"
  local dir; dir="$(release_dir "$target")"
  [[ -d "$dir" ]] || die "版本目录不存在: $dir"

  ln -sfn "$dir" "${CURRENT_LINK}.tmp"
  mv -T "${CURRENT_LINK}.tmp" "$CURRENT_LINK"
  ok "已切换 current -> ${target}"
}

# ---------------------------------------------------------------------------
# venv
# ---------------------------------------------------------------------------
venv_python() {
  local rel="$1"
  local py="${SHARED_VENV}/bin/python"
  [[ -x "$py" ]] || die "共享 venv 不存在: ${py}（先执行 install.sh）"
  echo "$py"
}

#: 依赖环境。venv 里**只有第三方依赖，不放本项目代码**。
#:
#: 三个刻意的设计，每一个都对应一个真实踩过的坑：
#:
#: 1. 用 ``uv sync --all-groups`` 而不是 ``uv pip install -e .``。
#:    后者只装 ``[project.dependencies]``，**不含 ``[dependency-groups] dev``** ——
#:    目标机上因此没有 pytest，而部署流程明确要求在切换版本前跑 L0/L1
#:    （install.sh 第 7 步），闸门直接无法执行。
#:
#: 2. ``--no-install-project``：项目本体**不装进 venv**，代码靠 unit 里的
#:    ``PYTHONPATH=%h/current/src`` 从 release 目录加载。
#:    若用 editable 安装，venv 里记的是**某个 release 的绝对路径**，
#:    而 venv 跨版本共享 —— 切 symlink 根本换不掉代码，回退会继续跑新版本。
#:    这样一来 symlink 才真正是"当前跑哪份代码"的唯一事实来源，
#:    回退也才真的只是切链接 + 重启。
#:
#: 3. 指纹未变时**照样执行一次 sync**。依赖没变时 uv 是增量的，只需重写
#:    少量元数据，通常数秒完成；换来的是"venv 状态与 lockfile 一致"这个不变式。
#:
#: semver 相应地从环境变量来（项目没装进 venv，dist metadata 取不到），
#: 见 :func:`export_release_env`。
ensure_venv() {
  local rel="$1"
  local py="${SHARED_VENV}/bin/python"

  if [[ ! -x "$py" ]]; then
    log "首次创建 venv（约需数分钟）"
    rm -rf "$SHARED_VENV"
    uv venv "$SHARED_VENV" --python 3.12
  fi

  local want; want="$(venv_fingerprint "$rel")"
  local have; have="$(stored_fingerprint)"
  if [[ "$want" == "$have" ]]; then
    ok "依赖指纹未变（${want}），增量同步"
  else
    log "依赖指纹 ${have} -> ${want}，同步依赖（约需数分钟）"
  fi

  # --frozen：不允许现场改 lockfile。lock 与 pyproject 不一致时宁可失败，
  # 也不要悄悄装一套和本地测过的不同的依赖。
  # --extra ml：B5 起目标机 venv 安装 ML 检测器重依赖（Presidio /
  # sentence-transformers / spacy，含 torch）。本地 `uv sync` 不带该 extra，
  # 保持 Windows 轻量 —— 依赖差异正是 D1/D2/D7 推迟项的载体。
  # 该 extra 只在 pyproject 声明了才带：**B5 之前的版本没有 ml extra**，
  # 回退到旧版本时若仍带 --extra ml，uv sync 会报 "Extra `ml` is not
  # defined"，手动回退（rollback.sh）直接挂掉。按目标 release 的 pyproject
  # 能力决定，而不是按"当前 common.sh 的能力"。
  local sync_args=(--all-groups --frozen --no-install-project)
  if grep -qE '^ml[[:space:]]*=' "${rel}/pyproject.toml" 2>/dev/null; then
    sync_args+=(--extra ml)
  fi
  ( cd "$rel" && UV_PROJECT_ENVIRONMENT="$SHARED_VENV" \
      uv sync "${sync_args[@]}" )

  printf '%s' "$want" > "${SHARED_VENV}/.fingerprint"
  ok "venv 就绪（仅含依赖，代码由 PYTHONPATH 从 release 加载）"
}

# ---------------------------------------------------------------------------
# 测试执行
# ---------------------------------------------------------------------------

#: 执行指定测试层。L0/L1 离线；L2/L3 需要服务在跑。
#: 关键约定：**L0/L1 在切换 current 之前跑**。此时失败可以零影响退出，
#: 服务仍指向上一个稳定版本，不需要任何回退动作。
run_layer() {
  local rel="$1" layer="$2"
  local py; py="$(venv_python "$rel")"
  local -a extra=()

  case "$layer" in
    unit)        extra=(-m unit) ;;
    integration) extra=(-m integration) ;;
    smoke)       extra=(-m target tests/smoke --allow-network) ;;
    functional)  extra=(-m target tests/functional --allow-network) ;;
    *) die "未知测试层: $layer" ;;
  esac

  log "执行 ${layer} ..."
  ( cd "$rel" && "$py" -m pytest "${extra[@]}" -q )
}

#: 执行在线测试层，失败自动重试一次。
#:
#: 为什么必须重试：L2/L3 打的是**真实模型**，而 ``temperature=1.0`` 让输出
#: 天然不确定。实测出现过一次 —— FT-01（"用两句话介绍你们的定期存款产品"，
#: 最基础的正常查询）在某次发布里被判成 ``escalate``，脚本据此把一个
#: **完全健康的版本自动回退了**；但同一句话随后连问三次全是 ``allow``。
#: 那不是代码回归，是模型这一次恰好说出了触发规则的话。
#:
#: 而且**事后查不出来**：`capture_prompts=false`（银行场景的隐私取舍）
#: 意味着失败的模型输出没有留存，trace 里也没有。不可复现的失败无法归因。
#:
#: 权衡：误回退的代价明显大于多跑一次测试 —— 好版本被撤下、故障排查方向
#: 被带偏。所以重试通过就只**告警并明确记为偶发**，不回退；再失败才是真问题。
#: 偶发本身不会被藏起来，它会留在部署日志里。
run_online_layer_with_retry() {
  local rel="$1" layer="$2" version="$3" retries="${4:-1}"
  local i
  for ((i = 0; i <= retries; i++)); do
    # AGENT_HOME 一起传进去：账本类断言需要知道"部署根在哪"，
    # 让测试自己硬编码目标机路径不如从部署流程拿同一份值。
    if AGENT_HOME="$AGENT_HOME" AGENT_BASE_URL="$BASE_URL" EXPECTED_VERSION="$version" \
       run_layer "$rel" "$layer"; then
      if (( i > 0 )); then
        warn "${layer} 第 $((i + 1)) 次尝试通过 —— 判定为模型输出偶发波动，本次不触发回退"
      fi
      return 0
    fi
    if (( i < retries )); then
      warn "${layer} 未通过，将重试（第 $((i + 1))/$((retries + 1)) 次）"
      warn "（在线测试打真实模型，输出不确定；单次失败不足以判定为回归）"
    fi
  done
  return 1
}
# ---------------------------------------------------------------------------
# systemd user unit
# ---------------------------------------------------------------------------

#: 安装/刷新 systemd user unit。
#:
#: Args:
#:   1: unit 模板路径。默认用本仓库的模板；调用方**应当传目标 release 的**，
#:      这样回退时 unit 描述的才是真正在跑的那一份。
#:
#: 放在 :func:`install_systemd_unit` 意义上的公共库、而不是 install.sh 里的原因：
#: **unit 也会随版本变**。模板新增一个 ``Environment=``（例如 B4 的
#: ``MINIMAX_AGENT_AUDIT__ROOT``）时，如果只有 install.sh 会渲染，
#: update.sh 切完 symlink 直接重启，跑的还是目标机上那份**旧 unit** ——
#: 新配置静默不生效、没有任何报错、服务照常健康。这种"以为生效了其实没有"
#: 的失败最难查，所以装/更/退三条路径都必须重新渲染。
install_systemd_unit() {
  local template="${1:-${DEPLOY_DIR}/systemd/minimax-agent.service.template}"
  [[ -f "$template" ]] || die "找不到 unit 模板: ${template}"

  local unit_dir="${HOME}/.config/systemd/user"
  mkdir -p "$unit_dir"
  # 模板里的 %h 会被 systemd 展开为**家目录**，而 AGENT_HOME 固定为
  # /data/workspace/minimax-agent。因此额外生成一个软链接指向它，
  # 让 unit 模板保持与家目录无关。
  ln -sfn "$AGENT_HOME" "${HOME}/.minimax-agent-home"

  local target="${unit_dir}/${SERVICE_NAME}"
  # 先渲染到临时文件再原子替换：半截的 unit 会让 systemd 载入失败，
  # 服务直接起不来 —— 那比"配置没更新"严重得多。
  sed 's|%h|'"${HOME}"'/.minimax-agent-home|g' "$template" > "${target}.tmp"
  mv -f "${target}.tmp" "$target"
  ok "已刷新 unit: ${target}"
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE_NAME" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# 服务控制（systemd user，无 sudo）
# ---------------------------------------------------------------------------

service_restart() {
  systemctl --user daemon-reload
  systemctl --user restart "$SERVICE_NAME"
  ok "服务已重启: ${SERVICE_NAME}"
}

service_status_line() {
  systemctl --user is-active "$SERVICE_NAME" 2>/dev/null || echo "inactive"
}

#: 等待 /healthz 就绪。服务刚起时立刻探测会失败，那不是发布失败。
wait_healthy() {
  local tries="${1:-30}"
  local i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS --max-time 3 "${BASE_URL}/healthz" >/dev/null 2>&1; then
      ok "服务就绪（第 ${i} 次探测）"
      return 0
    fi
    sleep 1
  done
  warn "等待 ${tries} 秒后 /healthz 仍不可达"
  return 1
}

# ---------------------------------------------------------------------------
# 状态记录
# ---------------------------------------------------------------------------
#: 极简 JSON 状态记录。不引入 jq（目标机没有），格式刻意保持一行便于 grep。
record_state() {
  local version="$1" status="$2" detail="${3:-}"
  local stamp; stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$STATE_DIR"
  # 保留最近 20 条历史；最后一行是当前状态
  {
    printf '{"stamp":"%s","version":"%s","status":"%s","detail":"%s"}\n' \
      "$stamp" "$version" "$status" "$detail"
  } >> "${STATE_DIR}/history.jsonl"
  tail -n 20 "${STATE_DIR}/history.jsonl" > "${STATE_DIR}/history.jsonl.tmp"
  mv "${STATE_DIR}/history.jsonl.tmp" "${STATE_DIR}/history.jsonl"

  cat > "$STATE_FILE" <<EOF
{
  "last_stable": "$(current_release || echo "")",
  "last_attempt": "$version",
  "last_status": "$status",
  "updated_at": "$stamp"
}
EOF
}

cleanup_releases() {
  local keep="${KEEP_RELEASES}"
  local current previous
  current="$(current_release || true)"
  previous="$(previous_release || true)"

  local d name
  while IFS= read -r d; do
    name="$(basename "$d")"
    [[ "$name" == "$current" || "$name" == "$previous" ]] && continue
    rm -rf "$d"
    log "清理旧版本: ${name}"
  done < <(find "$RELEASES_DIR" -maxdepth 1 -mindepth 1 -type d | sort -r | tail -n +$((keep + 1)))
}

__common_loaded=1
