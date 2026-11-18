"""Hue show engine: effects on a 20-spot layout, shown through 10-light streams.

An effect is a function  effect(t, layout) -> {spot_id: (r, g, b)}  that colours
ALL 20 spots. The engine then decides how to show it on a bridge that streams
only 10 lights from one area at a time:

  * stream  - the best-matching preset area (10 lights, 50 Hz), chosen by
              scoring each area against the upcoming ~1 s of the effect
              (brightness x motion), with dwell time + hysteresis so the
              0.3 s area switch only happens when it pays off.
  * ambient - lights outside the streamed area get a gentle, low-passed,
              dimmed version of the effect over REST (~1 update/s each),
              or stay dark when the segment says ambient=False.
  * pin     - a segment can pin a specific area ("use just these 10").

Run:  python hue_engine.py          (demo show)
Saves and restores light state.
"""
import colorsys, math, threading, time
import hue_setup as hs
import hue_stream as st

# ---- layout -----------------------------------------------------------------
class Spot:
    def __init__(self, s):
        self.id, self.name, self.ent = s["id"], s["name"], s["ent"]
        self.rail, self.row, self.idx = s["rail"], s["row"], s["idx"]
        self.x, self.y, self.z = hs.position_m(self.rail, self.row, self.idx)
        self.rank = (self.row - 1) * 4 + self.idx if self.rail in "LR" else None   # 1..8 along a rail


def rails(sp, which, ranks=range(1, 9)):
    return [s for s in sp if s.rail in which and s.rank in ranks]


def crossbar(sp, idxs):
    return [s for s in sp if s.rail == "T" and s.idx in idxs]


# preset 10-light areas (bridge cap). name -> selector(spots) -> list[Spot]
PRESETS = {
    "Attic Show L":      lambda sp: rails(sp, "L") + crossbar(sp, (1, 2)),
    "Attic Show R":      lambda sp: rails(sp, "R") + crossbar(sp, (3, 4)),
    "Attic Show Mix":    lambda sp: rails(sp, "LR", (1, 4, 5, 8)) + crossbar(sp, (2, 3)),
    "Attic Show Front":  lambda sp: rails(sp, "LR", (1, 2, 3, 4)) + crossbar(sp, (2, 3)),
    "Attic Show Back":   lambda sp: rails(sp, "LR", (5, 6, 7, 8)) + crossbar(sp, (1, 4)),
    "Attic Show Spread": lambda sp: rails(sp, "L", (2, 4, 6, 8)) + rails(sp, "R", (1, 3, 5, 7)) + crossbar(sp, (1, 4)),
    # front->back zigzag: L1, R2, L3, R4 ... (no crossbar); mirror starts on the right
    "Attic Show Zigzag":  lambda sp: rails(sp, "L", (1, 3, 5, 7)) + rails(sp, "R", (2, 4, 6, 8)),
    "Attic Show ZigzagR": lambda sp: rails(sp, "R", (1, 3, 5, 7)) + rails(sp, "L", (2, 4, 6, 8)),
    # all 4 crossbar spots + 3 evenly spread rail spots per side (staggered L/R)
    "Attic Show Flash":   lambda sp: crossbar(sp, (1, 2, 3, 4)) + rails(sp, "L", (1, 4, 7)) + rails(sp, "R", (2, 5, 8)),
}


# ---- effects (all return {spot.id: (r,g,b)}) --------------------------------
def hsv(h, s, v):
    return colorsys.hsv_to_rgb(h % 1.0, s, max(0.0, min(1.0, v)))


def fx_wave(speed=1.8, wavelength=4.0):
    def f(t, L):
        return {s.id: st.wave_colour(s.y, t, speed, wavelength) for s in L}
    return f


def fx_ripple(speed=2.2, reach=3.6, centre=4.0):
    def f(t, L):
        pos = (t * speed) % reach
        out = {}
        for s in L:
            d = abs(s.y - centre)
            out[s.id] = hsv(0.55 + 0.1 * d, 0.9, max(0.0, 1 - abs(d - pos) / 0.9) ** 1.5)
        return out
    return f


def fx_strobe(rate=4.0, duty=0.2, colour=(1, 1, 1)):
    def f(t, L):
        on = (t * rate) % 1 < duty
        return {s.id: (colour if on else (0, 0, 0)) for s in L}
    return f


def fx_chase(speed=3.0, hue=0.0, width=1.6):
    """Bright band running entrance -> window and back (ping-pong)."""
    def f(t, L):
        span = 7.0
        p = (t * speed) % (2 * span)
        p = p if p < span else 2 * span - p
        pos = 1.0 + p
        return {s.id: hsv(hue + 0.05 * s.y, 1.0, max(0.0, 1 - abs(s.y - pos) / width)) for s in L}
    return f


def fx_pulse(bpm=120, hue=0.8):
    def f(t, L):
        k = (1 - ((t * bpm / 60) % 1)) ** 3
        return {s.id: hsv(hue, 0.85, k) for s in L}
    return f


