#!/usr/bin/env python3
"""Hue Bridge Pro setup for light shows (stdlib only, Python 3.8+).

Steps (run in this order):
  python3 hue_setup.py register  --bridge <IP>   # press the bridge link button first
  python3 hue_setup.py lights                    # list lights + parsed attic positions
  python3 hue_setup.py create-area               # creates the 'Attic Show' entertainment area
  python3 hue_setup.py calibrate                 # lights each spot in order, restores state

Credentials go to ~/.hue_show.json (mode 0600). The bridge uses a self-signed
certificate, so TLS verification is skipped for this LAN-only connection.
"""
import argparse, json, os, re, ssl, sys, time, urllib.request

CFG = os.environ.get("HUE_SHOW_CONFIG") or os.path.expanduser("~/.hue_show.json")
NAME_RE = re.compile(r"Spot DG ([LRT])(\d)\.(\d)")

# ---- room model (metres): origin at entrance, y runs to the window ----------
ROOM_LEN = 9.0
RAIL_X = {"L": -1.0, "R": 1.0}          # rails are 2 m apart
RAIL_START, RAIL_LEN = 1.0, 7.0          # ~7 m rails, centred in the room
CROSSBAR_Y = 3.5                          # crossbar position (confirm with user)
T_X = [-0.75, -0.25, 0.25, 0.75]          # T1.1..T1.4 assumed left -> right
Z_DOWN, Z_UP = -0.5, 0.5                  # downlights vs. crossbar uplights


def position_m(rail, row, idx):
    """Return (x, y, z) in metres for a spot name like L2.3."""
    if rail == "T":
        return T_X[idx - 1], CROSSBAR_Y, Z_UP
    seg = RAIL_LEN / 2                    # two rails in sequence, row 1 then row 2
    start = RAIL_START + (row - 1) * seg
    y = start + (idx - 0.5) * seg / 4
    return RAIL_X[rail], y, Z_DOWN


def to_hue(x, y, z):
    """Metres -> Hue normalised coords. Hue: x left(-1)/right(+1), y back(-1)/front(+1)."""
    return {"x": round(max(-1, min(1, x / 1.5)), 3),
            "y": round(max(-1, min(1, 1 - 2 * y / ROOM_LEN)), 3),  # entrance = front
            "z": z}


# ---- tiny HTTP helper --------------------------------------------------------
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def call(method, url, body=None, headers=None, https=True):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=10, context=_ctx if https else None) as r:
        return json.loads(r.read() or b"{}")


def load_cfg():
    if not os.path.exists(CFG):
        sys.exit("Not registered yet. Run: hue_setup.py register --bridge <IP>")
    return json.load(open(CFG))


def v2(cfg, method, path, body=None):
    return call(method, f"https://{cfg['bridge']}/clip/v2/resource/{path}", body,
                {"hue-application-key": cfg["username"]})


# ---- commands ----------------------------------------------------------------
def is_configured():
    return os.path.exists(CFG)


def register(bridge, wait=30):
    """Register with the bridge (link button must be pressed). Saves credentials
    to CFG. Returns True on success, False if the button was not pressed in time."""
    for _ in range(max(1, int(wait / 2))):
        r = call("POST", f"https://{bridge}/api",
                 {"devicetype": "hue_show#setup", "generateclientkey": True})
        ok = r[0].get("success") if r else None
        if ok:
            cfg = {"bridge": bridge, "username": ok["username"], "clientkey": ok["clientkey"]}
            os.makedirs(os.path.dirname(CFG) or ".", exist_ok=True)
            fd = os.open(CFG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f)
            return True
        time.sleep(2)
    return False


def cmd_register(a):
    print("Press the link button on the bridge now (waiting up to 30 s)...")
    if register(a.bridge):
        print(f"Registered. Credentials saved to {CFG}")
    else:
        sys.exit("Link button was not pressed in time. Try again.")


def attic_lights(cfg):
    out = []
    for l in v2(cfg, "GET", "light")["data"]:
        m = NAME_RE.fullmatch(l["metadata"]["name"].strip())
        if m:
            rail, row, idx = m.group(1), int(m.group(2)), int(m.group(3))
            ent = next((s["rid"] for s in l.get("owner_services", []) if s["rtype"] == "entertainment"), None)
            out.append({"id": l["id"], "name": l["metadata"]["name"], "rail": rail,
                        "row": row, "idx": idx, "light": l, "ent": ent})
    return sorted(out, key=lambda s: ("LRT".index(s["rail"]), s["row"], s["idx"]))


