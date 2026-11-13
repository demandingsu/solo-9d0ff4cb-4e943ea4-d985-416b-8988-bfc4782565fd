#!/usr/bin/env bash
# 离线演示脚本：启动本地电表明细站（仅需 Python 标准库）并调用校时整理 API。
# 前置：API 已在 8000 端口运行
#   uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或直接使用 docker compose up --build（明细站在 meter-site:80）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${METER_SITE_PORT:-8081}"
API_URL="${API_URL:-http://127.0.0.1:8000/api/v1/calibrate-readings}"

python3 -m http.server "$PORT" --directory "$ROOT/meter-site" >/tmp/meter-site.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
sleep 1

PORT="$PORT" API_URL="$API_URL" python3 - "$ROOT/demo/calibrate-request.json" <<'PY'
import json, os, sys, urllib.request

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
payload["first_page_url"] = f"http://127.0.0.1:{os.environ['PORT']}/index.html"

request = urllib.request.Request(
    os.environ["API_URL"],
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=15) as resp:
    print(json.dumps(json.loads(resp.read()), ensure_ascii=False, indent=2))
PY
