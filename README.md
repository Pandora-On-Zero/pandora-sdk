# pandora-sdk

Hands from the 01 Glove app in a few lines of Python. The app does the work
(receiver, calibration, the hand model); this library connects to it over its
wire protocol and hands you each glove as joint angles, curls, a wrist
orientation and, if you want it, the full skeleton.

```python
import pandora_sdk

with pandora_sdk.connect() as gloves:
    gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
    for hand in gloves.frames():
        print(hand.angles_deg[1])    # index: MCP, PIP, DIP flexion, MCP abduction
```

**Status:** version 0.1, private. It reads hands, raw readings and the
pipeline's debug values, through a running app or straight from the receiver
with no app. It sends haptics and a Hall range to a glove, gives each glove's
calibration bounds, calibrates a glove with no app (`pandora calibrate`) and
updates firmware (`pandora update-firmware`). None of the sending, the
calibrating or the firmware update has been tried on a real glove yet: they
have met the app on simulated gloves, recorded packets and made-up receivers;
see [Not here yet](#not-here-yet).

## Installing

`pandora-sdk` uses `pandora-core` for the message format, and `pandora-core`
lives inside the private `Pandora-On-Zero/Pandora` repository. So installing
needs git, and a GitHub login that can read that repository.

```bash
pip install git+https://github.com/Pandora-On-Zero/pandora-sdk.git
```

On Windows, run this once first. The Pandora repository holds a few files
whose paths are longer than Windows allows by default, and without it pip's
download of that repository fails with "Filename too long":

```bash
git config --global core.longpaths true
```

Python 3.11 or newer. It brings in `websockets`, `msgpack`, `numpy` and `pyserial`.

To move an existing install to a newer `pandora-sdk`, add `--force-reinstall`:

```bash
pip install --force-reinstall git+https://github.com/Pandora-On-Zero/pandora-sdk.git
```

Without it pip may keep the `pandora-core` it already has, even with
`--upgrade`: pip judges an installed package by its version number, not by the
commit it came from, and keeps one whose version is the one asked for. That is
why a move of the pin normally raises the core's version (see
[Working on it](#working-on-it)); `--force-reinstall` makes sure either way.
This version needs `pandora-core` 0.3.0 (commit `661b64c`), which added
[the counts after earth-field cancellation](#counts-after-earth-field-cancellation)
to raw readings. An install that has 0.2.0 moves to it with the
`--force-reinstall` above.

## Before you connect

The app must be running with its protocol server on: **Settings ▸ General ▸
Advanced ▸ Protocol**. For a headless run (`main.exe --headless`), put this in
the app's `config.json` instead:

```json
"adapters": {"protocol": {"enabled": true}}
```

It listens on `127.0.0.1:21120`, this PC only.

## Using it

### The gloves

```python
for glove in gloves.devices():
    print(glove.id, glove.hand, glove.name, glove.connected)
```

A glove is known by its `id`, which never changes. Its hand is something about
it; two gloves can be worn on the same hand.

### Hands

`subscribe()` asks for a stream and returns what the app granted.

| Argument | Default | Meaning |
| --- | --- | --- |
| `stream` | `"frames"` | `"frames"` for finished hands, `"raw"` for what the glove sent, `"debug"` for values from inside the pipeline |
| `devices` | every glove | Glove ids to receive |
| `hands` | both | `["left"]`, `["right"]` or both |
| `rate_hz` | `0` | Messages a second per glove at most. `0` is every one, about 200 a second |
| `bones` | `True` | Whether each frame carries the 31-bone skeleton, which is most of its size |
| `keys` | `["*"]` | Debug only: which values, as name patterns (`*` any run of characters, `?` one) |
| `cancelled` | `False` | Raw only: each reading also carries the Hall counts after earth-field cancellation ([below](#counts-after-earth-field-cancellation)) |

There are two ways to take what arrives, and a third for a program that must
not wait on a queue ([a callback](#a-callback-per-message)).

**Every frame, in order**, for logging or anything that must not skip:

```python
for hand in gloves.frames():
    ...
```

**The hand as it is now**, for a control loop that runs at its own pace:

```python
while running:
    hand = gloves.latest("right")        # or a glove id; None before the first frame
    if hand is not None:
        robot.move(hand.angles_deg)
    time.sleep(0.01)
```

What a frame holds, fingers always in the order thumb, index, middle, ring,
pinky:

| Field | Meaning |
| --- | --- |
| `angles_deg` | Per finger, four joint angles in degrees from the open hand: three flexions from the knuckle outward, then the sideways angle at the base. What a robot hand is driven with |
| `curl` | Per finger, 0 straight to 1 curled |
| `splay` | Per finger, −1 to +1 sideways |
| `wrist_deg` | Pitch, roll, yaw in degrees, or `None` |
| `trigger`, `grip` | Gestures the app detected |
| `bones` | 31 × (x, y, z, qw, qx, qy, qz), or `None` when you asked for `bones=False` |
| `device`, `hand`, `seq`, `time_us` | Which glove, a per-glove counter, and the app's clock in microseconds |
| `wall_time_us` | The same instant on the wall clock, microseconds since 1970 (UTC), for lining frames up with another recording; `None` from an app older than this field. Measure intervals with `time_us`: the wall clock can be set back |

The exact meaning of every field is the protocol's specification,
`pandora-core/PROTOCOL.md` in the Pandora repository.

### Raw readings

For deriving your own signal with nobody's calibration applied:

```python
gloves.subscribe("raw", devices=["NRF_GLOVE_E5D1"])
for reading in gloves.raw():
    print(reading.hall[1][0])            # index finger, proximal sensor: (x, y, z) counts
```

A raw reading has the same `device`, `hand`, `seq`, `time_us` and
`wall_time_us` as a frame, and `hall_range`, the range its counts are in.

#### Counts after earth-field cancellation

With earth-field cancellation on for a glove, its pipeline subtracts the
earth's field, read by a reference sensor, from each Hall reading before it
normalises the curl. The [calibration bounds](#calibration-bounds) are in
those cancelled counts; `hall`, what the glove sent, is from before. A program
that maps counts between the bounds itself asks for the cancelled ones:

```python
glove = "NRF_GLOVE_E5D1"
gloves.subscribe("raw", devices=[glove], cancelled=True)
index = gloves.calibration(glove).calibrations["Index"]
for reading in gloves.raw():
    counts = reading.hall_cancelled[1][0]    # index, proximal: the space of index["prox_min"], index["prox_max"]
```

| Field | Meaning |
| --- | --- |
| `hall_cancelled` | Shaped as `hall`: per finger, the proximal and metacarpal counts as the pipeline normalised them, after gap fill and earth-field cancellation (and cancellation's output smoothing, when that is set) |
| `thumb_distal_cancelled` | The same for the thumb's third sensor, or `None` when the glove has none |
| `earth_cancelled` | Whether the earth's field was subtracted from any sensor of this packet |

- When nothing was cancelled (cancellation off for the glove, no hard/soft-iron
  fit, or nothing usable from the reference), `earth_cancelled` is `False` and
  the cancelled counts are equal to `hall` and `thumb_distal`.
- `hall` is the glove's own counts either way. The reference sensor has no
  cancelled counts: `reference` is the same in both spaces.
- Without `cancelled=True` the three fields are `None`.
- An app from before this option (no `"raw_cancelled"` in
  `gloves.server.capabilities`) grants the subscription with `cancelled` off
  and sends none; `subscribe()` returns what it granted.
- Without the app, a raw subscription with `cancelled=True` runs the pipeline
  of each glove it includes, as frames do (see [Raw only](#raw-only)).

### A callback per message

`on(stream, callback)` hands each message of a stream to your function as it
arrives, on the thread that reads the app's socket or the receiver, instead of
queueing it. For a program where every millisecond counts, such as one that
turns finger taps into key presses:

```python
import pandora_sdk

PRESSED = 1500                           # your own threshold, in Hall counts

def on_reading(reading):                 # runs on the reading thread: keep it short
    if abs(reading.hall[1][0][2]) > PRESSED:     # index finger, proximal sensor, z
        tap("z")                         # yours: press the key

with pandora_sdk.open_receiver() as gloves:
    gloves.on("raw", on_reading)
    gloves.subscribe("raw", hands=["right"])
    input("Tapping. Enter to stop.\n")
```

- Nothing else arrives while your function runs, so do the least you can
  there and hand anything slow to another thread.
- `latest()` still has the stream's messages; `read()` and the iterators get
  none of them. `gloves.on("raw", None)` goes back to queueing.
- Your function can call the source: send haptics, ask for `calibration()`.
- If it raises, the source ends: the error is logged, and the next read raises
  `Disconnected` saying what your function raised.
- It works the same through the app (`connect()`) and without it.

With `open_receiver()` and only `raw` subscribed, as here, no hand is made at
all: see [Raw only](#raw-only).

### Calibration bounds

The Hall counts each glove's curl is normalised between, for a program that
maps raw counts itself:

```python
bounds = gloves.calibration("NRF_GLOVE_E5D1")
print(bounds.hall_range, bounds.extrapolated)
print(bounds.calibrations["Index"]["prox_min"])     # (x, y, z) with the fist closed
```

`calibration()` with no glove named gives every glove's, by id. The app sends
a glove's bounds again whenever they change (a calibration, a profile loaded,
a new Hall range); `gloves.latest_calibration(glove_id)` is the newest it sent,
without asking. `extrapolated` means the bounds were captured at another Hall
range and rescaled to this one.

### Debug values

The pipeline's own named values per glove, for finding out why a hand looks
the way it does:

```python
gloves.subscribe("debug", keys=["curl.Index.*", "raw.link.rssi_dbm"], rate_hz=10)
for values in gloves.debug():
    print(values.device, values.values)              # {"curl.Index.prox": 0.42, ...}
```

The names, their units and their meaning are the pipeline's and change with
it; they are not held to the protocol's version. `read("debug")` and
`latest(glove, "debug")` work as for frames.

### Sending to a glove

```python
glove = "NRF_GLOVE_E5D1"
gloves.play_rom(glove, [0, 47, 47, 0, 0, 0])                  # a library effect on two motors
gloves.play_rtp(glove, [0, 0, 0, 0, 0, 90], [150] * 6)        # the back of the hand, 150 ms
gloves.stop(glove)                                            # every motor, at once
note = gloves.set_hall_range(glove, 1)                        # 0 is ±5 mT, 1 is ±50 mT
```

Motors are in the order thumb, index, middle, ring, pinky, back of the hand.

| Call | Does |
| --- | --- |
| `play_rom(glove, effects)` | Per motor, an effect of the motor driver's library (DRV2625, Immersion Library A), 1 to 123; 0 leaves that motor alone |
| `play_rtp(glove, amplitudes, durations_ms)` | Per motor, 1 to 127 drives, 128 to 255 brakes, 0 stops; each held for its duration, 0 to 65535 ms (0 is the firmware's default). To stream, send each frame with a duration longer than the gap to the next |
| `stop(glove)` | An all-zero RTP frame: every motor stops now |
| `set_hall_range(glove, range)` | Every Hall sensor on that range; the glove's calibration is rescaled to it and raw readings say it from then on. Returns what that did to the calibration, in words |

Each returns once the command went to the receiver; the glove does not answer.
A command the app could not pass on raises `ServerError` with the protocol's
code: `unknown_device` (the glove is not connected), `bad_haptics` or
`bad_hall_range` (a value out of range), `send_failed` (no receiver linked, a
firmware update holds it, or the write failed), `throttled` (the app was set to
limit how often a glove takes haptics; it does not by default). Through the
app, its own haptic settings still apply: which motors may run and the
wearer's strength. A Hall range set this way does not change the app's own
setting, which goes back to the glove when it next connects.

### Without the app

`open_receiver()` reads the receiver itself and runs each glove's pipeline in
your process. Everything after that line is the same as with `connect()`:
`subscribe`, `frames`, `raw`, `debug`, `read`, `latest`, `on`, `devices`,
`calibration`, `latest_calibration`, the commands for a glove, and the same
frames.

```python
with pandora_sdk.open_receiver() as gloves:
    gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
    for hand in gloves.frames():
        print(hand.angles_deg[1])
```

Only one program can hold the receiver. If the app is running, it has it: use
`connect()`, or close the app.

Each glove needs a **profile**: its hand and its calibration.
[`pandora calibrate`](#calibrating-a-glove) writes one. A glove that sends but
has no profile is left out, and `gloves.unprofiled` holds its id.

Profiles are files, one `<glove id>.json` per glove, in the folder
`.pandora/profiles` in your home folder (`pandora_sdk.default_profiles_dir()`).
`open_receiver()` reads that folder; `open_receiver("some/folder")` reads
another, and a dict of glove id to profile works too. A profile is the JSON
form of `pandora_core.glove.Profile`, the same thing a hand recording stores
per glove. A profile belongs to a glove and the hand wearing it: for another
wearer, calibrate again, or into another folder (`--out`).

`open_receiver()` sends a glove nothing unless asked, so the glove's Hall
sensors are on whatever range they were last put on. `pandora calibrate` puts
them on the range they power up in and calibrates there, so a profile it wrote
is right for a glove that has been switched on and used with nothing but this
library. If the app set another range (Debug ▸ Hall Effect Firmware Settings)
since the glove was switched on, switch the glove off and on before
`open_receiver()`, or put it on its profile's range with `set_hall_range`.

What is different without the app:

| | Through the app | Without the app |
| --- | --- | --- |
| Haptics | The app's haptic settings apply (which motors, the wearer's strength) | Sent exactly as given |
| `set_hall_range` | The app's other Hall settings and per-sensor tuning go with it; returns the app's note | Sent with the settings the sensors power up with, to every sensor (a sensor the profile had on its own range is on the new one too); returns `""`. Its pipeline takes the range from the glove's next packet, and the range is sent again when the glove comes back after two seconds of silence (it may have been switched off and on). The profile file is not changed |
| `calibration()` | The app's bounds | The bounds in the glove's profile, as its pipeline uses them; only gloves heard so far |
| `latest_calibration()` | The newest the app sent | Read when asked, so never behind |
| Debug values | Every stage the app runs | Every stage the glove's pipeline runs |
| A command for a glove | Refused unless it is connected | Refused unless it has a profile and sent in the last two seconds |
| Raw `hall` with gap fill on | A sensor that read all zeros carries the reading gap fill gave it | The glove's own zeros. Its cancelled counts are the filled reading, cancelled, as through the app |


One thread runs every glove's pipeline. On the PC this was written on, a
packet takes about a millisecond with both streams and the skeleton on, and a
glove sends 200 a second.

#### Raw only

A glove's pipeline runs only while a `frames` or `debug` subscription includes
it, or a `raw` one with `cancelled=True`, whose counts exist only once the
pipeline has had the packet. With none of these (only plain `raw`, or nothing
yet), each packet makes the raw message and nothing else: about 30
microseconds a packet on the PC this was written on, against 0.6 to 1.9
milliseconds with the pipeline. A frames subscription narrowed to one hand or
one glove leaves the others raw only.

- `calibration()`, `latest_calibration()` and `set_hall_range` work as
  ever: a glove's first packet, and its first after a range change, still go
  through a pipeline, which loads its profile and its range.
- **Subscribing to frames (or to raw with `cancelled`) later starts that
  glove's pipeline afresh at the next packet**, as when the glove is switched
  on: its smoothing and other filters, earth-field cancellation's among them,
  begin there and carry nothing from before. Starting costs a
  millisecond or two, once, on that packet.
- Through the app nothing changes: the app makes hands either way.


### When things go wrong

| Raised | When |
| --- | --- |
| `pandora_sdk.ConnectionFailed` | Nothing answered: the app is not running, or Protocol is off. Without the app: no receiver, or something else holds it |
| `pandora_sdk.ProtocolError` | The app speaks another protocol version (`.code` is `unsupported_protocol`) |
| `pandora_sdk.ServerError` | The app refused a request, or a command could not be sent; `.code` is the protocol's error code. Without the app the same codes are raised for the same reasons |
| `pandora_sdk.Disconnected` | The app went away, or the receiver was unplugged, while you were reading |
| `TimeoutError` | A `read(timeout=...)` or a request ran out of time |

If your loop reads more slowly than frames arrive, the oldest unread ones are
discarded (256 are kept per stream; `connect(buffer=...)` changes that) and
`gloves.dropped` counts them. `latest()` is never behind.

## The `pandora` command

Installed with the package, for looking at hands, calibrating a glove and
updating firmware without writing code. `pandora <command> --help` lists every option.

Through the running app:

```bash
pandora devices
```

```bash
pandora watch --hand right
```

```bash
pandora watch --raw --rate 0 --count 100
```

Straight from the receiver, with the app closed and the profiles
`pandora calibrate` wrote:

```bash
pandora watch --local
```

`--profiles DIR` reads another folder, and `--receiver COM7` names the
receiver's port when more than one is plugged in. A glove that is sending but
has no profile is named on the error stream, with the command that makes one.

### Calibrating a glove

Close the app, put the glove on, switch it on, and run:

```bash
pandora calibrate --hand right
```

It picks the one glove that is sending, prints each step (the pose to hold and
a countdown), and writes `<glove id>.json` into the profiles folder. A full run
is hand open, then a fist, three seconds each. A glove with finger IMUs adds
fingers together between the two and ends on circling each finger in turn,
about half a minute in all. The steps and what each captures are
`pandora-core`'s, the same ones the app's Calibration tab runs. Their text was
written for that tab, so it mentions "the hands shown"; there are none here.

| Option | Default | Meaning |
| --- | --- | --- |
| `--hand left\|right` | from the glove's profile | The hand the glove is worn on. Needed the first time |
| `--device ID` | the only glove sending | Which glove, when more than one is sending |
| `--run full\|splay_zero\|curl_axis` | `full` | What to calibrate. The last two are for a glove with finger IMUs and redo one part |
| `--hold SECONDS` | `3` | How long each still pose is held |
| `--ready SECONDS` | `3` | Time before the first step |
| `--out DIR` | `.pandora/profiles` in your home folder | Where the profile is written |
| `--name TEXT` | kept | A label for the glove, shown by `devices()` |
| `--receiver PORT` | the only receiver | The receiver's serial port |
| `--no-haptics` | buzz | No buzz when a step starts |

What it does besides reading:

- **It resets the glove's Hall sensors to their power-up settings** before the
  first step, and writes that range into the profile. The glove does not
  answer, so this is what it was told, not something the command could check.
- **It buzzes the back-of-hand motor** when each step starts and when the run
  is over, so you can keep your eyes on your hand. The finger motors stay off.
- **Calibrating again starts from the glove's file**: it keeps the hand, the
  name and anything the run does not measure, and replaces what it does.

The file is written only at the end of a run that stored something. If the
glove stops sending part way, or you press Ctrl+C, the old file is untouched.
A run that ends "incomplete" (a step failed, such as a finger pointing up
during the fingers-together step) still writes what the other steps captured,
lists each failure, and exits with status 1.

It does not calibrate earth-field cancellation; that is off unless a profile
turns it on, and the app's Hall Reference page is where it is calibrated.

### Updating firmware

Close the app, and run:

```bash
pandora update-firmware glove dfu_application.zip
```

```bash
pandora update-firmware receiver dfu_application.zip
```

It checks the image (a DFU zip, a signed `.bin`, or a build folder holding
`dfu_application.zip`; a zip built for the other device is refused), opens the
receiver, picks the one glove that is sending, prints the version running now,
and asks before writing anything. It then prints the update as it goes and
waits until the new image has booted and been confirmed: a few minutes for a
glove. Do not unplug the receiver or switch the glove off meanwhile. Ctrl+C
tells the device to abort the update.

| Option | Default | Meaning |
| --- | --- | --- |
| `receiver\|dongle\|glove` | | What to update. `dongle` is the receiver, as the app calls it |
| `--device ID` | the only glove sending | Which glove |
| `--receiver PORT` | the only receiver | The receiver's serial port |
| `--yes`, `-y` | ask | Flash without asking |
| `--verbose` | quiet | Also print every line the receiver sends |

It exits with 0 once the new image is confirmed, 1 when the update failed or
nothing was flashed, and 130 after Ctrl+C. While the app holds the receiver it
refuses to start, and says to close the app.

The update itself is `pandora-core`'s (`firmware_update.py`), the one the
app's Firmware tab and its `tools/ota_flash.py flash` run, and this takes
the same arguments as that tool, with the port as `--receiver`.

## Working on it

```bash
pip install -e ".[dev]"
```

```bash
python -m pytest
```

The tests run the client against a stand-in server, `tests/standin.py`, that
was typed out from the protocol's specification and imports nothing of
`pandora-core`. The client decodes with `pandora-core`, so a mistake in that
package's message code shows up here as a failing test and is not shared by
both ends. Reading the receiver, sending to a glove and calibrating are
tested against a made-up serial port, `tests/fakelink.py`, fed packets built
from the tracking packet's layout; the firmware update against a made-up
receiver that answers the DFU protocol (`tests/test_firmware.py`). The counts
after earth-field cancellation are also tested on 40 packets recorded from a
glove with cancellation on, copied from `pandora-core`'s golden vectors
(`tests/data/right_earth.json`). No test
can open a real port (`tests/conftest.py` refuses it), and every test gets a
temporary home folder, so none reads or writes your `.pandora/profiles`.

`pyproject.toml` pins `pandora-core` to one commit of the Pandora repository.
Move the pin when the core gains something this library should use. **The
commit you pin must carry a higher `pandora-core` version** (its
`pandora-core/pyproject.toml`) than the one pinned before: pip keeps an
installed core whose version matches, whatever commit it came from, so a pin
to a new commit with the same version leaves people on the old core. The pin
is at 0.3.0 now (`661b64c`). Then install again with
`pip install --force-reinstall -e ".[dev]"`, or, to replace only the core:

```bash
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.longpaths GIT_CONFIG_VALUE_0=true \
  pip install --force-reinstall --no-deps "pandora-core[serial] @ git+https://github.com/Pandora-On-Zero/Pandora.git@661b64c0aa914ecad881fe7c92e7ca0d4b1f6620#subdirectory=pandora-core"
```

## Not here yet

- Any of it tried on a real glove: the haptics and Hall range commands,
  `pandora calibrate` and `pandora update-firmware` have met simulated gloves,
  recorded packets and made-up receivers only.
- The glove's other settings (haptic drive, power-off time) and running a
  calibration through the app: the protocol has no messages for them yet.
- `open_receiver()` putting a glove on its profile's Hall range by itself.
  Until it does, see the note on ranges under [Without the app](#without-the-app);
  `set_hall_range` does it when asked.
- Reading which hand a glove is from the glove itself. `pandora calibrate`
  is told, with `--hand`.
- Reading the firmware version without flashing. The app's
  `tools/ota_flash.py version` does it.
- An `asyncio` client. This one is blocking, with one background thread.
- A licence, and a release on PyPI. Both wait for the repository going public,
  which also needs `pandora-core` to be installable by people outside the
  organisation.
