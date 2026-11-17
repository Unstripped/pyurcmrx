# pyurcmrx

Async Python client for URC Total Control MRX controllers on the local network. It powers the Home Assistant integration [open-control-ha](https://github.com/Unstripped/open-control-ha) but has no Home Assistant dependency.

This is a clean-room implementation. Its only source is the public [open-control-spec](https://github.com/Unstripped/open-control-spec); it contains no vendor code, keys, or assets. Not affiliated with Universal Remote Control, Inc.

Licensed under the [MIT License](https://github.com/Unstripped/pyurcmrx/blob/development/LICENSE).

## Implemented

Tracks [open-control-spec](https://github.com/Unstripped/open-control-spec) at commit `20acfa8`.

| Area | Spec | API |
|---|---|---|
| Discovery (UDP 61510) | §50 | `discover()`: probes every local subnet broadcast address (or a given `target`) from an ephemeral port and collects the replies, de-duplicated by MAC; `build_probe()`, `parse_find_ack()` (text fields cut at the first NUL and right-trimmed), `subnet_broadcast_addresses()` |
| Project catalog (anonymous FTP, `core_model.json`) | §53 §3.4, §30 §1.1, §20 §4 | `fetch_core_model()` loads the single `core_model.json` named in the client model's model-wide manifest (§20 §4.3; `client_model` defaults to `E207`) and checks its size and checksum; `parse_manifest()`, `select_manifest()`, `core_model_record()`, `verify_staged_core_model()`. `parse_core_model()`: rooms with their room menu, devices (UI object id and connected-device id), and buttons whose steps are `ButtonTrigger(trigger_code)` or `RoomPowerOff(room_id)`; jump and unknown step types are skipped |
| Control (TCP 51510) | §52 | `ControlClient`: `macro_action(trigger_code)` (`0x07DA`, 4-byte trigger code; `encode_trigger_code(object_id, button_id)`), `set_volume` (`0x07ED`, raw level with the control's min/max), `room_power_on/off`, `house_off`, room/zone audio `set_room_volume`, `set_room_mute`, `set_zone_volume`, `set_zone_mute` (§3.11; reply returned unchecked), `get_variables`, `get_net_status`, `get_file_time` (controller local wall-clock time and time-zone rule). A generic negative `0xFFFE` or a status other than `0x01` raises `ActionRejectedError`. Each request uses a fresh connection, except after `get_file_time`, the only request the spec documents as leaving the connection reusable |
| State telemetry (MQTT 1883) | §00 §5.2 | `subscribe()`, `parse_telemetry()`: events `RoomZoneStatus`, `HdaZoneStatus`, `HdaInputStatus`, `HdaAmpStatus`, `VariableStatus`, `LinkStatus`, and `OtherTelemetry` for unrecognised topics. No startup snapshot is guaranteed and `header.time_stamp` is local wall-clock time |

Relays, sensors and RS-232 ports cannot be driven directly from the LAN; they are reachable only through the dealer-programmed buttons and macros listed in the catalog.

## CLI

```
pyurcmrx discover [TARGET]              # default: every local subnet broadcast address
pyurcmrx catalog 192.168.1.100 [--client-model E207]   # or: pyurcmrx catalog --file core_model.json
pyurcmrx status 192.168.1.100
pyurcmrx variables 192.168.1.100
pyurcmrx macro 192.168.1.100 TRIGGER_CODE              # or: --object OBJECT --button BUTTON
pyurcmrx volume 192.168.1.100 TRIGGER_CODE LEVEL --min MIN --max MAX [--mute]
pyurcmrx room-volume 192.168.1.100 ROOM VALUE
pyurcmrx room-mute 192.168.1.100 ROOM on|off
pyurcmrx zone-volume 192.168.1.100 CON_DEV ZONE VALUE
pyurcmrx zone-mute 192.168.1.100 CON_DEV ZONE on|off
pyurcmrx watch 192.168.1.100
```

`macro` and `volume` print `acknowledged`: the controller's reply does not say whether the trigger code exists or the level was applied. The room/zone audio commands print the controller's first reply frame, or `no reply`, because the spec has not established what a successful reply looks like.

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
