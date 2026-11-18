"""Measure how fast streaming can switch between the two attic areas.

Each switch = close DTLS + stop area A, start area B + DTLS handshake.
Gap = time between the last frame to A and the first frame to B.
Saves and restores light state.
"""
import sys, time
import hue_setup as hs
import hue_stream as st


def main(switches=6, hold=1.5, hz=50):
    cfg = hs.load_cfg()
    spots = hs.attic_lights(cfg)
    hs.attach_entertainment_ids(cfg, spots)
    by_ent = {s["ent"]: s for s in spots}
    saved = {s["id"]: hs.snapshot(s["light"]) for s in spots}
    app_id = st.application_id(cfg)
    areas = {e["metadata"]["name"]: e for e in hs.v2(cfg, "GET", "entertainment_configuration")["data"]}
    cur = None            # (area, chans, dtls)
    gaps, started = [], []

    def open_area(key):
        area = areas[st.AREA_NAMES[key]]
        t_start = time.time()
        res = hs.v2(cfg, "PUT", f"entertainment_configuration/{area['id']}", {"action": "start"})
        if res.get("errors"):
            raise RuntimeError(res["errors"])
        t_api = time.time() - t_start
        chans = [(c["channel_id"], hs.position_m(*[by_ent[c["members"][0]["service"]["rid"]][k] for k in ("rail", "row", "idx")])[1])
                 for c in area["channels"]]
        d = st.DtlsStream(cfg["bridge"], app_id, cfg["clientkey"])
        d.handshake()
        return area, chans, d, t_api

    def stream(cur, secs):
        area, chans, d = cur[:3]
        t0, n, seq = time.time(), 0, 0
        while time.time() - t0 < secs:
            d.send(st.frame(seq, area["id"], [(cid, st.wave_colour(y, time.time())) for cid, y in chans]))
            seq += 1; n += 1
            time.sleep(max(0, t0 + n / hz - time.time()))

    try:
        order = ["L", "R"]
        for i in range(switches + 1):
            key = order[i % 2]
            t_last = time.time()
            if cur:
                cur[2].close()
                hs.v2(cfg, "PUT", f"entertainment_configuration/{cur[0]['id']}", {"action": "stop"})
            t_stop = time.time() - t_last
            cur = open_area(key)
            t_first = time.time()
            if i:
                gaps.append(t_first - t_last)
                print(f"switch {i} -> {key}: gap {gaps[-1]:.2f}s (stop {t_stop:.2f}s, api start {cur[3]:.2f}s, "
                      f"handshake {gaps[-1] - t_stop - cur[3]:.2f}s)")
            else:
                print(f"initial open {key}: {t_first - t_last:.2f}s")
            started.append(cur[0]["id"])
            stream(cur, hold)
        print(f"gap min/avg/max: {min(gaps):.2f}/{sum(gaps)/len(gaps):.2f}/{max(gaps):.2f}s")
    finally:
        if cur:
            cur[2].close()
            hs.v2(cfg, "PUT", f"entertainment_configuration/{cur[0]['id']}", {"action": "stop"})
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
