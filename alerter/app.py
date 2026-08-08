"""
Cat Sentry — alerter.

Subscribes to Frigate's MQTT event stream. When a cat is confirmed inside the
floor zone and the system is armed, sends a Telegram message to each configured
chat (normally one group containing everyone who should be woken). Serves a
small PIN-protected control page for arming.

Operational rule: this process must never die from bad input. A crashed
listener is worse than a missed message, so every handler swallows and logs.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
from flask import (Flask, abort, redirect, render_template, request, session,
                   url_for)
from waitress import serve

# ---------------------------------------------------------------- logging
# Never log the bot token or raw chat ids. Labels only.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cat-sentry")

# ---------------------------------------------------------------- config
MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
ZONE = os.getenv("ZONE", "floor")
LABEL = os.getenv("LABEL", "cat")
COOLDOWN_S = max(30, int(os.getenv("COOLDOWN_SECONDS", "600")))
AUTO_ARM = os.getenv("AUTO_ARM_AT", "").strip()
AUTO_DISARM = os.getenv("AUTO_DISARM_AT", "").strip()
CONTROL_PIN = os.getenv("CONTROL_PIN", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_API = "https://api.telegram.org"

STATE_DIR = Path("/state")
ARMED_FILE = STATE_DIR / "armed"
LOG_FILE = STATE_DIR / "events.jsonl"

MAX_ATTEMPTS = 5
LOCKOUT_S = 300


@dataclass(frozen=True)
class Target:
    label: str
    chat_id: str


def parse_targets(raw: str) -> list[Target]:
    """Parse 'label:chat_id,label:chat_id'. Skip anything malformed.

    A bare chat id without a label is accepted too. Group chat ids are
    negative, which is why the pattern allows a leading minus.
    """
    out: list[Target] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # rsplit: labels may legitimately contain a colon, chat ids never do.
        label, _, chat_id = chunk.rpartition(":")
        label, chat_id = label.strip(), chat_id.strip()
        if not label:
            label = "chat"
        if not re.fullmatch(r"-?\d{5,20}", chat_id):
            log.warning("Skipping target %s: chat id must be numeric", label)
            continue
        out.append(Target(label, chat_id))
    return out


TARGETS = parse_targets(os.getenv("TELEGRAM_CHATS", ""))

if not CONTROL_PIN:
    raise SystemExit("CONTROL_PIN is not set. Run: python setup.py")
if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise SystemExit("SECRET_KEY missing or too short. Run: python setup.py")

# ---------------------------------------------------------------- state
_lock = threading.Lock()
_last_sent = 0.0
_attempts: dict[str, list] = {}


def is_armed() -> bool:
    return ARMED_FILE.exists()


def set_armed(on: bool, source: str = "manual") -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if on:
        ARMED_FILE.touch()
    else:
        ARMED_FILE.unlink(missing_ok=True)
    log.info("Armed=%s (%s)", on, source)
    record({"kind": "arm" if on else "disarm", "source": source})


def record(entry: dict) -> None:
    """Append to the event log. Best effort — never raises."""
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write event log: %s", exc)


def recent(n: int = 6) -> list[dict]:
    try:
        lines = LOG_FILE.read_text().splitlines()[-200:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= n:
            break
    return out


def last_alert_ts() -> float | None:
    for e in recent(200):
        if e.get("kind") == "alert":
            try:
                return datetime.fromisoformat(e["at"]).timestamp()
            except (KeyError, ValueError):
                return None
    return None


# ---------------------------------------------------------------- sending
def send_telegram(text: str) -> tuple[int, int]:
    """Returns (delivered, total). Never raises.

    The token sits in the URL path, so nothing here may echo the URL or the
    exception's message -- only the exception class name.
    """
    if not TELEGRAM_TOKEN:
        if TARGETS:
            log.warning("TELEGRAM_TOKEN is not set — cannot send")
        return 0, len(TARGETS)

    url = f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendMessage"
    ok = 0
    for t in TARGETS:
        for attempt in (1, 2):
            try:
                resp = requests.post(
                    url,
                    data={"chat_id": t.chat_id, "text": text},
                    timeout=20,
                )
                if resp.ok:
                    ok += 1
                    log.info("Sent to %s", t.label)
                    break
                # Telegram explains refusals in a JSON "description" field --
                # e.g. "chat not found", "bot was kicked". Worth surfacing:
                # it is the difference between a config error and an outage.
                detail = ""
                try:
                    detail = str((resp.json() or {}).get("description", ""))[:120]
                except ValueError:
                    pass
                log.warning("Send to %s returned HTTP %s %s (attempt %d)",
                            t.label, resp.status_code, detail, attempt)
            except requests.RequestException as exc:
                log.warning("Send to %s failed (attempt %d): %s",
                            t.label, attempt, exc.__class__.__name__)
            if attempt == 1:
                time.sleep(3)
    return ok, len(TARGETS)


# ---------------------------------------------------------------- mqtt
def handle_event(payload: dict) -> None:
    after = payload.get("after") or {}
    if after.get("label") != LABEL:
        return
    if ZONE not in (after.get("current_zones") or []):
        return
    if not is_armed():
        log.info("Cat in %s — disarmed, no alert sent", ZONE)
        record({"kind": "seen", "armed": False})
        return

    with _lock:
        global _last_sent
        remaining = COOLDOWN_S - (time.time() - _last_sent)
        if remaining > 0:
            log.info("Cat in %s — suppressed, %ds of cooldown left", ZONE, int(remaining))
            record({"kind": "suppressed"})
            return
        _last_sent = time.time()

    stamp = datetime.now().strftime("%H:%M")
    ok, total = send_telegram(
        f"Cat Sentry: he's off the bed and on the floor ({stamp}). "
        f"Please come and pick him up."
    )
    record({"kind": "alert", "delivered": ok, "recipients": total})


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("MQTT connected")
        client.subscribe("frigate/events")
    else:
        log.error("MQTT connection refused (rc=%s)", rc)


def on_disconnect(client, userdata, rc):
    if rc != 0:
        log.warning("MQTT dropped (rc=%s) — auto-reconnecting", rc)


def on_message(client, userdata, msg):
    try:
        handle_event(json.loads(msg.payload.decode("utf-8", "replace")))
    except json.JSONDecodeError:
        log.warning("Ignoring non-JSON MQTT payload")
    except Exception:  # noqa: BLE001 — must never kill the listener
        log.exception("Error handling event")


def start_mqtt() -> None:
    client = mqtt.Client(client_id=f"cat-sentry-{secrets.token_hex(4)}")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except OSError as exc:
            log.warning("Waiting for MQTT broker (%s)", exc.__class__.__name__)
            time.sleep(5)
    client.loop_start()


def scheduler() -> None:
    def parse(s: str) -> dtime | None:
        m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", s)
        return dtime(int(m.group(1)), int(m.group(2))) if m else None

    arm_at, disarm_at = parse(AUTO_ARM), parse(AUTO_DISARM)
    if not (arm_at or disarm_at):
        return
    log.info("Schedule: arm %s, disarm %s", AUTO_ARM or "—", AUTO_DISARM or "—")
    last = None
    while True:
        now = datetime.now().time().replace(second=0, microsecond=0)
        if now != last:
            last = now
            if arm_at and now == arm_at and not is_armed():
                set_armed(True, "schedule")
            if disarm_at and now == disarm_at and is_armed():
                set_armed(False, "schedule")
        time.sleep(15)


# ---------------------------------------------------------------- web
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    MAX_CONTENT_LENGTH=16 * 1024,
)


@app.after_request
def harden(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return resp


def locked_out(ip: str) -> int:
    """Seconds remaining on lockout, 0 if none."""
    tries = [t for t in _attempts.get(ip, []) if time.time() - t < LOCKOUT_S]
    _attempts[ip] = tries
    if len(tries) >= MAX_ATTEMPTS:
        return int(LOCKOUT_S - (time.time() - tries[0]))
    return 0


def authed() -> bool:
    return session.get("ok") is True


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.get("/healthz")
def healthz():
    return {"ok": True}, 200


@app.get("/login")
def login_form():
    if authed():
        return redirect(url_for("index"))
    return render_template("login.html", error=None, wait=locked_out(request.remote_addr))


@app.post("/login")
def login():
    ip = request.remote_addr or "?"
    wait = locked_out(ip)
    if wait:
        return render_template("login.html", error="Too many attempts.", wait=wait), 429

    given = (request.form.get("pin") or "")[:32]
    if hmac.compare_digest(given, CONTROL_PIN):
        _attempts.pop(ip, None)
        session.clear()
        session.permanent = True
        session["ok"] = True
        csrf_token()
        log.info("Control page unlocked")
        return redirect(url_for("index"))

    _attempts.setdefault(ip, []).append(time.time())
    left = MAX_ATTEMPTS - len(_attempts[ip])
    log.warning("Failed PIN attempt (%d left)", max(0, left))
    return render_template(
        "login.html",
        error=f"Wrong PIN. {left} attempt{'s' if left != 1 else ''} left."
        if left > 0 else "Locked.",
        wait=locked_out(ip),
    ), 401


@app.post("/logout")
def logout():
    if request.form.get("csrf") != session.get("csrf"):
        abort(400)
    session.clear()
    return redirect(url_for("login_form"))


@app.get("/")
def index():
    if not authed():
        return redirect(url_for("login_form"))

    ts = last_alert_ts()
    quiet = None
    if ts:
        mins = int((time.time() - ts) // 60)
        quiet = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"

    return render_template(
        "index.html",
        armed=is_armed(),
        # One-shot: "wake" or "sleep". Consumed here so the transition plays
        # once, right after the tap, rather than replaying on every refresh.
        anim=session.pop("anim", None),
        csrf=csrf_token(),
        # Integer-divide alone renders anything under a minute as "0 min".
        cooldown=(f"{COOLDOWN_S} sec" if COOLDOWN_S < 60
                  else f"{COOLDOWN_S // 60} min"),
        targets=[t.label for t in TARGETS],
        quiet=quiet,
        schedule=(AUTO_ARM and AUTO_DISARM) and f"{AUTO_ARM} – {AUTO_DISARM}" or None,
        events=recent(5),
    )


@app.post("/toggle")
def toggle():
    if not authed():
        abort(403)
    if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400)
    turning_on = not is_armed()
    set_armed(turning_on)
    session["anim"] = "wake" if turning_on else "sleep"
    return redirect(url_for("index"))


@app.post("/test")
def test():
    if not authed():
        abort(403)
    if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400)
    ok, total = send_telegram("Cat Sentry test message. Everything is working.")
    record({"kind": "test", "delivered": ok, "recipients": total})
    return redirect(url_for("index"))


if __name__ == "__main__":
    log.info("Cat Sentry starting — %d Telegram chat(s), cooldown %ds",
             len(TARGETS), COOLDOWN_S)
    if not TARGETS:
        log.warning("No Telegram chats configured. Add them: python setup.py --recipients-only")
    elif not TELEGRAM_TOKEN:
        log.warning("Chats configured but TELEGRAM_TOKEN is missing — nothing will send")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=scheduler, daemon=True).start()
    start_mqtt()
    serve(app, host="0.0.0.0", port=8080, threads=4, ident="")
