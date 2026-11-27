#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

upstream=https://github.com/huaisha1224/Microsoft-Rewards.git
revision=65cc9773e1c096b994df74b2f8e34ef0b8769e18
for tool in git node npm python3; do
    command -v "$tool" >/dev/null || { echo "缺少 $tool" >&2; exit 1; }
done
node -e 'if (+process.versions.node.split(".")[0] < 24) process.exit(1)' ||
    { echo "请安装 Node.js 24 或更新版本" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    { echo "请安装 Python 3.10 或更新版本" >&2; exit 1; }

if [[ ! -e app ]]; then
    # Stage the source so a failed fetch never leaves a half-built app.
    staging=$(mktemp -d "$PWD/.app-build.XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT
    git -C "$staging" init -q
    git -C "$staging" fetch --depth 1 "$upstream" "$revision"
    git -C "$staging" checkout --detach FETCH_HEAD
    (
        cd -- "$staging"
        npm ci --ignore-scripts
        npm run build
        npm prune --omit=dev --ignore-scripts
    )
    mv -T -- "$staging" app
    trap - EXIT
fi
if [[ ! -f app/dist/index.js || ! -f app/node_modules/patchright/cli.js ]]; then
    echo "现有 app 缺少构建产物，请先备份并移走该目录，再运行安装脚本。" >&2
    exit 1
fi

if [[ ${1:-} == --engine-only ]]; then
    exit 0
fi
args=(install chromium)
if [[ ${1:-} == --with-deps ]]; then
    args=(install --with-deps chromium)
fi
export PLAYWRIGHT_BROWSERS_PATH="$PWD/browser"
node app/node_modules/patchright/cli.js "${args[@]}"
