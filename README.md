# ha-hue-shows

Music-synced Philips Hue light shows for Home Assistant, using the Hue
Entertainment API (DTLS streaming, 50 Hz).

**Status:** early. Built and tuned on a Hue Bridge Pro with 20 Perifo spots; the
goal is a general add-on for any Hue setup.

## Install (Home Assistant add-on)

1. Settings -> Add-ons -> Add-on Store -> menu -> Repositories.
2. Add `https://github.com/blumentarzan/ha-hue-shows` (private repos need access).
3. Install **Hue Shows** and open it from the sidebar.

## Layout

- `hue_shows/` - the add-on (`config.yaml`, `Dockerfile`, `run.sh`)
- `hue_shows/app/` - engine, streaming, web UI (Python stdlib only)
- `HANDOFF.md` - decisions, measured bridge limits, next steps

## Run on a PC (development)

```
python hue_shows/app/hue_setup.py register --bridge <IP>
python hue_shows/app/hue_live.py
```

then open http://127.0.0.1:8765.
