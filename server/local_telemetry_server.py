#!/usr/bin/env python3
import json
import os
import platform
import socket
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import median
from typing import Any, Deque, Dict

import psutil


def gb(value: float) -> float:
    return round(float(value) / (1024**3), 1)


_cpu_samples: Deque[float] = deque(maxlen=12)
_cpu_lock = threading.Lock()
_cpu_ready = threading.Event()


def _cpu_sampler() -> None:
    # Prime psutil baseline once, then keep sampling in background.
    psutil.cpu_percent(interval=None)
    while True:
        try:
            value = float(psutil.cpu_percent(interval=0.5))
            with _cpu_lock:
                _cpu_samples.append(value)
            _cpu_ready.set()
        except Exception:
            # Keep sampler alive even if psutil glitches.
            continue


def _read_smoothed_cpu_percent() -> float:
    if _cpu_ready.is_set():
        with _cpu_lock:
            if _cpu_samples:
                # Median is more stable than mean for short transient spikes/drops.
                return round(float(median(_cpu_samples)), 1)
    # Early fallback before first sampler tick.
    return round(float(psutil.cpu_percent(interval=None)), 1)


def collect_system_snapshot() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.expanduser("~"))
    load = None
    try:
        load = os.getloadavg()
    except Exception:
        load = (None, None, None)

    cpu_cores = os.cpu_count() or 1
    cpu_percent = _read_smoothed_cpu_percent()

    load_one = round(float(load[0]), 2) if load[0] is not None else None
    load_five = round(float(load[1]), 2) if load[1] is not None else None
    load_fifteen = round(float(load[2]), 2) if load[2] is not None else None
    load_per_core = round(float(load[0]) / cpu_cores, 3) if load[0] is not None and cpu_cores else None

    # Guard against occasional 0.0 snapshots when the machine clearly has load.
    if load_per_core is not None:
        inferred_cpu_from_load = round(max(0.0, min(100.0, load_per_core * 100.0)), 1)
        if cpu_percent < 0.5 and inferred_cpu_from_load >= 1.0:
            cpu_percent = inferred_cpu_from_load

    process_memory_mb = round(float(psutil.Process().memory_info().rss) / (1024**2), 1)

    if disk.percent >= 92 or (load_per_core is not None and load_per_core >= 2.0):
        health = "degraded"
    else:
        health = "healthy"

    summary = f"Load {load_per_core:.2f}/core, CPU {cpu_percent:.1f}%, RAM {vm.percent:.1f}%, Disk {disk.percent:.1f}%, RSS {process_memory_mb:.1f} MB" if load_per_core is not None else f"CPU {cpu_percent:.1f}%, RAM {vm.percent:.1f}%, Disk {disk.percent:.1f}%, RSS {process_memory_mb:.1f} MB"

    return {
        "source": "local-psutil",
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "health": health,
        "host": socket.gethostname() or "unknown",
        "platform": f"{platform.system()} {platform.release()}".strip(),
        "platformVersion": platform.version() or "",
        "cpuCores": cpu_cores,
        "cpuUsagePercent": cpu_percent,
        "ramUsage": {
            "usedPercent": round(float(vm.percent), 1),
            "usedGb": gb(vm.used),
            "availableGb": gb(vm.available),
            "totalGb": gb(vm.total),
        },
        "loadAverage": {
            "one": load_one,
            "five": load_five,
            "fifteen": load_fifteen,
            "perCore": load_per_core,
        },
        "diskUsage": {
            "path": "~",
            "usedPercent": round(float(disk.percent), 1),
            "freeGb": gb(disk.free),
            "totalGb": gb(disk.total),
        },
        "processMemoryMb": process_memory_mb,
        "summary": summary,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "mission-control-local-telemetry", "source": "local-psutil"})
            return
        if self.path == "/api/local/system":
            self._json(200, collect_system_snapshot())
            return
        self._json(404, {"error": "not_found", "path": self.path})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def main() -> None:
    host = os.getenv("MISSION_CONTROL_LOCAL_TELEMETRY_HOST", "127.0.0.1")
    port = int(os.getenv("MISSION_CONTROL_LOCAL_TELEMETRY_PORT", "8765"))

    sampler = threading.Thread(target=_cpu_sampler, name="mc-cpu-sampler", daemon=True)
    sampler.start()

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[mission-control-local-telemetry] listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
