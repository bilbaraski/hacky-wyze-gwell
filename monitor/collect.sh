#!/bin/sh
# Samples wyze-gwell stream health and appends a CSV row per camera.
# POSIX sh (no bashisms) so it can run in the docker:cli busybox image.
# Loops internally rather than relying on cron — this host has no crontab and
# no systemd lingering for the unprivileged user.

OUTDIR=${OUTDIR:-/monitor}
CSV="$OUTDIR/health.csv"
EVENTS="$OUTDIR/events.log"
INTERVAL=${INTERVAL:-300}
WINDOW=${WINDOW:-5m}

P2P=wyze-gwell-combined-wyze-p2p-1
MTX=wyze-gwell-combined-mediamtx-1

mkdir -p "$OUTDIR"

if [ ! -f "$CSV" ]; then
  echo "ts,camera,deadman,ffmpeg_died,other_err,started,mtx_publish,mtx_nostream,dropOld,rcvNxt,dup_pct,meter_sent,path" >> "$CSV"
fi

sample() {
  ts=$(date -Iseconds)
  p2plog=$(docker logs --since "$WINDOW" "$P2P" 2>&1)
  mtxlog=$(docker logs --since "$WINDOW" "$MTX" 2>&1)

  for pair in AC2F:front_window B095:kitchen_cam; do
    id=${pair%%:*}
    name=${pair##*:}

    cl=$(printf '%s\n' "$p2plog" | grep "$id")

    deadman=$(printf '%s\n' "$cl" | grep -c 'deadman timeout')
    ffdied=$(printf '%s\n' "$cl" | grep -c 'ffmpeg process died')
    othererr=$(printf '%s\n' "$cl" | grep 'Stream error' | grep -vc -e 'deadman timeout' -e 'ffmpeg process died')
    started=$(printf '%s\n' "$cl" | grep -c 'streaming started')

    # mtx_nostream is the HA-visible failure count: each rejected DESCRIBE is a
    # camera card showing "wrong response on DESCRIBE".
    mtxpub=$(printf '%s\n' "$mtxlog" | grep -c "is publishing to path 'live/$name'")
    mtxno=$(printf '%s\n' "$mtxlog" | grep -c "no stream is available on path 'live/$name'")

    last=$(printf '%s\n' "$cl" | grep 'DATA-KCP' | tail -1)
    rcv=$(printf '%s\n' "$last" | grep -o 'rcvNxt=[0-9]*' | cut -d= -f2)
    drop=$(printf '%s\n' "$last" | grep -o 'dropOld=[0-9]*' | cut -d= -f2)
    [ -z "$rcv" ] && rcv=0
    [ -z "$drop" ] && drop=0
    dup=$(awk -v d="$drop" -v r="$rcv" 'BEGIN{t=d+r; printf "%.1f", (t>0)? d*100/t : 0}')

    # meter_sent is kept only as a session-age proxy: it climbs for the life of
    # a session. recvACK/ack_pct were removed — recvACK stops incrementing after
    # handshake while sent climbs forever, so the ratio decays toward zero no
    # matter how healthy the link is. It read as "outbound path broken" for 13
    # days while dup_pct at 0.5% showed the camera was receiving our ACKs fine.
    ml=$(printf '%s\n' "$cl" | grep 'meter:' | tail -1)
    msent=$(printf '%s\n' "$ml" | grep -o 'sent=[0-9]*' | cut -d= -f2)
    [ -z "$msent" ] && msent=0

    # Did the relay fallback actually carry traffic? A non-192.168 source means
    # we were receiving over the relay rather than LAN direct.
    src=$(printf '%s\n' "$cl" | grep -o 'lastDataFrom: [0-9.]*' | tail -1 | awk '{print $2}')
    case "$src" in
      "") path=lan ;;
      192.168.*) path=lan ;;
      *) path="relay:$src" ;;
    esac

    echo "$ts,$name,$deadman,$ffdied,$othererr,$started,$mtxpub,$mtxno,$drop,$rcv,$dup,$msent,$path" >> "$CSV"
  done

  # Keep raw lifecycle lines so outage durations can be reconstructed later.
  printf '%s\n' "$p2plog" | grep -E 'Stream error|streaming started|Starting stream' >> "$EVENTS"

  # Bound our own footprint — this host's root filesystem runs tight.
  for f in "$CSV" "$EVENTS"; do
    if [ -f "$f" ]; then
      sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
      if [ "$sz" -gt 5000000 ]; then
        tail -n 20000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
      fi
    fi
  done
}

if [ "$1" = "once" ]; then
  sample
else
  while true; do
    sample
    sleep "$INTERVAL"
  done
fi
