"""Live mode: beat-driven effects + a small web UI (browser-mic beat detection).

  python hue_live.py                 # UI on http://127.0.0.1:8765 (this PC only)
  python hue_live.py --host 0.0.0.0  # reachable on the LAN (no auth! trusted LAN only)

Note: browsers only allow microphone access on https or localhost, so use the
page on this PC (localhost). A phone needs https (later: HA add-on Ingress).
The browser detects beats and POSTs them to /api/beat. The engine streams
effects (hue_engine.py) and restores the lights on Stop / Ctrl+C.
"""
import argparse, collections, json, os, statistics, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hue_engine as eng
import hue_setup as hs
import hue_stream as st


# ---- beat clock -------------------------------------------------------------
class BeatClock:
    def __init__(self):
        self.last = 0.0                    # time of last beat
        self.strength = 0.0
        self.count = 0
        self.intervals = collections.deque(maxlen=12)
        self.sim_bpm = 0.0                 # >0: internal metronome instead of mic
        self.min_gap = 0.25

    def beat(self, strength=1.0, now=None):
        now = now or time.time()
        if self.last and now - self.last < self.min_gap:
            return
        if self.last and now - self.last < 2.0:
            self.intervals.append(now - self.last)
        self.last, self.strength, self.count = now, max(0.0, min(1.0, strength)), self.count + 1

    @property
    def active(self):
        """Beats are arriving (or simulated). Tempo expires after 2.5 s of silence."""
        return bool(self.sim_bpm) or self.since() < 2.5

    @property
    def bpm(self):
        if self.sim_bpm:
            return self.sim_bpm
        if not self.active:
            self.intervals.clear()
            return 0.0
        if len(self.intervals) < 3:
            return 0.0
        iv = statistics.median(self.intervals)
        b = 60.0 / iv
        while b < 70: b *= 2
        while b > 180: b /= 2
        return round(b, 1)

    def since(self, now=None):
        return (now or time.time()) - self.last if self.last else 99.0

    def tick(self):                        # metronome for simulation
        if self.sim_bpm and self.since() >= 60.0 / self.sim_bpm:
            self.beat(1.0)


# ---- beat effects (read the clock live; they ignore t) ----------------------
def b_pulse(bc):
    def f(t, L):
        k = max(0.0, 1 - bc.since() / 0.45) ** 2 * (0.4 + 0.6 * bc.strength)
        h = (bc.count * 0.13) % 1
        return {s.id: eng.hsv(h + 0.03 * s.y, 0.9, k) for s in L}
    return f


def b_flash(bc):
    def f(t, L):
        on = bc.since() < 0.07 and bc.strength > 0.15
        return {s.id: ((1, 1, 1) if on else (0, 0, 0)) for s in L}
    return f


def b_step(bc):
    """One spot per beat, entrance -> window, with a short fading trail."""
    def f(t, L):
        rail = sorted([s for s in L if s.rank], key=lambda s: s.y)
        n = max(1, len(rail))
        k = max(0.0, 1 - bc.since() / 0.5)
        out = {s.id: (0, 0, 0) for s in L}
        for j, s in enumerate(rail):
            age = (bc.count - j) % n                    # beats since this spot's turn
            v = (1 - age / 3.0) if age < 3 else 0.0
            if age == 0:
                v = 0.35 + 0.65 * k
            out[s.id] = eng.hsv(0.58 + 0.05 * (j % 4), 0.9, max(0.0, v))
        return out
    return f


def b_chase_bpm(bc):
    def f(t, L):
        if not bc.active:                      # no music: go dark instead of running on
            return {s.id: (0, 0, 0) for s in L}
        sp = 2.0 + (bc.bpm or 100) / 60.0 * 1.6
        return eng.fx_chase(speed=sp, hue=0.08)(t, L)
    return f


# name -> (factory, suggested area or None, ambient)
CATALOG = {
    "wave":        (lambda bc: eng.fx_wave(), None, True),
    "ripple":      (lambda bc: eng.fx_ripple(), None, False),
    "chase":       (lambda bc: eng.fx_chase(), None, True),
    "runner":      (lambda bc: eng.fx_runner(), "Attic Show Zigzag", False),
    "beat pulse":  (b_pulse, "Attic Show Mix", False),
    "beat flash":  (b_flash, "Attic Show Flash", False),
    "beat step":   (b_step, "Attic Show Zigzag", False),
    "beat chase":  (b_chase_bpm, None, True),
}


