#!/bin/bash
# SEC2-OPS-01: 데이터 디렉터리 스냅샷(예: ~/.ttakkari/data 상당).
#
# 하는 일: 원본 디렉터리를 tar.gz로 묶고 파일별 sha256 manifest(JSON)를
#   함께 보관한다. --verify로 격리 위치에 풀어 manifest와 대조한다.
#   끝나면 복호화 임시 파일은 지운다.
#
# 운영 데이터를 건드리지 않는다. tar와 해시 읽기는 읽기 전용이다.
# 스냅샷 대상에 소켓·pid 파일(*.sock, *.pid)과 심볼릭 링크는 넣지 않는다.
# 암호화는 backup_db.sh와 같은 방식(openssl enc -aes-256-cbc -pbkdf2,
#   0600 암호 파일)이며, 암호화하면 평문 tar.gz는 디스크에서 지운다.
#
# 사용법(만들기):
#   scripts/snapshot_data.sh --source DIR [--dir DIR] [--passphrase-file FILE]
#                            [--retention-days N]
# 사용법(검증):
#   scripts/snapshot_data.sh --verify MANIFEST [--target DIR] [--passphrase-file FILE]
# 환경 변수: BACKUP_DIR, BACKUP_PASSPHRASE_FILE, RETENTION_DAYS
#
# 성공 시 manifest 경로를 stdout 마지막 줄에 출력한다.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/../.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "프로젝트 Python 환경 없음: $PYTHON" >&2; exit 3; }

BACKUP_DIR="${BACKUP_DIR:-$HOME/.ttakkari-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:-}"
SOURCE=""
VERIFY=""
TARGET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --dir) BACKUP_DIR="$2"; shift 2 ;;
    --passphrase-file) PASSPHRASE_FILE="$2"; shift 2 ;;
    --retention-days) RETENTION_DAYS="$2"; shift 2 ;;
    --verify) VERIFY="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    *) echo "사용법: $0 --source DIR [--dir DIR] [--passphrase-file FILE] [--retention-days N]" >&2;
       echo "       $0 --verify MANIFEST [--target DIR] [--passphrase-file FILE]" >&2; exit 2 ;;
  esac
done
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || { echo "보존 기간은 0 이상의 정수여야 한다." >&2; exit 2; }

check_passphrase_file() {
  [[ -f "$1" ]] || { echo "오류: 암호 파일 없음: $1" >&2; exit 2; }
  local mode
  if mode="$(stat -f %Lp "$1" 2>/dev/null)"; then :;
  elif mode="$(stat -c %a "$1" 2>/dev/null)"; then :;
  else echo "오류: 암호 파일 권한을 확인할 수 없음: $1" >&2; exit 2; fi
  if (( (8#$mode & 077) != 0 )); then
    echo "오류: 암호 파일은 소유자만 읽을 수 있어야 한다(0600): $1" >&2; exit 2;
  fi
  [[ -s "$1" ]] || { echo "오류: 암호 파일이 비어 있다: $1" >&2; exit 2; }
}

secure_delete() {
  if command -v shred >/dev/null 2>&1; then shred -u -- "$1";
  elif rm -P "$1" 2>/dev/null; then :;
  else rm -f -- "$1"; fi
}

ENCRYPT=0
if [[ -z "${TMPDIR:-}" ]]; then
  DEFAULT_OPS_TMP="$HOME/.ttakkari/operations/tmp"
  if [[ -d "$DEFAULT_OPS_TMP" ]]; then
    TMPDIR="$DEFAULT_OPS_TMP"
  fi
fi

if [[ -n "$PASSPHRASE_FILE" ]]; then
  check_passphrase_file "$PASSPHRASE_FILE"
  command -v openssl >/dev/null || { echo "openssl 없음. 암호화 스냅샷을 만들 수 없다." >&2; exit 3; }
  ENCRYPT=1
fi

# ── 검증 모드 ──────────────────────────────────────────────
if [[ -n "$VERIFY" ]]; then
  [[ -f "$VERIFY" ]] || { echo "오류: 스냅샷 산출물 없음: $VERIFY (먼저 snapshot_data.sh 실행)" >&2; exit 2; }
  ARCH="$("$PYTHON" - "$VERIFY" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
print(m["archive"])
EOF
)"
  [[ -f "$ARCH" ]] || { echo "오류: 아카이브 파일 없음: $ARCH" >&2; exit 2; }
  WANT="$("$PYTHON" - "$VERIFY" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
print(m["sha256"])
EOF
)"
  GOT="$(shasum -a 256 "$ARCH" | awk '{print $1}')"
  [[ "$WANT" == "$GOT" ]] || { echo "오류: 아카이브 sha256 불일치" >&2; exit 1; }
  WORK="$ARCH"
  DECRYPT_TMP=""
  cleanup_verify() { if [[ -n "$DECRYPT_TMP" ]]; then secure_delete "$DECRYPT_TMP"; fi; return 0; }
  trap cleanup_verify EXIT
  IS_ENC="$("$PYTHON" - "$VERIFY" <<'EOF'
import json, sys
print("true" if json.load(open(sys.argv[1])).get("encrypted") else "false")
EOF
)"
  if [[ "$IS_ENC" == "true" ]]; then
    [[ -n "$PASSPHRASE_FILE" ]] || { echo "오류: 암호화 스냅샷이므로 복호화 암호가 필요하다(--passphrase-file)." >&2; exit 2; }
    check_passphrase_file "$PASSPHRASE_FILE"
    DECRYPT_TMP="$(mktemp "${TMPDIR:-/tmp}/snapshot-verify-XXXXXX")"
    chmod 600 "$DECRYPT_TMP"
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
      -in "$ARCH" -out "$DECRYPT_TMP" -pass "file:$PASSPHRASE_FILE" \
      || { echo "오류: 복호화 실패(암호가 틀렸거나 산출물이 손상됨)" >&2; exit 1; }
    WORK="$DECRYPT_TMP"
  fi
  if [[ -z "$TARGET" ]]; then
    TARGET="$(mktemp -d "${TMPDIR:-/tmp}/snapshot-verify-XXXXXX")"
  elif [[ -e "$TARGET" ]]; then
    [[ -d "$TARGET" ]] || { echo "오류: 검증 대상이 디렉터리가 아님: $TARGET" >&2; exit 2; }
    [[ -z "$(ls -A "$TARGET")" ]] || { echo "오류: 검증 대상이 비어 있지 않음(덮어쓰기 방지): $TARGET" >&2; exit 2; }
  else
    mkdir -p "$TARGET"
  fi
  # 원본 위에 푸는 실수를 막는다.
  SRC_REAL="$("$PYTHON" - "$VERIFY" <<'EOF'
