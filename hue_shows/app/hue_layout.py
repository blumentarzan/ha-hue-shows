"""Layout model: where each Hue light is in the room, and which 10-light
entertainment areas (presets) the engine may stream.

Stored as JSON next to the credentials (HUE_SHOW_LAYOUT or <config dir>/hue_layout.json):

  {"room": {"length": 9.0, "width": 3.0},          # metres; y runs entrance -> window
   "spots":   [{"id", "name", "x", "y", "z", "group"}],   # x left(-)/right(+), z down(-)/up(+)
   "presets": [{"name": "<bridge area name>", "spots": [ids...]}]}   # <= 10 spots each

The Hue bridge caps an entertainment area at 10 lights and streams one area at
a time, so presets are the 10-light subsets the engine switches between.
"""
import json, os
import hue_setup as hs

MAX_AREA = 10
PATH = os.environ.get("HUE_SHOW_LAYOUT") or os.path.join(os.path.dirname(hs.CFG) or ".", "hue_layout.json")


def empty():
    return {"room": {"length": 9.0, "width": 3.0}, "spots": [], "presets": []}


# ---- bridge discovery ---------------------------------------------------------
def discover(cfg):
    """All lights on the bridge that can take part in entertainment streaming."""
    ents = {e["owner"]["rid"]: e["id"] for e in hs.v2(cfg, "GET", "entertainment")["data"]}
    out = []
    for l in hs.v2(cfg, "GET", "light")["data"]:
        ent = ents.get(l["owner"]["rid"])
        out.append({"id": l["id"], "name": l["metadata"]["name"], "ent": ent, "light": l})
    return sorted(out, key=lambda d: d["name"].lower())


def public_lights(lights):
    return [{"id": d["id"], "name": d["name"], "entertainment": bool(d["ent"])} for d in lights]


# ---- load / save / validate -----------------------------------------------------
def load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(layout):
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(layout, f, indent=1)
    os.replace(tmp, PATH)


def _num(v, lo, hi, what):
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number")
    if not lo <= v <= hi:
        raise ValueError(f"{what} out of range")
    return round(v, 3)


def validate(d):
    """Return a cleaned layout or raise ValueError."""
    if not isinstance(d, dict):
        raise ValueError("layout must be an object")
    room = d.get("room") or {}
    out = {"room": {"length": _num(room.get("length", 9), 1, 60, "room length"),
                    "width": _num(room.get("width", 3), 1, 60, "room width")},
           "spots": [], "presets": []}
    ids = set()
    for s in d.get("spots", []):
        sid = str(s.get("id", ""))
        if not sid or sid in ids:
            raise ValueError("spot ids must be unique")
        ids.add(sid)
        out["spots"].append({"id": sid, "name": str(s.get("name", ""))[:60],
                             "x": _num(s.get("x", 0), -60, 60, "x"), "y": _num(s.get("y", 0), -60, 60, "y"),
                             "z": _num(s.get("z", 0), -10, 10, "z"), "group": str(s.get("group", ""))[:20]})
    names = set()
    for p in d.get("presets", []):
        name = str(p.get("name", "")).strip()[:40]
        members = [str(i) for i in p.get("spots", [])]
        if not name or name in names:
            raise ValueError("preset names must be unique and not empty")
        if len(members) > MAX_AREA:
            raise ValueError(f"'{name}' has {len(members)} lights; the bridge allows at most {MAX_AREA} per area")
        if len(set(members)) != len(members) or any(m not in ids for m in members):
            raise ValueError(f"'{name}' contains unknown or duplicate lights")
        names.add(name)
        out["presets"].append({"name": name, "spots": members})
    return out


# ---- Hue coordinates -------------------------------------------------------------
def hue_pos(s, room):
    clamp = lambda v: round(max(-1.0, min(1.0, v)), 3)
    return {"x": clamp(s["x"] / (room["width"] / 2)), "y": clamp(1 - 2 * s["y"] / room["length"]), "z": clamp(s["z"])}


# ---- suggested presets (generic, geometry based) ----------------------------------
def _evenly(items, n):
    if len(items) <= n:
        return list(items)
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def suggest_presets(spots, prefix="Show"):
    """Candidate 10-light areas from geometry: left/right, front/back, spread,
    zigzag and centre-out. The user can edit or replace them."""
    if not spots:
        return []
    byy = sorted(spots, key=lambda s: (s["y"], s["x"]))
    if len(spots) <= MAX_AREA:
        return [{"name": f"{prefix} All", "spots": [s["id"] for s in byy]}]
    xs = sorted(s["x"] for s in spots)
    midx = (xs[0] + xs[-1]) / 2
    left = [s for s in byy if s["x"] < midx]
    right = [s for s in byy if s["x"] >= midx]
    half = len(byy) // 2
    front, back = byy[:half], byy[half:]
    out = []

    def add(name, group):
        ids = [s["id"] for s in group][:MAX_AREA]
        if ids and ids not in [p["spots"] for p in out]:
            out.append({"name": f"{prefix} {name}", "spots": ids})

    add("Left", _evenly(left, MAX_AREA)); add("Right", _evenly(right, MAX_AREA))
    add("Front", _evenly(front, MAX_AREA)); add("Back", _evenly(back, MAX_AREA))
    # spread: checkerboard. Group by depth, flip the left/right order every other
    # group so taking every second light alternates sides.
    groups, flat = {}, []
    for s in byy:
        groups.setdefault(round(s["y"], 1), []).append(s)
    for k, (_, g) in enumerate(sorted(groups.items())):
        flat += sorted(g, key=lambda s: s["x"], reverse=bool(k % 2))
    add("Spread A", _evenly(flat[0::2], MAX_AREA)); add("Spread B", _evenly(flat[1::2], MAX_AREA))
    # zigzag: walk entrance -> window, alternating left / right, never going back
    zl, zr, zig, last, take_left = list(left), list(right), [], -1e9, True
    while len(zig) < MAX_AREA:
        order = (zl, zr) if take_left else (zr, zl)
        nxt = next((s for pool in order for s in pool if s["y"] > last), None)
        if nxt is None:
            break
        (zl if nxt in zl else zr).remove(nxt)
        zig.append(nxt); last = nxt["y"]; take_left = nxt not in left            # next light comes from the other side
    add("Zigzag", zig)
    # centre-out: per side the first, the middle two and the last spot
    mix = []
    for side in (left, right):
        if side:
            m = len(side) // 2
            mix += [side[0], side[m - 1] if m else side[0], side[m], side[-1]]
    seen, mix2 = set(), []
    for s in mix:
        if s["id"] not in seen:
            seen.add(s["id"]); mix2.append(s)
    add("Mix", _evenly(mix2, MAX_AREA))
    return out


