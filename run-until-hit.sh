#!/usr/bin/env bash
# Loop: pars run пока не будет HIT.
# После каждой итерации проверяет что miss-IP освобождены.
set -u

LOG=/opt/pars/wlfinder/run-loop.log
: > "$LOG"

set -a; source /opt/pars/wlfinder/.env; set +a

i=0
while :; do
  i=$((i+1))
  printf '\n\n===== ITERATION %d — %s =====\n' "$i" "$(date -u +%FT%TZ)" | tee -a "$LOG"
  pars run --config /opt/pars/wlfinder/config.yaml --max-attempts 500 2>&1 | tee -a "$LOG"
  if grep -q "^HIT after" "$LOG"; then
    printf '\n>>> HIT DETECTED — stopping loop <<<\n' | tee -a "$LOG"
    break
  fi
  printf '\n(no hit yet, restarting in 5 s)\n' | tee -a "$LOG"
  sleep 5
  # предохранитель — не крутить больше 20 итераций
  if [ "$i" -ge 20 ]; then
    printf '\n>>> gave up after %d iterations <<<\n' "$i" | tee -a "$LOG"
    exit 2
  fi
done
