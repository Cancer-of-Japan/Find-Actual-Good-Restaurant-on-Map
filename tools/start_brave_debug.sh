#!/usr/bin/env bash
# Relaunch the user's Brave with a DevTools endpoint so Playwright can attach to
# it (config key: scraper.brave_cdp_url). Running inside the real desktop
# session preserves the logged-in profile + keyring, so Google shows the full,
# login-gated review set.
#
# Usage:  ./tools/start_brave_debug.sh [PORT]     (default 9222)
#
# NOTE: Brave cannot enable remote debugging on an already-running instance, so
# this gracefully closes the running Brave and relaunches it with the port.
# --restore-last-session brings your tabs back.
set -u
PORT="${1:-9222}"
BRAVE="${BRAVE_BIN:-/usr/bin/brave-browser-stable}"

up() {
  python3 - "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen("http://127.0.0.1:%s/json/version" % sys.argv[1], timeout=1).read()
PY
}

if up; then
  echo "Brave DevTools already listening on ${PORT}."
  exit 0
fi

# Inherit the live graphical-session env so a GUI launch over SSH lands on the
# real display and keyring. systemctl --user is authoritative and works even
# when Brave isn't currently running; fall back to a running GUI process.
if command -v systemctl >/dev/null 2>&1; then
  while IFS='=' read -r k v; do
    case "$k" in
      DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS)
        export "$k=$v" ;;
    esac
  done < <(systemctl --user show-environment 2>/dev/null)
fi
pid="$(pgrep -u "$USER" -f '/opt/brave.com/brave/brave' | head -n1 || true)"
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && [ -n "${pid:-}" ] && [ -r "/proc/$pid/environ" ]; then
  while IFS= read -r -d '' kv; do
    case "$kv" in
      DISPLAY=*|WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_RUNTIME_DIR=*|DBUS_SESSION_BUS_ADDRESS=*)
        export "${kv?}" ;;
    esac
  done < "/proc/$pid/environ"
fi
: "${DISPLAY:=:0}"
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"

if [ -n "${pid:-}" ]; then
  echo "Closing running Brave (to relaunch with debug port)..."
  pkill -u "$USER" -f '/opt/brave.com/brave/brave' 2>/dev/null || true
  for _ in $(seq 1 20); do
    pgrep -u "$USER" -f '/opt/brave.com/brave/brave' >/dev/null || break
    sleep 0.5
  done
  sleep 1
fi

echo "Launching Brave with --remote-debugging-port=${PORT} (DISPLAY=${DISPLAY}, WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset}) ..."
# --remote-allow-origins=* is required by Chromium >=111 for external CDP clients.
# --ozone-platform-hint=auto lets Brave pick Wayland when available, else X11.
nohup "$BRAVE" \
  --remote-debugging-port="${PORT}" \
  --remote-debugging-address=127.0.0.1 \
  --remote-allow-origins='*' \
  --ozone-platform-hint=auto \
  --restore-last-session \
  >"$HOME/.cache/brave-debug.log" 2>&1 &
disown || true

for _ in $(seq 1 60); do
  if up; then
    echo "Brave DevTools is up on 127.0.0.1:${PORT}."
    exit 0
  fi
  sleep 0.5
done

echo "ERROR: Brave DevTools did not come up on ${PORT}. See ~/.cache/brave-debug.log" >&2
exit 1
