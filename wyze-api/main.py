
import os
import re
import subprocess
import sys
import logging
from typing import Optional, Dict
import json
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from wyze_sdk import Client
from wyze_sdk.errors import WyzeClientError
from wyze_sdk.service.base import WpkNetServiceClient
from requests.exceptions import HTTPError
import hashlib
import wyze_sdk.signature

# Monkey-patch: wyze_sdk's md5_string passes non-bytes to hashlib.md5()
def _patched_md5_string(self, body):
    if not isinstance(body, bytes):
        body = str(body).encode('utf-8')
    return hashlib.md5(body).hexdigest()

wyze_sdk.signature.RequestVerifier.md5_string = _patched_md5_string



# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("wyze_api")

app = FastAPI()

WYZE_EMAIL = os.getenv("WYZE_EMAIL")
WYZE_PASSWORD = os.getenv("WYZE_PASSWORD")
API_ID = os.getenv("WYZE_KEY_ID") or os.getenv("API_ID")
API_KEY = os.getenv("WYZE_API_KEY") or os.getenv("API_KEY")
MARS_URL = os.getenv("MARS_URL", "https://wyze-mars-service.wyzecam.com") # Default? Check C# config
MARS_REGISTER_GW_USER_ROUTE = os.getenv("MARS_REGISTER_GW_USER_ROUTE", "/plugin/mars/v2/regist_gw_user/")
# Original C# defaults: GW_BE1_, GW_GC1_, GW_GC2_. Using broader GW_ to catch all GWELL variants (DUO, etc.)
VALID_MARS_DEVICE_PREFIX = os.getenv("VALID_MARS_DEVICE_PREFIX", "GW_") # Comma separated
LAN_SUBNET = os.getenv("LAN_SUBNET", "192.168.0.0/24")
LAN_SCAN_INTERFACE = os.getenv("LAN_SCAN_INTERFACE", "eth0")

# Models
class CameraInfo(BaseModel):
    cameraId: str
    streamName: Optional[str] = None
    lanIp: Optional[str] = None

class AccessCredential(BaseModel):
    accessId: str
    accessToken: str

# Manual IP Persistence
MANUAL_IPS_FILE = "data/manual_ips.json"

