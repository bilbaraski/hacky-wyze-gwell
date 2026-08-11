# wyze-gwell-bridge

Stream Wyze GWell cameras to RTSP/HLS via a pure Go P2P client. No cloud relay, no Android emulator — connects directly to cameras on your LAN.

## How it works

```
wyze-api (Python)  — Authenticates with Wyze, provides camera list + P2P tokens
wyze-p2p (Go)      — Connects to cameras via GWell P2P protocol, pipes H.264 to ffmpeg
mediamtx           — RTSP/HLS server, streams viewable in VLC or any player
```

The Go client implements the full GWell/IoTVideo P2P protocol: discovery, authentication (RC5/XOR crypto), KCP transport, and H.264 stream extraction. All reverse-engineered from the SDK.

## Quick start

### 1. Clone

```bash
git clone https://github.com/wlatic/wyze-gwell-bridge.git
cd wyze-gwell-bridge
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your Wyze credentials and network IPs
```

You need a [Wyze API key](https://developer-api-console.wyze.com/). Set `WYZE_EMAIL`, `WYZE_PASSWORD`, `WYZE_KEY_ID`, and `WYZE_API_KEY`.

### 3. Create macvlan network (one time)

All three services get their own LAN IPs via macvlan. Adjust the subnet, gateway, and interface for your network:

```bash
docker network create -d macvlan \
  --subnet=192.168.1.0/24 \
  --gateway=192.168.1.1 \
  -o parent=eth0 \
  directlan
```

### 4. Set camera LAN IPs

Wyze cloud doesn't reliably report LAN IPs for GWell cameras. Set them manually:

```bash
# Start just the API first
docker compose up -d wyze-api

# Wait for it to discover cameras
sleep 30

# Check what cameras were found
curl http://API_IP:8080/Camera/CameraList

# Set the LAN IP for each camera
curl -X POST http://API_IP:8080/Camera/SetManualIP \
  -H "Content-Type: application/json" \
  -d '{"cameraId":"GW_YOUR_CAMERA_MAC","ip":"192.168.1.100"}'
```

IPs persist in `data/manual_ips.json`.

### 5. Start everything

```bash
docker compose up -d
```

### 6. View streams

Streams are available at `rtsp://MEDIAMTX_IP:8554/live/CAMERA_NAME`. Open in VLC or any RTSP player.

The dashboard at `http://API_IP:8080/streams` shows all camera stream URLs.

## Supported cameras

Tested with Wyze cameras using the GWell/IoTVideo P2P protocol (device IDs starting with `GW_`). This includes Wyze Cam OG and similar models.

## Architecture

```
wyze-gwell-bridge/
├── docker-compose.yml       ← orchestrates 3 services
├── .env                     ← your credentials + network config
├── data/                    ← persistent state (manual_ips.json)
├── wyze-api/                ← Python: Wyze auth + camera management
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
└── wyze-p2p/                ← Go: P2P protocol + streaming
    ├── Dockerfile
    ├── go.mod
    ├── cmd/gwell-proxy/     ← entry point
    └── pkg/
        ├── gwell/           ← P2P protocol (crypto, frames, session)
        ├── stream/          ← H.264 extraction + ffmpeg RTSP publisher
        └── wyze/            ← API client
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WYZE_EMAIL` | (required) | Wyze account email |
| `WYZE_PASSWORD` | (required) | Wyze account password |
| `WYZE_KEY_ID` | (required) | Wyze API key ID |
| `WYZE_API_KEY` | (required) | Wyze API key |
| `MEDIAMTX_IP` | (required) | LAN IP for RTSP server |
| `API_IP` | (required) | LAN IP for API / dashboard |
| `P2P_IP` | (required) | LAN IP for P2P client |

## Troubleshooting

**Cameras not found:** Check `docker compose logs wyze-api` — ensure credentials are correct and cameras are online.

**No video:** Verify the camera LAN IP is correct and reachable from the P2P container. The camera must be on the same network.

**Stream dies after a few minutes:** Earlier versions had several issues causing streams to drop after 3–7 minutes. These have all been resolved — see the "Stream Stability" section in `wyze-p2p/IMPLEMENTATION_STATUS.md` for details. If you still see drops, check `docker compose logs wyze-p2p` for `dropAhead` counts or watchdog timeout messages.
**Stream dies every ~2-3 minutes with `deadman timeout`:** This means the session fell back to Wyze's TCP relay servers, which have a hard ~2-3 minute session timeout by design. Set the camera's LAN IP manually so the client can use LAN-direct instead:
```bash
curl -X POST http://<API_IP>:8080/Camera/SetManualIP -H "Content-Type: application/json" -d '{"cameraId":"GW_...","ip":"192.168.x.x"}'
```
After setting it, restart `wyze-p2p` and check the logs for `using LAN direct` instead of `using TCP relay for MTP`. Without a manual LAN IP, every session is relay-only and will cycle every 2-3 minutes indefinitely — this is expected relay-server behavior, not a bug.

**Multiple cameras:** The P2P client automatically discovers and streams all GWell cameras on your account. There's a 15-second stagger between camera connections to avoid P2P server rate limiting.

## Token caching

After the first successful connection, the bridge caches P2P credentials locally (`data/token_cache.json`). Subsequent restarts reconnect to cameras without any Wyze cloud API calls. The cache is valid for 7 days — after that, a single API refresh happens automatically. This means streams survive container restarts, host reboots, and temporary internet outages.

## License

MIT
