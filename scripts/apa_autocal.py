#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["websockets>=14"]
# ///
"""Run the U1 Adaptive PA suite and finish every test point automatically.

Drives the ``APA_COIL_CELL`` macro from ``adaptive_pa_macro.cfg`` over
Moonraker's websocket, reads the ``k0.015: area=-5421.000`` lines that
``FLOW_MEASURE_K`` prints, finds the sign flip, scores it exactly like
``APA_FINISH_CELL`` and writes the OrcaSlicer Adaptive PA rows. No copying
numbers out of the console.

When a sweep never changes sign the point is re-run with the K range moved
toward the zero crossing; when confidence is low it is re-run with a fine
0.001 step between the bracket ends.

  # Full suite on T0 (envelope comes from _APA_COIL_CFG on the printer)
  python scripts/apa_autocal.py --host 192.168.1.50 --tools 0 --temp 220

  # Tag the session with the filament (saved in results and in the folder name)
  python scripts/apa_autocal.py --host 192.168.1.50 --tools 0 --filament "Polymaker PLA Pro"

  # Several tools, skip the stress corner, only print the plan
  python scripts/apa_autocal.py --host 192.168.1.50 --tools 0,1,2,3 --skip-stress --dry-run

  # Continue an interrupted session (finished points are kept)
  python scripts/apa_autocal.py --host 192.168.1.50 --resume apa_results/20261020_141500

  # Offline: finish points from a console log saved from Fluidd/Mainsail
  python scripts/apa_autocal.py --parse-log console.txt

Requires: Python 3.9+, websockets 14+ (not needed for --parse-log).
With uv, ``uv run scripts/apa_autocal.py ...`` installs both automatically.
Outputs go to apa_results/<timestamp>[_<filament>]/ (results.json, orca_T<n>.txt,
console.log).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants shared with adaptive_pa_macro.cfg
# ---------------------------------------------------------------------------

CFG_MACRO = "gcode_macro _APA_COIL_CFG"
ENVELOPE_KEYS = ("line_area", "ratio", "slow_frac", "temp", "k_min", "k_max", "k_step",
                 "low_speed", "mid_speed", "high_speed", "low_accel", "mid_accel", "high_accel")

# (name, speed variable, accel variable) in the order APA_COIL_RUN_ALL uses
POINTS = [
    ("low_anchor", "low_speed", "low_accel"),
    ("high_flow", "high_speed", "low_accel"),
    ("high_force", "low_speed", "high_accel"),
    ("stress", "high_speed", "high_accel"),
    ("center", "mid_speed", "mid_accel"),
]

# Grade tiers from METHODOLOGY.md; "fair" and above are usable rows
GRADE_FAIR = 45
K_HARD_MAX = 0.2
FINE_STEP = 0.001
SECONDS_PER_K = 40.0
# A sweep that takes longer than this is treated as a hung printer
SWEEP_TIMEOUT_PER_K = 5 * SECONDS_PER_K
SWEEP_TIMEOUT_EXTRA = 600.0
# Console lines can trail the RPC reply slightly; wait this long before parsing
CONSOLE_DRAIN_S = 0.5
# flow_calculator.log is only a fallback, so don't let a slow download stall a sweep
FLOW_LOG_TIMEOUT = 5.0
# Keys run_point() writes; cleared before a point is (re)measured
RESULT_KEYS = ("status", "bracket", "k_star", "k_out", "confidence", "grade", "snr",
               "balance", "clean_zero", "peak", "samples")

AREA_RE = re.compile(r"\bk(\d+(?:\.\d+)?):\s*area\s*[=:]\s*([+-]?\d+(?:\.\d+)?)")
ABORT_RE = re.compile(r"abort calibration.*", re.IGNORECASE)
TEMP_REPORT_RE = re.compile(r"^B:\S+\s*/\S+\s")
POINT_BANNER_RE = re.compile(r"Point:\s*(\S+)\s*\|\s*Tool T(\d+)")
XY_BANNER_RE = re.compile(
    r"XY:\s*speed=([\d.]+)\s*mm/s\s+accel=([\d.]+)\s*->\s*Q=([\d.]+)"
)
FINISH_CONF_RE = re.compile(r"Confidence:\s*(\d+)/100")
FINISH_K_RE = re.compile(r"rounded\s+([\d.]+)")
FLOW_REQ_RE = re.compile(r"Received request: loop=\d+, slowv=([\d.]+), fastv=([\d.]+)")
FLOW_RES_RE = re.compile(r"Calculation result: ([+-]?\d+(?:\.\d+)?)")


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------

def parse_area(line: str) -> Optional[Tuple[float, float]]:
    """Return (k, area) from a FLOW_MEASURE_K console line, else None.

    Accepts the firmware format ``// k0.015: area=-5421.000`` as well as the
    ``k0.010: area: +1278`` style used in older notes.
    """
    m = AREA_RE.search(line)
    if not m:
        return None
    return round(float(m.group(1)), 3), float(m.group(2))


def expected_ks(k_min: float, k_max: float, k_step: float) -> List[float]:
    """K values FLOW_MEASURE_K will run: thousandths, MAX exclusive."""
    lo, hi, st = (int(round(x * 1000)) for x in (k_min, k_max, k_step))
    if st <= 0 or abs(k_step * 1000 - st) > 1e-6:
        raise ValueError("STEP must be a positive multiple of 0.001")
    return [i / 1000.0 for i in range(lo, hi, st)]


def find_bracket(samples: Dict[float, float]) -> Tuple[Optional[List[float]], str, int]:
    """First positive->negative step in k order.

    Returns (bracket, status, sign_changes); bracket is [k0, a0, k1, a1] or None.
    Status is ``ok``, ``kstar_above`` (every area > 0, K too low), ``kstar_below``
    (every area < 0, K too high), ``no_flip`` (signs change but never + -> -) or
    ``no_data``.
    """
    pts = sorted(samples.items())
    if not pts:
        return None, "no_data", 0
    signs = [a > 0 for _, a in pts if a != 0]
    changes = sum(1 for s0, s1 in zip(signs, signs[1:]) if s0 != s1)
    for (k0, a0), (k1, a1) in zip(pts, pts[1:]):
        if a0 >= 0 >= a1 and not (a0 == 0 and a1 == 0):
            return [k0, a0, k1, a1], "ok", changes
    if all(a > 0 for _, a in pts):
        return None, "kstar_above", changes
    if all(a < 0 for _, a in pts):
        return None, "kstar_below", changes
    return None, "no_flip", changes


def score(k0: float, a0: float, k1: float, a1: float, max_abs: float = 0.0) -> dict:
    """Python port of APA_FINISH_CELL: K* by linear zero-cross + confidence."""
    if k1 == k0:
        raise ValueError("K0 and K1 must differ")
    if a0 == 0:
        k_star = k0
    elif a1 == 0:
        k_star = k1
    elif a0 * a1 > 0:
        raise ValueError("A0 and A1 same sign - pick k steps that straddle zero")
    else:
        k_star = k0 - a0 * (k1 - k0) / (a1 - a0)
    k_out = round(k_star * 1000.0) / 1000.0

    abs0, abs1 = abs(a0), abs(a1)
    peak = max_abs if max_abs > 0 else max(abs0, abs1)
    sum_abs = abs0 + abs1
    balance = 0.0 if sum_abs == 0 else 1.0 - abs(abs0 - abs1) / sum_abs

    if peak >= 20000:
        snr_label, snr_pts = "strong signal", 40
    elif peak >= 5000:
        snr_label, snr_pts = "good signal", 34
    elif peak >= 1000:
        snr_label, snr_pts = "moderate signal", 22
    elif peak >= 300:
        snr_label, snr_pts = "weak signal", 12
    else:
        snr_label, snr_pts = "very weak signal", 5

    clean = peak > 0 and abs0 < 0.02 * peak and abs1 < 0.15 * peak
    conf = 30 + snr_pts + int(20.0 * balance) + (10 if clean else 0)
    if peak < 300:
        conf = min(conf, 32)
    elif peak < 1000:
        conf = min(conf, 44)
    conf = max(0, min(100, conf))

    if conf >= 80:
        grade = "excellent"
    elif conf >= 65:
        grade = "good"
    elif conf >= GRADE_FAIR:
        grade = "fair"
    elif conf >= 30:
        grade = "weak"
    else:
        grade = "unusable"
    return {
        "k_star": k_star, "k_out": k_out, "confidence": conf, "grade": grade,
        "snr": snr_label, "balance": balance, "clean_zero": clean, "peak": peak,
    }


def evaluate(samples: Dict[float, float]) -> dict:
    """Bracket + score for every sample collected for one test point."""
    bracket, status, changes = find_bracket(samples)
    out: dict = {"status": status, "notes": []}
    if changes > 1:
        out["notes"].append("area changes sign %d times - noisy sweep" % changes)
    if bracket is None:
        return out
    peak = max(abs(a) for a in samples.values())
    out["bracket"] = bracket
    out.update(score(*bracket, peak))
    return out


def no_flip_reason(status: str, samples: Dict[float, float]) -> str:
    """Why a sweep gave no K*, for the console and the point's notes."""
    if status == "kstar_below":
        return "no sign flip, every area < 0, K* below %.3f" % min(samples)
    if status == "kstar_above":
        return "no sign flip, every area > 0, K* above %.3f" % max(samples)
    if status == "no_flip":
        return "no sign flip, signs change but no + to - bracket"
    return "no area values received"


