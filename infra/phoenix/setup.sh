#!/usr/bin/env bash
# Phoenix 搭建 / 升级脚本
#
# 用法：
#   ./setup.sh install     首次安装
#   ./setup.sh upgrade     升级镜像并重建容器
#   ./setup.sh status      查看状态
#   ./setup.sh down        停止（保留数据）
#
# 设计约束：
# - 免 sudo。目标机用户已在 docker 组，脚本不使用任何需要 root 的操作。
# - 幂等。重复执行 install 不会破坏已有数据卷。
# - 不静默生成配置。env 缺失时报错并给出复制命令，而不是替用户填默认值。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/env"
COMPOSE_FILE="${SCRIPT_DIR}/compose.yaml"

# shellcheck disable=SC1090
load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ENV_FILE}"
    set +a
  fi
}

die() { echo "[phoenix] ERROR: $*" >&2; exit 1; }
ok()  { echo "[phoenix] OK: $*"; }
info() { echo "[phoenix] $*"; }

require_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    die "缺少配置文件 ${ENV_FILE}
请先执行： cp $(basename "${SCRIPT_DIR}")/env.template ${ENV_FILE}
并按机器修改其中的 AGENT_HOME 等值。"
  fi
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker"
  docker info >/dev/null 2>&1 || die "docker 守护进程不可用（可能权限不足或服务未启动）"
  docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件"
}

compose() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

do_install() {
  require_env_file
  require_docker
  load_env

  info "AGENT_HOME=${AGENT_HOME:-未设置}"
  info "镜像=${PHOENIX_IMAGE:-arizephoenix/phoenix}:${PHOENIX_IMAGE_TAG:-latest}"

  mkdir -p "${AGENT_HOME}/shared/data/phoenix"
  ok "数据目录就绪: ${AGENT_HOME}/shared/data/phoenix"

  info "拉取镜像（首次可能较慢，目标机走镜像加速器）..."
  compose pull

  info "启动容器..."
  compose up -d
  ok "容器已启动，等待健康检查..."
}

do_upgrade() {
  require_env_file
  require_docker
  load_env

  local before
  before="$(compose images -q phoenix 2>/dev/null || echo 'unknown')"

  info "拉取新镜像并重建..."
  compose pull
  compose up -d

  local after
  after="$(compose images -q phoenix 2>/dev/null || echo 'unknown')"
  if [[ "${before}" == "${after}" ]]; then
    info "镜像未变化（tag 指向同一镜像），仅重建容器"
  else
    ok "镜像已更新: ${before} -> ${after}"
  fi
  info "提醒：确认无误后，把 env.template 中的 PHOENIX_IMAGE_TAG 固定为具体版本，"
  info "      并同步更新 infra/BILL_OF_MATERIALS.yaml 的 version 字段"
}

do_status() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    info "未配置（无 env 文件），当前状态未知"
    return 0
  fi
  load_env
  docker ps --filter "name=minimax-agent-phoenix" \
    --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || true
}

do_down() {
  require_env_file
  require_docker
  info "停止容器（数据卷保留）..."
  compose down
  ok "已停止"
}

case "${1:-install}" in
  install) do_install ;;
  upgrade) do_upgrade ;;
  status)  do_status ;;
  down)    do_down ;;
  *)
    die "未知子命令: $1（可用: install / upgrade / status / down）"
    ;;
esac
