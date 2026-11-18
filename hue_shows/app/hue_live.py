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
import hue_layout as lay
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
        rail = sorted(L, key=lambda s: (s.y, s.x))
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


# name -> (factory, preset-name keyword to pin by default or None, ambient)
CATALOG = {
    "wave":        (lambda bc: eng.fx_wave(), None, True),
    "ripple":      (lambda bc: eng.fx_ripple(), None, False),
    "chase":       (lambda bc: eng.fx_chase(), None, True),
    "runner":      (lambda bc: eng.fx_runner(), "zigzag", False),
    "beat pulse":  (b_pulse, "mix", False),
    "beat flash":  (b_flash, "flash", False),
    "beat step":   (b_step, "zigzag", False),
    "beat chase":  (b_chase_bpm, None, True),
}


def preset_names():
    d = lay.load()
    return [p["name"] for p in d["presets"]] if d else []


def default_area(effect):
    """Pin an effect to a preset whose name contains its keyword (e.g. 'zigzag'),
    otherwise let the engine choose."""
    kw = CATALOG[effect][1]
    if kw:
        for n in preset_names():
            if kw in n.lower() and "zigzagr" not in n.lower():
                return n
    return "auto"


# ---- timeline (pre-analysed track, choreographed cues) -----------------------
import bisect, re

SHOW_DIR = os.environ.get("HUE_SHOW_DIR") or os.path.join(os.path.dirname(hs.CFG) or ".", "hue_shows_saved")


def validate_show(d):
    """Clean a show: {name, audio:{name,duration}, bpm, beats[], strengths[], energy[], cues[]}."""
    if not isinstance(d, dict):
        raise ValueError("show must be an object")
    pn = set(preset_names())
    def fl(v, what, lo=0.0, hi=36000.0):
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{what} must be a number")
        if not lo <= v <= hi:
            raise ValueError(f"{what} out of range")
        return round(v, 3)
    beats = [fl(b, "beat") for b in (d.get("beats") or [])][:30000]
    beats = sorted(beats)
    st_ = [max(0.0, min(1.0, float(x))) for x in (d.get("strengths") or [])][:len(beats)]
    st_ += [1.0] * (len(beats) - len(st_))
    cues = []
    for c in d.get("cues") or []:
        eff = c.get("effect")
        if eff not in CATALOG:
            raise ValueError(f"unknown effect '{eff}'")
        area = c.get("area", "default")
        if area not in ("auto", "default") and area not in pn:
            area = "auto"
        cues.append({"t": fl(c.get("t", 0), "cue time"), "effect": eff, "area": area,
                     "ambient": bool(c.get("ambient", CATALOG[eff][2]))})
    cues.sort(key=lambda c: c["t"])
    au = d.get("audio") or {}
    return {"name": re.sub(r"[^A-Za-z0-9 _.-]", "", str(d.get("name", "show")))[:60].strip() or "show",
            "audio": {"name": str(au.get("name", ""))[:120], "duration": fl(au.get("duration", 0), "duration")},
            "bpm": fl(d.get("bpm", 0), "bpm", 0, 400), "beats": beats, "strengths": [round(x, 3) for x in st_],
            "energy": [round(max(0.0, min(1.0, float(x))), 3) for x in (d.get("energy") or [])][:2000],
            "cues": cues}


class Timeline:
    """Runs a show's cues and beat grid against a playback position that the
    browser reports (audio element). The server keeps its own clock between
    syncs; /api/timeline/sync corrects drift. offset (s) shifts the lights
    ahead (+) or behind (-) the audio to compensate speaker latency."""

    def __init__(self):
        self.show = None
        self.playing = False
        self.start_wall = 0.0
        self.paused_at = 0.0
        self.offset = 0.0
        self.bi = 0
        self.ci = -1
        self.last_pos = 0.0

    def load(self, show):
        self.show, self.playing, self.paused_at = show, False, 0.0
        self._reseek(0.0)

    def raw_position(self):
        return time.time() - self.start_wall if self.playing else self.paused_at

    def _reseek(self, p):
        if not self.show:
            return
        p += self.offset
        self.bi = bisect.bisect_right(self.show["beats"], p)
        times = [c["t"] for c in self.show["cues"]]
        self.ci = bisect.bisect_right(times, p) - 1
        self.last_pos = p
        self.force_cue = True

    def play(self, pos, offset=None):
        if offset is not None:
            self.offset = offset
        self.start_wall, self.playing = time.time() - pos, True
        self._reseek(pos)

    def pause(self):
        self.paused_at, self.playing = self.raw_position(), False

    def seek(self, pos, offset=None):
        if offset is not None:
            self.offset = offset
        if self.playing:
            self.start_wall = time.time() - pos
        else:
            self.paused_at = pos
        self._reseek(pos)

    def sync(self, pos, offset=None):
        if offset is not None:
            self.offset = offset
        if self.playing and abs(pos - self.raw_position()) > 0.04:
            self.start_wall = time.time() - pos

    def tick(self, live):
        sh = self.show
        if not sh or not self.playing:
            return
        p = self.raw_position() + self.offset
        if p < self.last_pos - 0.1:                       # jumped back
            self._reseek(p - self.offset)
        self.last_pos = p
        dur = sh["audio"]["duration"]
        if dur and p > dur + 2.0:
            self.playing = False
            return
        cues = sh["cues"]
        ni = self.ci
        while ni + 1 < len(cues) and cues[ni + 1]["t"] <= p:
            ni += 1
        if ni != self.ci or getattr(self, "force_cue", False):
            self.ci, self.force_cue = ni, False
            if ni >= 0:
                c = cues[ni]
                live.state.update(effect=c["effect"], ambient=c["ambient"],
                                  area=default_area(c["effect"]) if c["area"] == "default" else c["area"])
        beats, fired = sh["beats"], None
        while self.bi < len(beats) and beats[self.bi] <= p:
            fired = self.bi
            self.bi += 1
        if fired is not None and p - beats[fired] < 0.25:     # skip stale beats after a stall
            live.bc.sim_bpm = 0.0
            live.bc.beat(sh["strengths"][fired])

    def status(self):
        sh = self.show
        return {"loaded": bool(sh), "name": sh["name"] if sh else "", "playing": self.playing,
                "position": round(self.raw_position(), 2), "cue": self.ci, "offset_ms": round(self.offset * 1000)}


