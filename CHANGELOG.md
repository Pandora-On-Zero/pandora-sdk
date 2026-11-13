# Changelog

What changed in each release of pandora-sdk, newest first. Every change a program may
notice raises the version.

## 0.4.0 (2026-11-13)

First public release.

- **Python 3.10** or newer (was 3.11), so ROS 2 Humble can use it.
- **pandora-core-rs 0.2.1**, whose wheels install on Python 3.10.
- **Licence:** PolyForm Noncommercial 1.0.0 (`LICENSE.md`).
- **New error code `no_access`:** a serial port this user may not open (on Linux, not in
  `dialout`) was reported as `in_use`; it now has its own code and says which group to join.
- README: installing from the public tag, and a Linux section (receiver detection, `dialout`,
  the CH343 udev rule, where profiles live).
- `PROTOCOL.md`, the app protocol `connect()` speaks, is attached to each release.

## 0.3.1 (2026-11-12)

- pandora-core-rs 0.2.0: complete type stubs (`FirmwareUpdater.link`, `glove_device_id`),
  and a compiled-module layout that works in Nuitka-built programs.

## 0.3.0 (2026-11-12)

- Runs on **pandora-core-rs**, the Rust core's Python binding (0.1.0, prebuilt wheels),
  instead of the Python core. The SDK's own API is unchanged. numpy is no longer needed.

## 0.2.2 (2026-11-11)

- Core 0.7.1: each glove keeps its own skeleton cache, so two gloves no longer share one.

## 0.2.1 (2026-11-11)

- Core 0.7.0.

## 0.2.0 (2026-11-11)

- Commands for a glove, through the app and without it: haptics (`play_rom`, `play_rtp`,
  `stop`), Hall range, calibration bounds, the debug stream, and wall-clock time on frames.
- `pandora update-firmware`: update the receiver's or a glove's firmware with no app.
- Raw readings: a glove's pipeline runs only while frames or debug values are wanted;
  `subscribe("raw", cancelled=True)` adds the counts after earth-field cancellation; raw
  readings carry `battery_mv` and two power flags; a glove with no profile is read raw only.
- `ConnectionFailed` and `Disconnected` carry a stable `code` and their cause;
  `Source.ended`, `wait_ended()` and `end_error`.
- `open_link()` with timeouts, `list_receivers()`, `send_hall_config()`,
  `set_hall_range(resend=False)`, `Local.on_line()` and `Local.bad_frames`.
- `pandora watch --raw --cancelled`, and `--local --raw` with no profile.

## 0.1.0 (2026-11-10)

- `pandora_sdk.connect()`: hands from the 01 Glove app over its WebSocket protocol.
- `pandora_sdk.open_receiver()`: hands straight from the receiver, with no app.
- `pandora calibrate`: calibrate a glove with no app.
- The `pandora` command: `devices`, `watch`.
