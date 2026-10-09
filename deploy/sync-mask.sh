#!/usr/bin/env bash
# 下发私密配置到目标机
#
# 用法（**在本机执行**，不是目标机）：
#   ./sync-mask.sh --target user@host
#   ./sync-mask.sh --target user@host --dry-run
#   TARGET=user@host ./sync-mask.sh
#
# 路径全部来自 deploy/config.yaml：
#   build.mask-config   本机私密配置（POSIX 写法会自动翻译成 Windows 路径）
#   runtime.mask-config 目标机落地位置
#
# 为什么不放进 install.sh：install.sh 跑在**目标机**上，而密钥在**本机**。
# 两端路径语义不同，硬塞进一个脚本会让"当前在哪台机器上"变得含糊。
# 同步后由 install/update 的 preflight 在目标机侧复核，两边职责清晰。
#
# 安全纪律：全程不打印文件内容。校验只做三件事 —— 存在、非空、含必需键名；
# 传输是否字节一致用 sha256 比对，同样不暴露内容。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOYCONFIG="${SCRIPT_DIR}/lib/deployconfig.py"

#: 目标机上必须存在的配置键（只校验键名，不看值）
REQUIRED_KEYS=(
  "MINIMAX_AGENT_LLM__API_KEY"
)

TARGET="${TARGET:-}"
DRY_RUN=0

# --- 输出（与 common.sh 保持一致的配色） -----------------------------------
log()  { printf '\033[36m[mask]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 64
}

# ---------------------------------------------------------------------------
parse_args() {
  while (($#)); do
    case "$1" in
      --target) TARGET="${2:-}"; shift 2 ;;
      --target=*) TARGET="${1#*=}"; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage ;;
      *) die "未知参数: $1（用 --help 查看用法）" ;;
    esac
  done
}

#: 调 deployconfig.py 读配置
dc() {
  python3 "$DEPLOYCONFIG" "$@"
}

#: 判断文件是否含某个键（只看键名，-q 抑制输出）
has_key() {
  grep -qE "^[[:space:]]*${2}[[:space:]]*[:=]" "$1"
}

# ---------------------------------------------------------------------------
validate_local() {
  local src="$1"
  [[ -f "$src" ]] || die "本机私密配置不存在: ${src}
请检查 deploy/config.yaml 的 build.mask-config，或用 --source 覆盖"
  [[ -s "$src" ]] || die "本机私密配置为空: ${src}"
  [[ -r "$src" ]] || die "本机私密配置不可读: ${src}"

  local key missing=0
  for key in "${REQUIRED_KEYS[@]}"; do
    if has_key "$src" "$key"; then
      log "已确认存在配置项: ${key}"
    else
      warn "缺少必需配置项: ${key}"
      missing=1
    fi
  done
  (( missing == 0 )) || die "本机私密配置缺少必需项，未下发"
  ok "本机配置校验通过（只校验键名，不读取值）"
}