# ---- engine -----------------------------------------------------------------
class Engine:
    def __init__(self):
        self.cfg = hs.load_cfg()
        spots = hs.attic_lights(self.cfg)
        hs.attach_entertainment_ids(self.cfg, spots)
        self.spots = [Spot(s) for s in spots]
        self.by_id = {s.id: s for s in self.spots}
        self.by_ent = {s.ent: s for s in self.spots}
        self.saved = {s["id"]: hs.snapshot(s["light"]) for s in spots}
        self.app_id = st.application_id(self.cfg)
        self.areas = self._ensure_areas()
        self.members = {n: {s.id for s in PRESETS[n](self.spots)} for n in PRESETS}
        self.cur = None                       # (name, area, chans, dtls)
        self.ambient_on = False
        self.rest_ids = set()
        self.lock = threading.Lock()
        self.smooth = {s.id: (0.0, 0.0, 0.0) for s in self.spots}
        self.stop_flag = threading.Event()
        self.log = []
        self.rest_updates = 0

    # -- bridge areas
    def _ensure_areas(self):
        have = {a["metadata"]["name"]: a for a in hs.v2(self.cfg, "GET", "entertainment_configuration")["data"]}
        for name, sel in PRESETS.items():
            if name in have:
                continue
            members = sel(self.spots)
            assert 0 < len(members) <= 10, (name, len(members))
            body = {"metadata": {"name": name}, "configuration_type": "music",
                    "locations": {"service_locations": [
                        {"service": {"rid": s.ent, "rtype": "entertainment"},
                         "position": hs.to_hue(s.x, s.y, s.z), "positions": [hs.to_hue(s.x, s.y, s.z)],
                         "equalization_factor": 1} for s in members]}}
            hs.v2(self.cfg, "POST", "entertainment_configuration", body)
            print(f"created area {name}")
        return {a["metadata"]["name"]: a for a in hs.v2(self.cfg, "GET", "entertainment_configuration")["data"]
                if a["metadata"]["name"] in PRESETS}

    def _close(self):
        if not self.cur:
            return
        name, area, chans, d = self.cur
        black = [(c, (0, 0, 0)) for c, _ in chans]
        for _ in range(3):
            d.send(st.frame(0, area["id"], black)); time.sleep(0.02)
        d.close()
        hs.v2(self.cfg, "PUT", f"entertainment_configuration/{area['id']}", {"action": "stop"})
        self.cur = None

    def switch(self, name, t, reason):
        t0 = time.time()
        self._close()
        area = self.areas[name]
        res = hs.v2(self.cfg, "PUT", f"entertainment_configuration/{area['id']}", {"action": "start"})
        if res.get("errors"):
            raise RuntimeError(res["errors"])
        d = st.DtlsStream(self.cfg["bridge"], self.app_id, self.cfg["clientkey"])
        d.handshake()
        chans = [(c["channel_id"], self.by_ent[c["members"][0]["service"]["rid"]].id) for c in area["channels"]]
        self.cur = (name, area, chans, d)
        with self.lock:
            self.rest_ids = {s.id for s in self.spots} - self.members[name]
        dt = time.time() - t0
        self.log.append((round(t, 1), name, reason, round(dt, 2)))
        print(f"  t={t:5.1f}s  -> {name:<18} ({reason}, switch {dt:.2f}s)")

    # -- selection
    def score(self, name, effect, t, window=4.0, steps=24):
        mem = self.members[name]
        total, prev = 0.0, None
        for i in range(steps):
            fr = effect(t + window * i / steps, self.spots)
            lum = {k: max(v) for k, v in fr.items() if k in mem}
            total += sum(lum.values())
            if prev:                                      # motion: change between samples
                total += 1.5 * sum(abs(lum[k] - prev[k]) for k in lum)
            prev = lum
        return total

    def best_area(self, effect, t):
        scores = {n: self.score(n, effect, t) for n in PRESETS}
        return max(scores, key=scores.get), scores

    # -- ambient (REST) for lights outside the stream
    def _ambient_loop(self):
        last = {}
        while not self.stop_flag.is_set():
            if not self.ambient_on:
                with self.lock:
                    todo = [i for i in self.rest_ids if last.get(i) != "off"]
                for i in todo:
                    if self.stop_flag.is_set():
                        break
                    try:
                        hs.v2(self.cfg, "PUT", f"light/{i}", {"on": {"on": False}})
                        last[i] = "off"
                    except Exception:
                        pass
                time.sleep(0.3)
                continue
            with self.lock:
                ids = list(self.rest_ids)
            for i in ids:
                if self.stop_flag.is_set() or not self.ambient_on:
                    break
                r, g, b = self.smooth[i]
                v = max(r, g, b) * 0.45
                if v < 0.02:
                    if last.get(i) != "off":
                        try:
                            hs.v2(self.cfg, "PUT", f"light/{i}", {"on": {"on": False}, "dynamics": {"duration": 800}})
                            last[i] = "off"
                        except Exception:
                            pass
                    continue
                X = 0.4124 * r + 0.3576 * g + 0.1805 * b
                Y = 0.2126 * r + 0.7152 * g + 0.0722 * b
                Z = 0.0193 * r + 0.1192 * g + 0.9505 * b
                tot = (X + Y + Z) or 1
                try:
                    hs.v2(self.cfg, "PUT", f"light/{i}", {"on": {"on": True}, "dimming": {"brightness": max(1, round(v * 100))},
                          "color": {"xy": {"x": round(X / tot, 4), "y": round(Y / tot, 4)}}, "dynamics": {"duration": 900}})
                    last[i] = "on"
                    self.rest_updates += 1
                except Exception:
                    pass
            time.sleep(0.05)

    # -- run a show
    def run(self, show, hz=50.0, dwell=4.0, gain=1.5):
        th = threading.Thread(target=self._ambient_loop, daemon=True)
        th.start()
        t_show = time.time()
        try:
            for seg in show:
                effect, secs = seg["effect"], seg["secs"]
                pin, self.ambient_on = seg.get("pin"), seg.get("ambient", True)
                print(f"[{seg.get('name', '')}] {secs:.0f}s  pin={pin or 'auto'}  ambient={self.ambient_on}")
                t0, n, last_sel, seq = time.time(), 0, -99.0, 0
                while time.time() - t0 < secs:
                    t = time.time() - t0
                    now = time.time() - t_show
                    if pin:
                        if not self.cur or self.cur[0] != pin:
                            self.switch(pin, now, "pinned"); n = 0; t0 = time.time(); t = 0
                    elif now - last_sel >= 0.25:
                        last_sel = now
                        best, sc = self.best_area(effect, t)
                        if not self.cur:
                            self.switch(best, now, "start"); n = 0; t0 = time.time() - t;
                        elif best != self.cur[0] and sc[best] > gain * sc[self.cur[0]] and now - self.log[-1][0] >= dwell:
                            self.switch(best, now, f"score {sc[best]:.1f} vs {sc[self.cur[0]]:.1f}")
                            n = 0; t0 = time.time() - t
                    frame = effect(t, self.spots)
                    for k, v in frame.items():                         # low-pass for ambient
                        o = self.smooth[k]
                        self.smooth[k] = tuple(o[j] + 0.08 * (v[j] - o[j]) for j in range(3))
                    name, area, chans, d = self.cur
                    d.send(st.frame(seq, area["id"], [(c, frame[i]) for c, i in chans]))
                    seq += 1; n += 1
                    time.sleep(max(0, t0 + n / hz - time.time()))
            print(f"done: {len(self.log)} area switch(es), {self.rest_updates} ambient REST updates")
        finally:
            self.shutdown()

    def shutdown(self):
        self.stop_flag.set()
        try:
            self._close()
        finally:
            time.sleep(0.5)
            for s in self.spots:
                p = self.saved[s.id]
                body = {"on": {"on": p["on"]}}
                if p["dimming"]:
                    body["dimming"] = {"brightness": p["dimming"]["brightness"]}
                if p["color"]:
                    body["color"] = p["color"]
                hs.v2(self.cfg, "PUT", f"light/{s.id}", body)
            print("Previous light state restored.")


