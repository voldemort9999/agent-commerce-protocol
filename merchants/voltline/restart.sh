#!/usr/bin/env bash
# Voltline ko sach me maar kar dobara chalao, aur chalne ka SABOOT maango.
#
#   bash merchants/voltline/restart.sh
#
# Do baatein is repo ne mehngi keemat par seekhi hain aur dono yahan lagi hain:
#   * `pkill` Windows par exit code 0 deta hai aur process zinda rehta hai (D-51).
#     Isliye netstat -> taskkill //F //PID, aur phir port ka khali hona check.
#   * Server chalu hone ka saboot port ka LISTENING dikhna NAHI hai — wo do baar
#     jhooth bol chuka hai. Saboot ek asli jawab hai: bina key 401.
set -u
PORT="${VOLTLINE_PORT:-8002}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

for pid in $(netstat -ano | grep -E "[:.]$PORT[[:space:]].*LISTENING" | awk '{print $NF}' | sort -u); do
  echo "killing pid $pid on :$PORT"
  taskkill //F //PID "$pid" > /dev/null 2>&1
done

for _ in 1 2 3 4 5 6 7 8 9 10; do
  netstat -ano | grep -qE "[:.]$PORT[[:space:]].*LISTENING" || break
  sleep 0.3
done

cd "$ROOT"
node merchants/voltline/main.mjs > merchants/voltline/server.log 2> merchants/voltline/server.log.err &

for _ in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/agent/manifest" 2>/dev/null)
  if [ "$code" = "401" ]; then
    echo "voltline up on :$PORT  (proof: unauthenticated manifest answered 401)"
    exit 0
  fi
  sleep 0.25
done

echo "voltline did NOT answer on :$PORT"
tail -20 merchants/voltline/server.log.err
exit 1
