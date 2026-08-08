#!/usr/bin/env bash
# Cat Sentry self-test. Run after `docker compose up -d`.
# Exits non-zero if anything is wrong, so it can gate a setup run.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PASS=0; FAIL=0; WARN=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; WARN=$((WARN+1)); }
head() { printf '\n\033[1m%s\033[0m\n' "$1"; }

head "Configuration"

if [ -f .env ]; then
  ok ".env exists"
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      # NTFS has no POSIX mode bits. stat synthesises 644 no matter what, and
      # chmod is a no-op, so check the ACL — that is the real access control.
      if icacls .env 2>/dev/null \
           | grep -qiE 'Everyone|BUILTIN\\Users|Authenticated Users'; then
        warn '.env readable by other accounts — icacls .env /inheritance:r /grant:r "%USERNAME%:F"'
      else
        ok ".env access restricted to the owner"
      fi
      ;;
    Darwin) ;;
    *)
      if command -v stat >/dev/null; then
        PERM=$(stat -c '%a' .env 2>/dev/null || echo "?")
        case "$PERM" in
          600|400) ok ".env permissions are $PERM" ;;
          "?")     warn "could not read .env permissions" ;;
          *)       warn ".env permissions are $PERM — tighten with: chmod 600 .env" ;;
        esac
      fi
      ;;
  esac
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  [ -n "${CAM_IP:-}" ]      && ok "camera IP set"        || bad "CAM_IP missing"
  [ -n "${CAM_PASSWORD:-}" ] && ok "camera password set" || bad "CAM_PASSWORD missing"
  # Note: ${#VAR:-0} is not valid bash — length and default cannot be combined,
  # and the resulting "bad substitution" aborts the rest of this block.
  SK="${SECRET_KEY:-}"
  [ "${#SK}" -ge 32 ] && ok "session key looks strong" \
                      || bad "SECRET_KEY too short — re-run setup.py"
  case "${CONTROL_PIN:-}" in
    ""|1234|0000|1111) bad "CONTROL_PIN missing or guessable" ;;
    *) ok "control PIN set" ;;
  esac
  if [ -n "${TELEGRAM_CHATS:-}" ] && [ -n "${TELEGRAM_TOKEN:-}" ]; then
    ok "$(echo "$TELEGRAM_CHATS" | tr ',' '\n' | grep -c .) Telegram chat(s) configured"
  elif [ -n "${TELEGRAM_CHATS:-}" ]; then
    bad "TELEGRAM_CHATS set but TELEGRAM_TOKEN missing — nothing can send"
  else
    warn "Telegram not set up — nothing will be sent (python setup.py --recipients-only)"
  fi
else
  bad ".env missing — run: python setup.py"
fi

if [ -f config/config.yml ]; then
  ok "config.yml rendered"
  grep -q "PLACEHOLDER" config/config.yml \
    && bad "zone/mask still contain PLACEHOLDER — draw them in the Frigate UI" \
    || ok "zone and mask coordinates drawn"
  grep -qE "^record:\s*$" config/config.yml && \
    grep -A1 "^record:" config/config.yml | grep -q "enabled: false" \
      && ok "recordings disabled" \
      || bad "recordings are ENABLED — this is a bedroom, turn them off"
else
  bad "config/config.yml missing — run: python setup.py"
fi

head "Secrets hygiene"
if grep -rqE "(apikey|password)\s*[:=]\s*[A-Za-z0-9]" config/config.yml 2>/dev/null; then
  bad "config.yml appears to contain a literal credential"
else
  ok "no literal credentials in config.yml"
fi
if git rev-parse --git-dir >/dev/null 2>&1; then
  git check-ignore -q .env && ok ".env is gitignored" || bad ".env is NOT gitignored"
fi

head "Containers"
for c in cs-mosquitto cs-frigate cs-alerter; do
  STATE=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null || echo 0)
  if [ "$STATE" = "running" ]; then
    if [ "${RESTARTS:-0}" -gt 3 ]; then
      warn "$c running but has restarted $RESTARTS times — check logs"
    else
      ok "$c running"
    fi
  else
    bad "$c is $STATE"
  fi
done

head "Exposure"
if docker port cs-mosquitto 2>/dev/null | grep -q .; then
  bad "MQTT broker is published to the host — it should be internal only"
else
  ok "MQTT broker not exposed to the host"
fi
if docker port cs-frigate 2>/dev/null | grep -q '0.0.0.0:5000'; then
  bad "Frigate UI is bound to 0.0.0.0 — should be 127.0.0.1 only"
else
  ok "Frigate UI restricted to localhost"
fi

head "Services"
curl -fsS --max-time 5 http://127.0.0.1:5000/api/version >/dev/null 2>&1 \
  && ok "Frigate responding" || bad "Frigate not responding on :5000"

curl -fsS --max-time 5 http://127.0.0.1:8080/healthz >/dev/null 2>&1 \
  && ok "control page responding" || bad "control page not responding on :8080"

CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8080/ 2>/dev/null)
[ "$CODE" = "302" ] && ok "control page requires the PIN" \
                    || warn "control page returned $CODE (expected a redirect to /login)"

if curl -fsS --max-time 8 http://127.0.0.1:5000/api/stats 2>/dev/null | grep -q '"bedroom"'; then
  ok "Frigate sees the camera"
  # Windows ships a fake `python3` App Execution Alias that only prints a
  # Microsoft Store advert and exits non-zero. It satisfies `command -v`, so
  # test that a candidate actually executes before trusting it.
  PY=""
  for c in python3 python py; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "" >/dev/null 2>&1; then
      PY="$c"; break
    fi
  done
  if [ -n "$PY" ]; then
    FPS=$(curl -fsS http://127.0.0.1:5000/api/stats 2>/dev/null \
          | "$PY" -c "import sys,json;print(json.load(sys.stdin)['cameras']['bedroom']['camera_fps'])" 2>/dev/null)
  else
    # No usable interpreter — fall back to a text scrape. Single camera by design.
    FPS=$(curl -fsS http://127.0.0.1:5000/api/stats 2>/dev/null \
          | tr ',' '\n' | grep -m1 '"camera_fps"' | grep -oE '[0-9]+(\.[0-9]+)?')
  fi
  [ -n "${FPS:-}" ] && [ "${FPS%.*}" -gt 0 ] 2>/dev/null \
    && ok "receiving frames (${FPS} fps)" \
    || warn "camera at 0 fps — check the RTSP stream"
else
  bad "Frigate is not receiving the bedroom camera"
fi

head "Result"
printf '  %d passed, %d warnings, %d failed\n\n' "$PASS" "$WARN" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
