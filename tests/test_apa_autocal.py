"""Tests for scripts/apa_autocal.py.

  python -m pytest tests            (or: python -m unittest discover tests)

The macro tests render adaptive_pa_macro.cfg with jinja2 and the end-to-end
tests run against a mock Moonraker built on websockets; each group is skipped
when its package is missing.
"""

import asyncio
import contextlib
import http.client
import importlib.util
import io
import json
import os
import random
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "apa_autocal", os.path.join(ROOT, "scripts", "apa_autocal.py"))
apa = importlib.util.module_from_spec(_spec)
sys.modules["apa_autocal"] = apa
_spec.loader.exec_module(apa)

try:
    import jinja2
except ImportError:
    jinja2 = None
try:
    import websockets
    from websockets.asyncio.server import serve
except ImportError:
    websockets = None

# Real FINISH inputs from a T2 suite at 265 C (Fluidd console history)
REAL_FINISH = [
    (0.015, 12740, 0.020, -3934, 45768),
    (0.015, 16165, 0.020, -2052, 39442),
    (0.015, 558, 0.020, -1139, 6486),
    (0.030, 15350, 0.035, -78431, 378535),
    (0.020, 2806, 0.025, -7475, 43836),
]


class TempDirMixin:
    def tmpdir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def patch(self, *args, **kwargs):
        patcher = mock.patch.object(*args, **kwargs)
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched


class ParseTests(unittest.TestCase):
    def test_firmware_format(self):
        self.assertEqual(apa.parse_area("// k0.015: area=-5421.000"), (0.015, -5421.0))

    def test_colon_format(self):
        self.assertEqual(apa.parse_area("k0.010: area: +1278"), (0.010, 1278.0))

    def test_other_lines(self):
        self.assertIsNone(apa.parse_area("B:28.1 /0.0 T0:29.9 /220.0"))
        self.assertIsNone(apa.parse_area("// k min: 0.015, max: 0.025, step: 0.005"))
        self.assertIsNone(apa.parse_area("mk0.015: area=1"))

    def test_expected_ks_exclusive_max(self):
        self.assertEqual(apa.expected_ks(0.005, 0.040, 0.005),
                         [0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035])
        self.assertEqual(apa.expected_ks(0.015, 0.025, 0.005), [0.015, 0.02])
        self.assertEqual(apa.expected_ks(0.016, 0.020, 0.001),
                         [0.016, 0.017, 0.018, 0.019])

    def test_expected_ks_rejects_sub_thousandth(self):
        with self.assertRaises(ValueError):
            apa.expected_ks(0.01, 0.02, 0.0025)

    def test_flow_log(self):
        log = (
            "2026-10-20 11:56:53 [INFO] Received request: loop=14, slowv=5.078, fastv=6.770, "
            "pt_len=28118\n"
            "2026-10-20 11:56:53 [INFO] Calculation result: -5421\n"
            "2026-10-20 11:57:00 [INFO] Received request: loop=14, slowv=2.539, fastv=3.385\n"
            "2026-10-20 11:57:01 [INFO] Calculation result: 999\n"
            "2026-10-20 11:57:32 [INFO] Received request: loop=14, slowv=5.078, fastv=6.770\n"
            "2026-10-20 11:57:32 [INFO] Calculation result: -18864\n")
        fast = 200 * 0.033851  # the ratio the macros used when this real log was recorded
        self.assertEqual(apa.flow_log_areas(log, fast * 0.75, fast), [-5421.0, -18864.0])

    def test_console_log(self):
        log = "\n".join([
            "// ========== APA TEST POINT START ==========",
            "// Point: center | Tool T2 (extruder2) | MODE=MEASURE TEMP=265",
            "// XY: speed=200.0 mm/s  accel=6000.0  ->  Q=16.2832 mm3/s",
            "// k0.010: area=30500.000",
            "// k0.015: area=12740.000",
            "// k0.020: area=-3934.000",
            "// ========== APA TEST POINT MEASURE DONE ==========",
        ])
        (p,) = apa.parse_console_log(log)
        self.assertEqual((p["name"], p["tool"], p["speed"], p["accel"], p["flow"]),
                         ("center", 2, 200.0, 6000.0, 16.2832))
        self.assertEqual(p["samples"], {0.01: 30500.0, 0.015: 12740.0, 0.02: -3934.0})

    def test_normalize_host(self):
        for given in ("192.168.1.50", "http://192.168.1.50", "ws://192.168.1.50/websocket",
                      " http://192.168.1.50/ "):
            self.assertEqual(apa.normalize_host(given), "192.168.1.50")
        self.assertEqual(apa.normalize_host("printer.local:7125"), "printer.local:7125")


