#!/usr/bin/env python3
"""Watch the Tapo camera for detector events and push a snapshot to Telegram.

Long-running counterpart to tapo_telegram.py: instead of taking one photo on
demand, it subscribes to the camera's ONVIF events and fires whenever a detector
trips -- the same source behind the Tapo app's push notifications.

Examples:
  python tapo_watch.py
  python tapo_watch.py --once
  python tapo_watch.py --debug
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from pathlib import Path

from onvif_events import Event, OnvifError, PullPointSession
from tapo_telegram import (
    build_rtsp_url,
    configure_console,
    load_env_file,
    parse_chat_ids,
    require_env,
    run_ffmpeg,
    send_photo,
)

# The camera holds each poll open this long, so idle cost is one request per window.
POLL_SECONDS = 30
# Subscriptions are created with a 10 minute lifetime; renew well before that.
RENEW_EVERY = 300
MAX_BACKOFF = 60

LABELS = {
    "Motion": "Movimiento",
    "People": "Persona detectada",
    "Intrusion": "Intrusión",
    "LineCross": "Cruce de línea",
    "Tamper": "Manipulación de la cámara",
    "TPSmartEvent": "Evento inteligente",
}


def log(message: str) -> None:
    # flush: Task Scheduler redirects this to a file, and buffered logs are useless
    # when you are trying to work out why the watcher went quiet.
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def env_number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name} debe ser un número, no {raw!r}") from None


def watched_events() -> set[str]:
    raw = os.getenv("WATCH_EVENTS", "Motion,People")
    names = {chunk.strip() for chunk in raw.split(",") if chunk.strip()}
    if not names:
        raise RuntimeError("WATCH_EVENTS no contiene ningún detector.")
    unknown = names - set(LABELS)
    if unknown:
        log(f"⚠ detectores no reconocidos en WATCH_EVENTS: {', '.join(sorted(unknown))}")
    return names


def dispatch(
    event: Event,
    *,
    token: str,
    chat_ids: list[str],
    output: Path,
    rtsp_url: str,
) -> None:
    """Capture one frame and fan it out. Never raises.

    A broken snapshot or a Telegram hiccup must not take the watcher down: it has
    to still be listening for the next event.
    """
    label = LABELS.get(event.name, event.name)
    caption = f"🚨 {label} — {datetime.now():%H:%M:%S}"
    try:
        run_ffmpeg(rtsp_url, output)
    except RuntimeError as exc:
        log(f"❌ no se pudo capturar el snapshot: {exc}")
        return
    for chat_id in chat_ids:
        try:
            send_photo(token, chat_id, output, caption)
            log(f"✅ {label} enviado a {chat_id}")
        except RuntimeError as exc:
            log(f"❌ {chat_id}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Atiende un solo evento y sale")
    parser.add_argument("--debug", action="store_true", help="Registra todos los eventos, incluso los no vigilados")
    parser.add_argument("--stream", choices=["1", "2"], help="Sobrescribe TAPO_STREAM")
    parser.add_argument("--output", default="snapshot.jpg", help="Ruta del JPG generado")
    args = parser.parse_args()

    configure_console()
    load_env_file(Path(__file__).with_name(".env"))
    if args.stream:
        os.environ["TAPO_STREAM"] = args.stream

    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_ids = parse_chat_ids(require_env("TELEGRAM_CHAT_ID"))
    output = Path(args.output).resolve()
    rtsp_url = build_rtsp_url()

    watched = watched_events()
    cooldown = env_number("MOTION_COOLDOWN", 60)
    port = int(env_number("ONVIF_PORT", 2020))

    session = PullPointSession(
        host=require_env("TAPO_IP"),
        user=require_env("TAPO_USER"),
        password=require_env("TAPO_PASSWORD"),
        port=port,
        pull_timeout=POLL_SECONDS,
    )

    log(f"Vigilando {', '.join(sorted(watched))} en {os.getenv('TAPO_IP')}:{port}")
    log(f"Destinos: {', '.join(chat_ids)} | cooldown {cooldown:.0f}s")

    connected = False
    backoff = 1.0
    next_renew = 0.0
    last_sent = float("-inf")
    # Detectors report both edges; we only care about the transition into firing.
    active: dict[str, bool] = {}

    try:
        while True:
            try:
                if not connected:
                    session.sync_clock()
                    session.subscribe()
                    connected = True
                    backoff = 1.0
                    next_renew = time.monotonic() + RENEW_EVERY
                    log("Suscripción ONVIF activa.")

                if time.monotonic() >= next_renew:
                    session.renew()
                    next_renew = time.monotonic() + RENEW_EVERY

                events = session.pull()
            except OnvifError as exc:
                connected = False
                session.unsubscribe()
                log(f"⚠ {exc} — reintento en {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            for event in events:
                if args.debug:
                    log(f"· {event.topic} {event.item}={event.value}")

                if event.name not in watched:
                    continue

                rising = event.value and not active.get(event.name, False)
                active[event.name] = event.value
                if not rising:
                    continue

                now = time.monotonic()
                if now - last_sent < cooldown:
                    log(f"· {LABELS.get(event.name, event.name)} omitido (cooldown)")
                    continue

                # Stamped before the attempt: a failing camera or bot must not turn
                # continuous motion into a retry storm.
                last_sent = now
                dispatch(
                    event,
                    token=token,
                    chat_ids=chat_ids,
                    output=output,
                    rtsp_url=rtsp_url,
                )
                if args.once:
                    return 0
    finally:
        session.unsubscribe()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Detenido.")
        raise SystemExit(130)
    except Exception as exc:
        log(f"❌ {exc}")
        raise SystemExit(1)