def load_manual_ips() -> Dict[str, str]:
    if os.path.exists(MANUAL_IPS_FILE):
        try:
            with open(MANUAL_IPS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load manual IPs: {e}")
    return {}

def save_manual_ips(ips: Dict[str, str]):
    try:
        with open(MANUAL_IPS_FILE, 'w') as f:
            json.dump(ips, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save manual IPs: {e}")

class ManualIPRequest(BaseModel):
    cameraId: str
    ip: str

class UnreachableReport(BaseModel):
    cameraId: str

def mac_from_device_id(device_id: str) -> Optional[str]:
    """GW_GC1 device IDs end in the camera's bare MAC (e.g. GW_GC1_D03F2775AC2F -> d0:3f:27:75:ac:2f)."""
    match = re.search(r"([0-9A-Fa-f]{12})$", device_id)
    if not match:
        return None
    hex_mac = match.group(1)
    return ":".join(hex_mac[i:i + 2] for i in range(0, 12, 2)).lower()

def detect_lan_interface() -> str:
    """Finds which network interface actually holds an address in LAN_SUBNET.
    Docker's interface naming (eth0 vs eth1 etc.) depends on network creation
    order, not the order networks are listed in compose, so this can't be
    hardcoded reliably — LAN_SCAN_INTERFACE is only a fallback if detection fails."""
    try:
        import ipaddress
        net = ipaddress.ip_network(LAN_SUBNET, strict=False)
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "inet":
                addr = parts[3].split("/")[0]
                if ipaddress.ip_address(addr) in net:
                    return parts[1]
    except Exception as e:
        logger.error(f"Failed to auto-detect LAN interface, falling back to {LAN_SCAN_INTERFACE}: {e}")
    return LAN_SCAN_INTERFACE

def discover_ip_by_mac(mac: str) -> Optional[str]:
    """Runs arp-scan against the LAN to find the current IP for a known MAC address."""
    iface = detect_lan_interface()
    try:
        result = subprocess.run(
            ["arp-scan", "--interface", iface, "--quiet", "--plain", "--retry=2", LAN_SUBNET],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        logger.error(f"arp-scan failed to run: {e}")
        return None

    target = mac.lower()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lower() == target:
            return parts[0]
    return None

class WyzeManager:
    def __init__(self):
        self.client: Optional[Client] = None
        self.cameras: Dict[str, CameraInfo] = {}
        self.manual_ips: Dict[str, str] = load_manual_ips()
        # NO token cache — tokens are ONE-TIME USE per the original C# implementation.
        # The IoTVideoSdk is very picky about this. Caching causes ASrv_tmpsubs_parse_fail (8020).
        self.supported_prefixes = [p.strip() for p in VALID_MARS_DEVICE_PREFIX.split(",") if p.strip()]
        self._ready = False  # Set True after startup prefetch completes
        self._consecutive_mars_failures = 0
        self._max_mars_failures = int(os.getenv("MAX_MARS_FAILURES", "3"))

    def login(self):
        if not self.client:
            if not WYZE_EMAIL or not WYZE_PASSWORD:
                logger.error("WYZE_EMAIL or WYZE_PASSWORD not set")
                return
            
            try:
                logger.info(f"Attempting login for {WYZE_EMAIL}")
                self.client = Client(email=WYZE_EMAIL, password=WYZE_PASSWORD, key_id=API_ID, api_key=API_KEY)
                logger.info("Login successful")
            except WyzeClientError as e:
                logger.error(f"Failed to login: {e}")
                self.client = None
            except Exception as e:
                logger.exception(f"Unexpected error during login: {e}")
                self.client = None

    def refresh_cameras(self):
        if not self.client:
            self.login()
        
        if not self.client:
             logger.warning("Cannot refresh cameras, no client")
             return

        try:
            logger.info("Refreshing camera list...")
            response = self.client._api_client().get_object_list()
            
            if not response or not response.data:
                logger.error("Failed to get response from Wyze API")
                return

            # Wyze API returns nested structure: {'code': '1', 'data': {'device_list': [...]}}
            data_dict = response.data.get("data")
            
            if not data_dict or "device_list" not in data_dict:
                logger.error(f"Failed to get device_list from Wyze API response. Keys: {response.data.keys() if response.data else 'None'}")
                return

            devices = data_dict["device_list"]
            logger.info(f"Received {len(devices)} devices from Wyze API")

            new_cameras = {}
            for device in devices:
                mac = device.get("mac")
                nickname = device.get("nickname")
                product_type = device.get("product_type")
                product_model = device.get("product_model")

                # Check filtering
                if self.supported_prefixes:
                     if not any(mac.startswith(p) for p in self.supported_prefixes):
                        continue
                
                # Basic filter for cameras if no specific prefix set
                if not self.supported_prefixes:
                    if product_type in ["Lock", "Scale", "Band", "Plug", "Bulb", "Sensor", "Mesh"]:
                        continue
                    is_camera = "Camera" in (product_type or "") or "Doorbell" in (product_type or "") or "Cam" in (product_model or "")
                    if not is_camera:
                        continue

                safe_nickname = (nickname or mac).lower().replace(' ', '_')
                stream_name = f"live/{safe_nickname}"
                
                # IP Logic: Manual Override > Cloud IP > None
                cloud_ip = device.get("ip")
                final_ip = self.manual_ips.get(mac, cloud_ip)

                new_cameras[mac] = CameraInfo(
                    cameraId=mac,
                    streamName=stream_name,
                    lanIp=final_ip
                )
                
                logger.info(f"Found camera: {mac} ({nickname}) -> {stream_name} [IP: {final_ip} {'(Manual)' if mac in self.manual_ips else '(Cloud)'}]")

            self.cameras = new_cameras
            logger.info(f"Refreshed. Total cameras: {len(self.cameras)}")

        except Exception as e:
            logger.error(f"Failed to refresh cameras: {e}")
            logger.exception("Traceback:")

    def _fetch_token_from_mars(self, device_id: str, _retry: bool = True) -> Optional[AccessCredential]:
        """Makes the actual external API call to Wyze Mars. This is slow (2-4s)."""
        if not self.client:
            self.login()

        if not self.client:
            return None

        try:
            wpk = WpkNetServiceClient(token=self.client._token, base_url=MARS_URL)
            path = MARS_REGISTER_GW_USER_ROUTE + device_id

            logger.info("Calling wpk.api_call...")
            resp = wpk.api_call(
                api_method=path,
                json={
                    "ttl_minutes": 10080,
                    "nonce": wpk.request_verifier.clock.nonce(),
                    "unique_id": wpk.phone_id
                },
                headers={
                    "appid": wpk.app_id
                },
                nonce=wpk.request_verifier.clock.nonce()
            )
            
            data_dict = getattr(resp, "data", None)
            if not data_dict and hasattr(resp, "_data"):
                data_dict = resp._data
            
            if data_dict and isinstance(data_dict, dict):
                 if "data" in data_dict:
                    data = data_dict["data"]

                 self._consecutive_mars_failures = 0
                 return AccessCredential(
                    accessId=data["accessId"],
                    accessToken=data["accessToken"]
                 )

            logger.error(f"Failed to get token response for {device_id}: {resp}")
            self._consecutive_mars_failures += 1
            return None

        except Exception as e:
            logger.exception(f"Error fetching Mars token for {device_id}: {e}")
            self._consecutive_mars_failures += 1

            is_auth_error = isinstance(e, HTTPError) and e.response is not None and e.response.status_code == 401
            if is_auth_error and _retry:
                logger.warning(f"Mars token fetch for {device_id} got 401 — forcing re-login and retrying once")
                self.client = None
                self.login()
                if self.client:
                    return self._fetch_token_from_mars(device_id, _retry=False)

            return None


    def get_fresh_camera_token(self, device_id: str) -> Optional[AccessCredential]:
         return self._fetch_token_from_mars(device_id)

    def set_manual_ip(self, device_id: str, ip: str):
        self.manual_ips[device_id] = ip
        save_manual_ips(self.manual_ips)
        # Update in-memory camera immediately if present
        if device_id in self.cameras:
             # Create a copy to update
             cam = self.cameras[device_id]
             # Pydantic models are immutable-ish by default but we can replace
             new_cam = CameraInfo(cameraId=cam.cameraId, streamName=cam.streamName, lanIp=ip)
             self.cameras[device_id] = new_cam
        logger.info(f"Set manual IP for {device_id} to {ip}")

    def rediscover_ip(self, device_id: str) -> dict:
        """Called when a camera is reported unreachable. Re-resolves its LAN IP
        by MAC via arp-scan and updates the manual override if it changed."""
        mac = mac_from_device_id(device_id)
        if not mac:
            return {"status": "error", "detail": f"Could not derive MAC from {device_id}"}

        old_ip = self.manual_ips.get(device_id)
        logger.info(f"Rediscovering {device_id} (MAC {mac}) via arp-scan on {LAN_SUBNET}...")
        new_ip = discover_ip_by_mac(mac)

        if not new_ip:
            logger.warning(f"arp-scan found no host for MAC {mac} ({device_id})")
            return {"status": "not_found", "mac": mac, "oldIp": old_ip}

        if new_ip == old_ip:
            logger.info(f"{device_id} still at {old_ip} — no change")
            return {"status": "unchanged", "mac": mac, "ip": new_ip}

        logger.info(f"{device_id} moved: {old_ip} -> {new_ip}")
        self.set_manual_ip(device_id, new_ip)
        return {"status": "updated", "mac": mac, "oldIp": old_ip, "newIp": new_ip}


manager = WyzeManager()

@app.on_event("startup")
def startup_event():
    import threading
    def _init():
        try:
            manager.login()
            manager.refresh_cameras()
            manager._ready = True
            logger.info(f"API fully ready — {len(manager.cameras)} cameras discovered")
        except Exception as e:
            logger.exception(f"Startup init failed: {e}")
            manager._ready = True
    threading.Thread(target=_init, daemon=True).start()


@app.get("/health")
def health():
    if not manager._ready:
        raise HTTPException(status_code=503, detail="API starting up, cameras not yet discovered")
    if manager._consecutive_mars_failures >= manager._max_mars_failures:
        raise HTTPException(
            status_code=503,
            detail=f"Wyze Mars auth failing ({manager._consecutive_mars_failures} consecutive errors) — session likely expired"
        )
    return {"status": "ok", "cameras": len(manager.cameras)}


@app.get("/Camera/CameraList")
def get_camera_list():
    return list(manager.cameras.keys())

@app.get("/Camera/DeviceInfo")
def get_device_info(deviceId: str):
    if deviceId in manager.cameras:
        return manager.cameras[deviceId]
    raise HTTPException(status_code=404, detail="Camera not found")

@app.get("/Camera/CameraToken")
def get_camera_token_endpoint(deviceId: str):
    token = manager.get_fresh_camera_token(deviceId)
    if token:
        return token
    raise HTTPException(status_code=500, detail=f"Failed to fetch token for {deviceId}")

@app.post("/Camera/SetManualIP")
def set_manual_ip(req: ManualIPRequest):
    manager.set_manual_ip(req.cameraId, req.ip)
    return {"status": "updated", "cameraId": req.cameraId, "ip": req.ip}

@app.post("/Camera/ReportUnreachable")
def report_unreachable(req: UnreachableReport):
    """Called by the P2P proxy when a camera stops responding at its known
    LAN IP (e.g. deadman timeout). Re-resolves the IP by MAC via arp-scan —
    only runs when something is actually broken, not on a schedule."""
    result = manager.rediscover_ip(req.cameraId)
    return {"cameraId": req.cameraId, **result}

@app.get("/streams")
def streams_dashboard():
    """Dashboard showing camera stream URLs and status (no video embedding)."""
    cams = list(manager.cameras.values())
    mediamtx_host = os.getenv("MEDIAMTX_HOST", "")
    rtsp_port = 8554

    cam_cards = ""
    for cam in cams:
        stream_name = cam.streamName or f"live/{cam.cameraId}"
        friendly_name = stream_name.split("/")[-1].replace("_", " ").title()
        rtsp_url = f"rtsp://{mediamtx_host}:{rtsp_port}/{stream_name}"
        ip_display = cam.lanIp or "Not set"

        cam_cards += f"""
        <div class="cam-card">
            <div class="cam-header">
                <span class="cam-name">{friendly_name}</span>
                <span class="cam-status">Configured</span>
            </div>
            <div class="cam-details">
                <div class="detail-row">
                    <span class="label">Device ID</span>
                    <span class="value mono">{cam.cameraId}</span>
                </div>
                <div class="detail-row">
                    <span class="label">LAN IP</span>
                    <span class="value ip-edit" onclick="setIp('{cam.cameraId}', '{cam.lanIp or ''}')">{ip_display}</span>
                </div>
                <div class="detail-row">
                    <span class="label">RTSP URL</span>
                    <span class="value mono selectable">{rtsp_url}</span>
                </div>
            </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Wyze GWell Bridge</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ background: #f5f5f5; color: #333; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; }}
        .header {{ text-align: center; margin-bottom: 30px; }}
        .header h1 {{ font-size: 1.6em; color: #222; }}
        .header p {{ color: #888; margin-top: 4px; }}
        .actions {{ text-align: center; margin-bottom: 20px; }}
        .btn {{ background: #2563eb; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.9em; }}
        .btn:hover {{ background: #1d4ed8; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 16px; max-width: 900px; margin: 0 auto; }}
        .cam-card {{ background: #fff; border-radius: 8px; border: 1px solid #e0e0e0; overflow: hidden; }}
        .cam-header {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; border-bottom: 1px solid #eee; }}
        .cam-name {{ font-weight: 600; font-size: 1.15em; }}
        .cam-status {{ background: #e3f2fd; color: #1565c0; padding: 3px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 500; }}
        .cam-details {{ padding: 14px 18px; }}
        .detail-row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #f5f5f5; }}
        .detail-row:last-child {{ border-bottom: none; }}
        .label {{ color: #888; font-size: 0.85em; }}
        .value {{ font-size: 0.9em; text-align: right; }}
        .mono {{ font-family: 'SF Mono', Monaco, Consolas, monospace; font-size: 0.82em; }}
        .selectable {{ user-select: all; cursor: text; }}
        .ip-edit {{ cursor: pointer; color: #2563eb; }}
        .ip-edit:hover {{ text-decoration: underline; }}
    </style>
    <script>
        function setIp(mac, currentIp) {{
            const newIp = prompt('Set LAN IP for ' + mac + ':', currentIp || '');
            if (newIp !== null && newIp !== currentIp) {{
                fetch('/Camera/SetManualIP', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{cameraId: mac, ip: newIp}})
                }}).then(r => r.json()).then(() => location.reload());
            }}
        }}
        function refreshCameras() {{
            fetch('/Camera/GetAllSupportedCameras', {{method: 'POST'}})
                .then(() => setTimeout(() => location.reload(), 5000));
        }}
    </script>
</head>
<body>
    <div class="header">
        <h1>Wyze GWell Bridge</h1>
        <p>{len(cams)} camera(s) configured</p>
    </div>
    <div class="actions">
        <button class="btn" onclick="refreshCameras()">Refresh Cameras</button>
    </div>
    <div class="grid">
        {cam_cards}
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)

@app.post("/Camera/GetAllSupportedCameras")
def trigger_refresh_cameras(background_tasks: BackgroundTasks):
    background_tasks.add_task(manager.refresh_cameras)
    return {"status": "refresh_queued"}

@app.get("/")
def root():
    """Redirect to /streams dashboard."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/streams")

