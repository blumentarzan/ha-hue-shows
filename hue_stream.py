#!/usr/bin/env python3
"""Hue Entertainment streaming proof of concept (DTLS-PSK, v2 API).

Python's stdlib has no DTLS-PSK, so this drives OpenSSL 3 (the libssl that
ships with Python) through ctypes. Streams a rainbow wave along the room
(entrance -> window) to the 'Attic Show L' / 'Attic Show R' areas.

  python hue_stream.py                      # both areas, 8 s
  python hue_stream.py --areas L --seconds 5

Saves light state first and restores it afterwards.
"""
import argparse, colorsys, ctypes, math, os, select, socket, ssl, struct, sys, time, urllib.request
import hue_setup as hs

AREA_NAMES = {"L": "Attic Show L", "R": "Attic Show R"}
CIPHER = b"PSK-AES128-GCM-SHA256"
SSL_ERROR_WANT_READ, SSL_ERROR_WANT_WRITE = 2, 3

# ---- OpenSSL via ctypes -----------------------------------------------------
_dlls = os.path.join(os.path.dirname(sys.executable), "DLLs")
if hasattr(os, "add_dll_directory") and os.path.isdir(_dlls):
    os.add_dll_directory(_dlls)
_sfx = "-arm64" if "arm64" in sys.executable.lower() or os.path.exists(os.path.join(_dlls, "libssl-3-arm64.dll")) else ("-x64" if os.name == "nt" else "")
try:
    crypto = ctypes.CDLL(os.path.join(_dlls, f"libcrypto-3{_sfx}.dll"))
    libssl = ctypes.CDLL(os.path.join(_dlls, f"libssl-3{_sfx}.dll"))
except OSError:
    from ctypes.util import find_library
    crypto, libssl = ctypes.CDLL(find_library("crypto")), ctypes.CDLL(find_library("ssl"))

c_vp, c_i, c_l = ctypes.c_void_p, ctypes.c_int, ctypes.c_long
PSK_CB = ctypes.CFUNCTYPE(ctypes.c_uint, c_vp, ctypes.c_char_p, c_vp, ctypes.c_uint, c_vp, ctypes.c_uint)

for fn, res, args in [
    (libssl.DTLS_client_method, c_vp, []),
    (libssl.SSL_CTX_new, c_vp, [c_vp]),
    (libssl.SSL_CTX_free, None, [c_vp]),
    (libssl.SSL_CTX_set_cipher_list, c_i, [c_vp, ctypes.c_char_p]),
    (libssl.SSL_CTX_set_psk_client_callback, None, [c_vp, PSK_CB]),
    (libssl.SSL_new, c_vp, [c_vp]),
    (libssl.SSL_free, None, [c_vp]),
    (libssl.SSL_set_bio, None, [c_vp, c_vp, c_vp]),
    (libssl.SSL_connect, c_i, [c_vp]),
    (libssl.SSL_write, c_i, [c_vp, c_vp, c_i]),
    (libssl.SSL_shutdown, c_i, [c_vp]),
    (libssl.SSL_get_error, c_i, [c_vp, c_i]),
    (libssl.SSL_ctrl, c_l, [c_vp, c_i, c_l, c_vp]),
    (crypto.BIO_new_dgram, c_vp, [c_i, c_i]),
    (crypto.BIO_ctrl, c_l, [c_vp, c_i, c_l, c_vp]),
    (crypto.BIO_ADDR_new, c_vp, []),
    (crypto.BIO_ADDR_rawmake, c_i, [c_vp, c_i, c_vp, ctypes.c_size_t, ctypes.c_ushort]),
    (crypto.BIO_ADDR_free, None, [c_vp]),
]:
    fn.restype, fn.argtypes = res, args


