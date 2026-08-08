# Cat Sentry

**A local computer-vision alert system that watches the floor beside my bed, not the bed
itself.** When my cat steps off the mattress at 3am, it sends a Telegram message to a
family group so they can collect him before he pees on the carpet — and it stays completely
silent when the only thing moving is me.

Runs on hardware I already owned. Total spend: **about USD 18** for a single camera.
No cloud, no subscription, no footage stored.

---

## The problem

My cat has a peeing problem, so he can't roam the house at night. I still want him
sleeping on my bed. The compromise: he stays on the bed, and the moment he's on the floor,
someone comes to get him.

That framing produces a specific and slightly awkward set of constraints:

| Requirement | Why it eliminates the obvious solution |
|---|---|
| Fire when the cat is on the floor | — |
| **Never** fire when I move in bed | Rules out every motion-based sensor |
| Room is fully dark | Rules out RGB cameras and phone-as-camera |
| It's my bedroom | Rules out vendor-cloud cameras and stored footage |
| Seconds matter | A cat that's decided to pee doesn't wait |
| Student budget | Rules out purpose-built hardware |

The load-bearing requirement is the second one. The system has to distinguish *cat on
floor* from *human in bed*, in total darkness, within a couple of seconds — and it has to
be wrong in the safe direction when it's wrong at all.

## Alternatives considered and rejected

**mmWave presence sensor (Aqara FP2, ~USD 80).** Zone-based radar, no camera, excellent
privacy story. Rejected because its headline AI feature is explicitly built to *filter pets
out* of detections, and vendor forums are full of cats triggering it unpredictably. A
sensor whose stated design goal is to ignore my target object is the wrong instrument, no
matter how good its zoning is.

**Pressure mats beside the bed (~USD 20).** Cheap, dumb, reliable in principle. Rejected:
cannot distinguish my feet from the cat's paws, and a cat trivially steps over or around a
mat. There is no tuning that fixes either problem.

**Battery/wire-free camera (Tapo C400).** No cable to route down the wall. Rejected: wakes
on PIR and sleeps between events, adding latency to a task whose entire value is latency.

**Raspberry Pi as compute host.** The reflexive homelab answer, and wrong in 2026. A Pi 5
plus PSU and storage costs more than a used x86 thin client, has no Intel iGPU for
accelerated inference, and dropped the H.264 hardware decode block the Pi 4 had — which is
exactly the codec the camera streams.

**Selected: IP camera + Frigate, on an existing desktop.** `cat` is a native class in the
detection model, so the human/cat distinction is handled by a trained model rather than by
heuristics I'd have to invent. IR illumination solves darkness. Masks and zones are drawn
per-pixel, so the bed is excluded *geometrically* rather than probabilistically.

## Architecture

```
 Tapo C200  ──RTSP/H.264──►  Frigate
 (IR, no WAN access)         │
                             ├─ motion gate   (cheap; bed is masked out here)
                             ├─ object detect (only on surviving motion)
                             └─ zone test     (floor ∧ ¬bed, 3-frame inertia)
                                    │
                             MQTT: frigate/events
                                    │
                              alerter (Python)
                             ├─ armed?      → file-backed state
                             ├─ cooldown?   → 10 min
                             └─ Telegram    → group chat with parents
                                    │
                             Flask control page :8080
                             (PIN-protected, phone home screen)
```

Three containers: `mosquitto` (broker, no host ports), `frigate` (detection, localhost
only), `alerter` (notification + control UI, LAN-visible and PIN-gated).

## Design decisions

**The bed is excluded geometrically, not by confidence threshold.** An alert requires the
object to be labelled `cat` *and* to be inside the `floor` zone, and the bed lies entirely
outside that polygon. So no confidence threshold and no model update can produce an alert
about the human in the bed: the safety property is geometric rather than probabilistic.

The motion mask over the bed is a *performance* measure on top of that, not the guarantee
itself — Frigate's own documentation is explicit that motion masks do not prevent object
detection, since tracking continues into a masked region once an object exists. Relying on
the mask alone would be a mistake; the zone test is what actually holds.

**Tuned to prefer false positives.** `threshold: 0.55` is permissive. The cost asymmetry is
stark — a false ping is one unnecessary message; a miss is a soiled mattress. Optimising
for precision here would be optimising the wrong metric entirely.

**10 fps rather than 5.** A cat crossing the zone is visible for 2–4 seconds, so at 10 fps
that's 20–40 frames and the system only needs *one* to clear threshold. Detection
reliability is not a single coin flip; doubling the frame rate roughly doubles the chances
per crossing, at negligible cost for one camera.

