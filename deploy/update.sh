#!/usr/bin/env bash
# 版本更新（含冒烟、功能测试与自动回退）
#
# 用法（在目标机上）：
#   ./update.sh /path/to/repo
#   ./update.sh /path/to/repo --sync-mask user@host   # 发布前刷新私密配置
#
# 私密配置的默认行为：**不随发布更新**。它跨版本不变，每次发布都覆盖只会
# 增加出错面（而且密钥不该由 CI/CD 流程经手）。只有显式 --sync-mask 才刷新。
#
# 完整流程：
#   0. 记录旧版本（回退目标）
#   1. PREFLIGHT     磁盘 / 端口 / 凭据 / 网络；失败即退出，current 不动
#   2. STAGE         暂存新版本到 releases/
#   3. VENV          依赖指纹比对，未变则复用
#   4. PRE-TESTS     在目标机上跑 L0 + L1；失败零影响退出
#   5. SWITCH        原子切换 current 并重启服务
#   6. L2 冒烟       版本号 + 存活 + 最薄端到端
#   7. L3 功能       12 个业务场景
#   8a. 全部通过     previous -> 旧版本，记录 success
#   8b. 任一失败     自动回退到 previous，二次冒烟确认，记录 failed
#
# 为什么 L0/L1 放在切换之前：那时候失败可以直接退出，服务仍指着上一个
# 稳定版本，不需要任何回退动作。把可离线的验证放到切换之后，等于主动制造
# 需要回退的场景。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

SOURCE_REPO=""
MASK_TARGET=""
NO_AUTO_ROLLBACK=0

parse_args() {
  while (($#)); do
    case "$1" in
      --sync-mask) MASK_TARGET="${2:-${TARGET:-}}"; shift 2 ;;
      --sync-mask=*) MASK_TARGET="${1#*=}"; shift ;;
      --no-auto-rollback) NO_AUTO_ROLLBACK=1; shift ;;
      -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      -*) die "未知参数: $1" ;;
      *) SOURCE_REPO="$1"; shift ;;
    esac
  done
  [[ -n "$SOURCE_REPO" ]] || SOURCE_REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
}

OLD_RELEASE=""
NEW_RELEASE=""
ROLLED_BACK=0

main() {
  parse_args "$@"
  log "=== 版本更新 ==="
  log "来源: ${SOURCE_REPO}"

  # 私密配置先刷新：它不参与回退，失败也不该阻断代码发布
  if [[ -n "$MASK_TARGET" ]]; then
    log "刷新私密配置 → ${MASK_TARGET}"
    if ! bash "${SCRIPT_DIR}/sync-mask.sh" --target "$MASK_TARGET"; then
      warn "私密配置刷新失败，继续发布代码（mask 是独立资产，不阻断发布）"
    fi
  fi

  OLD_RELEASE="$(current_release || true)"
  NEW_RELEASE="$(derive_version "$SOURCE_REPO")"
  # release 身份要剥掉 .dirty：目录一旦暂存就固定了。
  # 不剥的话冒烟的 EXPECTED_VERSION 与服务自报版本永远差一个后缀，
  # 版本比对会每次都误判失败，回退机制随之失效。
  NEW_RELEASE="$(release_identity "$NEW_RELEASE")"
  log "旧版本: ${OLD_RELEASE:-<无, 首次安装>}"
  log "新版本: ${NEW_RELEASE}"

  [[ "$NEW_RELEASE" == "$OLD_RELEASE" ]] && die "目标版本与当前版本相同，无需更新"

  # 与 install.sh 保持一致：uv 可能装在 ~/.local/bin 而不在 PATH 上
  # （非交互 ssh 会话尤其如此）。ensure_uv 是幂等的 —— 已装好时只补 PATH。
  ensure_uv
  require_cmd git curl python3 systemctl rsync

  # --- 1. PREFLIGHT ---------------------------------------------------
  step 1 7 "前置检查"
  "${SCRIPT_DIR}/preflight.sh" || die "前置检查未通过，更新中止（current 未改变）"

  # --- 2. STAGE -------------------------------------------------------
  step 2 7 "暂存新版本"
  local rel; rel="$(release_dir "$NEW_RELEASE")"
  rm -rf "$rel"
  mkdir -p "$rel"
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '.pytest_cache' --exclude '.ruff_cache' \
    --exclude '.coverage' --exclude '.env' --exclude '.release-env' \
    "${SOURCE_REPO}/" "${rel}/"
  export_release_env "$NEW_RELEASE" "$rel"
  # 组件运行时变量（如 D1 的 MODELS_HOME）同步进 shared/.env：服务进程的
  # EnvironmentFile 之一，缺了它 SentenceEmbedder 会回退到 HF 联网加载。
  sync_component_env "$rel"
  ok "已暂存 ${NEW_RELEASE}"

  # --- 3. VENV --------------------------------------------------------
  step 3 8 "依赖环境"
  ensure_venv "$rel"

  # --- 4. ML 模型资产（D1/D2，切换前）---------------------------------
  # ensure_venv 的 `uv sync` 会把**不在 uv.lock 里的包**从 venv 移除 ——
  # spacy 模型（en_core_web_lg）按 D2 设计不进 lock（模型与代码独立版本、
  # 由 infra/spacy 单独管理），所以**每次部署后必须重装**，否则 L0/L1 里
  # presidio 检测器会因模型缺失而尝试联网下载（离线闸门下直接失败）。
  # models 不在 venv 里（shared/models），uv sync 不影响它，但同样在这里
  # 做一次完整性校验 —— 模型坏掉时在切换前就暴露，而不是上线后才发现。
  # 两个脚本幂等：已装且版本一致时秒级跳过。
  step 4 8 "ML 模型资产（D1 嵌入模型 / D2 spacy）"
  # env 不进 git：部署时按实际环境从 env.template 生成（幂等，已存在则跳过）
  ensure_component_env "$rel" spacy
  ensure_component_env "$rel" models
  bash "${rel}/infra/spacy/setup.sh" install
  bash "${rel}/infra/models/setup.sh" verify

  # --- 5. PRE-TESTS（切换前，离线）------------------------------------
  step 5 8 "离线闸门（L0 + L1）"
  run_layer "$rel" unit
  run_layer "$rel" integration
  ok "离线闸门通过"

  # --- 6. SWITCH ------------------------------------------------------
  step 6 8 "切换版本并重启"
  # unit 必须跟着 release 一起刷新。模板里的 Environment= 是版本相关资产：
  # 不重渲染就跑的还是目标机上那份旧 unit，新配置静默失效、服务却照常健康
  # （例如 B4 的 MINIMAX_AGENT_AUDIT__ROOT 缺失 → 账本写进 release 目录）。
  # 放在 switch_release 之前，与本文件"准备工作先于切换"的原则一致。
  install_systemd_unit "${rel}/deploy/systemd/minimax-agent.service.template"
  switch_release "$NEW_RELEASE"
  service_restart
  if ! wait_healthy 30; then
    warn "服务未就绪"
    do_rollback "服务启动失败或超时"
    exit 1
  fi
  ok "新版本已上线"

  # --- 7/8. L2 + L3 ---------------------------------------------------
  # 私密配置没就位时，L2/L3 根本无法执行（没有 API Key）。这时**不静默跳过**：
  # 明确警告并把状态记为"未经在线验证"，而不是记成 success ——
  # 一次"全绿"但实际没验证过功能的部署，比一次失败更危险。
  if ! mask_is_ready; then
    warn "私密配置未就位 —— L2/L3 在线测试未被执行"
    warn "这不视为通过。部署状态标记为 '未经在线验证'。"
    warn "下发命令（本机执行）: deploy/sync-mask.sh --target <user@host>"
    record_state "$NEW_RELEASE" "unverified" "mask not ready, L2/L3 skipped"
    ok ""; ok "代码已发布（未经在线验证）: ${NEW_RELEASE}"
    return 0
  fi

  # 两者都打真实模型，输出不确定，所以走带重试的版本（见 common.sh 的说明）
  step 7 8 "L2 冒烟测试"
  if ! run_online_layer_with_retry "$rel" smoke "$NEW_RELEASE"; then
    do_rollback "L2 冒烟测试失败（已重试）"
    exit 1
  fi

  step 8 8 "L3 功能测试"
  if ! run_online_layer_with_retry "$rel" functional "$NEW_RELEASE"; then
    do_rollback "L3 功能测试失败（已重试）"
    exit 1
  fi

  # --- 8. 成功路径 ----------------------------------------------------
  if [[ -n "$OLD_RELEASE" && -d "$(release_dir "$OLD_RELEASE")" ]]; then
    ln -sfn "$(release_dir "$OLD_RELEASE")" "${PREVIOUS_LINK}.tmp"
    mv -T "${PREVIOUS_LINK}.tmp" "$PREVIOUS_LINK"
    ok "previous -> ${OLD_RELEASE}"
  fi

  record_state "$NEW_RELEASE" success "update"
  cleanup_releases
  ok ""
  ok "更新完成：${NEW_RELEASE}"
}

