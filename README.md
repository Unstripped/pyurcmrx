# pyurcmrx

Async Python client for URC Total Control MRX controllers on the local network. It powers the Home Assistant integration [open-control-ha](https://github.com/Unstripped/open-control-ha) but has no Home Assistant dependency.

This is a clean-room implementation. Its only source is the public [open-control-spec](https://github.com/Unstripped/open-control-spec), which is dedicated to the public domain under [CC0 1.0](https://github.com/Unstripped/open-control-spec/blob/main/LICENSE); it contains no vendor code, keys, or assets. Not affiliated with Universal Remote Control, Inc.

Licensed under the [MIT License](https://github.com/Unstripped/pyurcmrx/blob/development/LICENSE).

## Implemented

Tracks [open-control-spec](https://github.com/Unstripped/open-control-spec) at commit `ec98d25`.

| Area | Spec | API |
|---|---|---|
| Discovery (UDP 61510) | §50 | `discover()`: probes every local subnet broadcast address (or a given `target`) from an ephemeral port and collects the replies, de-duplicated by MAC; `build_probe()`, `parse_discovery_reply()` (text fields cut at the first NUL and right-trimmed, with the raw field bytes kept alongside as `description_raw` and `model_name_raw`), `subnet_broadcast_addresses()` |
| Project catalog (anonymous FTP, `core_model.json`) | §53 §3.4, §30 §1.1, §20 §4 | `fetch_core_model()` loads the single `core_model.json` named in the client model's model-wide manifest (§20 §4.3; `client_model` defaults to `E207`) and checks its size and checksum; `parse_manifest()`, `select_manifest()`, `core_model_record()`, `verify_staged_core_model()`. `parse_core_model()`: rooms with their room menu, devices (UI object id and connected-device id), and buttons whose steps are `ButtonTrigger(trigger_code)` or `RoomPowerOff(room_id)`; jump and unknown step types are skipped |
| Control (TCP 51510) | §52 | `ControlClient`. **Actions:** `macro_action(trigger_code)` (`MACRO_TRIGGER` `0x07DA`, `0x00` prefix byte + trigger code; `encode_trigger_code(object_id, button_id)`) reads every frame until `TRIGGER_DONE` `0x07DC` (`TRIGGER_ACK` `0x07DB`, then `ACTION_PROGRESS` `0x07DD` and `LEVEL_REPORT` `0x07E1` frames for a code that resolves to an action) and returns the last `LevelReport(min_level, max_level, level, mute)`, or `None`; a volume button is a controller-held press of up to 1 s whose send count and step size belong to the device. `set_volume` (`SET_LEVEL_ABSOLUTE` `0x07ED`): reaches only a device with a qualifying two-way script or `0x1100 + level` commands, never an amplifier zone, and its echo confirms framing only. `room_power_off` (`0x000D`), `house_off(timeout=)` (`0x000E`, reads `0x07DB`, `0x07DD`… until `0x000F`), `power_menu_query(room_id)` (`POWER_MENU_QUERY` `0x0012`, raw body; it switches nothing on and no room power-on opcode exists). **Room and amplifier-zone audio (§3.11):** `set_room_audio(room_id, RoomAudioControl, value)` (`0x00A2`: level, `MuteAction` mute, step up/down), `set_trigger_level` (`0x001B`), `set_room_mute` (`0x0006`, room ≤ 255), `set_room_mute_ext` (`0x007E`), `set_zone_mute(con_dev_id, zone_index 0–23, MuteAction)` (`0x0098`), `set_amp_zone_power([(hda_id, slot 0–15, on)])` (`0x0142`), `set_amp_zones_mute(mute, {hda_id: slots})` (`0x014A`), `set_amp_all_mute` (`0x0144`, not exercised live), `set_amp_zone_source(input_port, source_hda_id, [(hda_id, zone_id)])` (`0x0132`; an input with no bound source is silently ignored yet answered with success), `send_amp_json(hda_id, command)` (`0x012E`, `ALL_AMPLIFIERS`); all send the `0x00` prefix and are terminal, and success means the command was sent, not applied: read the zone state from MQTT telemetry. `set_room_volume` (`0x007D`) and `set_zone_volume` (`0x0097`) work only for connected devices of sub-type `0x0100`/`0x0103`, never amplifiers, and return their first reply unverified. **Queries:** `get_variables` (`VARIABLE_TABLE_QUERY`), `get_net_status` (`NETWORK_STATUS_QUERY`), `get_file_time` (`CONTROLLER_TIME_QUERY`: controller local wall-clock time and time-zone rule). **Outcome:** decided by response opcode per request; the status byte is per opcode (mute success is `0x0003` with status `0x02`) and never decides it. `0xFFFE`, or `0x012D` for the amplifier commands, raises `ActionRejectedError`. **Connections:** each request uses a fresh connection, except after `get_file_time`, the only request the spec documents as reusable. The controller registers one connection per client address and refuses the first request of any other (`01 03 00 FE FF 01`, not performed), so all clients for one `(host, port)` in an event loop share one connection: requests are serialized and a parked `get_file_time` connection is closed before another client connects. A rejection from another process is retried on new connections with backoff from 250 ms doubling to 2 s, giving up after 15 s with `AddressRejectedError` (`retry_delay`, `retry_max_delay`, `retry_window`), only for the eight opcodes where that frame can only be the rejection (§2.3); §3.11 commands are never retried |
| State telemetry (MQTT 1883) | §00 §5.2 | `subscribe()`, `parse_telemetry()`: events `RoomZoneStatus`, `HdaZoneStatus`, `HdaInputStatus`, `HdaAmpStatus`, `VariableStatus`, `LinkStatus`, and `OtherTelemetry` for unrecognised topics. For rooms backed by an amplifier zone, `hda/zone` is authoritative over the room's `zone_status`; `HdaAmpStatus.channel` is the 1-based amplifier channel and its `slot` property the matching `HdaZoneStatus` slot. No startup snapshot is guaranteed and `header.time_stamp` is local wall-clock time |

Relays, sensors and RS-232 ports cannot be driven directly from the LAN; they are reachable only through the dealer-programmed buttons and macros listed in the catalog.

## CLI

```
pyurcmrx discover [TARGET]              # default: every local subnet broadcast address
pyurcmrx catalog 192.168.1.100 [--client-model E207]   # or: pyurcmrx catalog --file core_model.json
pyurcmrx status 192.168.1.100
pyurcmrx variables 192.168.1.100
pyurcmrx macro 192.168.1.100 TRIGGER_CODE              # or: --object OBJECT --button BUTTON
pyurcmrx volume 192.168.1.100 TRIGGER_CODE LEVEL --min MIN --max MAX [--mute]
pyurcmrx trigger-level 192.168.1.100 TRIGGER_CODE LEVEL
pyurcmrx room-audio set 192.168.1.100 ROOM LEVEL
pyurcmrx room-audio mute 192.168.1.100 ROOM on|off|toggle
pyurcmrx room-audio step 192.168.1.100 ROOM up|down
pyurcmrx room-mute 192.168.1.100 ROOM on|off|toggle [--ext]
pyurcmrx zone-mute 192.168.1.100 HDA ZONE_INDEX on|off|toggle
pyurcmrx zones-mute 192.168.1.100 HDA on|off SLOT [SLOT ...]
pyurcmrx all-mute 192.168.1.100 on|off|toggle
pyurcmrx zone-power 192.168.1.100 HDA SLOT on|off
pyurcmrx zone-source 192.168.1.100 INPUT_PORT SOURCE_HDA HDA ZONE_ID
pyurcmrx amp-json 192.168.1.100 HDA|all '{"type":"set","zone":{"id":"z3lr","ctrl":{"volume":"40"}}}'
pyurcmrx power-menu 192.168.1.100 ROOM
pyurcmrx room-volume 192.168.1.100 ROOM VALUE         # 0x007D: sub-type 0x0100/0x0103 devices only
pyurcmrx zone-volume 192.168.1.100 CON_DEV ZONE VALUE # 0x0097: sub-type 0x0100/0x0103 devices only
pyurcmrx watch 192.168.1.100
```

`macro` prints `acknowledged`, then `level L (MIN..MAX)` (and `muted`) when the controller sent a level report; the reply does not say whether the trigger code exists or the device carried the action out. `volume` prints `acknowledged`: its reply confirms framing only, not that the level was applied. The amplifier-zone commands print `accepted` when the controller reports that it sent the command; `watch` shows whether the zone changed. `power-menu` prints the raw reply body. `room-volume` and `zone-volume` print the controller's first reply frame, or `no reply`, because their success reply is not established.

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