def retry_range(samples: Dict[float, float], status: str, k_min: float,
                k_max: float, k_step: float) -> Optional[Tuple[float, float, float]]:
    """Next (MIN, MAX, STEP) when the sweep did not cross zero."""
    span = max(k_max - k_min, 2 * k_step)
    ks = sorted(samples)
    if status == "kstar_above":
        lo = ks[-1] + k_step
        if lo >= K_HARD_MAX:
            return None
        return lo, min(lo + span, K_HARD_MAX), k_step
    if status == "kstar_below":
        hi = ks[0]
        if hi <= 0:
            return None
        return max(0.0, hi - span), hi, k_step
    return None


def refine_range(bracket: List[float], k_step: float) -> Optional[Tuple[float, float, float]]:
    """Fine scan strictly between the bracket ends (both already measured)."""
    k0, _, k1, _ = bracket
    if k_step <= FINE_STEP or round((k1 - k0) * 1000) <= 1:
        return None
    return k0 + FINE_STEP, k1, FINE_STEP


def fallback_pa(points: List[dict]) -> Optional[float]:
    """Median K* of usable points (fair or better), else of all finished points."""
    done = [p for p in points if p.get("status") == "ok"]
    good = [p for p in done if p["confidence"] >= GRADE_FAIR] or done
    if not good:
        return None
    return round(statistics.median(p["k_out"] for p in good), 3)