# ---- migration of the original attic setup --------------------------------------
def migrate_attic(cfg, lights):
    """If the bridge has lights named 'Spot DG L1.1' ... build the H layout the
    project started with (kept so the first installation keeps working)."""
    by_name = {d["name"].strip(): d for d in lights}
    spots = []
    for l in hs.attic_lights(cfg):
        x, y, z = hs.position_m(l["rail"], l["row"], l["idx"])
        spots.append({"id": l["id"], "name": l["name"], "x": round(x, 3), "y": round(y, 3), "z": z,
                      "group": l["rail"], "_rail": l["rail"], "_rank": (l["row"] - 1) * 4 + l["idx"],
                      "_idx": l["idx"]})
    if not spots:
        return None

    def pick(rails, ranks=range(1, 9)):
        return [s for s in spots if s["_rail"] in rails and s["_rank"] in ranks]

    def cross(idx):
        return [s for s in spots if s["_rail"] == "T" and s["_idx"] in idx]

    defs = {
        "Attic Show L": pick("L") + cross((1, 2)),
        "Attic Show R": pick("R") + cross((3, 4)),
        "Attic Show Mix": pick("LR", (1, 4, 5, 8)) + cross((2, 3)),
        "Attic Show Front": pick("LR", (1, 2, 3, 4)) + cross((2, 3)),
        "Attic Show Back": pick("LR", (5, 6, 7, 8)) + cross((1, 4)),
        "Attic Show Spread": pick("L", (2, 4, 6, 8)) + pick("R", (1, 3, 5, 7)) + cross((1, 4)),
        "Attic Show Zigzag": pick("L", (1, 3, 5, 7)) + pick("R", (2, 4, 6, 8)),
        "Attic Show ZigzagR": pick("R", (1, 3, 5, 7)) + pick("L", (2, 4, 6, 8)),
        "Attic Show Flash": cross((1, 2, 3, 4)) + pick("L", (1, 4, 7)) + pick("R", (2, 5, 8)),
    }
    clean = [{k: v for k, v in s.items() if not k.startswith("_")} for s in spots]
    return {"room": {"length": hs.ROOM_LEN, "width": 3.0}, "spots": clean,
            "presets": [{"name": n, "spots": [s["id"] for s in m]} for n, m in defs.items()]}


def load_or_migrate(cfg, lights):
    d = load()
    if d is not None:
        return validate(d)
    mig = migrate_attic(cfg, lights)
    if mig:
        mig = validate(mig)
        save(mig)
        return mig
    return None


# ---- bridge areas ------------------------------------------------------------------
def sync_areas(cfg, layout, ent_by_id):
    """Create/refresh one bridge entertainment area per preset. An existing area
    with the same name whose lights differ is replaced. Returns a report."""
    by_id = {s["id"]: s for s in layout["spots"]}
    have = {a["metadata"]["name"]: a for a in hs.v2(cfg, "GET", "entertainment_configuration")["data"]}
    report = {"created": [], "replaced": [], "unchanged": [], "errors": []}
    for p in layout["presets"]:
        want = {ent_by_id[i] for i in p["spots"] if ent_by_id.get(i)}
        cur = have.get(p["name"])
        if cur:
            have_ids = {m["service"]["rid"] for c in cur["channels"] for m in c["members"]}
            if have_ids == want:
                report["unchanged"].append(p["name"])
                continue
            hs.v2(cfg, "DELETE", f"entertainment_configuration/{cur['id']}")
        locs = []
        for i in p["spots"]:
            if not ent_by_id.get(i):
                continue
            pos = hue_pos(by_id[i], layout["room"])
            locs.append({"service": {"rid": ent_by_id[i], "rtype": "entertainment"},
                         "position": pos, "positions": [pos], "equalization_factor": 1})
        if not locs:
            report["errors"].append(f"{p['name']}: no entertainment-capable lights")
            continue
        body = {"metadata": {"name": p["name"]}, "configuration_type": "music",
                "locations": {"service_locations": locs}}
        try:
            hs.v2(cfg, "POST", "entertainment_configuration", body)
            report["replaced" if cur else "created"].append(p["name"])
        except Exception as ex:
            report["errors"].append(f"{p['name']}: {ex}")
    return report