import json, os, sys
print(os.path.realpath(json.load(open(sys.argv[1]))["source"]))
EOF
)"
  TGT_REAL="$("$PYTHON" - "$TARGET" <<'EOF'
import os, sys
print(os.path.realpath(sys.argv[1]))
EOF
)"
  [[ "$TGT_REAL" != "$SRC_REAL" ]] || { echo "오류: 검증 대상이 원본과 같다: $TARGET" >&2; exit 2; }
  tar -xzf "$WORK" -C "$TARGET"
  "$PYTHON" - "$VERIFY" "$TARGET" <<'EOF'
import hashlib
import json
import os
import sys

manifest_path, target = sys.argv[1:]
m = json.load(open(manifest_path))
fail = 0
seen = set()
for entry in m["files"]:
    rel = entry["path"]
    seen.add(rel)
    disk = os.path.join(target, rel)
    if not os.path.isfile(disk):
        print(f"MISMATCH {rel}: 복원 위치에 없음")
        fail = 1
        continue
    with open(disk, "rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    if digest != entry["sha256"]:
        print(f"MISMATCH {rel}: 해시 불일치")
        fail = 1
found = set()
for root, _dirs, names in os.walk(target):
    for name in names:
        found.add(os.path.relpath(os.path.join(root, name), target))
for extra in sorted(found - seen):
    print(f"MISMATCH {extra}: manifest에 없는 파일")
    fail = 1
if fail:
    print("스냅샷 검증 실패: 파일 불일치", file=sys.stderr)
    raise SystemExit(1)
print(f"스냅샷 검증 통과: {len(m['files'])}개 파일 일치")
EOF
  exit 0
fi

# ── 만들기 모드 ────────────────────────────────────────────
[[ -n "$SOURCE" ]] || { echo "오류: --source 필요" >&2; exit 2; }
[[ -d "$SOURCE" ]] || { echo "오류: 원본 디렉터리 없음: $SOURCE" >&2; exit 2; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)-$$"
ARCH="$BACKUP_DIR/data-$TS.tar.gz"
MANIFEST="$BACKUP_DIR/data-$TS.manifest.json"
ARCH_TMP="$ARCH.partial.$$"
MANIFEST_TMP="$MANIFEST.partial.$$"
[[ ! -e "$ARCH" && ! -e "$MANIFEST" ]] || { echo "동일한 스냅샷 이름이 이미 있다." >&2; exit 1; }
cleanup_partial() { rm -f "$ARCH_TMP" "$MANIFEST_TMP" "${ENC_TMP:-}"; }
trap cleanup_partial EXIT

"$PYTHON" - "$SOURCE" "$ARCH_TMP" "$ARCH" "$MANIFEST_TMP" "$TS" <<'EOF'
import hashlib
import json
import os
import stat
import sys
import tarfile

source, arch_tmp, arch_path, manifest_tmp, timestamp = sys.argv[1:]
entries = []
total = 0
# 아카이브와 manifest 가 같은 범위를 담도록 링크·특수 파일을 양쪽에서 제외한다.
with tarfile.open(arch_tmp, "w:gz", dereference=True) as archive:
    for root, dirs, names in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not d.endswith((".sock", ".pid"))
                         and not os.path.islink(os.path.join(root, d)))
        for name in dirs:
            disk = os.path.join(root, name)
            archive.add(disk, arcname=os.path.relpath(disk, source), recursive=False)
        for name in sorted(names):
            disk = os.path.join(root, name)
            if name.endswith((".sock", ".pid")) or not stat.S_ISREG(os.lstat(disk).st_mode):
                continue
            with open(disk, "rb", opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW)) as f:
                member = archive.gettarinfo(fileobj=f, arcname=os.path.relpath(disk, source))
                archive.addfile(member, f)
