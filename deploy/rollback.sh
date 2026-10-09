#!/usr/bin/env bash
# 手动回退
#
# 用法：
#   ./rollback.sh              回退到 previous
#   ./rollback.sh 0.1.0+gabc   回退到指定版本
#   ./rollback.sh --list       列出所有可用版本
#
# 自动回退已由 update.sh 内置；本脚本用于"发布成功、之后才发现问题"的场景。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

list_releases() {
  log "可用版本（新 → 旧）:"
  find "$RELEASES_DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -r | while read -r d; do
    local name; name="$(basename "$d")"
    local mark=" "
    [[ "$name" == "$(current_release || echo)" ]] && mark="*"
    [[ "$name" == "$(previous_release || echo)" ]] && mark="<"
    printf '  %s %s\n' "$mark" "$name"
  done
  log "  * = 当前   < = 回退目标"
}

main() {
  if [[ "${1:-}" == "--list" ]]; then
    list_releases
    return 0
  fi

  local target="${1:-}"
  if [[ -z "$target" ]]; then
    target="$(previous_release || true)"
    [[ -n "$target" ]] || die "没有可回退的历史版本。用 --list 查看现有版本。"
  fi

  local dir; dir="$(release_dir "$target")"
  [[ -d "$dir" ]] || die "版本不存在: ${target}（用 --list 查看）"

  local current; current="$(current_release || echo '<无>')"
  if [[ "$target" == "$current" ]]; then
    die "已经运行在 ${target}，无需回退"
  fi

  log "回退: ${current} -> ${target}"

  # **顺序很重要**：先把依赖环境准备好，再切换 symlink。
  #
  # 若像原先那样先 switch_release 再 ensure_venv，一旦依赖准备失败，脚本会在
  # set -e 下死掉，而 symlink 已经切过去了 —— 结果是 current 指向新版本、
  # 服务却还在跑旧版本（没重启），两者对不上。这种不一致状态最危险：
  # 看起来回退了，其实没回退，而后续 update.sh 会按 current 判断"旧版本"，
  # 决策依据从此是错的。
  # 与 update.sh「L0/L1 放在切换之前」是同一条原则。
  ensure_uv
  ensure_venv "$dir"

  # unit 用**目标 release** 的模板渲染：回退后 unit 描述的应当是真正在跑的
  # 那一份，而不是留下新版本的配置。
  install_systemd_unit "${dir}/deploy/systemd/minimax-agent.service.template"

  # 回退前把当前版本记为 previous：万一这次回退也不对，还能再退回去
  if [[ -n "$current" && "$current" != "<无>" && -d "$(release_dir "$current")" ]]; then
    ln -sfn "$(release_dir "$current")" "${PREVIOUS_LINK}.tmp"
    mv -T "${PREVIOUS_LINK}.tmp" "$PREVIOUS_LINK"
  fi

  switch_release "$target"
  service_restart

  if ! wait_healthy 30; then
    warn "回退后服务不健康。请查看 ${LOG_DIR}/stderr.log"
    record_state "$target" failed "manual rollback, health check failed"
    exit 1
  fi

  record_state "$target" success "manual rollback from ${current}"
  ok "回退完成，当前版本 ${target}"
  log "提示：应用回退不触碰基础设施（Phoenix 等），其版本独立演进"
}

main "$@"
