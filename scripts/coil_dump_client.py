#!/usr/bin/env python3
"""Stream U1 inductance-coil frequency and visualize / save CSV.

Uses Klipper's bulk-sensor dump endpoint (same pattern as ADXL dumps):

    inductance_coil/dump_inductance_coil  (mux key: sensor=<name>)

This is NOT a Prusa-style loadcell force stream — U1 exposes oscillation
*frequency* (Hz) as a back-pressure / displacement proxy.

Connection (pick one):

  # Direct Klippy unix domain socket (preferred; works on the printer host
  # or via SSH -L tunnel):
  python3 scripts/coil_dump_client.py --uds ~/printer_data/comms/klippy.sock \\
      --sensor extruder2

  # Over the network via Moonraker's Klippy *bridge* (not /websocket):
  #   /websocket  = Moonraker JSON-RPC  → Method not found for dump_*
  #   /klippysocket = Klippy dump bridge (use this; --url auto-rewrites /websocket)
  python3 scripts/coil_dump_client.py --url ws://192.168.1.50/websocket \\
      --sensor extruder2

  # Offline plot of a FREQUENCY_MEASURE CSV:
  python3 scripts/coil_dump_client.py --csv-in frequency-extruder2-run.csv

Requires: Python 3.8+, numpy, matplotlib. Optional: websockets (for --url).

Ctrl+C stops capture, saves CSV (if --csv), and freezes the plot.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import select
import socket
import sys
import threading
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

Sample = Tuple[float, float]  # time_s, frequency_hz


# ---------------------------------------------------------------------------
# Klippy UDS (webhooks) transport — messages framed with 0x03
# ---------------------------------------------------------------------------

class KlippyUdsClient:
    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)
        self.sock.setblocking(False)
        self._buf = b""
        self._id = 0
        self._lock = threading.Lock()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def send(self, obj: dict) -> int:
        if "id" not in obj:
            obj = dict(obj)
            obj["id"] = self._next_id()
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\x03"
        with self._lock:
            self.sock.sendall(raw)
        return obj["id"]

    def recv_messages(self, timeout: float = 0.2) -> List[dict]:
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return []
        try:
            chunk = self.sock.recv(65536)
        except BlockingIOError:
            return []
        if not chunk:
            raise ConnectionError("Klippy UDS closed")
        self._buf += chunk
        out: List[dict] = []
        while b"\x03" in self._buf:
            line, self._buf = self._buf.split(b"\x03", 1)
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line.decode("utf-8")))
            except json.JSONDecodeError:
                sys.stderr.write("WARN: bad JSON from UDS: %r\n" % (line[:120],))
        return out

    def start_coil_dump(self, sensor: str) -> None:
        # response_template marks streaming batches (same idea as ADXL dump clients)
        self.send({
            "id": self._next_id(),
            "method": "inductance_coil/dump_inductance_coil",
            "params": {
                "sensor": sensor,
                "response_template": {"key": "coil_dump"},
            },
        })


# ---------------------------------------------------------------------------
# Moonraker *bridge* WebSocket — ws://host/klippysocket
# ---------------------------------------------------------------------------
# Dump endpoints (adxl345/dump_*, inductance_coil/dump_*, …) must use
# Moonraker's Klippy bridge, NOT the primary /websocket JSON-RPC API.
# https://moonraker.readthedocs.io/en/latest/external_api/introduction/
# Bridge speaks Klippy framing: JSON + 0x03 separator.
# ---------------------------------------------------------------------------

def moonraker_bridge_url(url: str) -> str:
    """Map ws://host/websocket -> ws://host/klippysocket."""
    u = url.strip().rstrip("/")
    if u.endswith("/websocket"):
        u = u[: -len("/websocket")] + "/klippysocket"
    elif not u.endswith("/klippysocket"):
        u = u + "/klippysocket"
    if u.startswith("http://"):
        u = "ws://" + u[len("http://") :]
    elif u.startswith("https://"):
        u = "wss://" + u[len("https://") :]
    return u


