#!/usr/bin/env bash
# Marigold ko maaro, DOBARA BUILD karo, chalao — aur chalne ka saboot maango.
#
#   bash merchants/marigold/restart.sh
#
# Teen cheezein yahan jaan-boojhkar hain:
#
#  1. **Maarna pehle, build baad me.** Ye pehla COMPILED merchant hai, aur ek chalta hua
#     server apni hi `.exe` par lock rakhta hai — `cargo build` sidha
#     "Access is denied (os error 5)" par girta hai. Ye galti dikhne me
#     "build toot gaya" jaisi lagti hai jabki wajah sirf ek zinda process hai.
#
#  2. **`pkill` nahi.** Windows par wo exit code 0 deta hai aur process zinda chhod deta
#     hai — is repo ne wo ek baar mehngi keemat par seekha (ek "verify kiya hua" break
#     asal me laga hi nahi tha). `netstat` -> `taskkill`.
#
#  3. **Chalne ka saboot port ka khulna NAHI hai.** Port do baar jhooth bol chuka hai:
#     ek baar server chup-chaap mar gaya, ek baar LISTENING rehte hue jawab dena band kar
#     diya. Saboot ek asli `401` hai — bina key.
set -u

PORT=8003
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
EXE="$HERE/target/release/marigold.exe"

for pid in $(netstat -ano | grep -E ":$PORT[[:space:]].*LISTENING" | awk '{print $NF}' | sort -u); do
  echo "  port $PORT: pid $pid chal raha hai, maar rahe hain"
  taskkill //F //PID "$pid" >/dev/null 2>&1
done
sleep 0.6

echo "  building (release)..."
if ! (cd "$HERE" && cargo build --release 2>&1 | tail -3); then
  echo "  BUILD FAIL — upar dekho"
  exit 1
fi

cd "$ROOT" || exit 1
"$EXE" >"$HERE/server.log" 2>&1 &
echo "  started pid $!"

for _ in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/agent/manifest" 2>/dev/null)
  if [ "$code" = "401" ]; then
    echo "  OK   port $PORT ne bina key 401 diya"
    exit 0
  fi
  sleep 0.3
done

echo "  MISS port $PORT ne jawab nahi diya — $HERE/server.log dekho"
tail -5 "$HERE/server.log" 2>/dev/null
exit 1