def orca_row(k_out: float, flow: float, accel: float) -> str:
    return "%.3f, %.2f, %.0f" % (k_out, flow, accel)


def flow_log_areas(text: str, slowv: float, fastv: float) -> List[float]:
    """``Calculation result`` values from flow_calculator.log for one point's speeds."""
    out: List[float] = []
    cur: Optional[Tuple[float, float]] = None
    for line in text.splitlines():
        m = FLOW_REQ_RE.search(line)
        if m:
            cur = (float(m.group(1)), float(m.group(2)))
            continue
        m = FLOW_RES_RE.search(line)
        if m and cur and abs(cur[0] - slowv) < 0.0011 and abs(cur[1] - fastv) < 0.0011:
            out.append(float(m.group(1)))
    return out


def recover_from_flow_log(areas: List[float], baseline: Optional[int],
                          ks: List[float]) -> Optional[Dict[float, float]]:
    """Map the results one sweep added to flow_calculator.log onto its K values.

    ``baseline`` is how many matching results the log had before the sweep.
    Only an exact count of new results is trusted; anything else (log rotated,
    unreadable before the sweep, results missing) returns None rather than
    borrowing values from an earlier sweep at the same speeds.
    """
    if baseline is None or len(areas) - baseline != len(ks):
        return None
    return dict(zip(ks, areas[baseline:]))


def usable(p: dict) -> bool:
    """Point belongs in the Orca table: finished, fair or better, known flow."""
    return (p.get("status") == "ok" and p["confidence"] >= GRADE_FAIR
            and p.get("flow", 0) > 0)


def atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: file briefly held by a scanner/editor
                if attempt == 2:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def parse_console_log(text: str) -> List[dict]:
    """Split a saved console log into test points using the macro banners."""
    points: List[dict] = []
    cur: Optional[dict] = None
    for raw in text.splitlines():
        line = raw.strip().lstrip("/").strip()
        m = POINT_BANNER_RE.search(line)
        if m:
            cur = {"name": m.group(1), "tool": int(m.group(2)), "samples": {}}
            points.append(cur)
            continue
        if cur is None:
            continue
        m = XY_BANNER_RE.search(line)
        if m:
            cur.update(speed=float(m.group(1)), accel=float(m.group(2)),
                       flow=float(m.group(3)))
            continue
        p = parse_area(line)
        if p:
            cur["samples"][p[0]] = p[1]
    return points


def build_points(cfg: dict, names: Optional[List[str]], skip_stress: bool) -> List[dict]:
    out = []
    for name, sv, av in POINTS:
        if skip_stress and name == "stress":
            continue
        if names and name not in names:
            continue
        speed, accel = float(cfg[sv]), float(cfg[av])
        out.append({"name": name, "speed": speed, "accel": accel,
                    "flow": speed * float(cfg["line_area"])})
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def filament_slug(name: str) -> str:
    """Folder-safe form of a filament name: 'Polymaker PLA Pro' -> 'Polymaker_PLA_Pro'.

    Matches the ``_`` in the timestamp part of the folder name; ``-`` becomes ``_`` too.
    """
    return re.sub(r"[^A-Za-z0-9.+]+", "_", name).strip("_.")[:60]


