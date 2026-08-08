#!/usr/bin/env python3
"""
Cat Sentry setup wizard.

Collects the handful of things that genuinely cannot be automated -- camera
credentials, a Telegram bot token and chat, a PIN -- and writes .env and
config/config.yml.

Usage:
    python setup.py                    full wizard
    python setup.py --recipients-only  add/replace the Telegram bot and chats
    python setup.py --test-stream      pull one frame from the camera
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
CONFIG_TEMPLATE = ROOT / "config" / "config.yml.template"
CONFIG_PATH = ROOT / "config" / "config.yml"

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def head(msg: str) -> None:
    say(f"\n{BOLD}{msg}{RESET}")


def hint(msg: str) -> None:
    say(f"{DIM}{msg}{RESET}")


def ask(prompt: str, default: str = "", validate=None, secret: bool = False) -> str:
    """Prompt until the answer validates. Returns a stripped string."""
    suffix = f" [{default}]" if default else ""
    while True:
        raw = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        if not raw and default:
            raw = default
        if not raw:
            say("  Required.")
            continue
        if validate:
            problem = validate(raw)
            if problem:
                say(f"  {problem}")
                continue
        return raw


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{d}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


# --------------------------------------------------------------------------
# validators
# --------------------------------------------------------------------------

def v_ip(s: str) -> str | None:
    try:
        ipaddress.IPv4Address(s)
    except ValueError:
        return "Not a valid IPv4 address."
    return None


def v_token(s: str) -> str | None:
    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", s):
        return "Should look like 123456789:AAF... exactly as BotFather sent it."
    return None


def v_chat_id(s: str) -> str | None:
    if not re.fullmatch(r"-?\d{5,20}", s):
        return "A chat id is digits only; group ids start with a minus."
    return None


def v_pin(s: str) -> str | None:
    if not re.fullmatch(r"\d{4,8}", s):
        return "4 to 8 digits."
    if s in ("1234", "0000", "1111", "123456"):
        return "Pick something less guessable."
    return None


def v_name(s: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{0,23}", s):
        return "Letters and numbers, up to 24 characters."
    return None


def v_no_control(s: str) -> str | None:
    """Credentials must not contain characters that would break the RTSP URL."""
    if any(c in s for c in " @/:?#\\\n\r\t"):
        return "Cannot contain spaces or any of  @ / : ? # \\"
    return None


# --------------------------------------------------------------------------
# camera discovery
# --------------------------------------------------------------------------

def local_subnet() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 1))  # TEST-NET-1, never routed
        ip = s.getsockname()[0]
        s.close()
        return ".".join(ip.split(".")[:3])
    except OSError:
        return None


def probe(ip: str, port: int = 554, timeout: float = 0.4) -> str | None:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return ip
    except OSError:
        return None


def scan_for_camera() -> list[str]:
    base = local_subnet()
    if not base:
        return []
    say(f"  Scanning {base}.0/24 for devices with RTSP open...")
    targets = [f"{base}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=128) as pool:
        found = [r for r in pool.map(probe, targets) if r]
    return found


# --------------------------------------------------------------------------
# env file
# --------------------------------------------------------------------------

def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_env(values: dict[str, str]) -> None:
    lines = [
        "# Cat Sentry secrets. Never commit this file.",
        "# Regenerate with: python setup.py",
        "",
    ]
    for k, v in values.items():
        lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass  # Windows filesystems may not support this


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def collect_camera(existing: dict) -> dict:
    head("Camera")
    hint("The Tapo needs a Camera Account, which is separate from your TP-Link login.")
    hint("In the Tapo app: Settings > Advanced Settings > Camera Account.")

    ip = existing.get("CAM_IP", "")
    if not ip and ask_yes("\nScan the network for the camera?", True):
        found = scan_for_camera()
        if found:
            say("\n  Devices with RTSP open:")
            for i, f in enumerate(found, 1):
                say(f"    {i}. {f}")
            pick = input("\n  Which one is the camera? (number, or Enter to type it): ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(found):
                ip = found[int(pick) - 1]
        else:
            say("  Nothing found. The camera may still be connecting -- you can type the IP.")

    ip = ask("Camera IP", default=ip, validate=v_ip)
    hint("Tip: reserve this IP in your router's DHCP settings so it never changes.")

    user = ask("Camera Account username", default=existing.get("CAM_USER", ""),
               validate=v_no_control)
    pw = ask("Camera Account password", validate=v_no_control, secret=True)

    return {"CAM_IP": ip, "CAM_USER": user, "CAM_PASSWORD": pw}


def tg_api(token: str, method: str, params: dict | None = None) -> dict | None:
    """Call a Telegram Bot API method. Returns parsed JSON, or None.

    Never surfaces the exception text -- the token sits in the URL path and
    some error messages echo the URL straight back.
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:  # 4xx bodies still carry a useful ok/description payload
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def discover_chats(token: str) -> list[tuple[str, str]]:
    """Find every chat the bot can currently see.

    Any pending update carrying a `chat` object counts. Being added to a group
    generates one by itself, so this usually works without the user sending
    anything -- but /start is the reliable fallback.
    """
    data = tg_api(token, "getUpdates", {"timeout": 0, "limit": 100})
    if not data or not data.get("ok"):
        return []
    found: dict[str, str] = {}
    for upd in data.get("result", []):
        for value in upd.values():
            if not isinstance(value, dict):
                continue
            chat = value.get("chat")
            if not isinstance(chat, dict) or chat.get("id") is None:
                continue
            title = (chat.get("title") or chat.get("username")
                     or chat.get("first_name") or "chat")
            found[str(chat["id"])] = f"{title}  [{chat.get('type', '?')}]"
    return sorted(found.items(), key=lambda kv: kv[1])