class MathTests(unittest.TestCase):
    def test_bracket_and_score_user_guide_example(self):
        samples = {0.010: 12450, 0.015: 5128, 0.020: -5281, 0.025: -11800}
        r = apa.evaluate(samples)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["bracket"], [0.015, 5128, 0.020, -5281])
        self.assertEqual(r["k_out"], 0.017)
        self.assertAlmostEqual(r["k_star"], 0.017463, places=5)
        self.assertEqual((r["confidence"], r["grade"], r["clean_zero"]), (83, "excellent", False))

    def test_no_flip_statuses(self):
        self.assertEqual(apa.find_bracket({0.01: 5, 0.02: 3})[1], "kstar_above")
        self.assertEqual(apa.find_bracket({0.015: -5421, 0.02: -18864})[1], "kstar_below")
        self.assertEqual(apa.find_bracket({0.01: -5, 0.02: 3})[1], "no_flip")
        self.assertEqual(apa.find_bracket({})[1], "no_data")

    def test_exact_zero(self):
        r = apa.evaluate({0.01: 500, 0.015: 0, 0.02: -700})
        self.assertEqual(r["k_star"], 0.015)

    def test_noisy_note(self):
        r = apa.evaluate({0.01: 500, 0.015: -100, 0.02: 50, 0.025: -700})
        self.assertEqual(r["bracket"][:3:2], [0.01, 0.015])
        self.assertTrue(any("noisy" in n for n in r["notes"]))

    def test_no_flip_reason(self):
        below = {0.005: -137, 0.01: -1786, 0.035: -9977}  # reviewer's high_force sweep
        self.assertEqual(apa.no_flip_reason("kstar_below", below),
                         "no sign flip, every area < 0, K* below 0.005")
        self.assertEqual(apa.no_flip_reason("kstar_above", {0.005: 9, 0.035: 1}),
                         "no sign flip, every area > 0, K* above 0.035")
        self.assertEqual(apa.no_flip_reason("no_flip", {0.01: -5, 0.02: 3}),
                         "no sign flip, signs change but no + to - bracket")
        self.assertEqual(apa.no_flip_reason("no_data", {}), "no area values received")

    def test_retry_ranges(self):
        above = {k: 1000.0 for k in apa.expected_ks(0.005, 0.040, 0.005)}
        lo, hi, st = apa.retry_range(above, "kstar_above", 0.005, 0.040, 0.005)
        self.assertEqual(apa.expected_ks(lo, hi, st)[:2], [0.04, 0.045])
        below = {0.015: -5421.0, 0.02: -18864.0}
        lo, hi, st = apa.retry_range(below, "kstar_below", 0.015, 0.025, 0.005)
        self.assertEqual(apa.expected_ks(lo, hi, st), [0.005, 0.01])
        self.assertIsNone(apa.retry_range({0.0: -1.0}, "kstar_below", 0.0, 0.01, 0.005))

    def test_refine_range(self):
        fine = apa.refine_range([0.015, 558, 0.020, -1139], 0.005)
        self.assertEqual(apa.expected_ks(*fine), [0.016, 0.017, 0.018, 0.019])
        self.assertIsNone(apa.refine_range([0.015, 1, 0.016, -1], 0.001))

    def test_fallback_median(self):
        pts = [{"status": "ok", "k_out": k, "confidence": 80}
               for k in (0.012, 0.013, 0.016, 0.017, 0.023)]
        self.assertEqual(apa.fallback_pa(pts), 0.016)
        pts.append({"status": "ok", "k_out": 0.5, "confidence": 20})
        self.assertEqual(apa.fallback_pa(pts), 0.016)

    def test_orca_row(self):
        self.assertEqual(apa.orca_row(0.017, 8.1416, 2000.0), "0.017, 8.14, 2000")

    def test_result_keys_cover_evaluate(self):
        # run_point() clears RESULT_KEYS before a re-run; a new result field must be added there
        r = apa.evaluate({0.01: 500, 0.02: -700})
        self.assertLessEqual(set(r) - {"notes"}, set(apa.RESULT_KEYS))


class HelperTests(TempDirMixin, unittest.TestCase):
    def test_recover_from_flow_log(self):
        ks = [0.015, 0.02]
        self.assertEqual(apa.recover_from_flow_log([9.0, -5.0, -18.0], 1, ks),
                         {0.015: -5.0, 0.02: -18.0})
        self.assertIsNone(apa.recover_from_flow_log([9.0, -5.0], 1, ks))       # one missing
        self.assertIsNone(apa.recover_from_flow_log([-5.0, -18.0], None, ks))  # no baseline
        self.assertIsNone(apa.recover_from_flow_log([1, 2, 3, 4], 1, ks))      # extra results

    def test_usable(self):
        ok = {"status": "ok", "confidence": 80, "flow": 8.14}
        self.assertTrue(apa.usable(ok))
        self.assertFalse(apa.usable(dict(ok, confidence=44)))
        self.assertFalse(apa.usable(dict(ok, flow=0.0)))
        self.assertFalse(apa.usable(dict(ok, status="error")))

    def test_atomic_write_replaces_content(self):
        d = self.tmpdir()
        path = os.path.join(d, "out.txt")
        apa.atomic_write(path, "one\n")
        apa.atomic_write(path, "two\n")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "two\n")
        self.assertEqual(os.listdir(d), ["out.txt"])

    def test_atomic_write_failure_keeps_original(self):
        d = self.tmpdir()
        path = os.path.join(d, "out.txt")
        apa.atomic_write(path, "one\n")
        self.patch(apa.os, "replace", side_effect=OSError("disk full"))
        with self.assertRaises(OSError):
            apa.atomic_write(path, "two\n")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "one\n")
        self.assertEqual(os.listdir(d), ["out.txt"])

    def test_atomic_write_retries_locked_file(self):
        path = os.path.join(self.tmpdir(), "out.txt")
        real_replace = os.replace
        calls = []

        def flaky_replace(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError("locked by another program")
            real_replace(src, dst)

        self.patch(apa.os, "replace", side_effect=flaky_replace)
        self.patch(apa.time, "sleep")
        apa.atomic_write(path, "ok\n")
        self.assertEqual(len(calls), 3)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "ok\n")

    def test_get_text_missing_file_is_empty(self):
        mr = apa.Moonraker("printer")
        not_found = urllib.error.HTTPError("http://printer/x", 404, "Not Found", {}, None)
        self.patch(apa.urllib.request, "urlopen", side_effect=not_found)
        self.assertEqual(mr._get_text("/x", 5), "")
        server_error = urllib.error.HTTPError("http://printer/x", 500, "Boom", {}, None)
        self.patch(apa.urllib.request, "urlopen", side_effect=server_error)
        with self.assertRaises(urllib.error.HTTPError):
            mr._get_text("/x", 5)


