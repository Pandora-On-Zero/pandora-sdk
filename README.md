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
running app or straight from the receiver with no app. It cannot yet send
anything to a glove (haptics, settings) or calibrate one; see
[Not here yet](#not-here-yet).

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
with pandora_sdk.open_receiver("profiles/") as gloves:
    gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
    for hand in gloves.frames():
        print(hand.angles_deg[1])
```

Only one program can hold the receiver. If the app is running, it has it: use
`connect()`, or close the app.

Each glove needs a **profile**: its hand and its calibration. `profiles` is a
folder with one `<glove id>.json` per glove, or a dict of glove id to profile.
A profile is the JSON form of `pandora_core.glove.Profile`, the same thing a
hand recording stores per glove. A glove that sends but has no profile is left
out, and `gloves.unprofiled` holds its id.

Three limits today, each of them a piece of work still to come:

- **Nothing here writes a profile yet.** `pandora calibrate` will.
- **Nothing is sent to a glove**, so its Hall range is whatever it powers up
  in. A profile's calibration must have been taken at that range.
- **The pipeline's default files** (sensor positions, the hand's rest pose)
  are not inside an installed `pandora-core` yet. Until they are, pass them as
  `defaults=`; without them `open_receiver()` refuses to start, because hands
  made without them would be wrong.

One thread runs every glove's pipeline. On the PC this was written on, a
packet takes under a millisecond with both streams and the skeleton on, and a
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

Installed with the package, for looking at what the app sends without writing
code:

```bash
pandora devices
```

```bash
pandora watch --hand right
```

```bash
pandora watch --raw --rate 0 --count 100
```

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
both ends.

`pyproject.toml` pins `pandora-core` to one commit of the Pandora repository.
Move the pin when the protocol gains something this library should use.

## Not here yet

- Sending to a glove: haptics, Hall range and other settings. The protocol has
  no messages for them yet.
- `pandora calibrate`, which will write the profiles `open_receiver()` needs,
  and `pandora update-firmware`.
- `pandora watch` reading the receiver directly. It reads through the app.
- An `asyncio` client. This one is blocking, with one background thread.
- A licence, and a release on PyPI. Both wait for the repository going public,
  which also needs `pandora-core` to be installable by people outside the
  organisation.
