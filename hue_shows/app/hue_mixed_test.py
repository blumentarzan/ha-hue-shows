"""Stream the L area (50 Hz DTLS) while driving the R-area lights over REST.

Reports how many REST updates per second the R half actually achieves
(sequential, then parallel) and whether the stream stays at 50 Hz.
Saves and restores light state.
"""
import colorsys, threading, time
from concurrent.futures import ThreadPoolExecutor
import hue_setup as hs
import hue_stream as st

SECS = 8.0


def main():
    cfg = hs.load_cfg()
    spots = hs.attic_lights(cfg)
    hs.attach_entertainment_ids(cfg, spots)
    by_ent = {s["ent"]: s for s in spots}
    saved = {s["id"]: hs.snapshot(s["light"]) for s in spots}
    app_id = st.application_id(cfg)
    areas = {e["metadata"]["name"]: e for e in hs.v2(cfg, "GET", "entertainment_configuration")["data"]}
    L = areas["Attic Show L"]
    l_chans = [(c["channel_id"], hs.position_m(*[by_ent[c["members"][0]["service"]["rid"]][k]
               for k in ("rail", "row", "idx")])[1]) for c in L["channels"]]
    l_ids = {by_ent[c["members"][0]["service"]["rid"]]["id"] for c in L["channels"]}
    r_spots = [s for s in spots if s["id"] not in l_ids]
    print(f"streaming {len(l_ids)} spots, REST on {len(r_spots)} spots")

    stop = threading.Event()
    counts = {"rest": 0, "errors": 0, "frames": 0}

    def put(s, t):
        x, y = hs_xy(t, s)
        try:
            hs.v2(cfg, "PUT", f"light/{s['id']}", {"on": {"on": True}, "dimming": {"brightness": 60},
                  "color": {"xy": {"x": x, "y": y}}, "dynamics": {"duration": 100}})
            counts["rest"] += 1
        except Exception:
            counts["errors"] += 1

    def hs_xy(t, s):
        y_m = hs.position_m(s["rail"], s["row"], s["idx"])[1]
        r, g, b = st.wave_colour(y_m, t)
        X = 0.4124 * r + 0.3576 * g + 0.1805 * b
        Y = 0.2126 * r + 0.7152 * g + 0.0722 * b
        Z = 0.0193 * r + 0.1192 * g + 0.9505 * b
        tot = (X + Y + Z) or 1
        return round(X / tot, 4), round(Y / tot, 4)

    def rest_sequential():
        t0 = time.time()
        while not stop.is_set():
            for s in r_spots:
                if stop.is_set():
                    break
                put(s, time.time() - t0)

    def rest_parallel():
        t0 = time.time()
        with ThreadPoolExecutor(10) as ex:
            while not stop.is_set():
                list(ex.map(lambda s: put(s, time.time() - t0), r_spots))

    started = False
    d = None
    try:
        hs.v2(cfg, "PUT", f"entertainment_configuration/{L['id']}", {"action": "start"})
        started = True
        time.sleep(0.4)
        d = st.DtlsStream(cfg["bridge"], app_id, cfg["clientkey"])
        d.handshake()
        for mode, fn in (("sequential", rest_sequential), ("parallel x10", rest_parallel)):
            counts.update(rest=0, errors=0, frames=0)
            stop.clear()
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            t0, n, seq = time.time(), 0, 0
            while time.time() - t0 < SECS:
                t = time.time() - t0
                d.send(st.frame(seq, L["id"], [(c, st.wave_colour(y, t)) for c, y in l_chans]))
                seq += 1; n += 1
                time.sleep(max(0, t0 + n / 50 - time.time()))
            stop.set(); th.join(timeout=5)
            el = time.time() - t0
            print(f"[{mode}] stream {n / el:.0f} Hz | REST {counts['rest'] / el:.1f} updates/s total "
                  f"= {counts['rest'] / el / len(r_spots):.1f}/s per light | errors {counts['errors']}")
    finally:
        stop.set()
        if d:
            d.close()
        if started:
            hs.v2(cfg, "PUT", f"entertainment_configuration/{L['id']}", {"action": "stop"})
        time.sleep(0.5)
        for s in spots:
            p = saved[s["id"]]
            body = {"on": {"on": p["on"]}}
            if p["dimming"]:
                body["dimming"] = {"brightness": p["dimming"]["brightness"]}
            if p["color"]:
                body["color"] = p["color"]
            hs.v2(cfg, "PUT", f"light/{s['id']}", body)
        print("Previous light state restored.")


if __name__ == "__main__":
    main()