def attach_entertainment_ids(cfg, spots):
    """Light -> entertainment service id (via the owning device)."""
    ents = {e["owner"]["rid"]: e["id"] for e in v2(cfg, "GET", "entertainment")["data"]}
    for s in spots:
        s["ent"] = ents.get(s["light"]["owner"]["rid"])


def cmd_lights(a):
    cfg = load_cfg()
    spots = attic_lights(cfg)
    attach_entertainment_ids(cfg, spots)
    for s in spots:
        x, y, z = position_m(s["rail"], s["row"], s["idx"])
        print(f"{s['name']:<14} x={x:+.2f} y={y:4.2f} z={z:+.1f}  entertainment={'yes' if s['ent'] else 'NO'}")
    print(f"{len(spots)} attic spots found (expected 20).")


def cmd_create_area(a):
    cfg = load_cfg()
    spots = attic_lights(cfg)
    attach_entertainment_ids(cfg, spots)
    if len(spots) != 20 or any(not s["ent"] for s in spots):
        sys.exit("Expected 20 entertainment-capable spots; run 'lights' to inspect.")
    # The bridge caps an entertainment area at 10 lights (verified on firmware
    # 6.5.2071602000), so the 20 spots are split into two areas of 10.
    groups = {
        "Attic Show L": [s for s in spots if s["rail"] == "L"
                         or (s["rail"] == "T" and s["idx"] <= 2)],
        "Attic Show R": [s for s in spots if s["rail"] == "R"
                         or (s["rail"] == "T" and s["idx"] >= 3)],
    }
    for name, members in groups.items():
        body = {"metadata": {"name": name},
                "configuration_type": "music",
                "locations": {"service_locations": [
                    {"service": {"rid": s["ent"], "rtype": "entertainment"},
                     "position": to_hue(*position_m(s["rail"], s["row"], s["idx"])),
                     "positions": [to_hue(*position_m(s["rail"], s["row"], s["idx"]))],
                     "equalization_factor": 1}
                    for s in members]}}
        r = v2(cfg, "POST", "entertainment_configuration", body)
        print(name, len(members), "lights:", json.dumps(r))


def snapshot(l):
    return {"on": l["on"]["on"],
            "dimming": l.get("dimming"),
            "color": l.get("color", {}).get("xy") and {"xy": l["color"]["xy"]}}


def cmd_calibrate(a):
    cfg = load_cfg()
    spots = attic_lights(cfg)
    saved = {s["id"]: snapshot(s["light"]) for s in spots}
    path = os.path.expanduser("~/.hue_show_saved_state.json")
    json.dump(saved, open(path, "w"))
    print(f"Previous light state saved to {path}\nLighting each spot in order (red = rail L, green = R, blue = T)...")
    colours = {"L": (0.675, 0.322), "R": (0.17, 0.7), "T": (0.153, 0.048)}
    try:
        for s in spots:
            print(f"  {s['name']}")
            x, y = colours[s["rail"]]
            v2(cfg, "PUT", f"light/{s['id']}", {"on": {"on": True}, "dimming": {"brightness": 100},
                                                "color": {"xy": {"x": x, "y": y}}})
            time.sleep(a.delay)
            v2(cfg, "PUT", f"light/{s['id']}", {"on": {"on": False}})
    finally:
        for s in spots:                      # restore, even if interrupted
            p = saved[s["id"]]
            body = {"on": {"on": p["on"]}}
            if p["dimming"]:
                body["dimming"] = {"brightness": p["dimming"]["brightness"]}
            if p["color"]:
                body["color"] = p["color"]
            v2(cfg, "PUT", f"light/{s['id']}", body)
        print("Previous state restored.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register"); r.add_argument("--bridge", required=True); r.set_defaults(f=cmd_register)
    sub.add_parser("lights").set_defaults(f=cmd_lights)
    sub.add_parser("create-area").set_defaults(f=cmd_create_area)
    c = sub.add_parser("calibrate"); c.add_argument("--delay", type=float, default=0.8); c.set_defaults(f=cmd_calibrate)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
