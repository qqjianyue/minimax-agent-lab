#!/usr/bin/env bash
# 部署状态总览
#
# 排查问题的第一入口：当前跑的是哪个版本、上一次发布结果如何、
# 服务活没活、最近的测试与审计统计。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

section() { printf '\n%s\n' "── $* ────────────────────────────────────"; }

main() {
  echo "=========================================="
  echo " minimax-agent 部署状态"
  echo " AGENT_HOME=${AGENT_HOME}"
  echo "=========================================="

  section "版本"
  local cur prev
  cur="$(current_release || echo '<未部署>')"
  prev="$(previous_release || echo '<无>')"
  printf '  当前版本: %s\n' "$cur"
  printf '  回退目标: %s\n' "$prev"
  printf '  版本数量: %s（保留最近 %s 个）\n' \
    "$(find "$RELEASES_DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)" "$KEEP_RELEASES"

  section "服务"
  printf '  systemd:   %s\n' "$(service_status_line)"
  printf '  地址:      %s\n' "$BASE_URL"

  local health="unreachable" version=""
  if curl -fsS --max-time 3 "${BASE_URL}/healthz" >/dev/null 2>&1; then
    health="ok"
    if command -v python3 >/dev/null 2>&1; then
      version="$(curl -fsS --max-time 3 "${BASE_URL}/healthz" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"]["version"])' 2>/dev/null || echo "")"
    fi
  fi
  printf '  /healthz:  %s\n' "$health"
  [[ -n "$version" ]] && printf '  运行版本:  %s\n' "$version"

  if [[ "$health" != "ok" ]]; then
    printf '  排查:      tail -50 %s\n' "${LOG_DIR}/stderr.log"
  fi

  section "非密钥配置（shared/.env）"
  if [[ -f "$SHARED_ENV" ]]; then
    echo "  位置:   $SHARED_ENV"
    echo "  权限:   $(stat -c '%a' "$SHARED_ENV" 2>/dev/null || echo '?')"
  else
    echo "  缺失:   $SHARED_ENV"
  fi
  echo "  密钥不在这里 —— 见上方「私密配置」区块"

  section "私密配置（mask-config）"
  local mask; mask="$(mask_path)"
  if [[ -z "$mask" ]]; then
    echo "  deploy/config.yaml 未声明 runtime.mask-config"
  elif [[ ! -f "$mask" ]]; then
    echo "  状态:   !! 未部署"
    echo "  目标:   $mask"
    echo "  下发:   deploy/sync-mask.sh --target <user@host>   （在本机执行）"
  else
    echo "  目标:   $mask"
    echo "  大小:   $(wc -c < "$mask") 字节"
    echo "  权限:   $(stat -c '%a' "$mask" 2>/dev/null || echo '?')"
    # 指纹用于判断"是否变化"，不是内容
    echo "  指纹:   $(sha256sum "$mask" | cut -c1-12)"
    local key missing=""
    for key in "${REQUIRED_MASK_KEYS[@]}"; do
      if ! _mask_has_key "$mask" "$key"; then missing="$key"; fi
    done
    if [[ -z "$missing" ]]; then
      echo "  必需项: 齐全"
    else
      echo "  必需项: !! 缺少 $missing"
    fi
    if [[ -f "${mask}.bak" ]]; then
      echo "  备份:   ${mask}.bak 存在"
    fi
  fi
  echo "  （以上均为元信息；脚本与状态输出都不会显示配置内容）"

  section "项目配置（config.yaml）"
  local pc; pc="$(project_config_path)"
  echo "  位置:   $pc（方案 A：随 release 走，不单独下发）"
  local cur_rel; cur_rel="$(current_release || true)"
  if [[ -n "$cur_rel" && -f "${AGENT_HOME}/releases/${cur_rel}/config.yaml" ]]; then
    echo "  状态:   已随 release ${cur_rel} 就位"
    echo "  大小:   $(wc -c < "${AGENT_HOME}/releases/${cur_rel}/config.yaml") 字节"
  else
    echo "  状态:   未找到（当前 release 内无 config.yaml）"
  fi

  section "最近发布"
  if [[ -f "${STATE_DIR}/history.jsonl" ]]; then
    tail -n 8 "${STATE_DIR}/history.jsonl"
  else
    echo "  （无记录）"
  fi

  section "审计统计"
  local audit_dir="${SHARED_DATA}/audit"
  if [[ -d "$audit_dir" ]]; then
    printf '  审计文件数: %s\n' "$(find "$audit_dir" -name '*.jsonl' 2>/dev/null | wc -l)"
    printf '  审计体积:   %s\n' "$(du -sh "$audit_dir" 2>/dev/null | cut -f1)"
  else
    echo "  （B4 批次接入审计账本后可用）"
  fi

  section "基础设施"
  if command -v docker >/dev/null 2>&1; then
    docker ps --filter "name=minimax-agent-phoenix" \
      --format '  {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null || true
    docker ps --filter "name=minimax-agent-phoenix" -q 2>/dev/null | grep -q . \
      || echo "  （Phoenix 未运行；基础设施版本见 infra/BILL_OF_MATERIALS.yaml）"
  else
    echo "  docker 不可用"
  fi
}

main "$@"
