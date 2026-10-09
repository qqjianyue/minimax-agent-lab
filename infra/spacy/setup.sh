#!/usr/bin/env bash
# spaCy 模型（D2）搭建 / 校验脚本
#
# 用法（在目标机上执行）：
#   ./setup.sh install        安装 / 升级（装进 shared/venv，幂等：同版本已装则跳过）
#   ./setup.sh install --force 忽略已装版本强制重装
#   ./setup.sh verify         校验可加载（spacy.load 成功 + 版本匹配）
#   ./setup.sh status         查看状态
#
# 设计约束：
# - 装进 **shared/venv**（与 torch 等大依赖同一 venv，跨版本共享、指纹复用兼容）。
# - 免 sudo、幂等、不静默生成配置。
# - 校验以"能真正 load"为准：pip 显示已装不算数，spacy.load 失败即脚本失败，
#   不留"装了但用不了"的状态。
# - pip 源受限时在 env 配 PIP_INDEX_URL（如清华 TUNA 镜像）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/env"
MANIFEST="${SCRIPT_DIR}/manifest.json"

# shellcheck disable=SC1090
load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ENV_FILE}"
    set +a
  fi
}

die() { echo "[spacy] ERROR: $*" >&2; exit 1; }
ok()  { echo "[spacy] OK: $*"; }
info() { echo "[spacy] $*"; }

require_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    die "缺少配置文件 ${ENV_FILE}
请先执行： cp $(basename "${SCRIPT_DIR}")/env.template ${ENV_FILE}
并按机器修改其中的 AGENT_HOME / PIP_INDEX_URL。"
  fi
}

venv_python() { echo "${AGENT_HOME}/shared/venv/bin/python"; }

# 从 manifest.json 读字段（标准库 json，目标机唯一保证存在的解析器）
manifest_get() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8"))["model"]; print(d'$1')' "${MANIFEST}"
}

installed_version() {
  "$(venv_python)" -c 'import en_core_web_lg; print(en_core_web_lg.version)' 2>/dev/null || echo ""
}

# --- 子命令 -------------------------------------------------------------

do_install() {
  require_env_file
  load_env

  local force=0
  [[ "${1:-}" == "--force" ]] && force=1

  [[ -n "${AGENT_HOME:-}" ]] || die "env 中缺少 AGENT_HOME"
  local venv; venv="$(venv_python)"
  [[ -x "${venv}" ]] || die "venv 不存在或不可执行: ${venv}（请先完成应用部署）"

  local pkg version
  pkg="$(manifest_get .package)"
  version="$(manifest_get .version)"
  [[ -z "${SPACY_MODEL_VERSION:-}" || "${SPACY_MODEL_VERSION}" == "${version}" ]] \
    || die "env 的 SPACY_MODEL_VERSION(${SPACY_MODEL_VERSION}) 与 manifest.version(${version}) 不一致"

  if [[ $force -eq 0 ]]; then
    local current
    current="$(installed_version)"
    if [[ -n "${current}" && "${current}" == "${version}" ]]; then
      ok "已安装 ${current}，跳过（--force 可强制重装）"
      return 0
    fi
    [[ -n "${current}" ]] && info "当前版本 ${current} ≠ 目标 ${version}，升级"
  fi

  info "安装 ${pkg}==${version} 到 ${venv} ..."
  if [[ -n "${PIP_INDEX_URL:-}" ]]; then
    "$(venv_python)" -m pip install --index-url "${PIP_INDEX_URL}" "${pkg}==${version}"
  else
    "$(venv_python)" -m pip install "${pkg}==${version}"
  fi

  # 安装成功 ≠ 可用：必须能 load 才算数
  do_verify
  ok "安装完成并验证可加载"
}

do_verify() {
  require_env_file
  load_env

  [[ -n "${AGENT_HOME:-}" ]] || die "env 中缺少 AGENT_HOME"
  local venv; venv="$(venv_python)"
  [[ -x "${venv}" ]] || die "venv 不存在或不可执行: ${venv}"

  local expected model
  expected="$(manifest_get .version)"
  model="$(manifest_get .import_name)"
  info "加载 ${model}（期望版本 ${expected}）..."
  local actual
  actual="$("$(venv_python)" - "${model}" <<'PY' || die "spacy.load 失败 —— 安装不完整或依赖缺失"
import sys
try:
    import spacy
    model = sys.argv[1]
    nlp = spacy.load(model)
    # 模型包按惯例带 version 属性；拿不到就算加载失败
    print(__import__(model).version)
    # 走一次真实解析，确认管线可用而不只是可 import
    doc = nlp("The quick brown fox jumps over the lazy dog.")
    assert len(doc) > 0
except Exception as exc:  # noqa: BLE001
    print(f"load-error: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
PY
)"
  if [[ "${actual}" != "${expected}" ]]; then
    die "版本不匹配：期望 ${expected}，实际 ${actual}"
  fi
  ok "可加载，版本 ${actual}"
}

do_status() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    info "未配置（无 env 文件）。先 cp env.template env 再执行 ./setup.sh install"
    return 0
  fi
  load_env
  local current
  current="$(installed_version || true)"
  if [[ -z "${current}" ]]; then
    info "未安装。执行 ./setup.sh install"
    return 0
  fi
  echo "  已装版本: ${current}"
  echo "  期望版本: $(manifest_get .version)"
  if bash "${SCRIPT_DIR}/setup.sh" verify >/dev/null 2>&1; then
    echo "  可加载:   OK"
  else
    echo "  可加载:   失败（重跑 ./setup.sh install --force）"
  fi
}

case "${1:-status}" in
  install) do_install "${2:-}" ;;
  verify)  do_verify ;;
  status)  do_status ;;
  *)
    die "未知子命令: $1（可用: install / verify / status）"
    ;;
esac