def demo_show():
    return [
        {"name": "wave, auto, ambient wash", "effect": fx_wave(), "secs": 8},
        {"name": "ripple from the middle, auto, others dark", "effect": fx_ripple(), "secs": 7, "ambient": False},
        {"name": "flash, pinned to Spread (whole-room coverage)", "effect": fx_strobe(), "secs": 5,
         "pin": "Attic Show Spread", "ambient": False},
        {"name": "chase entrance->window, auto, ambient wash", "effect": fx_chase(), "secs": 9},
        {"name": "pulse, auto, others dark", "effect": fx_pulse(), "secs": 6, "ambient": False},
    ]


def fx_runner(speed=4.0, hue=0.6, width=1.1):
    """One bright spot running entrance -> window, with a short trail, looping."""
    def f(t, L):
        pos = 1.2 + (t * speed) % 6.6
        return {s.id: hsv(hue + 0.04 * s.y, 0.9, max(0.0, 1 - abs(s.y - pos) / width) ** 1.2) for s in L}
    return f


def demo_show2():
    return [
        {"name": "zigzag travel, front->back, left first", "effect": fx_runner(), "secs": 8,
         "pin": "Attic Show Zigzag", "ambient": False},
        {"name": "zigzag travel, right first", "effect": fx_runner(hue=0.05), "secs": 6,
         "pin": "Attic Show ZigzagR", "ambient": False},
        {"name": "flash: crossbar + evenly spread rails", "effect": fx_strobe(rate=5), "secs": 6,
         "pin": "Attic Show Flash", "ambient": False},
    ]


if __name__ == "__main__":
    import sys
    Engine().run(demo_show2() if "demo2" in sys.argv else demo_show())
