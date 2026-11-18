"""Demo: pick the best 10 lights per effect, switch areas between effects.

Segments (one area streams at a time, ~0.33 s switch):
  1. L area  - rainbow wave front->back
  2. R area  - rainbow wave front->back
  3. Mix area (rail spots 1,4,5,8 of L+R + T1.2,T1.3) - ripple from the middle
     out to the end spots
  4. Mix area - white flashes on all 10, everything else dark
Creates the 'Attic Show Mix' area on first run. Saves/restores light state.
"""
import colorsys, math, time
import hue_setup as hs
import hue_stream as st

MIX_NAME = "Attic Show Mix"


def mix_members(spots):
    # rail spot rank along the rail: row1.1..1.4 = 1..4, row2.1..2.4 = 5..8
    rank = lambda s: (s["row"] - 1) * 4 + s["idx"]
    return [s for s in spots if (s["rail"] in "LR" and rank(s) in (1, 4, 5, 8))
            or (s["rail"] == "T" and s["idx"] in (2, 3))]


def ensure_mix(cfg, spots):
    areas = hs.v2(cfg, "GET", "entertainment_configuration")["data"]
    for a in areas:
        if a["metadata"]["name"] == MIX_NAME:
            return a["id"]
    members = mix_members(spots)
    assert len(members) == 10
    body = {"metadata": {"name": MIX_NAME}, "configuration_type": "music",
            "locations": {"service_locations": [
                {"service": {"rid": s["ent"], "rtype": "entertainment"},
                 "position": hs.to_hue(*hs.position_m(s["rail"], s["row"], s["idx"])),
                 "positions": [hs.to_hue(*hs.position_m(s["rail"], s["row"], s["idx"]))],
                 "equalization_factor": 1} for s in members]}}
    r = hs.v2(cfg, "POST", "entertainment_configuration", body)
    print("created", MIX_NAME, r["data"][0]["rid"])
    return r["data"][0]["rid"]


def main():
    cfg = hs.load_cfg()
    spots = hs.attic_lights(cfg)
    hs.attach_entertainment_ids(cfg, spots)
    by_ent = {s["ent"]: s for s in spots}
    saved = {s["id"]: hs.snapshot(s["light"]) for s in spots}
    app_id = st.application_id(cfg)
    ensure_mix(cfg, spots)
    areas = {e["metadata"]["name"]: e for e in hs.v2(cfg, "GET", "entertainment_configuration")["data"]}
    cx = hs.ROOM_LEN / 2 - 0.5          # middle of the rails along y (m)
    cur = None

    def chans_of(area):
        out = []
        for c in area["channels"]:
            s = by_ent[c["members"][0]["service"]["rid"]]
            x, y, z = hs.position_m(s["rail"], s["row"], s["idx"])
            out.append((c["channel_id"], s, x, y))
        return out

    def run(name, secs, colour_fn, hz=50):
        nonlocal cur
        t_sw = time.time()
        if cur:
            for _ in range(3):                         # blank before stopping
                cur[2].send(st.frame(0, cur[0]["id"], [(c, (0, 0, 0)) for c, *_ in cur[1]]))
                time.sleep(0.02)
            cur[2].close()
            hs.v2(cfg, "PUT", f"entertainment_configuration/{cur[0]['id']}", {"action": "stop"})
        area = areas[name]
        res = hs.v2(cfg, "PUT", f"entertainment_configuration/{area['id']}", {"action": "start"})
        assert not res.get("errors"), res
        d = st.DtlsStream(cfg["bridge"], app_id, cfg["clientkey"])
        d.handshake()
        ch = chans_of(area)
        cur = (area, ch, d)
        print(f"{name}: switch {time.time() - t_sw:.2f}s, {len(ch)} lights, {secs:.0f}s")
        t0, n, seq = time.time(), 0, 0
        while time.time() - t0 < secs:
            t = time.time() - t0
            d.send(st.frame(seq, area["id"], [(c, colour_fn(t, s, x, y)) for c, s, x, y in ch]))
            seq += 1; n += 1
            time.sleep(max(0, t0 + n / hz - time.time()))

    wave = lambda t, s, x, y: st.wave_colour(y, t)

    def ripple(t, s, x, y):
        # rings expand from the middle (y=cx) outwards; 2 s per sweep, 3.5 m reach
        dist = abs(y - cx) + (0.0 if s["rail"] != "T" else 0.0)
        pos = (t * 2.2) % 3.5
        k = max(0.0, 1 - abs(dist - pos) / 0.9)
        hue = (0.55 + 0.1 * dist) % 1
        return colorsys.hsv_to_rgb(hue, 0.9, k ** 1.5)

    def flash(t, s, x, y):
        beat = (t * 4) % 1                              # 4 flashes per second
        return (1, 1, 1) if beat < 0.2 else (0, 0, 0)

    try:
        for s in spots:                                 # everything dark first
            hs.v2(cfg, "PUT", f"light/{s['id']}", {"on": {"on": False}})
        run("Attic Show L", 4, wave)
        run("Attic Show R", 4, wave)
        run(MIX_NAME, 6, ripple)
        run(MIX_NAME, 4, flash)
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
