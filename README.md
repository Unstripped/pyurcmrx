# pyurcmrx

Async Python client for URC Total Control MRX controllers on the local network. It is the protocol library behind a Home Assistant integration (published separately, HACS layout).

This is a clean-room implementation. Its only source is the public [open-control-spec](https://github.com/Unstripped/open-control-spec); it contains no vendor code, keys, or assets. Not affiliated with Universal Remote Control, Inc.

## Implemented

| Area | Spec | API |
|---|---|---|
| Discovery (UDP 61510) | §50 | `discover()`, `build_probe()`, `parse_find_ack()` |
| Project catalog (anonymous FTP, `core_model.json`) | §53 §3.4, §30 §1.1, §20 §3 | `fetch_core_model()`, `parse_core_model()`: rooms, devices, buttons with decoded `(device_id, func_id)` steps |
| Control (TCP 51510) | §52 | `ControlClient`: `macro_action` (with press/hold/release flags), `set_volume` (`0x07ED`), `room_power_on/off`, `house_off`, `get_variables`, `get_net_status`, `get_file_time` (project token for change detection); automatic keepalive |
| State telemetry (MQTT 1883) | §00 §5.2 | `subscribe()`, `parse_telemetry()`: room zone status, HDA zone status, variables, room links |

Relays, sensors and RS-232 ports cannot be driven directly from the LAN; they are reachable only through the dealer-programmed buttons and macros listed in the catalog.

## CLI

```
pyurcmrx discover
pyurcmrx catalog 192.168.1.100          # or: pyurcmrx catalog --file core_model.json
pyurcmrx status 192.168.1.100
pyurcmrx variables 192.168.1.100
pyurcmrx macro 192.168.1.100 ROOM DEVICE FUNCTION [--flags 0|1|2|3]
pyurcmrx volume 192.168.1.100 DEVICE LEVEL [--mute]
pyurcmrx watch 192.168.1.100
```

## Development

```
mise install
uv sync
uv run pytest
```

The MQTT subscriber test runs against a local `mosquitto` and is skipped when it is not installed.
