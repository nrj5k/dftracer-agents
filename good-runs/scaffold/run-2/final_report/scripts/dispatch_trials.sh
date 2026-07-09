#!/bin/bash
# Dispatch trials into USER-PROVIDED standing allocations as they become RUNning.
# Never spawns its own allocation. One 8-node trial per allocation at a time.
#
# Usage: dispatch_trials.sh "<alloc1 alloc2 ...>" "<run:cfg:affinity> ..."
#   e.g. dispatch_trials.sh "jobA jobB" "p1a_cache::" "p1b_async::"
WS="$PROJECT_ROOT/workspaces/scaffold/20260709_081340"
cd "$WS"
ALLOCS=($1); shift
TRIALS=("$@")

declare -A BUSY   # alloc -> pid

next=0
while [ $next -lt ${#TRIALS[@]} ] || [ ${#BUSY[@]} -gt 0 ]; do
  # reap finished
  for a in "${!BUSY[@]}"; do
    if ! kill -0 "${BUSY[$a]}" 2>/dev/null; then
      echo "[$(date +%T)] alloc $a freed"
      unset "BUSY[$a]"
    fi
  done
  # dispatch
  for a in "${ALLOCS[@]}"; do
    [ $next -ge ${#TRIALS[@]} ] && break
    [ -n "${BUSY[$a]:-}" ] && continue
    st=$(flux jobs -no '{state}' "$a" 2>/dev/null)
    [ "$st" != "RUN" ] && continue
    # A job reports RUN slightly before its broker URI is published; dispatching
    # into that window dies with "flux-uri: ERROR: URI not found for job".
    flux uri "$a" >/dev/null 2>&1 || { echo "[$(date +%T)] alloc $a RUN but URI not ready"; continue; }
    IFS=':' read -r RUN CFG AFF <<< "${TRIALS[$next]}"
    CFG="${CFG:-$WS/$RUN/config.yml}"
    echo "[$(date +%T)] dispatch $RUN -> alloc $a (affinity='${AFF}')"
    flux proxy "$a" bash "$WS/scripts/run_trial2.sh" "$RUN" "$CFG" "$AFF" \
        > "$WS/artifacts/${RUN}.log" 2>&1 &
    BUSY[$a]=$!
    next=$((next+1))
  done
  sleep 20
done
echo "ALL_TRIALS_DONE"
