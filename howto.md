# Synchronize Lyrion Music Server (LMS) volume slider with CamillaDSP volume

This howto adds **LMS volume sync** on top of a working Linux **squeezelite → ALSA Loopback → CamillaDSP → DAC** endpoint.

## Overview: What you are building

On the Camilla host, the squeezelite player feeds the CamillaDSP instance. LMS volume for that player is not applied by squeezelite; instead, volume is applied in CamillaDSP's **Volume** filter.

The Python sync script (`cdsp_lms_volume_sync.py`) reads and writes the CamillaDSP volume directly over its **websocket**, and talks to LMS over its **CLI** (port 9090) to keep both volume sliders aligned. It also syncs muting status across LMS and CamillaDSP.

### Audio path (PCM data)

The PCM data stays at 100% volume level through squeezelite and the loopback. The volume is controlled in CamillaDSP.