**`inertia: 3`.** An object must persist across three frames before the zone counts as
occupied — enough to stop a paw dangling off the mattress edge from firing.

**CPU detection, deliberately.** The host is a desktop x86 machine with an Intel iGPU, so
OpenVINO was available from the start. I chose CPU anyway: one camera at 15 fps, motion-gated in an
otherwise static dark room, is a trivial load, and the CPU path removes BIOS configuration
and device passthrough as failure modes during setup. The OpenVINO config ships commented
out as a documented drop-in swap. *Optimise when something hurts, not before.*

**No recordings, structurally.** `record.enabled: false`, and the frame cache is mounted as
tmpfs so decoded frames live in RAM and never touch the SSD. Snapshots are enabled only
during the tuning period, then switched off. This is a bedroom; the correct amount of
retained footage is zero, and the right way to guarantee that is to make writing impossible
rather than to remember not to.

**Camera denied WAN access at the router.** The realistic threat to a consumer IP camera is
the vendor's cloud, not an attacker already inside the LAN. Blocking egress means a
vendor-side compromise has no path to the device. Nothing is port-forwarded.

**The control page is PIN-gated because it has to be LAN-visible.** A phone needs to reach
it, which means anything else on the network can too. So: PIN with constant-time
comparison, CSRF tokens on every state-changing route, `HttpOnly`/`SameSite=Strict`
session cookies, a restrictive CSP with no JavaScript at all, and lockout after five failed
attempts. The MQTT broker, by contrast, publishes no host ports whatsoever, and the Frigate
UI binds to `127.0.0.1` — neither needs to be reachable from the phone, so neither is
reachable from anything.

**Arm state in a file, not memory.** Survives container restarts. A restart at 2am that
silently disarmed the system would be the worst possible failure mode.

**Cooldown of 10 minutes.** One restless night should not send twelve messages. Alert
fatigue would kill this system's usefulness faster than any technical fault.

**The alerter never dies from bad input.** Every MQTT handler and HTTP send swallows and
logs. A crashed listener fails silently for hours; a dropped message fails once.

## Known limitations

- **Detection is not guaranteed.** Motion blur under IR during fast movement, and the cat
  hugging a wall just outside the zone, both produce misses. A waterproof mattress
  protector remains the actual safety net; this system buys time, it doesn't replace the
  fallback.
- **The alert must wake a human.** A notification on a silenced phone delivers nothing.
  Everyone in the group has to whitelist Telegram as a Do Not Disturb exception — a social
  configuration step the software cannot enforce or verify.
- **Telegram is a third-party relay** with no uptime guarantee, and it requires the app
  installed on every recipient's phone. It replaced CallMeBot, which caps signups and was
  closed to new users; WhatsApp was ruled out because no supported API can post to a group.
- **Single camera, single angle.** Furniture creates blind spots more cameras would close.
- **Requires the host desktop to stay powered on** overnight, which is the real reason a
  dedicated low-power box would eventually be the better home for this.

## Results

*Recorded over the first three weeks of operation. Crossings are counted from Frigate
snapshots during the tuning period, when snapshots capture both detected and — via the
unmasked debug feed — missed events.*

| Metric | Value |
|---|---|
| Nights running | |
| Confirmed floor crossings | |
| Crossings that produced an alert | |
| Detection rate | |
| False positives | |
| Median latency, crossing → message sent | |
| Host CPU while idle | |

## Setup

```bash
python setup.py          # camera credentials, Telegram bot + group, PIN
python setup.py --test-stream
docker compose up -d --build
bash scripts/verify.sh
```

Then draw the bed mask and floor zone in the Frigate UI at `http://127.0.0.1:5000`, paste
the coordinates into `config/config.yml`, and restart Frigate. The control page is at
`http://<host-lan-ip>:8080` — add it to your phone's home screen.

**Run disarmed for the first several nights.** Review the snapshots each morning and adjust
the zone against what actually happened rather than what you imagined would happen. This
loop runs at one iteration per night and is the genuine cost of the project.

The two steps that genuinely need a human are the setup wizard, which collects camera
credentials and the Telegram bot token, and drawing the bed mask and floor zone by eye in
the Frigate UI. Everything else is scripted.

## Stack

Frigate · Docker Compose · MQTT (Mosquitto) · Python (Flask, waitress, paho-mqtt) ·
Telegram Bot API · TP-Link Tapo C200