def collect_telegram(existing: dict) -> dict:
    head("Telegram alerts")
    hint("You create the bot yourself. In Telegram:")
    hint("  1. Message @BotFather and send:  /newbot")
    hint("  2. Give it any name, then a username ending in 'bot'")
    hint("  3. It replies with a token like  123456789:AAF...")
    say()

    blank = {"TELEGRAM_TOKEN": "", "TELEGRAM_CHATS": ""}

    if not ask_yes("Have you created the bot and got the token?", True):
        say("\n  Skipping. Everything else will still build and run.")
        say("  Add it later with:  python setup.py --recipients-only")
        return blank

    while True:
        token = ask("Bot token", validate=v_token, secret=True)
        me = tg_api(token, "getMe")
        if me and me.get("ok"):
            say(f"  Connected to @{(me.get('result') or {}).get('username', '?')}")
            break
        say("  Telegram rejected that token, or the network is unreachable.")
        if not ask_yes("  Try again?", True):
            return blank

    head("Group chat")
    hint("Now put the bot where the alerts should land:")
    hint("  1. Create a Telegram group with everyone who should be woken")
    hint("  2. Add your bot to that group as a member")
    hint("  3. In the group, send:  /start")
    say()
    hint("Step 3 matters -- by default a bot cannot read ordinary group")
    hint("chatter, but commands always reach it.")
    say()

    chats: list[tuple[str, str]] = []
    while True:
        input("  Press Enter once the bot is in the group: ")
        chats = discover_chats(token)
        if chats:
            break
        say("  No chats visible yet. Send  /start  in the group.")
        if not ask_yes("  Look again?", True):
            break

    picked: list[str] = []
    if chats:
        say("\n  Chats the bot can see:")
        for i, (_cid, label) in enumerate(chats, 1):
            say(f"    {i}. {label}")
        raw = input("\n  Which one(s)? (number, or several like 1,2): ").strip()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(chats):
                cid, label = chats[int(part) - 1]
                # Strip delimiters: label and id are stored as "label:id".
                name = re.sub(r"[^A-Za-z0-9 ]", "", label.split("  [")[0]).strip()
                picked.append(f"{name or 'chat'}:{cid}")

    if not picked:
        say("\n  Nothing selected.")
        if ask_yes("  Enter a chat id by hand instead?", True):
            cid = ask("    Chat id", validate=v_chat_id)
            name = ask("    Label for it", default="Family", validate=v_name)
            picked.append(f"{name}:{cid}")

    say(f"\n  {len(picked)} chat(s) configured."
        if picked else "\n  No chats configured -- nothing will be sent.")
    return {"TELEGRAM_TOKEN": token, "TELEGRAM_CHATS": ",".join(picked)}


def collect_control(existing: dict) -> dict:
    head("Control page")
    hint("The arm/disarm page runs on your LAN so your phone can reach it.")
    hint("It is PIN-protected because anything on your network can see it.")
    say()

    pin = ask("PIN for the control page (4-8 digits)", validate=v_pin, secret=True)
    confirm = ask("Confirm PIN", validate=v_pin, secret=True)
    while pin != confirm:
        say("  PINs did not match.")
        pin = ask("PIN", validate=v_pin, secret=True)
        confirm = ask("Confirm PIN", validate=v_pin, secret=True)

    values = {
        "CONTROL_PIN": pin,
        "SECRET_KEY": existing.get("SECRET_KEY") or secrets.token_hex(32),
        "COOLDOWN_SECONDS": "600",
    }

    say()
    if ask_yes("Auto-arm at night and disarm in the morning?", True):
        values["AUTO_ARM_AT"] = ask("  Arm at (HH:MM)", default="23:00",
                                    validate=lambda s: None if re.fullmatch(
                                        r"([01]\d|2[0-3]):[0-5]\d", s) else "Use HH:MM.")
        values["AUTO_DISARM_AT"] = ask("  Disarm at (HH:MM)", default="08:00",
                                       validate=lambda s: None if re.fullmatch(
                                           r"([01]\d|2[0-3]):[0-5]\d", s) else "Use HH:MM.")
    else:
        values["AUTO_ARM_AT"] = ""
        values["AUTO_DISARM_AT"] = ""

    return values


