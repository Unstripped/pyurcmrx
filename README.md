# pyurcmrx

Async Python client for URC Total Control MRX controllers on the local network. It is the protocol library behind a Home Assistant integration (published separately, HACS layout).

This is a clean-room implementation. Its only source is the public [open-control-spec](https://github.com/Unstripped/open-control-spec); it contains no vendor code, keys, or assets. Not affiliated with Universal Remote Control, Inc.

## Implemented

| Area | Spec | API |
|---|---|---|
| Discovery (UDP 61510) | §50 | `discover()`, `build_probe()`, `parse_find_ack()` |
| Runtime control (TCP 51510) | §52 | `ControlClient`: `get_net_status`, `get_file_time`, `get_variables`, `macro_action`, `room_power_on/off`, `house_off` |
| Project catalog | §30 | `parse_mcs()`, `parse_mcs_info()`, `build_catalog()` |

Not implemented until the spec defines it: reading the project catalog from a running controller, state feedback (MQTT/UDP), relays, sensors, RS-232, and HDA zone state.

## CLI

```
pyurcmrx discover
pyurcmrx status 192.168.1.100
pyurcmrx variables 192.168.1.100
pyurcmrx macro 192.168.1.100 ROOM DEVICE MACRO [--flags N]
pyurcmrx catalog mcs.dat mcs_info.dat
```

## Development

```
mise install
uv sync
uv run pytest
```