class KlippyBridgeWsClient:
    """Klippy dump API over Moonraker /klippysocket (0x03-framed JSON)."""

    def __init__(self, url: str, api_key: Optional[str] = None):
        try:
            import websockets.sync.client as ws_sync
        except ImportError as e:
            raise SystemExit(
                "Network mode needs: pip install websockets\n%s" % (e,)
            ) from e
        self._connect = ws_sync.connect
        self.url = moonraker_bridge_url(url)
        self.headers = {}
        if api_key:
            self.headers["X-Api-Key"] = api_key
        self.ws = None
        self._id = 0
        self._buf = ""

    def connect(self):
        kwargs = {}
        if self.headers:
            kwargs["additional_headers"] = self.headers
        print("Connecting to Klippy bridge: %s" % self.url)
        try:
            self.ws = self._connect(self.url, **kwargs)
        except Exception as e:
            raise SystemExit(
                "Bridge connect failed (%s): %s\n\n"
                "On many printers (including Snapmaker U1), nginx does not expose\n"
                "/klippysocket for dump streams. Use instead:\n\n"
                "  # G-code capture via Moonraker HTTP (works from a laptop):\n"
                "  python scripts/coil_dump_client.py --moonraker http://PRINTER_IP \\\n"
                "      --sensor extruder2 --duration 120\n\n"
                "  Then run APA_COIL_CENTER (or similar) in Fluidd while it captures.\n"
                % (self.url, e)
            ) from e

    def close(self):
        if self.ws is not None:
            self.ws.close()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def send(self, obj: dict) -> int:
        if "id" not in obj:
            obj = dict(obj)
            obj["id"] = self._next_id()
        raw = json.dumps(obj, separators=(",", ":")) + "\x03"
        self.ws.send(raw)
        return obj["id"]

    def recv_messages(self, timeout: float = 0.2) -> List[dict]:
        try:
            chunk = self.ws.recv(timeout=timeout)
        except TimeoutError:
            return []
        except Exception:
            return []
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        self._buf += chunk
        out: List[dict] = []
        if "\x03" in self._buf:
            parts = self._buf.split("\x03")
            self._buf = parts.pop()
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                try:
                    out.append(json.loads(part))
                except json.JSONDecodeError:
                    sys.stderr.write(
                        "WARN: bad JSON from bridge: %r\n" % (part[:120],)
                    )
        else:
            try:
                out.append(json.loads(self._buf))
                self._buf = ""
            except json.JSONDecodeError:
                pass
        return out

    def start_coil_dump(self, sensor: str) -> None:
        mid = self.send({
            "method": "inductance_coil/dump_inductance_coil",
            "params": {
                "sensor": sensor,
                "response_template": {"key": "coil_dump"},
            },
        })
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            for msg in self.recv_messages(0.5):
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise SystemExit(
                            "Dump subscribe failed: %s\n"
                            "Check sensor name and [inductance_coil …] config."
                            % (msg["error"],)
                        )
                    print("Dump subscribed: %s" % (msg.get("result"),))
                    return
                if extract_samples(msg):
                    return
        print("WARN: no explicit subscribe ack (continuing anyway)")

# ---------------------------------------------------------------------------
# Sample extraction
# ---------------------------------------------------------------------------

def extract_samples(msg: dict) -> List[Sample]:
    """Pull (t, freq) pairs from dump batch messages (UDS or Moonraker-shaped)."""
    samples: List[Sample] = []

    # UDS webhook style: {"params": {"data": [[t,f],...], ...}, "key": "coil_dump"}
    params = msg.get("params")
    if isinstance(params, dict) and "data" in params:
        for row in params["data"]:
            if row is None or len(row) < 2:
                continue
            samples.append((float(row[0]), float(row[1])))
        return samples

    # Sometimes nested under result
    result = msg.get("result")
    if isinstance(result, dict) and "data" in result:
        for row in result["data"]:
            if row is None or len(row) < 2:
                continue
            samples.append((float(row[0]), float(row[1])))
        return samples

    # Moonraker method response error
    if "error" in msg:
        err = msg["error"]
        sys.stderr.write("API error: %s\n" % (err,))
    return samples


def load_csv(path: str) -> List[Sample]:
    samples: List[Sample] = []
    with open(path, newline="") as f:
        # Skip optional header
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].lower().startswith("time"):
                continue
            try:
                samples.append((float(row[0]), float(row[1])))
            except (ValueError, IndexError):
                continue
    return samples


def save_csv(path: str, samples: List[Sample]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "frequency"])
        for t, freq in samples:
            w.writerow(["%.6f" % t, "%.0f" % freq])
    print("Wrote %d samples to %s" % (len(samples), path))


# ---------------------------------------------------------------------------
# Live plot
# ---------------------------------------------------------------------------

