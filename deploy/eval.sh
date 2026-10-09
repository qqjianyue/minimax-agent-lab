#!/usr/bin/env bash
# C11 eval_harness 目标机真实跑分
#
# 从 **current release** 组装真实 guard pipeline（L1 规则 + L2 Presidio +
# L3 嵌入 + L4 LLM-judge，short_circuit 分层触发），对内置红队集跑分，
# 输出 precision/recall/F1 报告并落盘供历史对比。
#
# 用法（在目标机，repo 副本目录下）：
#   deploy/eval.sh                 # 内置红队集（16 enabled + 2 disabled）
#   deploy/eval.sh <dataset.json>  # 自定义数据集
#
# 报告落在 ${AGENT_HOME}/shared/data/reports/eval-<version>-<ts>.json。
# 这是 B7 的目标机验证入口，不随 update.sh 自动执行（评估打真实模型，
# 有成本与耗时，按需运行）。
#
# 服务环境复刻：eval 进程与 systemd 服务读到同一份配置 ——
# shared/.env（非密钥）+ mask-config.yaml（密钥）+ current/src（代码）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

DATASET_PATH="${1:-}"

main() {
  local cur rel mask
  cur="$(current_release || true)"
  [[ -n "$cur" ]] || die "尚未部署任何版本，先跑 deploy/update.sh"

  rel="$(release_dir "$cur")"
  log "评估目标: current -> ${cur}（${rel}）"

  # --- 服务环境复刻 ---
  if [[ -f "$SHARED_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$SHARED_ENV"
    set +a
  fi
  mask="$(mask_path)"
  export MINIMAX_AGENT_SECRETS_FILE="${SHARED_ENV}"
  export MINIMAX_AGENT_MASK_CONFIG_FILE="${mask:-}"
  export PYTHONPATH="${rel}/src:${PYTHONPATH:-}"
  export PYTHONUNBUFFERED=1

  local py="${SHARED_VENV}/bin/python"
  [[ -x "$py" ]] || die "venv python 不存在: $py"

  local ts out
  ts="$(date +%Y%m%d-%H%M%S)"
  out="${SHARED_DATA}/reports/eval-${cur}-${ts}.json"
  export EVAL_DATASET_PATH="${DATASET_PATH}"
  export EVAL_REPORT_OUT="${out}"

  log "=== C11 eval_harness 真实跑分（pipeline 模式） ==="
  "$py" - <<'PYEOF'
import os
from agent_service.container import build_container
from eval_harness.dataset import builtin_dataset, load_dataset
from eval_harness.runner import EvalRunner
from eval_harness.report import build_report, render_report, save_report

dataset_path = os.environ.get("EVAL_DATASET_PATH", "")
ds = load_dataset(dataset_path or None)
container = build_container()
runner = EvalRunner(container.pipeline)
verdicts = runner.run(ds)
report = build_report(
    target_name=runner.target_name,
    dataset=ds,
    mode=runner.mode,
    verdicts=verdicts,
)
print(render_report(report), flush=True)
out = os.environ["EVAL_REPORT_OUT"]
save_report(report, out)
print(f"[eval] 报告已保存: {out}", flush=True)
PYEOF
  ok "真实红队跑分完成（v${cur}）"
}

# 把参数传进 python heredoc 的环境变量（避免内联脚本拼接引号）
main "$@"