class DtlsStream:
    def __init__(self, host, identity, psk_hex, port=2100):
        self.ctx = libssl.SSL_CTX_new(libssl.DTLS_client_method())
        if not libssl.SSL_CTX_set_cipher_list(self.ctx, CIPHER):
            raise RuntimeError("cipher list rejected")
        ident, psk = identity.encode(), bytes.fromhex(psk_hex)

        def cb(_ssl, _hint, id_buf, id_max, psk_buf, psk_max):
            ctypes.memmove(id_buf, ident + b"\0", len(ident) + 1)
            ctypes.memmove(psk_buf, psk, len(psk))
            return len(psk)
        self._cb = PSK_CB(cb)                      # keep alive
        libssl.SSL_CTX_set_psk_client_callback(self.ctx, self._cb)
        self.ssl = libssl.SSL_new(self.ctx)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((host, port))
        self.sock.settimeout(1.0)
        bio = crypto.BIO_new_dgram(self.sock.fileno(), 0)
        addr = crypto.BIO_ADDR_new()
        raw = socket.inet_aton(host)
        crypto.BIO_ADDR_rawmake(addr, socket.AF_INET, raw, 4, socket.htons(port))
        crypto.BIO_ctrl(bio, 32, 0, addr)          # BIO_CTRL_DGRAM_SET_CONNECTED
        crypto.BIO_ADDR_free(addr)
        libssl.SSL_set_bio(self.ssl, bio, bio)
        libssl.SSL_ctrl(self.ssl, 32, 1 << 12, None)   # SSL_OP_NO_QUERY_MTU
        libssl.SSL_ctrl(self.ssl, 17, 1200, None)      # DTLS_SET_LINK_MTU

    def handshake(self, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            r = libssl.SSL_connect(self.ssl)
            if r == 1:
                return
            err = libssl.SSL_get_error(self.ssl, r)
            if err in (SSL_ERROR_WANT_READ, SSL_ERROR_WANT_WRITE):
                select.select([self.sock], [], [], 0.5)
                libssl.SSL_ctrl(self.ssl, 74, 0, None)  # DTLSv1_handle_timeout
                continue
            raise RuntimeError(f"DTLS handshake failed (SSL error {err})")
        raise RuntimeError("DTLS handshake timed out")

    def send(self, data):
        buf = ctypes.create_string_buffer(data, len(data))
        libssl.SSL_write(self.ssl, buf, len(data))

    def close(self):
        try:
            libssl.SSL_shutdown(self.ssl)
        finally:
            libssl.SSL_free(self.ssl)
            libssl.SSL_CTX_free(self.ctx)
            self.sock.close()


# ---- bridge helpers ---------------------------------------------------------
def application_id(cfg):
    req = urllib.request.Request(f"https://{cfg['bridge']}/auth/v1",
                                 headers={"hue-application-key": cfg["username"]})
    with urllib.request.urlopen(req, timeout=10, context=hs._ctx) as r:
        return r.headers["hue-application-id"]


def frame(seq, area_id, levels):
    """levels: list of (channel_id, (r,g,b) 0..1)."""
    out = b"HueStream" + bytes([2, 0, seq & 0xFF, 0, 0, 0, 0]) + area_id.encode()
    for cid, (r, g, b) in levels:
        out += bytes([cid]) + struct.pack(">HHH", *(int(max(0, min(1, v)) * 65535) for v in (r, g, b)))
    return out


def wave_colour(y_m, t, speed=1.8, wavelength=4.0):
    phase = (y_m / wavelength - t * speed / wavelength) % 1.0
    return colorsys.hsv_to_rgb(phase, 1.0, 0.55 + 0.45 * math.sin(2 * math.pi * phase) ** 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--areas", nargs="+", choices=["L", "R"], default=["L", "R"])
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--hz", type=float, default=50.0)
    a = ap.parse_args()

    cfg = hs.load_cfg()
    spots = hs.attic_lights(cfg)
    hs.attach_entertainment_ids(cfg, spots)
    by_ent = {s["ent"]: s for s in spots}
    saved = {s["id"]: hs.snapshot(s["light"]) for s in spots}
    app_id = application_id(cfg)
    areas = {e["metadata"]["name"]: e for e in hs.v2(cfg, "GET", "entertainment_configuration")["data"]}

    sessions = []   # (area_id, [(channel_id, y_m)], DtlsStream)
    started = []
    try:
        for key in a.areas:
            area = areas[AREA_NAMES[key]]
            res = hs.v2(cfg, "PUT", f"entertainment_configuration/{area['id']}", {"action": "start"})
            if res.get("errors"):
                print(f"[{key}] start failed: {res['errors']}")
                continue
            started.append(area["id"])
            chans = []
            for c in area["channels"]:
                sp = by_ent[c["members"][0]["service"]["rid"]]
                chans.append((c["channel_id"], hs.position_m(sp["rail"], sp["row"], sp["idx"])[1]))
            time.sleep(0.5)
            d = DtlsStream(cfg["bridge"], app_id, cfg["clientkey"])
            d.handshake()
            print(f"[{key}] streaming to {area['metadata']['name']} ({len(chans)} channels)")
            sessions.append((area["id"], chans, d))

        t0, seq, n = time.time(), 0, 0
        while time.time() - t0 < a.seconds and sessions:
            t = time.time() - t0
            for area_id, chans, d in sessions:
                d.send(frame(seq, area_id, [(cid, wave_colour(y, t)) for cid, y in chans]))
            seq += 1
            n += 1
            time.sleep(max(0, t0 + n / a.hz - time.time()))
        print(f"sent {n} frames at ~{n / max(time.time() - t0, 1e-6):.0f} Hz to {len(sessions)} area(s)")
    finally:
        for _, _, d in sessions:
            d.close()
        for aid in started:
            hs.v2(cfg, "PUT", f"entertainment_configuration/{aid}", {"action": "stop"})
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