# 캡처 뒤 원본이 바뀌어도 manifest 는 실제 보관한 바이트의 해시여야 한다.
with tarfile.open(arch_tmp, "r:gz") as archive:
    for member in archive:
        if member.isfile():
            with archive.extractfile(member) as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            entries.append({"path": member.name, "size": member.size, "sha256": digest})
            total += member.size
entries.sort(key=lambda e: e["path"])
with open(arch_tmp, "rb") as f:
    arch_digest = hashlib.file_digest(f, "sha256").hexdigest()
with open(manifest_tmp, "w", encoding="utf-8") as f:
    json.dump(
        {"kind": "data", "created_at": timestamp, "source": source,
         "archive": arch_path, "sha256": arch_digest,
         "files": entries, "file_count": len(entries),
         "total_bytes": total, "encrypted": False},
        f, indent=2, sort_keys=True,
    )
    f.write("\n")
print(f"파일 {len(entries)}개, 총 {total}바이트 기록")
EOF
mv "$ARCH_TMP" "$ARCH"
mv "$MANIFEST_TMP" "$MANIFEST"

if [[ "$ENCRYPT" -eq 1 ]]; then
  ENC="$ARCH.enc"
  ENC_TMP="$ENC.partial.$$"
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in "$ARCH" -out "$ENC_TMP" -pass "file:$PASSPHRASE_FILE"
  "$PYTHON" - "$MANIFEST" "$ENC_TMP" "$ENC" "$PASSPHRASE_FILE" <<'EOF'
import hashlib
import json
import sys

manifest_path, enc_tmp, enc_path, passfile = sys.argv[1:]
with open(enc_tmp, "rb") as f:
    enc_digest = hashlib.file_digest(f, "sha256").hexdigest()
with open(manifest_path, encoding="utf-8") as f:
    m = json.load(f)
m["plaintext_sha256"] = m["sha256"]
m["sha256"] = enc_digest
m["archive"] = enc_path
m["encrypted"] = True
with open(passfile, "rb") as f:
    m["key_fingerprint"] = hashlib.pbkdf2_hmac("sha256", f.read().strip(), b"ttakkari-backup-key-fp-v1", 200_000).hex()[:16]
with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(m, f, indent=2, sort_keys=True)
    f.write("\n")
EOF
  mv "$ENC_TMP" "$ENC"
  secure_delete "$ARCH"
  echo "스냅샷 암호화 완료: $ENC"
fi

"$PYTHON" - "$MANIFEST" "$BACKUP_DIR/latest-data.json" <<'EOF'
import json
import os
import sys

manifest_path, latest_path = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as f:
    m = json.load(f)
latest = {
    "kind": "data",
    "created_at": m["created_at"],
    "manifest": manifest_path,
    "archive": m["archive"],
    "sha256": m["sha256"],
    "encrypted": bool(m.get("encrypted", False)),
    "key_fingerprint": m.get("key_fingerprint"),
    "file_count": m["file_count"],
    "total_bytes": m["total_bytes"],
}
tmp = latest_path + ".partial"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(latest, f, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp, latest_path)
EOF

if ! find "$BACKUP_DIR" -maxdepth 1 -name 'data-*.tar.gz' -mtime +"$RETENTION_DAYS" -delete; then
  echo "경고: 스냅샷 보존 기간 정리 실패" >&2
fi
if ! find "$BACKUP_DIR" -maxdepth 1 -name 'data-*.tar.gz.enc' -mtime +"$RETENTION_DAYS" -delete; then
  echo "경고: 암호화 스냅샷 보존 기간 정리 실패" >&2
fi
if ! find "$BACKUP_DIR" -maxdepth 1 -name 'data-*.manifest.json' -mtime +"$RETENTION_DAYS" -delete; then
  echo "경고: 스냅샷 manifest 보존 기간 정리 실패" >&2
fi

echo "스냅샷 완료: $MANIFEST"