class LivePlotter:
    def __init__(self, title: str, window_s: float = 30.0):
        import matplotlib.pyplot as plt

        self.plt = plt
        self.window_s = window_s
        self.times: Deque[float] = deque()
        self.freqs: Deque[float] = deque()
        self.t0: Optional[float] = None
        self.fig, self.ax = plt.subplots(figsize=(10, 5))
        self.fig.canvas.manager.set_window_title(title)
        self.line, = self.ax.plot([], [], lw=1.0, color="#1f77b4")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Coil frequency (Hz)")
        self.ax.set_title(title)
        self.ax.grid(True, alpha=0.3)
        self.status = self.ax.text(
            0.02, 0.98, "", transform=self.ax.transAxes,
            va="top", ha="left", fontsize=9,
            bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5),
        )
        plt.ion()
        plt.show(block=False)

    def add(self, samples: List[Sample]) -> None:
        for t, f in samples:
            if self.t0 is None:
                self.t0 = t
            rel = t - self.t0
            self.times.append(rel)
            self.freqs.append(f)
        # trim window
        while self.times and (self.times[-1] - self.times[0]) > self.window_s:
            self.times.popleft()
            self.freqs.popleft()

    def draw(self, total_n: int) -> None:
        if not self.times:
            self.fig.canvas.draw_idle()
            self.fig.canvas.flush_events()
            return
        xs = list(self.times)
        ys = list(self.freqs)
        self.line.set_data(xs, ys)
        self.ax.relim()
        self.ax.autoscale_view()
        y0, y1 = min(ys), max(ys)
        mid = 0.5 * (y0 + y1)
        span = max(y1 - y0, 1.0)
        self.status.set_text(
            "n=%d  f=%.0f Hz  range=%.0f..%.0f  (delta=%.0f)"
            % (total_n, ys[-1], y0, y1, span)
        )
        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()
        self.plt.pause(0.001)

    def block(self) -> None:
        self.plt.ioff()
        self.plt.show()


def plot_static(samples: List[Sample], title: str) -> None:
    import matplotlib.pyplot as plt
    import numpy as np

    if not samples:
        raise SystemExit("No samples to plot")
    t0 = samples[0][0]
    ts = np.array([s[0] - t0 for s in samples])
    fs = np.array([s[1] for s in samples])
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(ts, fs, lw=1.0)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Coil frequency (Hz)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    # Detrended view optional second axis if variation is small vs absolute
    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Moonraker HTTP: FREQUENCY_MEASURE start/stop + download CSV
# ---------------------------------------------------------------------------

class MoonrakerHttp:
    def __init__(self, base: str, api_key: Optional[str] = None):
        self.base = base.rstrip("/")
        if self.base.startswith("ws://"):
            self.base = "http://" + self.base[len("ws://") :]
        elif self.base.startswith("wss://"):
            self.base = "https://" + self.base[len("wss://") :]
        for suffix in ("/websocket", "/klippysocket"):
            if self.base.endswith(suffix):
                self.base = self.base[: -len(suffix)]
        self.api_key = api_key

    def _headers(self, content_type: Optional[str] = "application/json") -> dict:
        h = {}
        if content_type:
            h["Content-Type"] = content_type
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    def _open(self, req, timeout: float = 15.0):
        import urllib.error
        import urllib.request

        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError("HTTP %s: %s" % (e.code, body)) from e
        except urllib.error.URLError as e:
            raise RuntimeError("URL error talking to %s: %s" % (self.base, e)) from e

    def get_json(self, path: str, timeout: float = 10.0) -> dict:
        import urllib.request

        req = urllib.request.Request(
            self.base + path, headers=self._headers(None), method="GET"
        )
        with self._open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_json(self, path: str, body: dict, timeout: float = 15.0) -> dict:
        import urllib.request

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, headers=self._headers(), method="POST"
        )
        with self._open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def ping(self) -> dict:
        return self.get_json("/server/info", timeout=5.0)

    def run_gcode(self, script: str, timeout: float = 20.0) -> None:
        self.post_json(
            "/printer/gcode/script", {"script": script}, timeout=timeout
        )

    def download(self, root: str, filename: str, dest: str) -> None:
        import urllib.request
        from urllib.parse import quote

        path = "/server/files/%s/%s" % (root, quote(filename, safe="/"))
        req = urllib.request.Request(
            self.base + path, headers=self._headers(None), method="GET"
        )
        with self._open(req, timeout=30) as resp:
            data = resp.read()
        with open(dest, "wb") as f:
            f.write(data)

