# pyurcmrx

Async Python client for URC Total Control MRX controllers on the local network. It powers the Home Assistant integration [open-control-ha](https://github.com/Unstripped/open-control-ha) but has no Home Assistant dependency.

This is a clean-room implementation. Its only source is the public [open-control-spec](https://github.com/Unstripped/open-control-spec); it contains no vendor code, keys, or assets. Not affiliated with Universal Remote Control, Inc.

Licensed under the [MIT License](https://github.com/Unstripped/pyurcmrx/blob/development/LICENSE).

## Implemented

| Area | Spec | API |
|---|---|---|
| Discovery (UDP 61510) | §50 | `discover()`: probes every local subnet broadcast address (or a given `target`) from an ephemeral port and collects the replies, de-duplicated by MAC; `build_probe()`, `parse_find_ack()`, `subnet_broadcast_addresses()` |
| Project catalog (anonymous FTP, `core_model.json`) | §53 §3.4, §30 §1.1, §20 §3 | `fetch_core_model()`, `parse_core_model()`: rooms, devices, buttons with decoded `(device_id, func_id)` steps |
| Control (TCP 51510) | §52 | `ControlClient`: `macro_action` (with press/hold/release flags), `set_volume` (`0x07ED`), `room_power_on/off`, `house_off`, `get_variables`, `get_net_status`, `get_file_time` (controller local time and time-zone rule). Each request uses a fresh connection, except after `get_file_time`, the only request the spec documents as leaving the connection reusable |
| State telemetry (MQTT 1883) | §00 §5.2 | `subscribe()`, `parse_telemetry()`: room zone status, HDA zone status, variables, room links |

Relays, sensors and RS-232 ports cannot be driven directly from the LAN; they are reachable only through the dealer-programmed buttons and macros listed in the catalog.

## CLI

```
pyurcmrx discover [TARGET]              # default: every local subnet broadcast address
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

The MQTT subscriber test runs against a local `mosquitto` and is skipped when it is not installed; set `PYURCMRX_REQUIRE_MOSQUITTO=1` (as CI does) to fail instead of skip.

## Releasing

1. Bump `version` in `pyproject.toml` and merge to `development`.
2. Tag `vX.Y.Z` on `development` and push the tag. `release.yml` runs the tests, checks the tag matches `pyproject.toml`, builds, publishes to PyPI and creates the GitHub release.

One-time setup: a PyPI trusted publisher (project `pyurcmrx`, owner `Unstripped`, repo `pyurcmrx`, workflow `release.yml`, environment `pypi`) and a GitHub environment named `pypi`.
