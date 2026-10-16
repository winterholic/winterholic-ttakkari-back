#!/usr/bin/env bash
# Mac Studio 에서 백엔드와 Cloudflare 터널을 로그인 시 자동으로 띄우는 launchd 서비스(LaunchAgent)를 설치한다.
# 포트는 열지 않는다. 백엔드는 127.0.0.1:8787 에만 붙고, 터널이 바깥(ttakkari-api.winterholic.net)과 잇는다.
# 사용: ./install-services.sh backend | deployer | tunnel | all     해제: ./install-services.sh uninstall
set -euo pipefail
BACK="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
LOGS="$HOME/.ttakkari/logs"
# launchd 는 셸 PATH 를 모른다. 에이전트가 claude·codex·git 을 찾을 수 있게 지금 PATH 를 그대로 넘긴다.
RUN_PATH="${TTK_RUN_PATH:-$PATH}"
mkdir -p "$LA" "$LOGS"

backend() {
  cat > "$LA/net.winterholic.ttakkari.backend.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>net.winterholic.ttakkari.backend</string>
  <key>WorkingDirectory</key><string>$BACK</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v uv)</string><string>run</string><string>ttakkari</string><string>serve</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>8787</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$RUN_PATH</string><key>HOME</key><string>$HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/backend.log</string>
  <key>StandardErrorPath</key><string>$LOGS/backend.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/net.winterholic.ttakkari.backend" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/net.winterholic.ttakkari.backend.plist"
  echo "backend service installed"
}

deployer() {
  TTK_INSTALL_BACK="$BACK" TTK_INSTALL_PATH="$RUN_PATH" TTK_INSTALL_UV="$(command -v uv)" python3 - <<'PLIST'
import os
import plistlib
from pathlib import Path
root = Path.home()
with (root / "Library/LaunchAgents/net.winterholic.ttakkari.deployer.plist").open("wb") as f:
    plistlib.dump({
        "Label": "net.winterholic.ttakkari.deployer",
        "WorkingDirectory": os.environ["TTK_INSTALL_BACK"],
        "ProgramArguments": [os.environ["TTK_INSTALL_UV"], "run", "--no-sync", "python", "-m",
                             "ttakkari.deployment", "serve"],
        "EnvironmentVariables": {"PATH": os.environ["TTK_INSTALL_PATH"], "HOME": str(root)},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(root / ".ttakkari/logs/deployer.log"),
        "StandardErrorPath": str(root / ".ttakkari/logs/deployer.log"),
    }, f)
PLIST
  launchctl bootout "gui/$(id -u)/net.winterholic.ttakkari.deployer" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/net.winterholic.ttakkari.deployer.plist"
  echo "deployer service installed"
}

tunnel() {
  local cfg="$HOME/.cloudflared/ttakkari.yml"
  [ -f "$cfg" ] || { echo "터널 설정이 없습니다: $cfg (먼저 터널을 만드세요)"; exit 1; }
  cat > "$LA/net.winterholic.ttakkari.tunnel.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>net.winterholic.ttakkari.tunnel</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v cloudflared)</string><string>tunnel</string><string>--config</string><string>$cfg</string><string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/tunnel.log</string>
  <key>StandardErrorPath</key><string>$LOGS/tunnel.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/net.winterholic.ttakkari.tunnel" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/net.winterholic.ttakkari.tunnel.plist"
  echo "tunnel service installed"
}

# 접수 1순위 로컬 Qwen(runs/desk_qwen)용 llama.cpp 서버. 모델·바이너리 경로는 환경변수로 받는다(없으면 설치하지 않는다).
# 측정(common30-v2, 2026-10-16)과 같은 조건: CPU-only(--device none --gpu-layers 0), 6 thread, parallel 1. ctx 는 긴 작업판을 위해 8192.
# 서버가 없거나 죽어 있어도 접수는 Luna/Haiku 로 동작한다(연결 실패 시 건너뛴다). `all` 에는 넣지 않는다(모델 경로가 필요하다).
# 기본 경로: ~/private-resources/service-data/ttakkari-qwen/{model,llama}. 다른 곳에 두면
#   TTK_QWEN_MODEL=/path/Qwen3.5-4B-Q4_K_M.gguf TTK_LLAMA_SERVER=/path/llama-server ./install-services.sh qwen
qwen() {
  local base="$HOME/private-resources/service-data/ttakkari-qwen"
  local model="${TTK_QWEN_MODEL:-$base/model/Qwen3.5-4B-Q4_K_M.gguf}" server="${TTK_LLAMA_SERVER:-$base/llama/llama-b11655/llama-server}"
  [ -f "$model" ] || { echo "TTK_QWEN_MODEL 에 gguf 경로를 주세요 (없음: '$model')"; exit 1; }
  [ -x "$server" ] || { echo "TTK_LLAMA_SERVER 에 llama-server 경로를 주세요 (없음: '$server')"; exit 1; }
  cat > "$LA/net.winterholic.ttakkari.qwen.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>net.winterholic.ttakkari.qwen</string>
  <key>ProgramArguments</key><array>
    <string>$server</string><string>--model</string><string>$model</string>
    <string>--device</string><string>none</string><string>--gpu-layers</string><string>0</string>
    <string>--threads</string><string>6</string><string>--threads-batch</string><string>6</string>
    <string>--ctx-size</string><string>8192</string><string>--parallel</string><string>1</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>18080</string>
    <string>--jinja</string><string>--no-webui</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/qwen.log</string>
  <key>StandardErrorPath</key><string>$LOGS/qwen.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/net.winterholic.ttakkari.qwen" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/net.winterholic.ttakkari.qwen.plist"
  echo "qwen service installed (127.0.0.1:18080)"
}

case "${1:-all}" in
  backend) backend ;;
  deployer) deployer ;;
  tunnel) tunnel ;;
  qwen) qwen ;;
  all) backend; deployer; tunnel ;;
  uninstall)
    for n in backend deployer tunnel qwen; do
      launchctl bootout "gui/$(id -u)/net.winterholic.ttakkari.$n" 2>/dev/null || true
      rm -f "$LA/net.winterholic.ttakkari.$n.plist"
    done
    echo "uninstalled" ;;
  *) echo "usage: $0 backend|deployer|tunnel|qwen|all|uninstall"; exit 2 ;;
esac
