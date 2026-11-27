#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export REWARDS_DATA_DIR="${REWARDS_DATA_DIR:-$PWD/data}"
export REWARDS_PORT="${REWARDS_PORT:-8765}"
if [[ ${REWARDS_CONTAINER:-} == 1 ]]; then
    mkdir -p "$REWARDS_DATA_DIR/engine-logs" "$REWARDS_DATA_DIR/panel-logs" "$REWARDS_DATA_DIR/diagnostics"
    export DISPLAY=:99
    Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp -ac &
    for attempt in {1..40}; do
        if [[ -S /tmp/.X11-unix/X99 ]]; then
            break
        fi
        sleep .1
    done
    [[ -S /tmp/.X11-unix/X99 ]] || { echo "无法启动容器图形会话" >&2; exit 1; }
else
    if [[ ! -f app/dist/index.js || ! -d browser ]]; then
        echo "首次使用请先运行 bash install-linux.sh --with-deps" >&2
        exit 1
    fi
    echo '运行后可在另一个终端读取 data/.api-token，按 README 的地址打开面板。'
fi
exec python3 -u panel/server.py
