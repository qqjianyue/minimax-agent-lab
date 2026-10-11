#!/usr/bin/env bash
# spaCy 模型（D2）搭建 / 校验脚本
#
# 用法（在目标机上执行）：
#   ./setup.sh install        安装 / 升级（装进 shared/venv，幂等：同版本已装则跳过）
#   ./setup.sh install --force 忽略已装版本强制重装
#   ./setup.sh verify         校验每个模型都能真正 load（spacy.load 成功 + 版本匹配）
#   ./setup.sh status         查看状态
#
# 设计约束：
# - 装进 **shared/venv**（与 torch 等大依赖同一 venv，跨版本共享、指纹复用兼容）。
# - 免 sudo、幂等、不静默生成配置。
# - 校验以"能真正 load"为准：pip 显示已装不算数，spacy.load 失败即脚本失败，
#   不留"装了但用不了"的状态。
# - **多模型**：B4 起有 en / zh 两个 spaCy 模型（B4 关卡 0 实测中文召回率
#   33.3%，换 zh 模型后升到 100%）。逐个安装、逐个校验。
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

require_tools() {
  # uv 创建的 venv 默认不带 pip（No module named pip），统一走 uv pip install。
  # 非交互 ssh 会话 PATH 常缺 ~/.local/bin（uv 默认安装位置），先补再查。
  export PATH="${HOME}/.local/bin:${PATH}"
  # 同样地，非交互会话不加载 .bashrc，UV_CACHE_DIR 的声明不生效；
  # 必须自己设 —— 否则缓存落到 $HOME/.cache/uv，在 /home 很小的机器上
  # 下载大 wheel（如 torch）会 No space left on device。
  local uv_cache="${UV_CACHE_DIR:-/data/workspace/.cache/uv}"
  mkdir -p "${uv_cache}"
  export UV_CACHE_DIR="${uv_cache}"
  for cmd in uv python3; do
    command -v "$cmd" >/dev/null 2>&1 || die "未找到 ${cmd}（需要 uv：curl -LsSf https://astral.sh/uv/install.sh | sh）"
  done
}

venv_python() { echo "${AGENT_HOME}/shared/venv/bin/python"; }

# 从 manifest.json 读字段（标准库 json，目标机唯一保证存在的解析器）
# 注意：dict 必须用下标访问（d["models"][0]["package"]），不能点号访问。
#
# 两种形态都支持：
#   manifest_get .import_name            —— 取第一个模型（单模型时代的调用方）
#   manifest_get .import_name <index>    —— 取第 index 个模型
#   manifest_get models.count            —— 模型个数
manifest_get() {
  python3 -c 'import json,sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
key = sys.argv[2].lstrip(".")
if key == "models.count":
    print(len(d["models"]))
else:
    idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    print(d["models"][idx][key])' "${MANIFEST}" "$1" "${2:-0}"
}

# 某个模型已装的版本；未装返回空串。
installed_version() {
  "$(venv_python)" -c "import ${1}; print(${1}.__version__)" 2>/dev/null || echo ""
}

# --- 子命令 -------------------------------------------------------------

do_install() {
  require_env_file
  require_tools
  load_env

  local force=0
  [[ "${1:-}" == "--force" ]] && force=1

  [[ -n "${AGENT_HOME:-}" ]] || die "env 中缺少 AGENT_HOME"
  local venv; venv="$(venv_python)"
  [[ -x "${venv}" ]] || die "venv 不存在或不可执行: ${venv}（请先完成应用部署）"

  local count; count="$(manifest_get models.count)"
  local i
  for (( i=0; i<count; i++ )); do
    _install_one "$i" "$force"
  done

  ok "全部模型安装完成并通过加载校验"
}

_install_one() {
  local idx="$1" force="$2"
  local pkg version import_name
  pkg="$(manifest_get .package "$idx")"
  version="$(manifest_get .version "$idx")"
  import_name="$(manifest_get .import_name "$idx")"

  info "── ${pkg}==${version} (lang=$(manifest_get .lang_code "$idx"))"

  if [[ $force -eq 0 ]]; then
    local current
    current="$(installed_version "${import_name}")"
    if [[ -n "${current}" && "${current}" == "${version}" ]]; then
      ok "${import_name} 已安装 ${current}，跳过（--force 可强制重装）"
      return 0
    fi
    [[ -n "${current}" ]] && info "当前版本 ${current} ≠ 目标 ${version}，升级"
  fi

  # spaCy 模型包不发 PyPI：官方分发是 GitHub Releases 的 wheel。
  # 目标机 GitHub 不可达时，可把 PIP_INDEX_URL 换成镜像源，或预置
  # MODEL_WHEEL_URL_<index> 指向可达镜像/本地文件。
  # 逐模型覆盖用 MODEL_WHEEL_URL_EN / _ZH（见 env.template）。
  local override="" wheel_url
  case "$(manifest_get .lang_code "$idx")" in
    en) override="${MODEL_WHEEL_URL_EN:-}" ;;
    zh) override="${MODEL_WHEEL_URL_ZH:-}" ;;
  esac
  wheel_url="${override:-https://github.com/explosion/spacy-models/releases/download/${import_name}-${version}/${import_name}-${version}-py3-none-any.whl}"

  if [[ -n "${PIP_INDEX_URL:-}" ]]; then
    uv pip install --python "$(venv_python)" --index-url "${PIP_INDEX_URL}" "${wheel_url}"
  else
    uv pip install --python "$(venv_python)" "${wheel_url}"
  fi

  _verify_one "$idx"
}

do_verify() {
  require_env_file
  load_env

  [[ -n "${AGENT_HOME:-}" ]] || die "env 中缺少 AGENT_HOME"
  local venv; venv="$(venv_python)"
  [[ -x "${venv}" ]] || die "venv 不存在或不可执行: ${venv}"

  local count; count="$(manifest_get models.count)"
  local i
  for (( i=0; i<count; i++ )); do
    _verify_one "$i"
  done
}

_verify_one() {
  local idx="$1"
  local expected model
  expected="$(manifest_get .version "$idx")"
  model="$(manifest_get .import_name "$idx")"
  info "加载 ${model}（期望版本 ${expected}）..."
  local actual
  actual="$("$(venv_python)" - "${model}" <<'PY' || die "spacy.load 失败 —— 安装不完整或依赖缺失（中文模型还需要 spacy-pkuseg>=1.0.1）"
import sys
try:
    import spacy
    model = sys.argv[1]
    nlp = spacy.load(model)
    # 模型包按惯例暴露 __version__（不是 version —— 踩过：
    # 访问 .version 会 AttributeError，被误判成加载失败）
    print(__import__(model).__version__)
    # 走一次真实解析，确认管线可用而不只是可 import。
    # 中英文各来一句：中文模型缺 spacy-pkuseg 时会在这一步才炸。
    doc = nlp("The quick brown fox jumps over the lazy dog.")
    assert len(doc) > 0
    doc = nlp("客户张伟在北京办理定期存款业务")
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
  local count; count="$(manifest_get models.count)"
  local i import_name current
  for (( i=0; i<count; i++ )); do
    import_name="$(manifest_get .import_name "$i")"
    current="$(installed_version "${import_name}" || true)"
    printf '  %-18s 已装: %-10s 期望: %s\n' \
      "${import_name}" "${current:-未安装}" "$(manifest_get .version "$i")"
  done
  if bash "${SCRIPT_DIR}/setup.sh" verify >/dev/null 2>&1; then
    echo "  可加载:   全部 OK"
  else
    echo "  可加载:   有失败（重跑 ./setup.sh install --force）"
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
