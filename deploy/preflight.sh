#!/usr/bin/env bash
# 部署前置检查（preflight）
#
# 在任何部署动作之前运行。只读，不修改目标机任何状态。
# 任何一项失败都应中止部署 —— 尤其是磁盘空间和端口占用，
# 它们会在部署中途才暴露，届时的清理成本远高于提前拦截。
#
# 用法：./preflight.sh [--strict]
#   --strict  把 WARNING 也视为失败（CI / 首次部署建议开启）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 复用 common.sh 里的路径解析与校验逻辑（避免两处各写一份密钥校验规则）。
# common.sh 会开启 errexit，这里显式关掉：本脚本要**跑完全部检查再汇总**，
# 不能因为第一项失败就不看后面的。
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"
set +e

STRICT=0
[[ "${1:-}" == "--strict" ]] && STRICT=1

AGENT_HOME="${AGENT_HOME:-/data/workspace/minimax-agent}"
MIN_FREE_MB=5120          # 大依赖（torch 等）需要数 GB
PHOENIX_PORT="${PHOENIX_PORT:-6006}"
SERVICE_PORT=8080

FAIL=0
WARN=0

pass() { echo "  [ OK ] $*"; }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN + 1)); }

echo "=========================================="
echo " 部署前置检查  (AGENT_HOME=${AGENT_HOME})"
echo "=========================================="

# --- 1. 基础工具 ---
echo
echo "[1/8] 基础工具"
for cmd in bash git curl tar rsync; do
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "$cmd"
  else
    fail "$cmd 未安装"
  fi
done

if command -v uv >/dev/null 2>&1; then
  pass "uv $(uv --version 2>/dev/null | head -1)"
else
  fail "uv 未安装（依赖安装依赖它；免 sudo 安装：curl -LsSf https://astral.sh/uv/install.sh | sh）"
fi

# --- 2. Python ---
echo
echo "[2/8] Python"
if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    pass "python3 ${PY_VER}（满足 >=3.12）"
  else
    fail "python3 ${PY_VER} 过低，需要 >=3.12"
  fi
  if python3 -c 'import venv' 2>/dev/null; then
    pass "venv 模块可用"
  else
    fail "venv 模块不可用，无法创建隔离环境"
  fi
else
  fail "python3 未安装"
fi

# --- 3. 磁盘空间 ---
echo
echo "[3/8] 磁盘空间"
DF_OUT="$(df -Pm "$(dirname "${AGENT_HOME}")" 2>/dev/null | tail -1)"
if [[ -n "${DF_OUT}" ]]; then
  FREE_MB="$(echo "${DF_OUT}" | awk '{print $4}')"
  MOUNT="$(echo "${DF_OUT}" | awk '{print $6}')"
  if (( FREE_MB >= MIN_FREE_MB )); then
    pass "挂载点 ${MOUNT} 剩余 ${FREE_MB} MB（需 >= ${MIN_FREE_MB}）"
  else
    fail "挂载点 ${MOUNT} 剩余 ${FREE_MB} MB，不足 ${MIN_FREE_MB} MB"
  fi
else
  fail "无法获取 ${AGENT_HOME} 所在分区信息"
fi

# 特别提醒：/home 往往是小分区，项目不要放在家目录下
HOME_FREE_MB="$(df -Pm "${HOME}" 2>/dev/null | tail -1 | awk '{print $4}')"
if [[ -n "${HOME_FREE_MB}" ]] && (( HOME_FREE_MB < MIN_FREE_MB )); then
  warn "家目录所在分区剩余仅 ${HOME_FREE_MB} MB —— 确认部署目录未落在 ${HOME} 下"
fi

# --- 4. 端口占用 ---
echo
echo "[4/8] 端口占用"
check_port() {
  local port="$1" label="$2"
  local owner
  owner="$(ss -tlnp 2>/dev/null | grep -E "[:.]${port}[[:space:]]" || true)"
  if [[ -z "${owner}" ]]; then
    pass "${label} 端口 ${port} 空闲"
    return 0
  fi

  # 更新流程里旧版本的服务**本来就应该在跑** —— 不然就没有"回退"这回事了。
  # 把"被自己的服务占用"报成告警，会让每一次更新都挂一条看不懂的 WARN。
  # 所以先看占用者是不是本服务：是就说明一切正常，不是才需要人管。
  if systemctl --user is-active --quiet "${SERVICE_NAME}" 2>/dev/null \
     && [[ "${owner}" == *"pid="* ]] \
     && curl -fsS --max-time 3 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
    pass "${label} 端口 ${port} 由本服务占用（更新流程中属正常）"
  else
    warn "${label} 端口 ${port} 被占用：${owner}"
  fi
}
check_port "${PHOENIX_PORT}" "Phoenix"
check_port "${SERVICE_PORT}" "agent-service"

