#!/usr/bin/env sh
set -eu

TOP_MARKETS="${AUTO_TOP_MARKETS:-10}"
MAX_ORDERS="${AUTO_MAX_ORDERS:-1}"
MIN_CONFIDENCE="${AUTO_MIN_CONFIDENCE:-0.8}"
DEFAULT_SIZE="${AUTO_DEFAULT_SIZE:-5}"
ANALYSIS_TIMEOUT_S="${AUTO_ANALYSIS_TIMEOUT_S:-45}"
INTERVAL_SECONDS="${AUTO_INTERVAL_SECONDS:-60}"
SIGNATURE_TYPE="${AUTO_SIGNATURE_TYPE:-1}"
LIVE_MODE="${AUTO_LIVE_MODE:-false}"
RUNTIME_CONFIG_PATH="${RUNTIME_CONFIG_PATH:-config/runtime_config.json}"

echo "[auto-loop] 启动自动交易循环"
echo "[auto-loop] LIVE_MODE=${LIVE_MODE}, TOP_MARKETS=${TOP_MARKETS}, MAX_ORDERS=${MAX_ORDERS}, INTERVAL_SECONDS=${INTERVAL_SECONDS}"

while true; do
  eval "$(
    python - <<PY
import json, os, pathlib
cfg_path = pathlib.Path("${RUNTIME_CONFIG_PATH}")
cfg = {}
if cfg_path.exists():
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
def pick(key, env_key, default):
    v = cfg.get(key, os.getenv(env_key, default))
    return v
print(f"TOP_MARKETS={pick('top_markets','AUTO_TOP_MARKETS','10')}")
print(f"MAX_ORDERS={pick('max_orders','AUTO_MAX_ORDERS','1')}")
print(f"MIN_CONFIDENCE={pick('min_confidence','AUTO_MIN_CONFIDENCE','0.8')}")
print(f"DEFAULT_SIZE={pick('default_size','AUTO_DEFAULT_SIZE','5')}")
print(f"ANALYSIS_TIMEOUT_S={pick('analysis_timeout_s','AUTO_ANALYSIS_TIMEOUT_S','45')}")
print(f"INTERVAL_SECONDS={pick('interval_seconds','AUTO_INTERVAL_SECONDS','60')}")
print(f"SIGNATURE_TYPE={pick('signature_type','AUTO_SIGNATURE_TYPE','1')}")
live = pick('live_mode','AUTO_LIVE_MODE','false')
print(f"LIVE_MODE={'true' if str(live).lower()=='true' else 'false'}")
PY
  )"

  if [ "${LIVE_MODE}" = "true" ]; then
    echo "[auto-loop] 执行真实自动下单"
    python run_bot.py auto-trade \
      --top-markets "${TOP_MARKETS}" \
      --max-orders "${MAX_ORDERS}" \
      --min-confidence "${MIN_CONFIDENCE}" \
      --default-size "${DEFAULT_SIZE}" \
      --analysis-timeout-s "${ANALYSIS_TIMEOUT_S}" \
      --live \
      --signature-type "${SIGNATURE_TYPE}" \
      --confirm-live YES || true
  else
    echo "[auto-loop] 执行 dry-run 自动分析"
    python run_bot.py auto-trade \
      --top-markets "${TOP_MARKETS}" \
      --max-orders "${MAX_ORDERS}" \
      --min-confidence "${MIN_CONFIDENCE}" \
      --default-size "${DEFAULT_SIZE}" \
      --analysis-timeout-s "${ANALYSIS_TIMEOUT_S}" || true
  fi

  echo "[auto-loop] sleep ${INTERVAL_SECONDS}s"
  sleep "${INTERVAL_SECONDS}"
done