def format_tool_report(tool: int, temp, points: List[dict],
                       filament: Optional[str] = None) -> str:
    title = "T%d  %s C" % (tool, temp) + ("  %s" % filament if filament else "")
    lines = ["", "=== %s ===" % title,
             "%-11s %6s %6s %6s  %-6s %4s  %-9s %s"
             % ("point", "speed", "accel", "flow", "K*", "conf", "grade", "notes")]
    for p in points:
        if p.get("status") == "ok":
            lines.append("%-11s %6.0f %6.0f %6.2f  %-6.3f %4d  %-9s %s" % (
                p["name"], p["speed"], p["accel"], p["flow"], p["k_out"],
                p["confidence"], p["grade"], "; ".join(p.get("notes", []))))
        else:
            lines.append("%-11s %6.0f %6.0f %6.2f  %-6s %4s  %-9s %s" % (
                p["name"], p["speed"], p["accel"], p["flow"], "-", "-",
                p.get("status", "pending"), "; ".join(p.get("notes", []))))
    rows = [orca_row(p["k_out"], p["flow"], p["accel"]) for p in points if usable(p)]
    left_out = [p["name"] for p in points if p.get("status") == "ok" and not usable(p)]
    lines.append("--- OrcaSlicer: Filament > Adaptive pressure advance measurements ---")
    lines.extend(rows or ["(no usable rows)"])
    if left_out:
        lines.append("(left out - confidence below %d or flow unknown: %s)"
                     % (GRADE_FAIR, ", ".join(left_out)))
    fb = fallback_pa(points)
    if fb is not None:
        lines.append("Fallback static Pressure advance (median K*): %.3f" % fb)
    return "\n".join(lines)


