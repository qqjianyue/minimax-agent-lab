#!/usr/bin/env bash
# 首次安装
#
# 用法（在**目标机**上执行，仓库需已存在于目标机）：
#   ./install.sh /path/to/repo
#   ./install.sh /path/to/repo --with-mask user@host   # 先下发私密配置再安装
#   ./install.sh --mask-only --with-mask user@host      # 只刷新私密配置
#
# 与 update.sh 的区别：install 负责"从无到有"——建目录、装依赖、装 systemd
# unit、渲染配置。update 之后只跑 update.sh，不要重复执行本脚本。
#
# 全程免 sudo：用户在 docker 组，systemd user unit 因 linger 已可用。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

SOURCE_REPO=""
MASK_TARGET=""
MASK_ONLY=0

parse_args() {
  while (($#)); do
    case "$1" in
      --with-mask) MASK_TARGET="${2:-${TARGET:-}}"; shift 2 ;;
      --with-mask=*) MASK_TARGET="${1#*=}"; shift ;;
      --mask-only) MASK_ONLY=1; shift ;;
      -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      -*) die "未知参数: $1" ;;
      *) SOURCE_REPO="$1"; shift ;;
    esac
  done
  [[ -n "$SOURCE_REPO" ]] || SOURCE_REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
}

#: 下发私密配置。委托给 sync-mask.sh —— 它跑在**能访问密钥文件的那台机器**上，
#: 而 install.sh 通常跑在目标机。职责分开后，"当前在哪台机器"始终是明确的。
sync_mask_if_requested() {
  if [[ -z "$MASK_TARGET" ]]; then
    return 0
  fi
  log "下发私密配置 → ${MASK_TARGET}"
  bash "${SCRIPT_DIR}/sync-mask.sh" --target "$MASK_TARGET"
}

main() {
  parse_args "$@"
  if (( MASK_ONLY == 1 )); then
    mask_only
    return 0
  fi
  log "=== 首次安装 ==="
  log "来源: ${SOURCE_REPO}"
  log "目标: ${AGENT_HOME}"

  # 私密配置先落地：preflight 会检查它，顺序反了会误报失败
  sync_mask_if_requested

  # uv 放在 require_cmd 之前装：首次安装的前提就是"什么都没有"，
  # 不能因为缺包管理器而在最前面就退出。
  ensure_uv
  require_cmd git curl python3 systemctl

  # --- 1. 前置检查 ----------------------------------------------------
  step 1 8 "前置检查"
  "${SCRIPT_DIR}/preflight.sh" || die "前置检查未通过，安装中止"

  # --- 2. 目录 --------------------------------------------------------
  step 2 8 "创建目录结构"
  ensure_layout

  # --- 3. 暂存首个版本 ------------------------------------------------
  step 3 8 "暂存发布版本"
  local version rel
  version="$(derive_version "$SOURCE_REPO")"
  # release 身份要剥掉 .dirty：目录一旦暂存就固定了，
  # 留着它会让 release 目录名和冒烟的期望版本与服务自报值对不上。
  version="$(release_identity "$version")"
  rel="$(release_dir "$version")"
  [[ -d "$rel" ]] && die "版本 ${version} 已存在；请改用 update.sh"
  mkdir -p "$rel"
  # 排除不该进 release 的东西
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '.pytest_cache' --exclude '.ruff_cache' \
    --exclude '.coverage' --exclude '.env' \
    "${SOURCE_REPO}/" "${rel}/"
  export_release_env "$version" "$rel"
  ok "已暂存版本 ${version}"

  # --- 4. 共享配置 ----------------------------------------------------
  step 4 8 "准备共享配置（非密钥参数）"
  if [[ -f "$SHARED_ENV" ]]; then
    ok "已存在，保留: ${SHARED_ENV}"
  else
    cp "${SCRIPT_DIR}/env.template" "$SHARED_ENV"
    chmod 600 "$SHARED_ENV"
    ok "已从模板生成 ${SHARED_ENV}"
  fi
  log "密钥不在这里 —— 它在 ${MASK_PATH:-<未配置>}，由 app 直接加载"

  step 4 8 "确认私密配置"
  if mask_is_ready; then
    ok "私密配置已就位: $(mask_path)"
  else
    die "私密配置未就位。请在本机执行：
     deploy/sync-mask.sh --target <user@host>
     或重新运行：./install.sh --with-mask <user@host>"
  fi

  # --- 5. 依赖 --------------------------------------------------------
  step 5 8 "安装依赖"
  ensure_venv "$rel"

  # --- 6. systemd unit ------------------------------------------------
  step 6 8 "注册 systemd user service"
  install_systemd_unit

  # --- 7. 离线测试（切换前）------------------------------------------
  step 7 8 "本地闸门（L0 + L1）"
  run_layer "$rel" unit
  run_layer "$rel" integration
  ok "离线闸门通过"

  # --- 8. 启动与在线测试 ---------------------------------------------
  step 8 8 "切换版本并启动"
  switch_release "$version"
  service_restart
  wait_healthy 30 || die "服务未就绪。日志: ${LOG_DIR}/stderr.log"
  run_online_layers "$rel" "$version"

  record_state "$version" success "install"
  ok ""
  ok "安装完成，当前版本 ${version}"
  log "服务地址: ${BASE_URL}"
  log "查看状态: ${SCRIPT_DIR}/status.sh"
}

step() {
  printf '\n[%s/%s] %s\n' "$1" "$2" "$3"
}

#: 只刷新私密配置：换密钥后单独用，不重跑整个安装
mask_only() {
  log "=== 仅刷新私密配置 ==="
  [[ -n "$MASK_TARGET" ]] || die "请用 --with-mask <user@host> 指定目标机"
  bash "${SCRIPT_DIR}/sync-mask.sh" --target "$MASK_TARGET"
  if systemctl --user is-active "$SERVICE_NAME" >/dev/null 2>&1; then
    log "重启服务以加载新配置…"
    service_restart
    wait_healthy 30 || warn "服务未就绪，请查看 ${LOG_DIR}/stderr.log"
  else
    log "服务当前未运行，无需重启"
  fi
  ok "私密配置已刷新"
}

#: 在线测试层。缺 API Key 时**显式跳过并说明**，不静默通过 ——
#: 一次"全绿"但实际没验证过功能的部署，比一次失败更危险。
run_online_layers() {
  local rel="$1" version="$2"

  if ! mask_is_ready; then
    warn "私密配置未就位 —— 跳过 L2/L3 在线测试"
    warn "这些测试没有被执行过，部署状态应视为'未经在线验证'"
    record_state "$version" "installed-unverified" "mask not ready"
    return 0
  fi

  export AGENT_BASE_URL="$BASE_URL" EXPECTED_VERSION="$version"
  run_online_layer_with_retry "$rel" smoke "$version" || \
    warn "L2 冒烟未通过（已重试），安装记录为待排查"
  run_online_layer_with_retry "$rel" functional "$version" || \
    warn "L3 功能未通过（已重试），安装记录为待排查"
  ok "在线测试结束"
}

main "$@"