# ---- live runner ------------------------------------------------------------
class Live:
    def __init__(self):
        self.bc = BeatClock()
        self.engine = None
        self.thread = None
        self.running = False
        self.state = {"effect": "wave", "area": "auto", "ambient": True}
        self.info = {"area": None, "fps": 0, "switches": 0, "rest": 0, "error": None}
        self._fx_cache = {}

    def effect(self):
        n = self.state["effect"]
        if n not in self._fx_cache:
            self._fx_cache = {n: CATALOG[n][0](self.bc)}
        return self._fx_cache[n]

    def start(self):
        if self.running:
            return
        self.running = True
        self.info["error"] = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=15)

    def _loop(self):
        try:
            e = self.engine = eng.Engine()
            threading.Thread(target=e._ambient_loop, daemon=True).start()
            t0 = time.time(); seq = 0; last_sel = -99.0; last_sw = -99.0; fr = 0; fps_t = time.time()
            hz = 50.0; n = 0; t_loop = time.time()
            while self.running:
                now = time.time() - t0
                self.bc.tick()
                s = self.state
                effect = self.effect()
                e.ambient_on = s["ambient"]
                want = s["area"]
                if want != "auto" and want in e.areas:
                    if not e.cur or e.cur[0] != want:
                        e.switch(want, now, "pinned"); last_sw = now
                elif now - last_sel >= 0.25:
                    last_sel = now
                    best, sc = e.best_area(effect, now)
                    if not e.cur:
                        e.switch(best, now, "start"); last_sw = now
                    elif best != e.cur[0] and sc[best] > 1.5 * sc[e.cur[0]] and now - last_sw >= 4.0:
                        e.switch(best, now, "score"); last_sw = now
                frame = effect(now, e.spots)
                for k, v in frame.items():
                    o = e.smooth[k]
                    e.smooth[k] = tuple(o[j] + 0.08 * (v[j] - o[j]) for j in range(3))
                if e.cur:
                    name, area, chans, d = e.cur
                    d.send(st.frame(seq, area["id"], [(c, frame[i]) for c, i in chans]))
                    seq += 1; fr += 1
                if time.time() - fps_t >= 1.0:
                    self.info.update(fps=round(fr / (time.time() - fps_t)), area=e.cur[0] if e.cur else None,
                                     switches=len(e.log), rest=e.rest_updates)
                    fr, fps_t = 0, time.time()
                n += 1
                time.sleep(max(0, t_loop + n / hz - time.time()))
        except Exception as ex:                       # surface to the UI
            self.info["error"] = repr(ex)
            print("live error:", repr(ex))
        finally:
            self.running = False
            if self.engine:
                self.engine.shutdown()
                self.engine = None
            self.info.update(area=None, fps=0)

    def status(self):
        return {"running": self.running, "state": self.state, "info": self.info,
                "configured": hs.is_configured(), "default_bridge": os.environ.get("HUE_BRIDGE", ""),
                "bpm": self.bc.bpm, "beats": self.bc.count, "active": self.bc.active, "sim_bpm": self.bc.sim_bpm,
                "effects": {k: v[1] for k, v in CATALOG.items()},
                "areas": ["auto"] + list(eng.PRESETS)}


LIVE = Live()
HERE = os.path.dirname(os.path.abspath(__file__))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/status":
            return self._json(LIVE.status())
        if self.path in ("/", "/index.html"):
            b = open(os.path.join(HERE, "hue_ui.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/api/beat":
            LIVE.bc.beat(float(body.get("strength", 1.0)))
            return self._json({"ok": True})
        if self.path == "/api/set":
            s = LIVE.state
            if body.get("effect") in CATALOG:
                s["effect"] = body["effect"]
                if "area" not in body:
                    s["area"] = CATALOG[body["effect"]][1] or "auto"
                if "ambient" not in body:
                    s["ambient"] = CATALOG[body["effect"]][2]
            if "area" in body and (body["area"] == "auto" or body["area"] in eng.PRESETS):
                s["area"] = body["area"]
            if "ambient" in body:
                s["ambient"] = bool(body["ambient"])
            if "sim_bpm" in body:
                LIVE.bc.sim_bpm = max(0.0, min(200.0, float(body["sim_bpm"])))
            return self._json(LIVE.status())
        if self.path == "/api/register":
            bridge = str(body.get("bridge", "")).strip()
            if not bridge or not all(c.isalnum() or c in ".-:" for c in bridge):
                return self._json({"ok": False, "error": "enter the bridge IP address"}, 400)
            try:
                ok = hs.register(bridge, wait=30)       # press the link button during this window
            except Exception as ex:
                return self._json({"ok": False, "error": f"cannot reach bridge: {ex}"})
            return self._json({"ok": ok, "error": None if ok else "link button not pressed in time"})
        if self.path == "/api/start":
            if not hs.is_configured():
                return self._json({"error": "bridge not set up"}, 400)
            LIVE.start()
            return self._json(LIVE.status())
        if self.path == "/api/stop":
            LIVE.stop()
            return self._json(LIVE.status())
        self.send_error(404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"UI on http://{a.host}:{a.port}  (Ctrl+C to quit; lights are restored)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        LIVE.stop()


if __name__ == "__main__":
    main()