def run_moonraker_gcode_capture(args: argparse.Namespace) -> None:
    """Start FREQUENCY_MEASURE, wait, stop+save CSV via Moonraker, then plot.

    This avoids /klippysocket (often not exposed). Capture is buffered on the
    printer, then downloaded — not a true live stream, but works from a laptop.
    """
    sensor = args.sensor
    mr = MoonrakerHttp(args.moonraker, api_key=args.api_key)
    name = args.name or time.strftime("%m%d_%H%M")
    # FREQUENCY_MEASURE is muxed on PROBE=<coil name>
    start_cmd = "FREQUENCY_MEASURE PROBE=%s" % sensor
    stop_cmd = "FREQUENCY_MEASURE PROBE=%s NAME=%s" % (sensor, name)

    print("Moonraker HTTP capture @ %s" % mr.base)
    print("Sensor/PROBE=%s  NAME=%s" % (sensor, name))

    print("Checking /server/info ...")
    try:
        info = mr.ping()
        st = info.get("result", info)
        print("  klippy_state=%s" % (st.get("klippy_state"),))
    except Exception as e:
        raise SystemExit(
            "Cannot reach Moonraker at %s (%s)\n"
            "Try: curl -m 5 %s/server/info" % (mr.base, e, mr.base)
        ) from e

    # Ensure idle-ish: long APA runs can make gcode/script wait a long time
    print("Sending: %s" % start_cmd)
    try:
        mr.run_gcode(start_cmd, timeout=15.0)
    except Exception as e:
        raise SystemExit(
            "Failed to start FREQUENCY_MEASURE: %s\n\n"
            "Tips:\n"
            "  - Printer must be Idle (not mid-print / mid-APA suite)\n"
            "  - Try in Fluidd console:  %s\n"
            "  - If Fluidd works, use manual start/stop then:\n"
            "      python scripts/coil_dump_client.py --csv-in <downloaded.csv>\n"
            % (e, start_cmd)
        ) from e

    print("Capture started on printer.")
    print("Now run your APA macro / extrusion in Fluidd.")
    if args.duration and args.duration > 0:
        print("Waiting %.0f s ..." % args.duration)
        try:
            time.sleep(args.duration)
        except KeyboardInterrupt:
            print("\nStopping early...")
    else:
        print("Press Enter when the test is finished (or Ctrl+C)...")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            print()

    print("Sending: %s" % stop_cmd)
    try:
        mr.run_gcode(stop_cmd, timeout=30.0)
    except Exception as e:
        raise SystemExit(
            "Failed to stop FREQUENCY_MEASURE: %s\n"
            "In Fluidd try: %s\n"
            "Then download gcodes/frequency_data/frequency-%s-%s.csv"
            % (e, stop_cmd, sensor, name)
        ) from e

    # write_to_file() spawns a background process and returns immediately.
    # A multi-minute capture is ~300k+ lines; 2–3 s is not enough and yields a
    # truncated download. Wait longer, then still prefer the on-printer file if unsure.
    rel = "frequency_data/frequency-%s-%s.csv" % (sensor, name)
    dest = args.csv or ("coil_%s_%s.csv" % (sensor, name))
    if args.duration and args.duration > 0:
        write_wait = max(12.0, min(90.0, 5.0 + 0.04 * args.duration))
    else:
        write_wait = 20.0
    print(
        "Waiting %.0fs for async CSV write on printer (FREQUENCY_MEASURE "
        "write_to_file is backgrounded)..." % write_wait
    )
    time.sleep(write_wait)

    print("Downloading gcodes/%s -> %s" % (rel, dest))
    try:
        mr.download("gcodes", rel, dest)
    except Exception as e:
        print("Download failed: %s" % e)
        print(
            "Manual path: Fluidd → Files → gcodes/frequency_data/\n"
            "  frequency-%s-%s.csv  then:\n"
            "  python scripts/coil_dump_client.py --csv-in thatfile.csv"
            % (sensor, name)
        )
        raise SystemExit(1) from e

    samples = load_csv(dest)
    print("Loaded %d samples from %s" % (len(samples), dest))
    if not samples:
        raise SystemExit("CSV empty — measure may not have recorded samples")
    if len(samples) >= 2:
        dur = samples[-1][0] - samples[0][0]
        print("Capture duration: %.1f s (%d samples)" % (dur, len(samples)))
        print(
            "Tip: if this looks short vs your test wall-clock, re-download the "
            "full file from Fluidd → gcodes/frequency_data/frequency-%s-%s.csv"
            % (sensor, name)
        )
    if not args.no_plot:
        plot_static(samples, "U1 coil — %s (%s)" % (sensor, name))

# ---------------------------------------------------------------------------
# Main capture loop
# ---------------------------------------------------------------------------