def write_orca_file(path: str, tool: int, points: List[dict],
                    filament: Optional[str] = None, temp=None) -> None:
    lines = ["# T%d Adaptive PA rows: PA, flow_mm3_s, accel_mm_s2" % tool]
    if filament:
        lines.append("# filament: %s" % filament)
    if temp is not None:
        lines.append("# nozzle temperature: %s C" % temp)
    fb = fallback_pa(points)
    if fb is not None:
        lines.append("# fallback static pressure advance: %.3f" % fb)
    lines.extend(orca_row(p["k_out"], p["flow"], p["accel"]) for p in points if usable(p))
    atomic_write(path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Moonraker websocket client
# ---------------------------------------------------------------------------

class ApaError(Exception):
    """Problem to report to the user and stop (printer unreachable, macros missing...)."""


class GcodeError(RuntimeError):
    """A G-code command failed; the current test point is marked as error."""


def normalize_host(host: str) -> str:
    h = re.sub(r"^[a-z]+://", "", host.strip())
    return h.split("/", 1)[0]


class Moonraker:
    def __init__(self, host: str, api_key: Optional[str] = None,
                 on_line: Optional[Callable[[str], None]] = None):
        self.host = normalize_host(host)
        self.api_key = api_key
        self.on_line = on_line
        self.lines: List[str] = []
        self._ws = None
        self._id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self._closed_error: tuple = ()  # websockets' ConnectionClosed once connected

    async def connect(self) -> None:
        try:
            import websockets
        except ImportError as e:
            raise ApaError("Needs: pip install websockets (%s)" % e) from e
        headers = {"X-Api-Key": self.api_key} if self.api_key else None
        kwargs = {"max_size": None}
        if headers:
            kwargs["additional_headers"] = headers
        try:
            self._ws = await websockets.connect("ws://%s/websocket" % self.host, **kwargs)
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as e:
            raise ApaError(
                "Cannot reach Moonraker at ws://%s/websocket (%s: %s)\n"
                "Check the address, and that the web UI (Fluidd/Mainsail) opens at http://%s/"
                % (self.host, type(e).__name__, e, self.host)) from e
        self._closed_error = websockets.exceptions.ConnectionClosed
        self._reader = asyncio.create_task(self._read())
        await self.call("server.connection.identify", {
            "client_name": "apa-autocal", "version": "1.0", "type": "agent",
            "url": "https://github.com/djsplice/u1-adaptive-pa-autocal"})

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()

    def _dispatch(self, msg: dict) -> None:
        if msg.get("method") == "notify_gcode_response":
            for line in msg.get("params", []):
                self.lines.append(line)
                if self.on_line:
                    self.on_line(line)
        elif msg.get("id") in self._pending:
            fut = self._pending.pop(msg["id"])
            if not fut.done():
                fut.set_result(msg)

    async def _read(self) -> None:
        reason = "closed"
        try:
            async for raw in self._ws:
                try:
                    self._dispatch(json.loads(raw))
                except Exception as e:  # one bad message must not end the session
                    sys.stderr.write("WARN: ignored Moonraker message (%s: %s)\n"
                                     % (type(e).__name__, e))
        except Exception as e:
            reason = "%s: %s" % (type(e).__name__, e)
        finally:  # connection gone: fail every waiter instead of hanging
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Moonraker connection lost (%s)" % reason))
            self._pending.clear()

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout: Optional[float] = 30.0) -> dict:
        self._id += 1
        req_id = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps({"jsonrpc": "2.0", "method": method,
                                            "params": params or {}, "id": req_id}))
            msg = await asyncio.wait_for(fut, timeout)
        except self._closed_error as e:  # one error type for "connection gone"
            raise ConnectionError("Moonraker connection lost (%s)" % e) from e
        finally:
            self._pending.pop(req_id, None)
        if "error" in msg:
            err = msg["error"]
            raise GcodeError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
        return msg.get("result")

    async def gcode(self, script: str, timeout: Optional[float] = None) -> None:
        await self.call("printer.gcode.script", {"script": script}, timeout=timeout)

    async def query(self, objects: Dict[str, Optional[List[str]]]) -> dict:
        res = await self.call("printer.objects.query", {"objects": objects})
        return res["status"]

    def _get_text(self, path: str, timeout: float) -> str:
        req = urllib.request.Request("http://%s%s" % (self.host, path))
        if self.api_key:
            req.add_header("X-Api-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:  # file not created yet: same as empty
                return ""
            raise

    async def get_text(self, path: str, timeout: float) -> str:
        """HTTP GET from Moonraker without blocking the websocket's event loop.

        A missing file (404) reads as an empty string.
        """
        return await asyncio.to_thread(self._get_text, path, timeout)


# ---------------------------------------------------------------------------
# Calibration session
# ---------------------------------------------------------------------------

def heater_name(tool: int) -> str:
    return "extruder" if tool == 0 else "extruder%d" % tool


class Session:
    def __init__(self, args: argparse.Namespace, out_dir: str, state: dict):
        self.args = args
        self.out_dir = out_dir
        self.state = state
        self.heated: set = set()
        log_path = os.devnull if args.dry_run else os.path.join(out_dir, "console.log")
        self._log = open(log_path, "a", encoding="utf-8")
        self.mr = Moonraker(args.host, args.api_key, on_line=self._on_line)

    # -- console ------------------------------------------------------------
    def _on_line(self, line: str) -> None:
        if TEMP_REPORT_RE.match(line):
            return
        self._log.write(time.strftime("%H:%M:%S ") + line + "\n")
        self._log.flush()
        if parse_area(line) or ABORT_RE.search(line) or line.startswith("!!"):
            print("    " + line.lstrip("/ ").strip(), flush=True)

    def save(self) -> None:
        atomic_write(os.path.join(self.out_dir, "results.json"),
                     json.dumps(self.state, indent=1))

    # -- preflight ----------------------------------------------------------
    async def preflight(self) -> dict:
        info = await self.mr.call("server.info")
        if info.get("klippy_state") != "ready":
            raise ApaError("Klipper is not ready (state: %s)" % info.get("klippy_state"))
        st = await self.mr.query({CFG_MACRO: None, "print_stats": ["state"]})
        cfg = st.get(CFG_MACRO) or {}
        if "line_area" not in cfg:
            raise ApaError(
                "adaptive_pa_macro.cfg is not loaded on the printer (no _APA_COIL_CFG).\n"
                "Copy it next to printer.cfg and add [include adaptive_pa_macro.cfg], or on\n"
                "paxx12 extended firmware put it in config/extended/klipper/, then restart\n"
                "Klipper.")
        if st.get("print_stats", {}).get("state") in ("printing", "paused"):
            raise ApaError("Printer is busy (print_stats: %s)" % st["print_stats"]["state"])
        return {k: cfg[k] for k in ENVELOPE_KEYS}

    # -- one sweep ----------------------------------------------------------
    async def sweep(self, tool: int, p: dict, temp: int, k_min: float, k_max: float,
                    k_step: float, cfg: dict) -> Dict[float, float]:
        ks = expected_ks(k_min, k_max, k_step)
        cmd = ("APA_COIL_CELL NAME=%s SPEED=%g ACCEL=%g TEMP=%d EXTRUDER=%d "
               "MIN=%.3f MAX=%.3f STEP=%.3f" % (p["name"], p["speed"], p["accel"],
                                                temp, tool, k_min, k_max, k_step))
        print("  %s: K %.3f..%.3f step %.3f (%d values, ~%.0f min)" % (
            p["name"], ks[0], ks[-1], k_step, len(ks), len(ks) * SECONDS_PER_K / 60 + 0.5),
            flush=True)
        if self.args.dry_run:
            print("    would send: " + cmd)
            return {}
        fast = p["speed"] * float(cfg["ratio"])
        slow = fast * float(cfg["slow_frac"])
        baseline = await self._flow_log_areas(slow, fast)
        start = len(self.mr.lines)
        self.heated.add(tool)
        limit = len(ks) * SWEEP_TIMEOUT_PER_K + SWEEP_TIMEOUT_EXTRA
        try:
            await self.mr.gcode(cmd, timeout=limit)
        except asyncio.TimeoutError as e:
            raise ApaError("%s did not finish within %.0f min - is the printer stuck?"
                           % (p["name"], limit / 60)) from e
        await asyncio.sleep(CONSOLE_DRAIN_S)
        got: Dict[float, float] = {}
        for line in self.mr.lines[start:]:
            if ABORT_RE.search(line):
                raise GcodeError(line.lstrip("/ ").strip())
            a = parse_area(line)
            if a:
                got[a[0]] = a[1]
        missing = [k for k in ks if k not in got]
        if missing:
            after = await self._flow_log_areas(slow, fast)
            recovered = recover_from_flow_log(after or [], None if baseline is None
                                              else len(baseline), ks)
            if recovered is None:
                print("    WARN: %d area line(s) missing and flow_calculator.log can't be "
                      "matched to this sweep" % len(missing))
            else:
                got.update({k: recovered[k] for k in missing})
                print("    recovered %d area value(s) from flow_calculator.log" % len(missing))
        return got

    async def _flow_log_areas(self, slow: float, fast: float) -> Optional[List[float]]:
        """Matching results currently in flow_calculator.log, or None if unreadable."""
        try:
            text = await self.mr.get_text("/server/files/logs/flow_calculator.log",
                                          timeout=FLOW_LOG_TIMEOUT)
        except (OSError, ValueError, HTTPException):  # best effort: never stop the run
            return None
        return flow_log_areas(text, slow, fast)

    # -- one test point -----------------------------------------------------
    async def run_point(self, tool: int, p: dict, temp: int, cfg: dict) -> None:
        k_min, k_max, k_step = (float(cfg["k_min"]), float(cfg["k_max"]), float(cfg["k_step"]))
        samples: Dict[float, float] = {}
        for key in RESULT_KEYS:  # a re-run starts clean, not from a failed attempt
            p.pop(key, None)
        p["sweeps"] = []
        p["notes"] = []
        retries = 0
        while True:
            got = await self.sweep(tool, p, temp, k_min, k_max, k_step, cfg)
            if self.args.dry_run:
                return
            p["sweeps"].append({"min": k_min, "max": k_max, "step": k_step,
                                "samples": sorted(got.items())})
            samples.update(got)
            res = evaluate(samples)
            if res["status"] == "ok":
                break
            nxt = retry_range(samples, res["status"], k_min, k_max, k_step)
            if nxt is None or retries >= self.args.max_retries:
                reason = no_flip_reason(res["status"], samples)
                print("    %s: %s" % (p["name"], reason), flush=True)
                p.update(res)
                p["notes"].append("%s (%d sweep(s))" % (reason, len(p["sweeps"])))
                return
            retries += 1
            print("    %s: every area %s 0 -> moving K range" % (
                p["name"], ">" if res["status"] == "kstar_above" else "<"), flush=True)
            k_min, k_max, k_step = nxt

        if res["confidence"] < self.args.refine_below:
            fine = refine_range(res["bracket"], k_step)
            if fine:
                print("    %s: confidence %d -> fine scan" % (p["name"], res["confidence"]),
                      flush=True)
                got = await self.sweep(tool, p, temp, *fine, cfg)
                p["sweeps"].append({"min": fine[0], "max": fine[1], "step": fine[2],
                                    "samples": sorted(got.items())})
                samples.update(got)
                res = evaluate(samples)
        p.update(res)
        p["samples"] = sorted(samples.items())
        await self.console_finish(p)

    async def console_finish(self, p: dict) -> None:
        """Echo the result via APA_FINISH_CELL so the web UI console shows it; cross-check it."""
        k0, a0, k1, a1 = p["bracket"]
        cmd = ("APA_FINISH_CELL K0=%.3f A0=%.3f K1=%.3f A1=%.3f FLOW=%.4f ACCEL=%g "
               "NAME=%s MAX_ABS=%.3f" % (k0, a0, k1, a1, p["flow"], p["accel"],
                                         p["name"], p["peak"]))
        start = len(self.mr.lines)
        try:
            await self.mr.gcode(cmd, timeout=30)
        except (GcodeError, asyncio.TimeoutError) as e:
            p["notes"].append("APA_FINISH_CELL failed: %s" % (str(e) or type(e).__name__))
            return
        await asyncio.sleep(CONSOLE_DRAIN_S)
        text = "\n".join(self.mr.lines[start:])
        mc, mk = FINISH_CONF_RE.search(text), FINISH_K_RE.search(text)
        if mc and mk and (int(mc.group(1)) != p["confidence"]
                          or abs(float(mk.group(1)) - p["k_out"]) > 1e-9):
            p["notes"].append("macro reports K*=%s conf=%s" % (mk.group(1), mc.group(1)))

    # -- whole run ----------------------------------------------------------
    async def run(self) -> None:
        try:
            await self.mr.connect()
            await self._run()
        finally:
            try:
                await self.heaters_off(sorted(self.heated))
            finally:
                await self.mr.close()
                self._log.close()

    async def _run(self) -> None:
        live_cfg = await self.preflight()
        cfg = self.state.setdefault("envelope", live_cfg)
        if cfg != live_cfg and not self.args.dry_run:
            print("NOTE: resuming with the envelope saved in results.json")
        temp = self.state.setdefault(
            "temp", self.args.temp if self.args.temp is not None else int(cfg["temp"]))
        plan = build_points(cfg, self.args.point_names, self.args.skip_stress)
        wanted = {p["name"] for p in plan}
        tools = self.state.setdefault("tools", {})
        for t in self.args.tool_list:
            entry = tools.setdefault(str(t), {"points": []})
            known = {p["name"] for p in entry["points"]}
            entry["points"].extend(dict(p) for p in plan if p["name"] not in known)

        total = sum(1 for t in self.args.tool_list for p in tools[str(t)]["points"]
                    if p["name"] in wanted and p.get("status") != "ok")
        filament = self.state.get("filament")
        print("APA autocal @ %s  %stools=%s  temp=%s  points to run=%d (~%d min before retries)"
              % (self.mr.host, "filament=%s  " % filament if filament else "",
                 ",".join(map(str, self.args.tool_list)), temp, total,
                 total * len(expected_ks(cfg["k_min"], cfg["k_max"], cfg["k_step"]))
                 * SECONDS_PER_K / 60))
        print("Output: %s" % self.out_dir)
        for tool in self.args.tool_list:
            entry = tools[str(tool)]
            print("\n--- T%d ---" % tool, flush=True)
            for p in entry["points"]:
                if p["name"] not in wanted:
                    continue
                if p.get("status") == "ok":
                    print("  %s: done (K*=%.3f), skipping" % (p["name"], p["k_out"]))
                    continue
                try:
                    await self.run_point(tool, p, temp, cfg)
                except GcodeError as e:
                    p["status"] = "error"
                    p.setdefault("notes", []).append(str(e))
                    print("    %s failed: %s" % (p["name"], e), flush=True)
                if not self.args.dry_run:
                    self.save()
                    if p.get("status") == "ok":
                        print("    %s: K*=%.3f  confidence %d (%s)" % (
                            p["name"], p["k_out"], p["confidence"], p["grade"]), flush=True)
            if not self.args.dry_run:
                entry["fallback_pa"] = fallback_pa(entry["points"])
                self.save()
                write_orca_file(os.path.join(self.out_dir, "orca_T%d.txt" % tool),
                                tool, entry["points"], filament, temp)
                await self.heaters_off([tool])
        if not self.args.dry_run:
            for tool in self.args.tool_list:
                print(format_tool_report(tool, temp, tools[str(tool)]["points"], filament))
            print("\nSaved: %s" % self.out_dir)

    async def heaters_off(self, tools: List[int]) -> None:
        # FLOW_MEASURE_K leaves the nozzle at 170 C; turn it off explicitly.
        for t in tools:
            try:
                await self.mr.gcode("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0"
                                    % heater_name(t), timeout=10)
                self.heated.discard(t)
            except Exception as e:  # never let cleanup hide the original error
                print("WARN: could not confirm T%d heater off (%s); check it in Fluidd/Mainsail"
                      % (t, str(e) or type(e).__name__), flush=True)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_parse_log(path: str, filament: Optional[str] = None) -> None:
    with open(path, encoding="utf-8", errors="replace") as f:
        points = parse_console_log(f.read())
    if not points:
        raise ApaError("No APA test point banners found in %s" % path)
    by_tool: Dict[int, List[dict]] = {}
    for p in points:
        p.update(evaluate(p["samples"]))
        if "flow" not in p:
            p.update(speed=0.0, accel=0.0, flow=0.0)
            p["notes"].append("no XY banner - speed/flow unknown")
        by_tool.setdefault(p["tool"], []).append(p)
    for tool, pts in sorted(by_tool.items()):
        print(format_tool_report(tool, "?", pts, filament))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the APA suite over Moonraker and finish every point automatically.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--host", help="Printer address, e.g. 192.168.1.50 "
                     "(default with --resume: the session's printer)")
    src.add_argument("--parse-log", default=None,
                     help="Offline: finish points from a saved console log")
    ap.add_argument("--api-key", default=None, help="Moonraker API key (if required)")
    ap.add_argument("--tools", default=None,
                    help="Comma-separated tool indexes (default: 0, or the resumed session's)")
    ap.add_argument("--temp", type=int, default=None,
                    help="Nozzle temperature (default: temp in _APA_COIL_CFG)")
    ap.add_argument("--filament", default=None,
                    help="Filament name, e.g. \"Polymaker PLA Pro\"; saved with the results "
                    "and added to the session folder name")
    ap.add_argument("--points", default=None,
                    help="Subset of points, e.g. center,high_flow (default: all)")
    ap.add_argument("--skip-stress", action="store_true", help="Skip the stress corner")
    ap.add_argument("--max-retries", type=int, default=2,
                    help="Re-runs with a moved K range when no sign flip (default: 2)")
    ap.add_argument("--refine-below", type=int, default=GRADE_FAIR,
                    help="Fine 0.001 scan when confidence is below this (default: %d, 0 = off)"
                    % GRADE_FAIR)
    ap.add_argument("--out", default="apa_results", help="Results parent directory")
    ap.add_argument("--resume", default=None, help="Session directory to continue")
    ap.add_argument("--dry-run", action="store_true",
                    help="Check the printer and print the plan; no heating or motion")
    args = ap.parse_args(argv)

    # Console lines from the printer may contain characters a Windows console can't show
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    if args.parse_log:
        try:
            run_parse_log(args.parse_log, args.filament)
        except ApaError as e:
            print(e, file=sys.stderr)
            return 1
        return 0

    try:
        args.tool_list = [int(t) for t in (args.tools or "").split(",") if t.strip()]
    except ValueError:
        ap.error("--tools must look like 0,1,2")
    if any(t < 0 or t > 3 for t in args.tool_list):
        ap.error("tool indexes must be 0..3")
    args.point_names = [n.strip() for n in args.points.split(",")] if args.points else None
    unknown = set(args.point_names or []) - {name for name, _, _ in POINTS}
    if unknown:
        ap.error("unknown point(s) %s; choose from %s"
                 % (", ".join(sorted(unknown)), ", ".join(name for name, _, _ in POINTS)))
    if args.max_retries < 0 or not 0 <= args.refine_below <= 100:
        ap.error("--max-retries must be >= 0 and --refine-below 0..100")
    if args.temp is not None and args.temp <= 0:
        ap.error("--temp must be a positive temperature")
    if args.filament is not None:
        args.filament = " ".join(args.filament.split())
        if not filament_slug(args.filament):
            ap.error("--filament needs at least one letter or digit")

    state: dict = {}
    if args.resume:
        out_dir = args.resume
        try:
            with open(os.path.join(out_dir, "results.json"), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError) as e:
            ap.error("cannot resume from %s: %s" % (out_dir, e))
        if args.temp is not None and "temp" in state and args.temp != state["temp"]:
            ap.error("session was measured at %s C; start a new session for %s C"
                     % (state["temp"], args.temp))
        if args.filament and state.get("filament") not in (None, args.filament):
            ap.error("session was measured with filament %r; start a new session for %r"
                     % (state["filament"], args.filament))
        args.host = args.host or state.get("host")
        state.pop("host", None)  # a new --host (printer got a new IP) replaces the saved one
    else:
        out_dir = os.path.join(args.out, time.strftime("%Y%m%d_%H%M%S"))
        if args.filament:
            out_dir += "_" + filament_slug(args.filament)
    if args.filament:
        state.setdefault("filament", args.filament)
    if not args.host:
        ap.error("--host is required (or use --parse-log)")
    if not args.tool_list:
        args.tool_list = [int(t) for t in state.get("tools", {})] or [0]
    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)
    state.setdefault("host", normalize_host(args.host))
    state.setdefault("started", time.strftime("%Y-%m-%d %H:%M:%S"))

    session = Session(args, out_dir, state)

    def hint() -> str:
        # Only point at --resume when there is a saved session to continue
        if os.path.exists(os.path.join(out_dir, "results.json")):
            return "\nFinished points are saved; continue with: --resume %s" % out_dir
        return ""

    try:
        asyncio.run(session.run())
    except ApaError as e:
        print("%s%s" % (e, hint()), file=sys.stderr)
        return 1
    except (ConnectionError, GcodeError, asyncio.TimeoutError) as e:
        print("Stopped: %s%s" % (str(e) or "printer did not answer in time", hint()),
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. The point that was running finishes on the printer; cancel it "
              "from Fluidd/Mainsail if needed.\nCheck the nozzle heater: the heater-off sent "
              "on exit may only run after that point, or not at all if the connection "
              "dropped.%s" % hint())
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