def _macro_template(name):
    with open(os.path.join(ROOT, "adaptive_pa_macro.cfg"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"\[gcode_macro %s\].*?\ngcode:\n(.*?)(?=\n\[|\Z)" % re.escape(name),
                  text, re.S)
    body = "\n".join(line[4:] if line.startswith("    ") else line
                     for line in m.group(1).splitlines())
    env = jinja2.Environment("{%", "%}", "{", "}")
    return env.from_string(body)


@unittest.skipIf(jinja2 is None, "jinja2 not installed")
class MacroParityTests(unittest.TestCase):
    """The Python score() must match APA_FINISH_CELL in adaptive_pa_macro.cfg."""

    def run_macro(self, k0, a0, k1, a1, max_abs):
        out = []
        tpl = _macro_template("APA_FINISH_CELL")
        tpl.render(
            printer={"gcode_macro _APA_COIL_CFG": {"pending_valid": 0}},
            params={"K0": str(k0), "A0": str(a0), "K1": str(k1), "A1": str(a1),
                    "MAX_ABS": str(max_abs), "FLOW": "8.1416", "ACCEL": "2000", "NAME": "t"},
            action_respond_info=lambda s: out.append(s) or "",
            action_raise_error=lambda s: (_ for _ in ()).throw(ValueError(s)))
        text = "\n".join(out)
        conf = int(re.search(r"Confidence: (\d+)/100", text).group(1))
        k_out = float(re.search(r"rounded ([\d.]+)", text).group(1))
        return conf, k_out

    def check(self, k0, a0, k1, a1, max_abs):
        mine = apa.score(k0, a0, k1, a1, max_abs)
        self.assertEqual(self.run_macro(k0, a0, k1, a1, max_abs),
                         (mine["confidence"], mine["k_out"]),
                         (k0, a0, k1, a1, max_abs))

    def test_real_and_documented_inputs(self):
        self.check(0.015, 5128, 0.020, -5281, 12450)
        for case in REAL_FINISH:
            self.check(*case)

    def test_random_inputs(self):
        rnd = random.Random(1)
        for _ in range(400):
            k0 = rnd.randrange(0, 60) / 1000.0
            k1 = k0 + rnd.choice((0.001, 0.002, 0.005))
            a0 = rnd.choice((0, rnd.uniform(1, 4e5), rnd.uniform(1, 400)))
            a1 = -rnd.choice((rnd.uniform(1, 4e5), rnd.uniform(1, 900)))
            if a0 == 0 and a1 == 0:
                continue
            max_abs = rnd.choice((0, max(abs(a0), abs(a1)) * rnd.uniform(1, 30)))
            self.check(round(k0, 3), round(a0, 3), round(k1, 3), round(a1, 3),
                       round(max_abs, 3))


@unittest.skipIf(jinja2 is None, "jinja2 not installed")
class MacroBehaviourTests(unittest.TestCase):
    CFG = {"line_area": 0.081416, "fil_area": 2.405282, "ratio": 0.033849,
           "low_speed": 100.0, "mid_speed": 200.0, "high_speed": 250.0,
           "low_accel": 2000.0, "mid_accel": 6000.0, "high_accel": 10000.0,
           "temp": 220, "k_min": 0.005, "k_max": 0.04, "k_step": 0.005}

    def render(self, name, **params):
        info = []
        gcode = _macro_template(name).render(
            printer={"gcode_macro _APA_COIL_CFG": self.CFG},
            params={k: str(v) for k, v in params.items()},
            action_respond_info=lambda s: info.append(s) or "",
            action_raise_error=lambda s: (_ for _ in ()).throw(ValueError(s)))
        return gcode, info

    def set_vars(self, **params):
        gcode, info = self.render("APA_COIL_SET_ENVELOPE", **params)
        found = re.findall(r"VARIABLE=(\w+) VALUE=(\S+)", gcode)
        return {k: float(v) for k, v in found}, info

    def test_no_hash_in_macro_bodies(self):
        with open(os.path.join(ROOT, "adaptive_pa_macro.cfg"), encoding="utf-8") as f:
            bodies = re.findall(r"\ngcode:\n(.*?)(?=\n\[|\Z)", f.read(), re.S)
        self.assertTrue(bodies)
        for body in bodies:
            # column-0 "####" separators between sections are plain comments
            for line in body.splitlines():
                if line.startswith(" "):
                    self.assertNotIn("#", line)

    def test_macro_names_are_callable(self):
        # Klipper ends a command name at the first digit: APA_COIL_PRESET_U1_DEFAULT
        # would run as APA_COIL_PRESET_U1 with "_DEFAULT" as an argument.
        with open(os.path.join(ROOT, "adaptive_pa_macro.cfg"), encoding="utf-8") as f:
            names = re.findall(r"^\[gcode_macro (\S+)\]", f.read(), re.M)
        self.assertTrue(names)
        for name in names:
            self.assertRegex(name, r"^[A-Z_]+\d*$")

    def test_geometry_matches_documented_default(self):
        v, _ = self.set_vars(LAYER_HEIGHT=0.2, LINE_WIDTH=0.45, FIL_DIA=1.75)
        self.assertAlmostEqual(v["line_area"], 0.081416, places=6)
        self.assertAlmostEqual(v["fil_area"], 2.405282, places=6)
        self.assertAlmostEqual(v["ratio"], 0.033849, places=6)

    def test_geometry_other_nozzle(self):
        v, _ = self.set_vars(LAYER_HEIGHT=0.3, LINE_WIDTH=0.62)
        self.assertAlmostEqual(v["line_area"], 0.3 * 0.32 + 3.14159265 * 0.15 ** 2, places=6)

    def test_untouched_geometry_kept(self):
        v, _ = self.set_vars(HIGH_SPEED=230)
        self.assertEqual((v["line_area"], v["ratio"], v["high_speed"]), (0.081416, 0.033849, 230.0))

    def test_unrelated_params_keep_mid_point(self):
        with mock.patch.object(self, "CFG", dict(self.CFG, mid_speed=180.0, mid_accel=5000.0)):
            v, _ = self.set_vars(LAYER_HEIGHT=0.2, LINE_WIDTH=0.45)
            self.assertEqual((v["mid_speed"], v["mid_accel"]), (180.0, 5000.0))
            v, _ = self.set_vars(HIGH_SPEED=300)
            self.assertEqual((v["mid_speed"], v["mid_accel"]), (200.0, 5000.0))
            v, _ = self.set_vars(HIGH_ACCEL=12000)
            self.assertEqual((v["mid_speed"], v["mid_accel"]), (180.0, 7000.0))

    def test_mvs_caps_high_speed(self):
        v, info = self.set_vars(MVS=16.3)
        self.assertEqual(v["high_speed"], 200.2)
        self.assertEqual(v["mid_speed"], 150.1)
        self.assertTrue(any("capped" in s for s in info))

    def test_mvs_below_low_speed_errors(self):
        with self.assertRaises(ValueError):
            self.set_vars(MVS=5)

    def test_point_macro_keeps_active_tool(self):
        gcode, _ = self.render("APA_COIL_HIGH_FLOW")
        self.assertNotIn("EXTRUDER=", gcode)
        gcode, _ = self.render("APA_COIL_HIGH_FLOW", EXTRUDER=2)
        self.assertIn("EXTRUDER=2", gcode)


# ---------------------------------------------------------------------------
# End-to-end against a mock Moonraker
# ---------------------------------------------------------------------------

ENVELOPE = {
    "line_area": 0.081416, "fil_area": 2.405282, "ratio": 0.033849, "temp": 220,
    "k_min": 0.005, "k_max": 0.04, "k_step": 0.005, "loop": 14, "slow_frac": 0.75,
    "low_speed": 100.0, "mid_speed": 200.0, "high_speed": 250.0,
    "low_accel": 2000.0, "mid_accel": 6000.0, "high_accel": 10000.0,
    "last_k_star": 0.0, "pending_valid": 0,
}
# name -> (true K*, area per 0.005 K); stress fails like a real macro error by default
MODEL = {
    "low_anchor": (0.0172, 10000.0),
    "high_flow": (0.0471, 40000.0),   # above the default range -> moved range
    "high_force": (0.0167, 150.0),    # peak < 1000 caps confidence at 44 -> fine scan
    "center": (0.0118, 8000.0),
}


class MockMoonraker:
    """Minimal Moonraker: answers the JSON-RPC calls the tool makes, simulating the macros.

    Options (all optional):
      model              per-point (K*, gain), defaults to MODEL
      fail_stress        stress point fails like a macro error (default True)
      drop_console_for   point whose area lines never reach the console
      flow_log_short     flow_calculator.log misses a sweep's last result
      send_garbage       send a non-JSON frame before each sweep's console lines
      abort_for          point that prints "abort calibration"
      hang_for           point whose APA_COIL_CELL never answers
      close_for          point during which the connection is dropped
      finish_conf_offset added to the confidence APA_FINISH_CELL reports
      klippy_state, print_state, envelope   preflight answers
    """

    def __init__(self, **opts):
        self.opts = opts
        self.model = opts.get("model", MODEL)
        self.commands = []
        self.methods = []
        self.flow_log = []
        self.port = None
        self._ready = threading.Event()
        self._thread = None
        self._loop = None
        self._stop = None

    def area(self, name, k):
        k_star, gain = self.model[name]
        return round(gain * (k_star - k) / 0.005, 3)

    def log_result(self, fast, area):
        self.flow_log.append("[INFO] Received request: loop=14, slowv=%.3f, fastv=%.3f"
                             % (fast * 0.75, fast))
        self.flow_log.append("[INFO] Calculation result: %d" % area)

    async def send_lines(self, ws, lines):
        for line in lines:
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notify_gcode_response",
                                      "params": [line]}))

    async def handler(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            method, params, rid = msg["method"], msg.get("params", {}), msg["id"]
            self.methods.append(method)
            result, error = "ok", None
            if method == "server.info":
                result = {"klippy_state": self.opts.get("klippy_state", "ready")}
            elif method == "printer.objects.query":
                result = {"status": {
                    "gcode_macro _APA_COIL_CFG": self.opts.get("envelope", ENVELOPE),
                    "print_stats": {"state": self.opts.get("print_state", "standby")}}}
            elif method == "printer.gcode.script":
                script = params["script"]
                self.commands.append(script)
                name = dict(re.findall(r"(\w+)=(\S+)", script)).get("NAME")
                if script.startswith("APA_COIL_CELL") and name == self.opts.get("close_for"):
                    await ws.close()
                    return
                if script.startswith("APA_COIL_CELL") and name == self.opts.get("hang_for"):
                    continue  # never answered
                lines, error = self.run_gcode(script)
                if self.opts.get("send_garbage") and script.startswith("APA_COIL_CELL"):
                    await ws.send("this is not json")
                await self.send_lines(ws, lines)
            reply = {"jsonrpc": "2.0", "id": rid}
            if error:
                reply["error"] = {"code": 400, "message": error}
            else:
                reply["result"] = result
            await ws.send(json.dumps(reply))

    def run_gcode(self, script):
        p = dict(re.findall(r"(\w+)=(\S+)", script))
        if script.startswith("APA_COIL_CELL"):
            if p["NAME"] == "stress" and self.opts.get("fail_stress", True):
                return [], "!! Expected active extruder extruder, got extruder1"
            if p["NAME"] == self.opts.get("abort_for"):
                return ["// abort calibration: heating timeout"], None
            fast = float(p["SPEED"]) * ENVELOPE["ratio"]
            lines = ["B:28.1 /0.0 C:27.1 /0.0 T0:29.9 /220.0"]
            ks = apa.expected_ks(float(p["MIN"]), float(p["MAX"]), float(p["STEP"]))
            for i, k in enumerate(ks):
                a = self.area(p["NAME"], k)
                if not (self.opts.get("flow_log_short") and i == len(ks) - 1):
                    self.log_result(fast, a)
                if p["NAME"] != self.opts.get("drop_console_for"):
                    lines.append("// k%.3f: area=%.3f" % (k, a))
            return lines, None
        if script.startswith("APA_FINISH_CELL"):
            s = apa.score(float(p["K0"]), float(p["A0"]), float(p["K1"]), float(p["A1"]),
                          float(p["MAX_ABS"]))
            conf = s["confidence"] + self.opts.get("finish_conf_offset", 0)
            return ["// K* (zero cross): %s  ->  rounded %s" % (s["k_star"], s["k_out"]),
                    "// Confidence: %d/100 (%s)" % (conf, s["grade"])], None
        return [], None

    def start(self):
        self._thread = threading.Thread(target=asyncio.run, args=(self._main(),), daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("mock Moonraker did not start")

    def stop(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(self.handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()


@unittest.skipIf(websockets is None, "websockets not installed")
class EndToEndTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.patch(apa, "CONSOLE_DRAIN_S", 0)
        # No real HTTP in tests: flow_calculator.log is unreadable unless a test serves it
        self.patch(apa.Moonraker, "_get_text", side_effect=OSError("no HTTP in tests"))

    def start_mock(self, **opts):
        mock_server = MockMoonraker(**opts)
        mock_server.start()
        self.addCleanup(mock_server.stop)
        return mock_server

    def serve_flow_log(self, mock_server, error=None):
        """Answer flow_calculator.log downloads from the mock printer's log (or raise error)."""
        def get_text(path, timeout):
            if error is not None:
                raise error
            return "\n".join(mock_server.flow_log)
        self.patch(apa.Moonraker, "_get_text", side_effect=get_text)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = apa.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def run_tool(self, mock_server, *extra):
        out = self.tmpdir()
        rc, stdout, stderr = self.main("--host", "127.0.0.1:%d" % mock_server.port,
                                       "--out", out, "--tools", "0", *extra)
        self.assertEqual(rc, 0, stderr)
        (session,) = os.listdir(out)
        d = os.path.join(out, session)
        return d, self.load(d), stdout

    @staticmethod
    def load(d):
        with open(os.path.join(d, "results.json"), encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def cells(mock_server):
        return [c for c in mock_server.commands if c.startswith("APA_COIL_CELL")]

    def only_point(self, res):
        (p,) = res["tools"]["0"]["points"]
        return p

    # -- normal runs --------------------------------------------------------
    def test_full_suite(self):
        m = self.start_mock()
        d, res, _ = self.run_tool(m)
        pts = {p["name"]: p for p in res["tools"]["0"]["points"]}

        self.assertEqual(pts["low_anchor"]["k_out"], 0.017)
        self.assertEqual(len(pts["low_anchor"]["sweeps"]), 1)
        self.assertEqual(pts["center"]["k_out"], 0.012)

        hf = pts["high_flow"]
        self.assertEqual(hf["status"], "ok")
        self.assertEqual(len(hf["sweeps"]), 2)
        self.assertEqual(hf["sweeps"][1]["min"], 0.04)
        self.assertEqual(hf["k_out"], 0.047)

        hfo = pts["high_force"]
        self.assertEqual(len(hfo["sweeps"]), 2)
        self.assertEqual(hfo["sweeps"][1]["step"], 0.001)
        self.assertEqual(hfo["k_out"], 0.017)

        self.assertEqual(pts["stress"]["status"], "error")
        self.assertTrue(all("macro reports" not in n for p in pts.values()
                            for n in p.get("notes", [])))

        self.assertEqual(res["tools"]["0"]["fallback_pa"], 0.017)
        with open(os.path.join(d, "orca_T0.txt"), encoding="utf-8") as f:
            orca = f.read()
        self.assertIn("0.017, 8.14, 2000", orca)
        self.assertIn("0.047, 20.35, 2000", orca)
        self.assertNotIn("8.14, 10000", orca)  # weak high_force row is left out
        self.assertTrue(m.commands[-1].startswith("SET_HEATER_TEMPERATURE HEATER=extruder "))
        with open(os.path.join(d, "console.log"), encoding="utf-8") as f:
            self.assertNotIn("B:28.1", f.read())

    def test_dry_run_sends_no_motion(self):
        m = self.start_mock()
        out = self.tmpdir()
        rc, stdout, _ = self.main("--host", "127.0.0.1:%d" % m.port, "--out", out, "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(os.listdir(out), [])
        self.assertEqual(m.commands, [])
        self.assertEqual(m.methods, ["server.connection.identify", "server.info",
                                     "printer.objects.query"])
        self.assertIn("would send: APA_COIL_CELL NAME=center", stdout)

    def test_k_star_below_range_moves_down(self):
        m = self.start_mock(model=dict(MODEL, center=(0.002, 8000.0)))
        _, res, _ = self.run_tool(m, "--points", "center")
        p = self.only_point(res)
        self.assertEqual((p["status"], p["k_out"]), ("ok", 0.002))
        self.assertEqual([s["min"] for s in p["sweeps"]], [0.005, 0.0])

    def test_retries_exhausted(self):
        m = self.start_mock(model=dict(MODEL, center=(0.5, 1000.0)))
        _, res, _ = self.run_tool(m, "--points", "center", "--max-retries", "1")
        p = self.only_point(res)
        self.assertEqual((p["status"], len(p["sweeps"])), ("kstar_above", 2))
        self.assertIn("no sign flip, every area > 0, K* above 0.070 (2 sweep(s))", p["notes"])

    def test_no_flip_reason_printed_with_retries_off(self):
        # every area < 0 from the first K, like the reviewer's high_force run
        m = self.start_mock(model=dict(MODEL, center=(0.002, 8000.0)))
        _, res, stdout = self.run_tool(m, "--points", "center", "--max-retries", "0")
        self.assertEqual(self.only_point(res)["status"], "kstar_below")
        self.assertIn("center: no sign flip, every area < 0, K* below 0.005", stdout)

    def test_macro_mismatch_is_noted(self):
        m = self.start_mock(finish_conf_offset=1)
        _, res, _ = self.run_tool(m, "--points", "center")
        self.assertTrue(any(n.startswith("macro reports") for n in self.only_point(res)["notes"]))

    def test_abort_line_marks_point_error(self):
        m = self.start_mock(abort_for="center")
        _, res, _ = self.run_tool(m, "--points", "center")
        p = self.only_point(res)
        self.assertEqual(p["status"], "error")
        self.assertIn("abort calibration: heating timeout", p["notes"])

    def test_filament_name_is_saved(self):
        m = self.start_mock()
        d, res, stdout = self.run_tool(m, "--points", "center", "--filament", " Polymaker  PLA/Pro ")
        self.assertTrue(os.path.basename(d).endswith("_Polymaker_PLA_Pro"))
        self.assertEqual(res["filament"], "Polymaker PLA/Pro")
        self.assertIn("filament=Polymaker PLA/Pro", stdout)
        self.assertIn("=== T0  220 C  Polymaker PLA/Pro ===", stdout)
        with open(os.path.join(d, "orca_T0.txt"), encoding="utf-8") as f:
            orca = f.read()
        self.assertIn("# filament: Polymaker PLA/Pro\n# nozzle temperature: 220 C", orca)

        m.commands.clear()  # resume keeps the saved name without passing it again
        rc, stdout, _ = self.main("--resume", d, "--points", "center,low_anchor")
        self.assertEqual(rc, 0)
        self.assertEqual(self.load(d)["filament"], "Polymaker PLA/Pro")
        self.assertIn("Polymaker PLA/Pro ===", stdout)

    # -- resume -------------------------------------------------------------
    def test_resume_skips_finished_points(self):
        m = self.start_mock()
        d, _, _ = self.run_tool(m, "--points", "center")
        m.commands.clear()
        rc, _, _ = self.main("--resume", d, "--points", "center,low_anchor")  # host from session
        self.assertEqual(rc, 0)
        cells = self.cells(m)
        self.assertEqual(len(cells), 1)
        self.assertIn("NAME=low_anchor", cells[0])

    def test_resume_runs_only_requested_tools_and_points(self):
        m = self.start_mock()
        d = self.tmpdir()
        plan = apa.build_points(ENVELOPE, ["stress", "center"], False)
        state = {"host": "127.0.0.1:%d" % m.port, "temp": 220,
                 "envelope": {k: ENVELOPE[k] for k in apa.ENVELOPE_KEYS},
                 "tools": {t: {"points": [dict(p) for p in plan]} for t in ("0", "1")}}
        with open(os.path.join(d, "results.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)

        rc, _, _ = self.main("--resume", d, "--tools", "1", "--points", "stress,center",
                             "--skip-stress")
        self.assertEqual(rc, 0)
        cells = self.cells(m)
        self.assertEqual(len(cells), 1)
        self.assertIn("NAME=center", cells[0])
        self.assertIn("EXTRUDER=1", cells[0])
        res = self.load(d)
        self.assertNotIn("status", res["tools"]["0"]["points"][1])  # T0 center untouched
        self.assertNotIn("status", res["tools"]["1"]["points"][0])  # T1 stress skipped

    def test_resume_reruns_failed_point_cleanly(self):
        m = self.start_mock()
        d, res, _ = self.run_tool(m, "--points", "stress")
        self.assertEqual(self.only_point(res)["status"], "error")
        m.opts["fail_stress"] = False
        m.model = dict(MODEL, stress=(0.011, 20000.0))
        rc, _, _ = self.main("--resume", d, "--points", "stress")
        self.assertEqual(rc, 0)
        p = self.only_point(self.load(d))
        self.assertEqual((p["status"], p["k_out"], p["notes"]), ("ok", 0.011, []))

    # -- flow_calculator.log fallback -----------------------------------------
    def test_missing_console_lines_come_from_flow_log(self):
        m = self.start_mock(drop_console_for="center")
        fast = 200 * ENVELOPE["ratio"]
        for a in (99999, 88888, 77777, -1, -2, -3, -4):  # an older center sweep
            m.log_result(fast, a)
        self.serve_flow_log(m)
        _, res, stdout = self.run_tool(m, "--points", "center")
        p = self.only_point(res)
        self.assertEqual((p["status"], p["k_out"]), ("ok", 0.012))
        self.assertIn("recovered 7 area value(s)", stdout)

    def test_short_flow_log_is_not_trusted(self):
        m = self.start_mock(drop_console_for="center", flow_log_short=True)
        m.log_result(200 * ENVELOPE["ratio"], 55555)  # stale value that could fill the gap
        self.serve_flow_log(m)
        _, res, stdout = self.run_tool(m, "--points", "center", "--max-retries", "0")
        p = self.only_point(res)
        self.assertEqual(p["status"], "no_data")
        self.assertEqual(p["sweeps"][0]["samples"], [])
        self.assertIn("can't be matched to this sweep", stdout)

    def test_broken_flow_log_download_does_not_stop_run(self):
        m = self.start_mock(drop_console_for="center")
        self.serve_flow_log(m, error=http.client.IncompleteRead(b"partial"))
        _, res, stdout = self.run_tool(m, "--points", "center", "--max-retries", "0")
        self.assertEqual(self.only_point(res)["status"], "no_data")
        self.assertIn("can't be matched", stdout)

    # -- failures -----------------------------------------------------------
    def test_bad_message_does_not_end_session(self):
        m = self.start_mock(send_garbage=True)
        _, res, _ = self.run_tool(m, "--points", "center")
        p = self.only_point(res)
        self.assertEqual((p["status"], p["k_out"]), ("ok", 0.012))

    def test_connection_drop_mid_sweep(self):
        m = self.start_mock(close_for="center")
        d = self.tmpdir()
        rc, _, stderr = self.main("--host", "127.0.0.1:%d" % m.port, "--out", d,
                                  "--points", "low_anchor,center")
        self.assertEqual(rc, 1)
        self.assertIn("Moonraker connection lost", stderr)
        self.assertIn("--resume", stderr)
        (session,) = os.listdir(d)
        pts = {p["name"]: p for p in self.load(os.path.join(d, session))["tools"]["0"]["points"]}
        self.assertEqual(pts["low_anchor"]["status"], "ok")  # saved before the drop
        self.assertNotIn("status", pts["center"])

    def test_hung_sweep_times_out(self):
        m = self.start_mock(hang_for="center")
        self.patch(apa, "SWEEP_TIMEOUT_PER_K", 0)
        self.patch(apa, "SWEEP_TIMEOUT_EXTRA", 0.3)
        rc, _, stderr = self.main("--host", "127.0.0.1:%d" % m.port, "--out", self.tmpdir(),
                                  "--points", "center")
        self.assertEqual(rc, 1)
        self.assertIn("center did not finish", stderr)
        self.assertTrue(m.commands[-1].startswith("SET_HEATER_TEMPERATURE"))  # still sent

    def test_preflight_errors(self):
        cases = [({"klippy_state": "startup"}, "Klipper is not ready"),
                 ({"envelope": {}}, "adaptive_pa_macro.cfg is not loaded"),
                 ({"print_state": "printing"}, "Printer is busy")]
        for opts, text in cases:
            with self.subTest(text=text):
                m = self.start_mock(**opts)
                rc, _, stderr = self.main("--host", "127.0.0.1:%d" % m.port, "--dry-run")
                self.assertEqual(rc, 1)
                self.assertIn(text, stderr)
                self.assertEqual(m.commands, [])

    def test_unreachable_printer_returns_error(self):
        with socket.socket() as s:  # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        rc, _, stderr = self.main("--host", "127.0.0.1:%d" % port, "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("Cannot reach Moonraker", stderr)


class ArgumentTests(TempDirMixin, unittest.TestCase):
    def assert_usage_error(self, argv, text):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            apa.main(argv)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn(text, err.getvalue())

    def write_session(self, state):
        d = self.tmpdir()
        with open(os.path.join(d, "results.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        return d

    def test_unknown_point(self):
        self.assert_usage_error(["--host", "x", "--points", "centre"], "unknown point(s) centre")

    def test_missing_resume_dir(self):
        self.assert_usage_error(["--resume", os.path.join(self.tmpdir(), "nope")],
                                "cannot resume")

    def test_resume_temperature_change(self):
        d = self.write_session({"host": "x", "temp": 220, "tools": {}})
        self.assert_usage_error(["--resume", d, "--temp", "230"], "measured at 220 C")

    def test_resume_filament_change(self):
        d = self.write_session({"host": "x", "temp": 220, "filament": "PLA", "tools": {}})
        self.assert_usage_error(["--resume", d, "--filament", "PETG"],
                                "measured with filament 'PLA'")

    def test_filament_without_letters(self):
        self.assert_usage_error(["--host", "x", "--filament", " / "], "--filament needs")

    def test_filament_slug(self):
        self.assertEqual(apa.filament_slug("eSUN PLA+ (black)"), "eSUN_PLA+_black")
        self.assertEqual(apa.filament_slug("Bambu PETG-HF 1.75"), "Bambu_PETG_HF_1.75")

    def test_non_positive_temperature(self):
        self.assert_usage_error(["--host", "x", "--temp", "0"], "--temp must be")

    def test_host_and_parse_log_are_exclusive(self):
        self.assert_usage_error(["--host", "x", "--parse-log", "f.txt"], "not allowed with")


class ParseLogTests(TempDirMixin, unittest.TestCase):
    def run_parse_log(self, text):
        path = os.path.join(self.tmpdir(), "console.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(apa.main(["--parse-log", path]), 0)
        return out.getvalue()

    def test_rows_from_saved_console(self):
        # center point of the real T3 run (PLA, 220 C)
        out = self.run_parse_log(
            "// Point: center | Tool T3 (extruder3) | MODE=MEASURE TEMP=220\n"
            "// XY: speed=200.0 mm/s  accel=6000.0  ->  Q=16.2832 mm3/s\n"
            "// k0.005: area=27191.000\n// k0.010: area=6844.000\n"
            "// k0.015: area=-10963.000\n// k0.020: area=-24837.000\n")
        self.assertIn("=== T3", out)
        self.assertIn("0.012, 16.28, 6000", out)

    def test_point_without_xy_banner_is_left_out(self):
        out = self.run_parse_log("// Point: center | Tool T0 (extruder)\n"
                                 "// k0.010: area=2880.000\n// k0.015: area=-5120.000\n")
        self.assertIn("no XY banner", out)
        self.assertIn("(no usable rows)", out)

    def test_no_banners(self):
        path = os.path.join(self.tmpdir(), "empty.txt")
        open(path, "w").close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(apa.main(["--parse-log", path]), 1)
        self.assertIn("No APA test point banners", err.getvalue())


if __name__ == "__main__":
    unittest.main()
