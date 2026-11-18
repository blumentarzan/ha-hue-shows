"""Hue show engine: effects on a layout, shown through 10-light streams.

An effect is a function  effect(t, spots) -> {spot_id: (r, g, b)}  that colours
ALL spots of the layout. The engine then decides how to show it on a bridge that
streams only 10 lights from one area at a time:

  * stream  - the best-matching preset area (<= 10 lights, 50 Hz), chosen by
              scoring each area against the upcoming ~4 s of the effect
              (brightness x motion), with dwell time + hysteresis so the
              0.3 s area switch only happens when it pays off.
  * ambient - lights outside the streamed area get a gentle, low-passed,
              dimmed version of the effect over REST (~1 update/s each),
              or stay dark when the segment says ambient=False.
  * pin     - a segment can pin a specific area ("use just these 10").

Layout and presets come from hue_layout (editable in the web UI).
Saves and restores light state.
"""
import colorsys, math, threading, time
import hue_layout as lay
import hue_setup as hs
import hue_stream as st


class Spot:
    def __init__(self, d, ent):
        self.id, self.name, self.ent = d["id"], d["name"], ent
        self.x, self.y, self.z, self.group = d["x"], d["y"], d["z"], d.get("group", "")


def span_of(L):
    ys = [s.y for s in L] or [0.0]
    return min(ys), max(ys)


# ---- effects (all return {spot.id: (r,g,b)}) --------------------------------
def hsv(h, s, v):
    return colorsys.hsv_to_rgb(h % 1.0, s, max(0.0, min(1.0, v)))


def fx_wave(speed=1.8, wavelength=4.0):
    def f(t, L):
        return {s.id: st.wave_colour(s.y, t, speed, wavelength) for s in L}
    return f


def fx_ripple(speed=2.2):
    """Rings expand from the middle of the room outwards."""
    def f(t, L):
        lo, hi = span_of(L)
        centre, reach = (lo + hi) / 2, max(1.0, (hi - lo) / 2 + 0.5)
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
        lo, hi = span_of(L)
        span = max(1.0, hi - lo)
        p = (t * speed) % (2 * span)
        p = p if p < span else 2 * span - p
        pos = lo + p
        return {s.id: hsv(hue + 0.05 * s.y, 1.0, max(0.0, 1 - abs(s.y - pos) / width)) for s in L}
    return f


def fx_pulse(bpm=120, hue=0.8):
    def f(t, L):
        k = (1 - ((t * bpm / 60) % 1)) ** 3
        return {s.id: hsv(hue, 0.85, k) for s in L}
    return f


def fx_runner(speed=4.0, hue=0.6, width=1.1):
    """One bright spot running entrance -> window, with a short trail, looping."""
    def f(t, L):
        lo, hi = span_of(L)
        pos = lo - 0.3 + (t * speed) % max(1.0, hi - lo + 0.6)
        return {s.id: hsv(hue + 0.04 * s.y, 0.9, max(0.0, 1 - abs(s.y - pos) / width) ** 1.2) for s in L}
    return f


# ---- engine -----------------------------------------------------------------
class Engine:
    def __init__(self, layout=None):
        self.cfg = hs.load_cfg()
        lights = lay.discover(self.cfg)
        layout = layout or lay.load_or_migrate(self.cfg, lights)
        if not layout or not layout["spots"]:
            raise RuntimeError("no layout yet: add your lights in the Layout tab")
        self.layout = layout
        found = {d["id"]: d for d in lights}
        ents = {i: d["ent"] for i, d in found.items()}
        self.spots = [Spot(d, ents[d["id"]]) for d in layout["spots"]
                      if d["id"] in found and ents.get(d["id"])]
        if not self.spots:
            raise RuntimeError("none of the layout's lights support entertainment streaming")
        self.by_id = {s.id: s for s in self.spots}
        self.by_ent = {s.ent: s for s in self.spots}
        self.saved = {s.id: hs.snapshot(found[s.id]["light"]) for s in self.spots}
        self.app_id = st.application_id(self.cfg)
        self.members = {p["name"]: {i for i in p["spots"] if i in self.by_id}
                        for p in layout["presets"] if any(i in self.by_id for i in p["spots"])}
        if not self.members:
            raise RuntimeError("no presets yet: use 'Suggest presets' in the Layout tab")
        report = lay.sync_areas(self.cfg, layout, ents)
        for n in report["created"] + report["replaced"]:
            print(f"area {n}: created/updated")
        self.areas = {a["metadata"]["name"]: a for a in
                      hs.v2(self.cfg, "GET", "entertainment_configuration")["data"]
                      if a["metadata"]["name"] in self.members}
        self.members = {n: m for n, m in self.members.items() if n in self.areas}
        self.cur = None                       # (name, area, chans, dtls)
        self.ambient_on = False
        self.rest_ids = set()
        self.lock = threading.Lock()
        self.smooth = {s.id: (0.0, 0.0, 0.0) for s in self.spots}
        self.stop_flag = threading.Event()
        self.log = []
        self.rest_updates = 0

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
        scores = {n: self.score(n, effect, t) for n in self.members}
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

    # -- run a scripted show (segments); live mode lives in hue_live
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
                            self.switch(best, now, "start"); n = 0; t0 = time.time() - t
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


def demo_show(presets):
    """Generic demo: auto-picked areas for wave / ripple / chase, pinned strobe."""
    pin = presets[0] if presets else None
    return [
        {"name": "wave, auto, ambient wash", "effect": fx_wave(), "secs": 8},
        {"name": "ripple from the middle, auto, others dark", "effect": fx_ripple(), "secs": 7, "ambient": False},
        {"name": "strobe, pinned", "effect": fx_strobe(), "secs": 5, "pin": pin, "ambient": False},
        {"name": "chase, auto, ambient wash", "effect": fx_chase(), "secs": 9},
    ]


if __name__ == "__main__":
    e = Engine()
    e.run(demo_show(list(e.members)))
