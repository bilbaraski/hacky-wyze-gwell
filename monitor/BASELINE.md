# Measurement window — started 2026-09-09 ~18:15 UTC

## What to review

`monitor/health.csv` — one row per camera per 5 min. `monitor/events.log` — raw
lifecycle lines for reconstructing outage durations.

Columns that matter most:

- `deadman` / `ffmpeg_died` / `other_err` — session death events. This is the
  headline number: how often does a stream drop.
- `mtx_nostream` — HA-visible failures. Each one is a DESCRIBE that got
  rejected, i.e. a camera card showing "wrong response on DESCRIBE".
- `dup_pct` — share of received KCP segments that were duplicates of data we
  already had. High means the camera is retransmitting because it isn't seeing
  our ACKs.
- `ack_pct` — share of our outbound meter probes that got answered. This is the
  outbound-path health proxy. It was ~6-9% at the start of the window.
- `path` — `lan` or `relay:<ip>`. If relay ever appears, the relay fallback
  added on 09-09 is doing its job.

Note `dup_pct` / `ack_pct` are cumulative per KCP session, not per 5-min window,
so they reset when a session restarts and drift within a session. Compare
like-for-like (e.g. value at a similar age into a session), or just watch the
event counts, which are true per-window deltas.

## Pre-change baseline (observed 2026-09-09, before the changes below)

- Drop events: ~15 per 2 hours across both cameras
- `dup_pct`: ~40%
- `ack_pct`: ~24% (meter sent=59 / recvACK=14)
- Recovery per drop: ~145s (120s deadman + 10s backoff + ~15s handshake)
- Longest observed clean stretch: ~35 min

Caveat: the p2p container was recreated several times during that session, so
these came from log observation rather than a clean recorded baseline. Treat as
approximate.

## Changes made 2026-09-09 (all four within ~1 hour, so attribution is coupled)

1. `deadmanTimeout` 120s -> 25s (`cmd/gwell-proxy/main.go`). Expected effect:
   recovery ~145s -> ~50s. Should show up as shorter gaps in `events.log`, not
   as fewer `deadman` events.
2. KCP congestion-window growth moved from `flush()` to `Input()`, gated on
   `sndUna` advancing (`pkg/gwell/kcp.go`). Correctness only — expected to
   change nothing observable, since `NoDelay(0,5,10,1)` sets `nc=1` (congestion
   control off) and the send queue is always empty on this workload.
3. Relay fallback made reachable when the LAN path goes stale (`pkg/gwell/
   session.go`). Previously the `len(lanMTPAddrs) > 0` branch always returned,
   so the relay code below it was dead. Expected effect: fewer `deadman` events,
   and `path` showing `relay:` during stalls.
4. mediamtx `MTX_READTIMEOUT=25s` (was 10s default). Expected effect: stalls
   between 10-25s no longer tear down the published path, so `mtx_nostream`
   and `ffmpeg_died` should drop.

## What would tell us what

- Fewer `deadman` but `path` still always `lan` -> #1/#4 did the work, and the
  outbound-asymmetry theory behind #3 needs revisiting.
- `path` shows `relay:` during stalls and `deadman` drops -> #3 is working.
- `ffmpeg_died` and `mtx_nostream` drop but `deadman` unchanged -> #4 only.
- Nothing improves -> the remaining cause is upstream of all of this (camera
  radio / AP behaviour), and the next move is physical, not code.