# --- 5. 凭据 ---
echo
echo "[5/8] 凭据配置"
SHARED_ENV="${AGENT_HOME}/shared/.env"
if [[ -f "${SHARED_ENV}" ]]; then
  pass "共享配置存在: ${SHARED_ENV}"
  if grep -qE '^[[:space:]]*MINIMAX_AGENT_LLM__API_KEY[[:space:]]*=[[:space:]]*[^[:space:]]' "${SHARED_ENV}"; then
    pass "检测到 API Key 配置项"
  else
    warn "未在 ${SHARED_ENV} 中检测到 MINIMAX_AGENT_LLM__API_KEY（L2/L3 将失败）"
  fi
else
  warn "共享配置不存在: ${SHARED_ENV}（首次部署时创建，勿提交进仓库）"
fi

# --- 6. 网络 ---
echo
echo "[6/8] 外部连通性"
check_http() {
  local url="$1" label="$2"
  local code
  # `|| true` 而不是 `|| echo 000`：curl **拿到响应码之后**仍可能因超时等原因
  # 返回非 0（例如经代理访问几十 MB 的页面），此时 stdout 里已经有有效状态码。
  # 原来的写法会把 200 和 000 拼成 "200000"，报成"返回了 HTTP 200000"。
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${url}" 2>/dev/null)" || true
  case "${code:-000}" in
    200|301|302|401|403) pass "${label} 可达 (HTTP ${code})" ;;
    000) warn "${label} 不可达（网络或代理问题）" ;;
    *)   warn "${label} 返回 HTTP ${code}" ;;
  esac
}
# 用 JSON API 而不是 /simple/：后者是全量项目索引（几十 MB），
# 每次 preflight 都要等它传完才返回，纯粹浪费时间。
check_http "https://pypi.org/pypi/uv/json" "PyPI"
check_http "https://api.minimax.cn/v1/models" "MiniMax API"

# --- 7. 私密配置 ---
# 只做元信息检查：存在、非空、权限、必需键是否**存在**。
# 绝不读取或回显内容 —— verify_mask 内部全部使用 stat / wc / grep -q。
echo
echo "[7/8] 私密配置"
if [[ ! -f "$DEPLOYCONFIG" ]]; then
  warn "deploy/lib/deployconfig.py 不存在，跳过私密配置检查"
elif ! command -v python3 >/dev/null 2>&1; then
  warn "python3 不可用，无法解析 deploy/config.yaml，跳过私密配置检查"
else
  MASK_PATH="$(mask_path)"
  if [[ -z "$MASK_PATH" ]]; then
    warn "deploy/config.yaml 未声明 runtime.mask-config，跳过"
  elif verify_mask "$MASK_PATH"; then
    pass "私密配置就绪: ${MASK_PATH}"
  else
    fail "私密配置未就绪: ${MASK_PATH}"
    warn "  下发命令（在本机执行）: deploy/sync-mask.sh --target <user@host>"
  fi
fi

# --- 8. 部署配置本身 ---
echo
echo "[8/8] 部署配置"
if [[ -f "${DEPLOY_DIR}/config.yaml" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    if python3 "$DEPLOYCONFIG" list >/dev/null 2>&1; then
      pass "deploy/config.yaml 可解析"
      # 只列键名与路径（该文件不含凭据），便于确认配的是哪台机器
      while IFS= read -r line; do pass "  ${line}"; done < <(python3 "$DEPLOYCONFIG" list 2>/dev/null)
    else
      fail "deploy/config.yaml 解析失败"
    fi
  fi
else
  warn "deploy/config.yaml 不存在（部署脚本将使用默认 AGENT_HOME）"
fi

# --- 汇总 ---
echo
echo "=========================================="
if (( FAIL > 0 )); then
  echo " 结果: ${FAIL} 项失败, ${WARN} 项警告 —— 不可部署"
  echo "=========================================="
  exit 1
fi
if (( STRICT == 1 && WARN > 0 )); then
  echo " 结果: 0 项失败, ${WARN} 项警告（--strict 模式视为失败）"
  echo "=========================================="
  exit 1
fi
echo " 结果: 0 项失败, ${WARN} 项警告 —— 可以部署"
echo "=========================================="
exit 0