def run_stream(args: argparse.Namespace) -> None:
    sensor = args.sensor
    all_samples: List[Sample] = []

    if args.uds:
        client: object = KlippyUdsClient(args.uds)
        print("Connected to Klippy UDS: %s" % args.uds)
        client.start_coil_dump(sensor)
    elif args.url:
        client = KlippyBridgeWsClient(args.url, api_key=args.api_key)
        client.connect()
        client.start_coil_dump(sensor)
    else:
        raise SystemExit("Provide --uds or --url (or --csv-in for offline plot)")

    print("Streaming sensor=%r  (Ctrl+C to stop)" % (sensor,))
    print("Note: extrude or run a cal while this runs to see dynamics.")

    plotter = None
    if not args.no_plot:
        try:
            plotter = LivePlotter(
                title="U1 coil frequency — %s" % sensor,
                window_s=args.window,
            )
        except Exception as e:
            print("Live plot unavailable (%s); continuing CSV-only." % e)
            plotter = None

    t_end = None
    if args.duration and args.duration > 0:
        t_end = time.monotonic() + args.duration

    last_draw = 0.0
    try:
        while True:
            if t_end is not None and time.monotonic() >= t_end:
                print("Duration reached.")
                break
            msgs = client.recv_messages(0.25)
            new: List[Sample] = []
            for msg in msgs:
                new.extend(extract_samples(msg))
            if new:
                all_samples.extend(new)
                if plotter:
                    plotter.add(new)
            now = time.monotonic()
            if plotter and (now - last_draw) > 0.1:
                plotter.draw(len(all_samples))
                last_draw = now
            if not new and not msgs:
                if plotter:
                    plotter.draw(len(all_samples))
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        try:
            client.close()
        except Exception:
            pass

    print("Captured %d samples." % len(all_samples))
    if args.csv and all_samples:
        save_csv(args.csv, all_samples)
    if plotter is not None:
        print("Close the plot window to exit.")
        plotter.block()
    elif all_samples and not args.no_plot:
        plot_static(all_samples, "U1 coil frequency — %s" % sensor)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Stream/visualize U1 inductance coil frequency (Hz)."
    )
    p.add_argument(
        "--uds",
        default=None,
        help="Klippy unix socket path (e.g. ~/printer_data/comms/klippy.sock "
             "or /tmp/klippy_uds). Preferred for dump_inductance_coil.",
    )
    p.add_argument(
        "--url",
        default=None,
        help="ws://host/websocket or ws://host/klippysocket for live dump "
             "(U1 often blocks /klippysocket; prefer --moonraker).",
    )
    p.add_argument(
        "--moonraker",
        default=None,
        help="Moonraker HTTP base, e.g. http://192.168.1.217 — uses "
             "FREQUENCY_MEASURE on the printer then downloads CSV (laptop-friendly).",
    )
    p.add_argument(
        "--name",
        default=None,
        help="NAME tag for FREQUENCY_MEASURE CSV (default: timestamp)",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="Optional Moonraker API key",
    )
    p.add_argument(
        "--sensor",
        default="extruder",
        help="Coil mux name: extruder, extruder1, extruder2, extruder3 "
             "(default: extruder)",
    )
    p.add_argument(
        "--csv",
        default=None,
        help="Write/download samples to this CSV path",
    )
    p.add_argument(
        "--csv-in",
        default=None,
        help="Plot an existing FREQUENCY_MEASURE CSV offline (no stream)",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="With --moonraker: wait N seconds then stop (0 = press Enter). "
             "With live dump: auto-stop after N seconds.",
    )
    p.add_argument(
        "--window",
        type=float,
        default=30.0,
        help="Live plot time window in seconds (default 30)",
    )
    p.add_argument(
        "--no-plot",
        action="store_true",
        help="Do not open a plot window (CSV only)",
    )
    args = p.parse_args()

    if args.csv_in:
        samples = load_csv(args.csv_in)
        print("Loaded %d samples from %s" % (len(samples), args.csv_in))
        if args.csv:
            save_csv(args.csv, samples)
        if not args.no_plot:
            plot_static(samples, os.path.basename(args.csv_in))
        return

    if args.moonraker:
        run_moonraker_gcode_capture(args)
        return

    if not args.uds and not args.url:
        p.error("Need --moonraker, --uds, --url, or --csv-in")

    # Default CSV path if streaming and none given
    if args.csv is None and not args.no_plot:
        args.csv = time.strftime("coil_%Y%m%d_%H%M%S.csv")
        print("Will save CSV to %s on exit (override with --csv)." % args.csv)

    run_stream(args)


if __name__ == "__main__":
    main()
