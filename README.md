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

**Status:** version 0.1, private. It reads hands and raw readings, through a
running app or straight from the receiver with no app, and it calibrates a
glove with no app (`pandora calibrate`). Calibrating has been run on recorded
packets only, not yet on a real glove. It has no way yet for your program to
send haptics or settings to a glove; see [Not here yet](#not-here-yet).

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

Without it pip keeps the `pandora-core` it already has, even with `--upgrade`:
every commit of `pandora-core` carries the same version number, so pip takes
the old one for the one now asked for. The new `pandora-sdk` then fails on the
first thing the old core lacks.

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
| `stream` | `"frames"` | `"frames"` for finished hands, `"raw"` for what the glove sent |
| `devices` | every glove | Glove ids to receive |
| `hands` | both | `["left"]`, `["right"]` or both |
| `rate_hz` | `0` | Messages a second per glove at most. `0` is every one, about 200 a second |
| `bones` | `True` | Whether each frame carries the 31-bone skeleton, which is most of its size |

There are two ways to take what arrives.

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

The exact meaning of every field is the protocol's specification,
`pandora-core/PROTOCOL.md` in the Pandora repository.

### Raw readings

For deriving your own signal with nobody's calibration applied:

```python
gloves.subscribe("raw", devices=["NRF_GLOVE_E5D1"])
for reading in gloves.raw():
    print(reading.hall[1][0])            # index finger, proximal sensor: (x, y, z) counts
```

### Without the app

`open_receiver()` reads the receiver itself and runs each glove's pipeline in
your process. Everything after that line is the same as with `connect()`:
`subscribe`, `frames`, `raw`, `read`, `latest`, `devices`, and the same frames.

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

`open_receiver()` sends a glove nothing, so the glove's Hall sensors are on
whatever range they were last put on. `pandora calibrate` puts them on the
range they power up in and calibrates there, so a profile it wrote is right
for a glove that has been switched on and used with nothing but this library.
If the app set another range (Debug ▸ Hall Effect Firmware Settings) since the
glove was switched on, switch the glove off and on before `open_receiver()`.

One thread runs every glove's pipeline. On the PC this was written on, a
packet takes about a millisecond with both streams and the skeleton on, and a
glove sends 200 a second.

### When things go wrong

| Raised | When |
| --- | --- |
| `pandora_sdk.ConnectionFailed` | Nothing answered: the app is not running, or Protocol is off. Without the app: no receiver, or something else holds it |
| `pandora_sdk.ProtocolError` | The app speaks another protocol version (`.code` is `unsupported_protocol`) |
| `pandora_sdk.ServerError` | The app refused a request; `.code` is the protocol's error code |
| `pandora_sdk.Disconnected` | The app went away, or the receiver was unplugged, while you were reading |
| `TimeoutError` | A `read(timeout=...)` or a request ran out of time |

If your loop reads more slowly than frames arrive, the oldest unread ones are
discarded (256 are kept per stream; `connect(buffer=...)` changes that) and
`gloves.dropped` counts them. `latest()` is never behind.

## The `pandora` command

Installed with the package, for looking at hands and calibrating a glove
without writing code. `pandora <command> --help` lists every option.

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
both ends. Reading the receiver and calibrating are tested against a made-up
serial port, `tests/fakelink.py`, fed packets built from the tracking packet's
layout. No test opens a real port, and every test gets a temporary home
folder, so none reads or writes your `.pandora/profiles`.

`pyproject.toml` pins `pandora-core` to one commit of the Pandora repository.
Move the pin when the core gains something this library should use, then
install again with `pip install --force-reinstall -e ".[dev]"`, for the reason
given under [Installing](#installing).

## Not here yet

- Sending to a glove from your program: haptics, Hall range and other
  settings. The protocol has no messages for them yet, and `open_receiver()`
  has no call for them. `pandora calibrate` sends its own two commands (the
  Hall reset and the buzz) and nothing else does.
- `open_receiver()` putting a glove on its profile's Hall range. Until it
  does, see the note on ranges under [Without the app](#without-the-app).
- Reading which hand a glove is from the glove itself. `pandora calibrate`
  is told, with `--hand`.
- `pandora calibrate` tried on a real glove. It has only met recorded packets.
- `pandora update-firmware`.
- An `asyncio` client. This one is blocking, with one background thread.
- A licence, and a release on PyPI. Both wait for the repository going public,
  which also needs `pandora-core` to be installable by people outside the
  organisation.
