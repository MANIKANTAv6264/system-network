import asyncio
import json
import time
import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Live System Monitor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GPU_AVAILABLE = False
try:
    import pynvml

    pynvml.nvmlInit()
    GPU_AVAILABLE = True
except Exception:
    GPU_AVAILABLE = False

def get_gpu_stats():
    """Returns a list of GPU dicts, or [] if no NVIDIA GPU / pynvml missing."""
    if not GPU_AVAILABLE:
        return []

    gpus = []
    try:
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = None
            gpus.append(
                {
                    "name": name,
                    "load_percent": util.gpu,
                    "vram_used_mb": round(mem.used / (1024 ** 2), 1),
                    "vram_total_mb": round(mem.total / (1024 ** 2), 1),
                    "temp_c": temp,
                }
            )
    except Exception:
        return []
    return gpus

import subprocess
import platform

_drive_type_cache = {}
_drive_type_last_refresh = 0
DRIVE_TYPE_REFRESH_SECONDS = 30  # media type doesn't change often; avoid shelling out every poll

def _refresh_drive_types_windows():
    """
    Uses PowerShell to map DriveLetter -> MediaType (SSD/HDD/Unknown).
    Runs a Get-Partition -> Get-Disk -> Get-PhysicalDisk chain.
    Cached and refreshed only every DRIVE_TYPE_REFRESH_SECONDS.
    """
    global _drive_type_cache, _drive_type_last_refresh
    script = (
        "Get-Partition | Where-Object DriveLetter | ForEach-Object {"
        "  $part = $_;"
        "  $disk = Get-Disk -Number $part.DiskNumber -ErrorAction SilentlyContinue;"
        "  $phys = if ($disk) { Get-PhysicalDisk -DeviceNumber $disk.Number -ErrorAction SilentlyContinue } else { $null };"
        "  [PSCustomObject]@{ DriveLetter = $part.DriveLetter; MediaType = $phys.MediaType }"
        "} | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(result.stdout.strip()) if result.stdout.strip() else []
        if isinstance(data, dict):
            data = [data]
        new_cache = {}
        for entry in data:
            letter = entry.get("DriveLetter")
            media = entry.get("MediaType") or "Unknown"
            if letter:
                new_cache[f"{letter}:"] = media
        _drive_type_cache = new_cache
    except Exception:
        pass  # keep old cache (or empty) if this fails
    _drive_type_last_refresh = time.time()

def get_drive_type(mountpoint: str) -> str:
    """Returns 'SSD', 'HDD', or 'Unknown' for a given mountpoint like 'C:\\'."""
    if platform.system() != "Windows":
        return "Unknown"

    if time.time() - _drive_type_last_refresh > DRIVE_TYPE_REFRESH_SECONDS:
        _refresh_drive_types_windows()

    letter = mountpoint.rstrip("\\").rstrip("/")  # 'C:\\' -> 'C:'
    return _drive_type_cache.get(letter, "Unknown")

def get_disks():
    """Returns a list of every real fixed drive with usage + type label."""
    disks = []
    for part in psutil.disk_partitions(all=False):
        # Skip CD-ROM / empty removable drives that error out
        if "cdrom" in part.opts or part.fstype == "":
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, FileNotFoundError, OSError):
            continue

        disks.append(
            {
                "mountpoint": part.mountpoint,
                "device": part.device,
                "fstype": part.fstype,
                "type": get_drive_type(part.mountpoint),
                "percent": usage.percent,
                "used_gb": round(usage.used / (1024 ** 3), 2),
                "total_gb": round(usage.total / (1024 ** 3), 2),
            }
        )
    return disks

_last_net = psutil.net_io_counters()
_last_time = time.time()

def get_network_speed():
    global _last_net, _last_time
    now = time.time()
    current = psutil.net_io_counters()
    elapsed = max(now - _last_time, 0.001)

    upload_kbps = (current.bytes_sent - _last_net.bytes_sent) / 1024 / elapsed
    download_kbps = (current.bytes_recv - _last_net.bytes_recv) / 1024 / elapsed

    _last_net = current
    _last_time = now
    return round(upload_kbps, 1), round(download_kbps, 1)

def collect_stats():
    cpu_percent = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    freq = psutil.cpu_freq()
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    upload_kbps, download_kbps = get_network_speed()

    try:
        temps = psutil.sensors_temperatures()
        cpu_temp = None
        for entries in temps.values():
            if entries:
                cpu_temp = entries[0].current
                break
    except Exception:
        cpu_temp = None

    try:
        battery = psutil.sensors_battery()
        battery_percent = battery.percent if battery else None
    except Exception:
        battery_percent = None

    return {
        "timestamp": time.time(),
        "cpu": {
            "percent": cpu_percent,
            "per_core": per_core,
            "freq_mhz": round(freq.current, 0) if freq else None,
            "cores_logical": psutil.cpu_count(logical=True),
            "cores_physical": psutil.cpu_count(logical=False),
            "temp_c": cpu_temp,
        },
        "ram": {
            "percent": mem.percent,
            "used_gb": round(mem.used / (1024 ** 3), 2),
            "total_gb": round(mem.total / (1024 ** 3), 2),
            "available_gb": round(mem.available / (1024 ** 3), 2),
        },
        "swap": {
            "percent": swap.percent,
            "used_gb": round(swap.used / (1024 ** 3), 2),
            "total_gb": round(swap.total / (1024 ** 3), 2),
        },
        "disk": get_disks(),
        "network": {
            "upload_kbps": upload_kbps,
            "download_kbps": download_kbps,
        },
        "gpu": get_gpu_stats(),
        "battery_percent": battery_percent,
    }

@app.websocket("/ws")
async def stats_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = collect_stats()
            await websocket.send_text(json.dumps(data))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass

@app.get("/")
async def index():
    return FileResponse("index.html")

app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("Starting server at http://localhost:8000  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=8000)