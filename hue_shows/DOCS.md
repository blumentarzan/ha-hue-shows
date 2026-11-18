# Hue Shows

Music-synced light shows for Philips Hue, streamed with the Hue Entertainment
API (DTLS, 50 Hz).

## First start

1. Open **Hue Shows** from the sidebar.
2. Enter your Hue Bridge IP, click **Connect**, and press the round link button
   on the bridge within 30 seconds.
3. Choose an effect and click **Start lights**.

For beat sync, click **Use microphone** (Home Assistant is served over https, so
the browser allows microphone access) or pick a simulated BPM.

## Options

- **bridge**: optional. Pre-fills the bridge IP on the setup card.

## Limits of the Hue Bridge (Pro, firmware 6.5)

- An entertainment area holds at most 10 lights.
- Only one stream runs at a time per bridge.

The engine works around both by choosing the best 10-light area for each effect.

## Current status

Early version. The light layout (names like `Spot DG L1.1`) and the preset
areas are still specific to one installation; a layout editor is planned.
Always restores the lights' previous state when stopped.