# ---------------------------------------------------------------------------
step() { printf '\n[%s/%s] %s\n' "$1" "$2" "$3"; }

# ---------------------------------------------------------------------------
do_rollback() {
  local reason="$1"

  echo
  warn "===================== 自动回退 ====================="
  warn "原因: ${reason}"

  if (( NO_AUTO_ROLLBACK == 1 )); then
    warn "--no-auto-rollback 已指定，跳过自动回退。"
    warn "当前 current 指向 ${NEW_RELEASE}，服务可能不可用。"
    warn "请手工执行: ${SCRIPT_DIR}/rollback.sh"
    record_state "$NEW_RELEASE" failed "${reason} (rollback skipped)"
    return 0
  fi

  # 回退目标是**本次更新开始前正在跑的那个版本**（OLD_RELEASE），
  # 不是 previous 链接。
  #
  # previous 只在**成功路径**上更新（第 8 步）。拿它当回退目标会出错：
  # 连续两次更新时，第二次失败会退到"上上次"的版本，而不是刚刚被替换掉的那个。
  # 首次 install 之后 previous 甚至根本不存在 —— 那正是这次遇到的情况：
  # 明明 0.1.0 正在跑，回退却报"没有可回退的历史版本"。
  local target="$OLD_RELEASE"
  if [[ -z "$target" ]]; then
    target="$(previous_release || true)"
  fi
  if [[ -z "$target" ]] || [[ ! -d "$(release_dir "$target")" ]]; then
    warn "没有可回退的历史版本（首次安装失败）。"
    warn "请手工排查: ${LOG_DIR}/stderr.log"
    record_state "$NEW_RELEASE" failed "${reason} (no rollback target)"
    return 0
  fi

  log "回退目标: ${target}"
  switch_release "$target"
  service_restart

  if wait_healthy 30; then
    ok "回退成功，当前版本 ${target}"
    ROLLED_BACK=1
    record_state "$NEW_RELEASE" failed "${reason} -> rolled back to ${target}"
  else
    warn "回退后服务仍不健康！需要人工介入。"
    warn "日志: ${LOG_DIR}/stderr.log"
    record_state "$NEW_RELEASE" failed "${reason} -> ROLLBACK ALSO FAILED"
  fi

  # 失败的版本目录**保留**，便于离线排查；只是不再被指向
  log "失败版本保留在 ${NEW_RELEASE}，供排查使用"
}

main "$@"
