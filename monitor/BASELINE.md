# Stream health — measurement and findings

## Result of the 2026-09-09 -> 09-22 window

|                        | before (09-09) | after            |
| ---------------------- | -------------- | ---------------- |
| drop events            | ~180/day       | 0-9/day          |
| median session length  | ~2-3 min       | 21h front / 9.7h kitchen |
| duplicate rate         | ~40%           | 0.5%             |
| HA-visible failures    | constant       | zero for 11 days |

09-16, 09-17 and 09-18 recorded zero drop events. Longest unbroken session was
92.4h — ~67M packets, no reconnect.

## Which of the four changes actually did it

Four changes landed together on 09-09, so attribution had to come from the data.

- **mediamtx `MTX_READTIMEOUT` 10s -> 25s — this is the one that mattered.**
  The 10s default tore down the published path on a brief stall, which killed
  ffmpeg, which killed the session, which forced a re-handshake. A cascade.
  Absorb the stall and it never starts. Corroborated by the duplicate collapse:
  duplicates concentrate in early-session, so when sessions stop churning the
  rate falls off a cliff (the 74% once measured was 11s into a session).
- **Relay fallback: used once** in 7,072 samples (`relay:52.20.100.241`,
  09-19 20:22). Effectively inert. Caveat: `path` records where data was
  *received*, so relay *sends* wouldn't appear — it can't be fully excluded,
  but it is clearly not the mechanism.
- **KCP cwnd growth: confirmed no-op.** `streamLoop` calls `NoDelay(0,5,10,1)`,
  so `nc=1` disables the window, and the send queue is empty on this
  receive-only workload. Kept for correctness, not effect.
- **`deadmanTimeout` 120s -> 25s:** shortened recovery, as intended. Cannot
  explain drops falling 20-95x — a more sensitive detector fires *more*, not
  less.

## A metric that lied, now removed

`ack_pct` (and `meter_ack`) were dropped from the CSV on 09-22. `meter_sent` is
cumulative and climbs for the life of a session; `recvACK` stops incrementing
after handshake and sits at ~15. The ratio therefore decays toward zero no
matter how healthy the link is:

    00:04  sent=21365 ack=15
    01:03  sent=23112 ack=15

This was read as "only 6-9% of outbound probes answered" and used to argue the
outbound path was chronically broken — which motivated the relay work. It was
an artifact. `dup_pct` at 0.5% is the load-bearing evidence: if the camera
weren't receiving our ACKs it would retransmit, and it isn't. `meter_sent` is
kept only as a session-age proxy.

## What the columns mean

- `deadman` / `ffmpeg_died` / `other_err` — session deaths. True per-window
  counts; the trustworthy headline.
- `mtx_nostream` — HA-visible failures. Each is a rejected DESCRIBE, i.e. a
  camera card showing "wrong response on DESCRIBE".
- `dup_pct` — share of received KCP segments that were duplicates. Rises when
  the camera is retransmitting. Cumulative per session, so it resets on
  reconnect and drifts within a session; compare at similar session age.
- `rcvNxt` — packets this session. Going backwards means a new session, which
  is how session lifetimes above were derived.
- `path` — `lan` or `relay:<ip>`, based on where data arrived from.

## Open, as of 09-22

- **kitchen_cam is drifting.** deadman by day: 09-19 2, 09-20 5, 09-21 5, while
  front_window stayed at 1-3. `mtx_nostream` appeared 09-20 (6) and 09-21 (56),
  kitchen only. Still far better than baseline. `dup_pct` stays low, so this
  looks environmental (Wi-Fi) rather than protocol.
- **Discovery could not recover from a failed startup** — fixed 09-22 after a
  DNS outage left both cameras down ~4.5h. See the commit; discovery now
  retries and `/health` reports empty camera lists.
- **Sampling gaps:** ~538 samples/day against 576 expected (~7%), so event
  counts are slight undercounts. Log rotation added 09-22, which should help —
  unbounded logs made `docker logs --since` slow enough to miss windows.

`health.csv.v1` holds the original 13-day window under the old schema.
