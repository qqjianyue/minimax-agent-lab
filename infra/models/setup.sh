#!/usr/bin/env bash
# 嵌入模型（D1）搭建 / 校验脚本
#
# 用法（在目标机上执行）：
#   ./setup.sh install        首次下载 / 更新权重（幂等：已锁定且校验一致则跳过）
#   ./setup.sh install --force 忽略 lock 强制重新下载
#   ./setup.sh verify         完整性校验（比对 manifest.lock.json，fail-closed）
#   ./setup.sh status         查看状态
#
# 设计约束：
# - 目标机**直接下载**（HF_ENDPOINT 镜像备用），不依赖本机中转。
#   目标机网络受限时先配 env 的 HF_ENDPOINT=https://hf-mirror.com。
# - 免 sudo、幂等、不静默生成配置（env 缺失时报错并给出复制命令）。
# - 完整性凭证 = manifest.lock.json：首次 install 成功后生成，写入部署根
#   integrity/models/（${AGENT_HOME}/integrity/models/manifest.lock.json）。
#   **不跟应用版本号**：模型跨版本共享（shared/models），凭证只跟模型身份
#   （repo+revision，锁内嵌）；模型升级时重新生成覆盖，不做历史留档。
#   verify 只认凭证，fail-closed；并校验凭证的模型身份 == manifest 声明。
# - 文件级 sha256 逐文件比对，缺一个文件或 hash 不匹配即失败 ——
#   模型的完整性是 D1 的启动前提（B5 落地 C5 时应用侧校验会复用本脚本）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/env"
MANIFEST="${SCRIPT_DIR}/manifest.json"

# 完整性凭证路径：依赖 env 的 AGENT_HOME（load_env 之后才可求值），故用函数。
# 放在部署根 integrity/ 下（不在 repo 副本 / release 内，rsync --delete 不触及）。
lock_path() { echo "${AGENT_HOME}/integrity/models/manifest.lock.json"; }

# shellcheck disable=SC1090
load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ENV_FILE}"
    set +a
  fi
}

die() { echo "[models] ERROR: $*" >&2; exit 1; }
ok()  { echo "[models] OK: $*"; }
info() { echo "[models] $*"; }

require_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    die "缺少配置文件 ${ENV_FILE}
请先执行： cp $(basename "${SCRIPT_DIR}")/env.template ${ENV_FILE}
并按机器修改其中的 MODELS_HOME / HF_ENDPOINT。"
  fi
}

require_tools() {
  for cmd in curl python3 sha256sum; do
    command -v "$cmd" >/dev/null 2>&1 || die "未找到 ${cmd}"
  done
}

# 从 manifest.json 读字段：$1 = json 路径（如 .model.repo）
# 用 python3 是因为标准库 json 是目标机唯一保证存在的解析器。
# 注意：dict 必须用下标访问（d["model"]["repo"]），不能点号访问。
manifest_get() {
  python3 -c 'import json,sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
v = d
for key in sys.argv[2].lstrip(".").split("."):
    v = v[key]
print(v)' "${MANIFEST}" "$1"
}

# 逐行输出 manifest 中的文件清单（列表字段专用）
manifest_files() {
  python3 -c 'import json,sys; [print(f) for f in json.load(open(sys.argv[1],encoding="utf-8"))["files"]]' "${MANIFEST}"
}

model_dir() {
  # 目录名取 manifest 中 repo 的最后一段（BAAI/bge-base-zh-v1.5 -> bge-base-zh-v1.5）
  echo "${MODELS_HOME}/$(manifest_get .model.repo | awk -F/ '{print $NF}')"
}

# --- 下载与锁定 ---------------------------------------------------------

# $1 = manifest 中的文件相对路径
download_one() {
  local file="$1" url dest
  dest="$(model_dir)/${file}"
  url="${HF_ENDPOINT:-https://huggingface.co}/$(manifest_get .model.repo)/resolve/$(manifest_get .model.revision)/${file}"
  mkdir -p "$(dirname "${dest}")"
  info "  下载 ${file} ..."
  # -C - 断点续传；--retry 对瞬时网络错误重试。-f 让 4xx/5xx 直接失败而非落 HTML。
  curl -fL --retry 3 --connect-timeout 15 --max-time 1200 -C - -o "${dest}" "${url}"
}