def show_path(name):
    safe = re.sub(r"[^A-Za-z0-9 _.-]", "", name).strip()
    if not safe:
        raise ValueError("bad show name")
    return os.path.join(SHOW_DIR, safe + ".json")


# ---- live runner ------------------------------------------------------------
class Live:
    def __init__(self):
        self.tl = Timeline()
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
                self.tl.tick(self)
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
                "timeline": self.tl.status(), "configured": hs.is_configured(), "default_bridge": os.environ.get("HUE_BRIDGE", ""),
                "bpm": self.bc.bpm, "beats": self.bc.count, "active": self.bc.active, "sim_bpm": self.bc.sim_bpm,
                "effects": {k: v[1] for k, v in CATALOG.items()},
                "areas": ["auto"] + preset_names()}


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

    def _timeline_post(self, body):
        tl, p = LIVE.tl, self.path.rsplit("/", 1)[-1]
        off = body.get("offset_ms")
        off = None if off is None else max(-1.0, min(1.0, float(off) / 1000.0))
        try:
            if p == "load":
                tl.load(validate_show(body.get("show", body)))
            elif p == "save":
                show = validate_show(body.get("show", body))
                os.makedirs(SHOW_DIR, exist_ok=True)
                with open(show_path(show["name"]), "w") as f:
                    json.dump(show, f)
                return self._json({"ok": True, "name": show["name"]})
            elif p == "delete":
                os.remove(show_path(str(body.get("name", ""))))
                return self._json({"ok": True})
            elif not tl.show:
                return self._json({"error": "no show loaded"}, 400)
            elif p == "play":
                tl.play(max(0.0, float(body.get("position", 0))), off)
            elif p == "pause":
                tl.pause()
            elif p == "seek":
                tl.seek(max(0.0, float(body.get("position", 0))), off)
            elif p == "sync":
                tl.sync(max(0.0, float(body.get("position", 0))), off)
            else:
                return self._json({"error": "unknown timeline call"}, 404)
        except (ValueError, TypeError) as ex:
            return self._json({"error": str(ex)}, 400)
        except OSError as ex:
            return self._json({"error": f"file error: {ex}"}, 500)
        return self._json({"ok": True, "timeline": tl.status()})

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/timeline/list":
            names = sorted(f[:-5] for f in os.listdir(SHOW_DIR) if f.endswith(".json")) if os.path.isdir(SHOW_DIR) else []
            return self._json({"shows": names})
        if path == "/api/timeline/get":
            from urllib.parse import parse_qs
            try:
                with open(show_path(parse_qs(query).get("name", [""])[0])) as f:
                    return self._json({"show": validate_show(json.load(f))})
            except (OSError, ValueError):
                return self._json({"error": "show not found"}, 404)
        if self.path == "/api/status":
            return self._json(LIVE.status())
        if self.path == "/api/layout":
            if not hs.is_configured():
                return self._json({"error": "bridge not set up"}, 400)
            try:
                cfg = hs.load_cfg()
                lights = lay.discover(cfg)
                layout = lay.load_or_migrate(cfg, lights) or lay.empty()
                return self._json({"layout": layout, "lights": lay.public_lights(lights)})
            except Exception as ex:
                return self._json({"error": f"cannot read bridge: {ex}"}, 502)
        if self.path in ("/", "/index.html"):
            b = open(os.path.join(HERE, "hue_ui.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self.send_error(404)

    def _layout_post(self, body):
        if not hs.is_configured():
            return self._json({"error": "bridge not set up"}, 400)
        try:
            layout = lay.validate(body.get("layout", body))
        except ValueError as ex:
            return self._json({"error": str(ex)}, 400)
        if self.path == "/api/layout/suggest":
            return self._json({"presets": lay.suggest_presets(layout["spots"], str(body.get("prefix", "Show"))[:20])})
        if LIVE.running:
            return self._json({"error": "stop the lights before changing the layout"}, 409)
        try:
            if self.path == "/api/layout":
                lay.save(layout)
                return self._json({"ok": True, "layout": layout})
            cfg = hs.load_cfg()                      # /api/layout/apply
            lay.save(layout)
            ents = {d["id"]: d["ent"] for d in lay.discover(cfg)}
            return self._json({"ok": True, "report": lay.sync_areas(cfg, layout, ents)})
        except Exception as ex:
            return self._json({"error": f"bridge error: {ex}"}, 502)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/api/beat":
            LIVE.bc.beat(float(body.get("strength", 1.0)))
            return self._json({"ok": True})
        if self.path in ("/api/layout", "/api/layout/suggest", "/api/layout/apply"):
            return self._layout_post(body)
        if self.path.startswith("/api/timeline/"):
            return self._timeline_post(body)
        if self.path == "/api/set":
            s = LIVE.state
            if body.get("effect") in CATALOG:
                s["effect"] = body["effect"]
                if "area" not in body:
                    s["area"] = default_area(body["effect"])
                if "ambient" not in body:
                    s["ambient"] = CATALOG[body["effect"]][2]
            if "area" in body and (body["area"] == "auto" or body["area"] in preset_names()):
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