def write_config() -> None:
    """Render config.yml from the template, preserving any zones already drawn."""
    if not CONFIG_TEMPLATE.exists():
        say("  config/config.yml.template missing -- cannot render config.")
        return

    template = CONFIG_TEMPLATE.read_text()

    if CONFIG_PATH.exists():
        current = CONFIG_PATH.read_text()
        # Don't clobber hand-drawn zone/mask coordinates on a re-run.
        for marker in ("### MASK", "### ZONE"):
            old = extract_marked(current, marker)
            if old and "PLACEHOLDER" not in old:
                template = replace_marked(template, marker, old)
        say("  Existing zone coordinates preserved.")

    CONFIG_PATH.write_text(template)


def extract_marked(text: str, marker: str) -> str | None:
    m = re.search(rf"{re.escape(marker)}-START(.*?){re.escape(marker)}-END", text, re.S)
    return m.group(1) if m else None


def replace_marked(text: str, marker: str, body: str) -> str:
    return re.sub(
        rf"({re.escape(marker)}-START).*?({re.escape(marker)}-END)",
        lambda m: m.group(1) + body + m.group(2),
        text,
        flags=re.S,
    )


def test_stream() -> int:
    env = read_env()
    missing = [k for k in ("CAM_IP", "CAM_USER", "CAM_PASSWORD") if not env.get(k)]
    if missing:
        say(f"Camera not configured yet ({', '.join(missing)}). Run: python setup.py")
        return 1

    out = ROOT / "scripts" / "first-frame.jpg"
    url = f"rtsp://{env['CAM_USER']}:{env['CAM_PASSWORD']}@{env['CAM_IP']}:554/stream1"

    head("Pulling one frame from the camera")
    hint("Using the ffmpeg inside the Frigate image, so you don't need it installed.")

    # ffmpeg is no longer on PATH inside the Frigate image -- it lives under
    # /usr/lib/ffmpeg/<version>/bin. Resolve the newest one at run time so this
    # keeps working when the image bumps its ffmpeg version.
    # The URL is passed as an argument, not interpolated into the shell string,
    # so the password is never exposed to shell parsing.
    resolve = (
        'FF=$(command -v ffmpeg || ls -d /usr/lib/ffmpeg/*/bin/ffmpeg 2>/dev/null'
        ' | sort -V | tail -1); '
        '[ -n "$FF" ] || { echo "no ffmpeg found in image" >&2; exit 127; }; '
        'exec "$FF" -rtsp_transport tcp -i "$1" -frames:v 1 -y /out/first-frame.jpg'
    )
    cmd = [
        "docker", "run", "--rm", "--network", "host",
        "-v", f"{out.parent}:/out",
        "--entrypoint", "sh",
        "ghcr.io/blakeblackshear/frigate:stable",
        "-c", resolve, "_", url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    if proc.returncode == 0 and out.exists():
        say(f"\n  Frame saved: {out}")
        say("  Open it and check you can see the floor around the bed.")
        return 0

    # Never echo the URL back -- it contains the password.
    stderr = proc.stderr.replace(env["CAM_PASSWORD"], "********")
    say("\n  Could not read the stream.")
    say("  Most likely: wrong IP, wrong Camera Account credentials, or RTSP not enabled.")
    say(f"\n{DIM}{stderr[-800:]}{RESET}")
    return 1


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Cat Sentry setup")
    p.add_argument("--recipients-only", action="store_true")
    p.add_argument("--test-stream", action="store_true")
    args = p.parse_args()

    if args.test_stream:
        return test_stream()

    existing = read_env()

    say(f"\n{BOLD}Cat Sentry setup{RESET}")
    say(f"{DIM}Nothing you type here is printed back or logged.{RESET}")

    if args.recipients_only:
        if not existing:
            say("\nNo .env yet. Run the full wizard first: python setup.py")
            return 1
        existing.pop("RECIPIENTS", None)  # legacy CallMeBot key, no longer used
        existing.update(collect_telegram(existing))
        write_env(existing)
        # `restart` reuses the container's existing environment and would
        # silently keep the old values; `up -d` recreates it from .env.
        say("\nTelegram updated. Apply with:  docker compose up -d alerter")
        return 0

    values: dict[str, str] = {}
    values.update(collect_camera(existing))
    values.update(collect_telegram(existing))
    values.update(collect_control(existing))
    values["ZONE"] = "floor"
    values["LABEL"] = "cat"

    write_env(values)
    write_config()

    head("Done")
    say(f"  Wrote .env (permissions 600) and config/config.yml")
    say("\n  Next:")
    say("    python setup.py --test-stream     check the camera")
    say("    docker compose up -d --build      start everything")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nCancelled. Nothing was written.")
        sys.exit(130)