# ---------------------------------------------------------------------------
main() {
  parse_args "$@"
  command -v python3 >/dev/null 2>&1 || die "需要 python3（解析 deploy/config.yaml）"
  command -v ssh >/dev/null 2>&1 || die "需要 ssh"
  command -v scp >/dev/null 2>&1 || die "需要 scp"
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
    || die "需要 sha256sum 或 shasum"

  [[ -n "$TARGET" ]] || die "未指定目标机。用 --target user@host 或设置 TARGET 环境变量"

  local src dst
  src="$(dc resolve build.mask-config --as local)"
  dst="$(dc resolve runtime.mask-config --as remote)"

  log "本机来源: ${src}"
  log "目标位置: ${dst}"
  log "目标机器: ${TARGET}"

  validate_local "$src"

  local local_sum
  local_sum="$(file_sha256 "$src")"

  if (( DRY_RUN == 1 )); then
    warn "--dry-run：以下操作未执行"
    log "  1. ssh ${TARGET} mkdir -p $(dirname "$dst")"
    log "  2. 备份远端现有文件为 ${dst}.bak"
    log "  3. scp 到远端临时路径后 chmod 600"
    log "  4. 校验：存在 / 非空 / 权限 600 / 必需键存在 / sha256 一致"
    log "  远端将收到的文件指纹: ${local_sum:0:12}"
    return 0
  fi

  # --- 1. 远端目录 ----------------------------------------------------
  log "准备远端目录…"
  ssh -o BatchMode=yes "$TARGET" "mkdir -p '$(dirname "$dst")'"

  # --- 2. 备份 --------------------------------------------------------
  if ssh -o BatchMode=yes "$TARGET" "test -f '$dst'"; then
    log "备份远端现有文件…"
    ssh -o BatchMode=yes "$TARGET" "cp -p '$dst' '${dst}.bak'"
    ok "已备份为 ${dst}.bak"
  else
    log "远端暂无同名文件，跳过备份"
  fi

  # --- 3. 传输（先传临时文件，再原子改名）------------------------------
  # 直接 scp 到最终路径会有一个"文件存在但内容不完整"的窗口；
  # 传到 .tmp 再 mv，中断时目标文件要么是旧的、要么是新的。
  local tmp="${dst}.incoming"
  log "传输中…"
  if ! scp -q "$src" "${TARGET}:${tmp}"; then
    warn "传输失败，清理临时文件"
    ssh -o BatchMode=yes "$TARGET" "rm -f '${tmp}'" || true
    die "传输失败（未改动远端现有配置）"
  fi

  # --- 4. 落地 + 权限 ---------------------------------------------------
  ssh -o BatchMode=yes "$TARGET" "mv -f '${tmp}' '${dst}' && chmod 600 '${dst}'"
  ok "已写入并设置权限 600"

  # --- 5. 校验 ---------------------------------------------------------
  if ! verify_remote "$dst" "$local_sum"; then
    warn "校验失败，自动恢复备份…"
    if ssh -o BatchMode=yes "$TARGET" "test -f '${dst}.bak'"; then
      ssh -o BatchMode=yes "$TARGET" "cp -p '${dst}.bak' '${dst}' && chmod 600 '${dst}'"
      warn "已恢复上一份配置"
    else
      warn "没有可恢复的备份，远端文件保持现状（请人工确认）"
    fi
    die "下发校验未通过"
  fi

  ok "下发完成并通过全部校验"
  log "提示：目标机 service 需重启才会加载新配置"
  log "      ssh ${TARGET} 'systemctl --user restart minimax-agent.service'"
}

# ---------------------------------------------------------------------------
#: 计算文件 sha256（只输出指纹，不碰内容）
#:
#: 为什么要清洗：Git Bash(MSYS) 下 `sha256sum` 的输出会带一个**前导反斜杠**
#: （`\9e4d63...`，65 个字符），这是 MSYS 路径转换的副作用。本脚本在本机
#: 算出的指纹要拿去和目标机 Linux 的 `sha256sum` 做**逐字符相等**比较，
#: 不清洗就必然不相等 —— 传输明明完好也会被判"内容指纹不一致"，首次同步
#: 还会因为没有备份而走进死胡同。所以这里剥掉所有非十六进制字符。
#:
#: 目标机侧（REMOTE_CHECK 里）不需要清洗：那是原生 Linux，没有 MSYS。
file_sha256() {
  local raw out
  if command -v sha256sum >/dev/null 2>&1; then
    raw="$(sha256sum "$1" | cut -d' ' -f1)"
  else
    raw="$(shasum -a 256 "$1" | cut -d' ' -f1)"
  fi
  out="$(printf '%s' "$raw" | tr -dc '0-9a-fA-F')"
  # 必须是 64 个十六进制字符。空值会让下游的相等比较退化成
  # "两个空串相等"，从而**静默通过**一个根本没算出来的校验。
  [[ ${#out} -eq 64 ]] || die "无法计算 $1 的 sha256（只得到 ${#out} 个十六进制字符）"
  printf '%s' "$out"
}

#: 在**远端**做全部校验。任何一项不过就非零退出。
#: 注意这里每条命令都不回显文件内容。
verify_remote() {
  local dst="$1" expect_sum="$2"

  ssh -o BatchMode=yes "$TARGET" bash -s -- "$dst" "$expect_sum" "${REQUIRED_KEYS[@]}" <<'REMOTE_CHECK'
set -euo pipefail
dst="$1"; expect_sum="$2"; shift 2

fail() { echo "  [FAIL] $*" >&2; exit 1; }

[[ -f "$dst" ]]        || fail "文件不存在: $dst"
[[ -s "$dst" ]]        || fail "文件为空: $dst"

perm="$(stat -c '%a' "$dst")"
[[ "$perm" == "600" ]] || fail "权限应为 600，实际 $perm"

actual_sum="$(sha256sum "$dst" | cut -d' ' -f1)"
[[ "$actual_sum" == "$expect_sum" ]] || fail "内容指纹不一致（传输可能被截断或修改）"

for key in "$@"; do
  grep -qE "^[[:space:]]*${key}[[:space:]]*[:=]" "$dst" \
    || fail "缺少必需配置项: $key"
  echo "  [ OK ] 配置项存在: $key"
done

echo "  [ OK ] 存在 / 非空 / 权限 600 / 指纹一致"
REMOTE_CHECK
}

main "$@"
