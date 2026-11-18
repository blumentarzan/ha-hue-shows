
Hue attic light shows: handoff
Goal: professional, fast Hue light shows in the attic (Estrich), driven from Home Assistant, with music sync. Final form: a general HA add-on with an Ingress web UI.

Setup facts
Hue Bridge Pro at 192.168.5.17 (firmware 6.5.2071602000), one Hue integration in HA (entry a2a5e4c63786074e76f09327c74737b9), HA 2026.11.
20 Hue Perifo cylinder spots in HA area "Estrich", entities light.perifo_cylinder_spot_*, friendly names Spot DG <L|R|T><row>.<n>.
L (left rail): rows 1 and 2, 4 spots each. R (right rail): same. T1 (crossbar): 4 spots.
Layout: H lying in the room. Entrance at the front, window at the back, ~9 m long. L and R rails are 2 m apart, ~7 m long, centred. Two rails in sequence per side (row 1 front, row 2 back). Crossbar at ~3.5 m.
L and R spots point down, crossbar T spots point up.
1.1 -> 1.4 counts front to back on rails; T counts left to right.
Layout and order CONFIRMED in the attic with calibrate (2026-11-18).
Not yet working or integrated: Sonos "Estrich" (off), WLED strip (off, not in HA).
Existing attic config to leave alone: light.estrich group, input_select.light_scenes_attic, automation KNXLink - Light Attic. Existing bridge areas Eliot, Music area, DG (DG = 10 attic spots) also left alone.
Tools on this PC: real Python is 3.12 ARM64 at C:\Users\adoer\AppData\Local\Programs\Python\Python312-arm64\python.exe (plain `python` is only a Store stub). Credentials in ~/.hue_show.json.
Decisions
Output: Hue Entertainment API (DTLS streaming, 50 Hz works). The REST API is too slow for shows.
Music: (1) phone/browser mic with beat detection in the browser, (4) pre-analysed tracks with choreographed timelines. No line-in/mic hardware.
Form: general HA add-on (Ingress UI), pluggable outputs (Hue first, WLED later), layout editor with H-shape preset.
Tests may stop and start the attic lights. Save and restore state afterwards.
Hard limits found (verified, matches Hueblog)
- An entertainment area holds max 10 lights (11+ rejected by the bridge).
- Only one DTLS stream at a time per bridge: a second area's handshake times out.
So only 10 of 20 spots can stream simultaneously. Two areas exist: "Attic Show L" (L rail + T1.1, T1.2) and "Attic Show R" (R rail + T1.3, T1.4).
Area switching (hue_switch_test.py, 6 switches L<->R): gap 0.29-0.39 s, avg 0.33 s, fully reliable (stop ~0.1 s, API start ~0.17 s, DTLS handshake only ~0.02 s). Lights go dark/hold during the gap. Fine for scene changes or every few bars, too slow for beat-by-beat alternation.
Mixed mode (hue_mixed_test.py): stream L at 49-50 Hz while driving the 10 R spots over REST. Stream is unaffected. REST gets only ~9 updates/s total sequential (0.9/s per light), ~13/s parallel (1.3/s per light, 2 errors). So the REST half is only good for slow fades/static colours, not motion.
Strategy (agreed with user): pick the best 10 lights per effect, others off; switch between pre-made 10-light areas. REST half is acceptable only as a gentle ambient wash. Third area "Attic Show Mix" = rail spots ranked 1,4,5,8 on L and R (middle two + two ends) + T1.2, T1.3; used for middle-out ripple and all-white flashes. hue_demo.py runs L wave, R wave, Mix ripple, Mix flash (switch 0.24-0.46 s). Interpretation of "from the middle use the two outer ones" was a guess; confirm with the user.
hue_engine.py (first engine, works): effects return colours for all 20 spots; engine streams the best-scoring preset 10-light area (scored over next 4 s: brightness x motion; dwell 4 s, gain 1.5x before switching), lights outside the stream get a dimmed low-passed ambient wash via REST or stay dark per segment (ambient flag); segments can pin an area. Presets (bridge areas): L, R, Mix, Front, Back, Spread. Demo show: wave, ripple, strobe, chase, pulse = 3 switches in 35 s (short 1 s lookahead caused 8 switches and was too twitchy). Effects so far: wave, ripple, strobe, chase, pulse.
New presets (user's ideas, 2026-11-18): "Attic Show Zigzag" = L1,R2,L3,R4,L5,R6,L7,R8 front->back (8 lights, no crossbar, "looks great for light travel"), "Attic Show ZigzagR" = mirror (R first); "Attic Show Flash" = all 4 crossbar + L ranks 1,4,7 + R ranks 2,5,8 ("looks great for flash"). Preset list now 9 areas. `python hue_engine.py demo2` runs a pinned show of these three.
Live mode (hue_live.py + hue_ui.html): stdlib HTTP server, `python hue_live.py` -> http://127.0.0.1:8765. UI: start/stop lights, effect buttons, area (auto or pinned preset), ambient toggle, microphone beat detection in the browser (low-band energy vs running average, POSTs /api/beat) or a simulated BPM metronome. BeatClock keeps the BPM (median of intervals, folded to 70-180). Beat effects: beat pulse (Mix), beat flash (Flash), beat step (Zigzag; one spot per beat), beat chase (speed follows BPM). Tested via API with simulated 128 BPM: 50 fps, correct areas, clean stop/restore. NOT yet tested: real microphone input (needs localhost or https; phone needs https -> HA add-on Ingress). No auth on the server: keep on 127.0.0.1 or a trusted LAN.
Done
hue_setup.py: register, lights, create-area (makes the two areas), calibrate. All run and working.
hue_stream.py: DTLS-PSK streaming via ctypes into Python's bundled OpenSSL 3; rainbow wave front to back on one area, 50 Hz, smooth (confirmed visually). Saves/restores light state.
Next
Decide how to cover all 20 spots given one stream at a time: alternate areas (gap while switching), mix streaming + REST for the other half, or design shows around 10 spots. Test how fast stop/start of areas is.
Effects engine (chase, wave, strobe, pulse) with the layout model.
Add-on skeleton, web UI, browser-mic beat detection, timeline mode. (Add-on runs on Linux, so DTLS can use a normal library there.)
WLED output, Sonos/Spotify later.