write_lock() {
  # 逐文件计算 sha256，生成完整性凭证 manifest.lock.json（与 manifest 声明对齐）
  local lock; lock="$(lock_path)"
  mkdir -p "$(dirname "$lock")"
  python3 - "$(model_dir)" "${MANIFEST}" "$lock" <<'PY'
import json, hashlib, os, sys, datetime

model_dir, manifest_path, lock_path = sys.argv[1], sys.argv[2], sys.argv[3]
manifest = json.load(open(manifest_path, encoding="utf-8"))

def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

files = []
total = 0
for rel in manifest["files"]:
    path = os.path.join(model_dir, rel)
    if not os.path.isfile(path):
        print(f"[models] ERROR: 缺少文件 {rel}", file=sys.stderr)
        sys.exit(1)
    size = os.path.getsize(path)
    total += size
    files.append({"path": rel, "sha256": sha256_of(path), "bytes": size})

lock = {
    "model": manifest["model"],
    "files": files,
    "total_bytes": total,
    "downloaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
with open(lock_path, "w", encoding="utf-8") as fh:
    json.dump(lock, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print(f"[models] OK: 已写入 {lock_path}（{len(files)} 文件，{total/1048576:.0f} MiB）")
PY
}

# --- 子命令 -------------------------------------------------------------

do_install() {
  require_env_file
  require_tools
  load_env

  local force=0
  [[ "${1:-}" == "--force" ]] && force=1

  [[ -n "${MODELS_HOME:-}" ]] || die "env 中缺少 MODELS_HOME"
  local repo revision
  repo="$(manifest_get .model.repo)"
  revision="$(manifest_get .model.revision)"
  [[ -z "${MODEL_NAME:-}" || "${MODEL_NAME}" == "${repo}" ]] \
    || die "env 的 MODEL_NAME(${MODEL_NAME}) 与 manifest.repo(${repo}) 不一致"

  local dir lock; dir="$(model_dir)"; lock="$(lock_path)"
  info "模型: ${repo}@${revision}"
  info "目标目录: ${dir}"

  if [[ $force -eq 0 && -f "$lock" ]]; then
    if bash "${SCRIPT_DIR}/setup.sh" verify >/dev/null 2>&1; then
      ok "已下载且完整性校验通过，跳过（--force 可强制重下）"
      return 0
    fi
    info "凭证存在但校验未通过，重新下载"
  fi

  mkdir -p "${dir}"
  local file
  for file in $(manifest_files); do
    download_one "${file}"
  done
  write_lock
  ok "安装完成。完整性凭证已写入 ${lock}（integrity/，覆盖即更新）"
}

do_verify() {
  require_env_file
  require_tools
  load_env

  local lock; lock="$(lock_path)"
  # 兼容迁移：新位置（integrity/）无凭证、但组件目录有历史遗留 lock → 迁移。
  # 旧 release 里的 lock 保留不动（历史备份）；迁移后 integrity 独立于
  # repo 副本 / release，rsync --delete 不触及。
  if [[ ! -f "$lock" && -f "${SCRIPT_DIR}/manifest.lock.json" ]]; then
    mkdir -p "$(dirname "$lock")"
    cp "${SCRIPT_DIR}/manifest.lock.json" "$lock"
    info "已迁移历史 manifest.lock.json -> ${lock}"
  fi
  [[ -f "$lock" ]] || die "没有完整性凭证 ${lock} —— 尚未安装。请先执行 ./setup.sh install"
  [[ -n "${MODELS_HOME:-}" ]] || die "env 中缺少 MODELS_HOME"

  # 凭证与声明一致：凭证内嵌的模型身份必须等于 manifest 声明（防旧凭证配新声明）
  local lock_repo lock_rev
  lock_repo="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d["model"]["repo"])' "$lock")"
  lock_rev="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d["model"]["revision"])' "$lock")"
  [[ "$lock_repo" == "$(manifest_get .model.repo)" ]] \
    || die "凭证与声明不一致：凭证 repo=${lock_repo}，声明 $(manifest_get .model.repo)"
  [[ "$lock_rev" == "$(manifest_get .model.revision)" ]] \
    || die "凭证与声明不一致：凭证 revision=${lock_rev}，声明 $(manifest_get .model.revision)"

  local dir; dir="$(model_dir)"
  local rc=0
  python3 - "${dir}" "$lock" <<'PY' || rc=1
import json, hashlib, os, sys

model_dir, lock_path = sys.argv[1], sys.argv[2]
lock = json.load(open(lock_path, encoding="utf-8"))

def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

failed = False
for entry in lock["files"]:
    path = os.path.join(model_dir, entry["path"])
    if not os.path.isfile(path):
        print(f"[models] ERROR: 缺少文件 {entry['path']}", file=sys.stderr)
        failed = True
        continue
    actual = sha256_of(path)
    if actual != entry["sha256"]:
        print(f"[models] ERROR: {entry['path']} sha256 不匹配（期望 {entry['sha256'][:12]}…，实际 {actual[:12]}…）", file=sys.stderr)
        failed = True
if failed:
    sys.exit(1)
print(f"[models] OK: {len(lock['files'])} 文件全部通过 sha256 校验（{lock['total_bytes']/1048576:.0f} MiB）")
PY
  exit $rc
}

do_status() {
  require_env_file
  load_env
  local lock; lock="$(lock_path)"
  if [[ ! -f "$lock" ]]; then
    info "未安装（无完整性凭证 ${lock}）。执行 ./setup.sh install"
    return 0
  fi
  local dir; dir="$(model_dir)"
  echo "  lock:     ${lock}"
  echo "  模型目录: ${dir}"
  python3 - "$lock" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"  文件数:   {len(lock['files'])}")
print(f"  总体积:   {lock['total_bytes']/1048576:.0f} MiB")
print(f"  下载时间: {lock['downloaded_at']}")
PY
  if bash "${SCRIPT_DIR}/setup.sh" verify >/dev/null 2>&1; then
    echo "  完整性:   OK"
  else
    echo "  完整性:   未通过（重跑 ./setup.sh install）"
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
