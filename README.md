# pyurcmrx

Async Python client for URC Total Control MRX controllers on the local network. It powers the Home Assistant integration [open-control-ha](https://github.com/Unstripped/open-control-ha) but has no Home Assistant dependency.

This is a clean-room implementation. Its only source is the public [open-control-spec](https://github.com/Unstripped/open-control-spec), which is dedicated to the public domain under [CC0 1.0](https://github.com/Unstripped/open-control-spec/blob/main/LICENSE); it contains no vendor code, keys, or assets. Not affiliated with Universal Remote Control, Inc.

Licensed under the [MIT License](https://github.com/Unstripped/pyurcmrx/blob/development/LICENSE).

## Implemented

Tracks [open-control-spec](https://github.com/Unstripped/open-control-spec) at commit `31927a4`.

| Area | Spec | API |
|---|---|---|
| Discovery (UDP 61510) | §50 | `discover()`: probes every local subnet broadcast address (or a given `target`) from an ephemeral port and collects the replies, de-duplicated by MAC; `build_probe()`, `parse_discovery_reply()` (text fields cut at the first NUL and right-trimmed), `subnet_broadcast_addresses()` |
| Project catalog (anonymous FTP, `core_model.json`) | §53 §3.4, §30 §1.1, §20 §4 | `fetch_core_model()` loads the single `core_model.json` named in the client model's model-wide manifest (§20 §4.3; `client_model` defaults to `E207`) and checks its size and checksum; `parse_manifest()`, `select_manifest()`, `core_model_record()`, `verify_staged_core_model()`. `parse_core_model()`: rooms with their room menu, devices (UI object id and connected-device id), and buttons whose steps are `ButtonTrigger(trigger_code)` or `RoomPowerOff(room_id)`; jump and unknown step types are skipped |
| Control (TCP 51510) | §52 | `ControlClient`: `macro_action(trigger_code)` (`MACRO_TRIGGER` `0x07DA`, `0x00` prefix byte + trigger code; `encode_trigger_code(object_id, button_id)`) reads every frame until `TRIGGER_DONE` `0x07DC`: `TRIGGER_ACK` `0x07DB`, then, for a code that resolves to an action, `ACTION_PROGRESS` `0x07DD` (start/end) and `LEVEL_REPORT` `0x07E1` frames. It returns the last `LevelReport(min_level, max_level, level, mute)` of the sequence, or `None` when none arrived; a volume button is a controller-held press whose step count and size belong to the device. `set_volume` (`SET_LEVEL_ABSOLUTE` `0x07ED`, `0x00` prefix byte, raw level with min/max): the trigger must resolve to a connected-device binding, no accepting trigger is known and the echoed reply confirms framing only, so use it only with a trigger confirmed against the device and otherwise step with `macro_action`. `room_power_on/off`, `house_off`, room/zone audio `set_room_volume`, `set_room_mute`, `set_zone_volume`, `set_zone_mute` (§3.11; none has been observed to succeed; reply returned unchecked), `get_variables` (`VARIABLE_TABLE_QUERY`), `get_net_status` (`NETWORK_STATUS_QUERY`), `get_file_time` (`CONTROLLER_TIME_QUERY`: controller local wall-clock time and time-zone rule). A generic negative `0xFFFE` or a status other than `0x01` raises `ActionRejectedError`. Each request uses a fresh connection, except after `get_file_time`, the only request the spec documents as leaving the connection reusable |
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

`macro` prints `acknowledged`, then `level L (MIN..MAX)` (and `muted`) when the controller sent a level report; the reply does not say whether the trigger code exists or the device carried the action out. `volume` prints `acknowledged`: its reply confirms framing only, not that the level was applied. The room/zone audio commands print the controller's first reply frame, or `no reply`, because none has been observed to succeed and the spec has not established what a successful reply looks like.

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
