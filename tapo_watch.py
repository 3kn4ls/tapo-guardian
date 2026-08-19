#!/usr/bin/env python3
"""Watch the Tapo camera for detector events and push a snapshot to Telegram.

Long-running counterpart to tapo_telegram.py: instead of taking one photo on
demand, it subscribes to the camera's ONVIF events and fires whenever a detector
trips -- the same source behind the Tapo app's push notifications.

Examples:
  python tapo_watch.py
  python tapo_watch.py --once
  python tapo_watch.py --debug

Con HEALTH_PORT definido expone GET /health para la liveness probe de Kubernetes.
"""
from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
# Liveness threshold. A healthy loop touches the beat every POLL_SECONDS, and a
# reconnect costs at most MAX_BACKOFF, so 150s clears both without false alarms.
STALE_AFTER = 150

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


class Heartbeat:
    """Last time the watch loop made progress, shared with the health server.

    The loop and the HTTP thread touch this from different threads, hence the lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._beat = time.monotonic()

    def beat(self) -> None:
        with self._lock:
            self._beat = time.monotonic()

    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self._beat


def start_health_server(port: int, heartbeat: Heartbeat) -> None:
    """Serve GET /health on a daemon thread for the Kubernetes liveness probe.

    The watcher already recovers from ONVIF failures on its own, so this exists for
    the one case that self-healing cannot reach: a loop wedged on a socket that
    never returns, or a subscription the camera dropped silently. Both leave the
    process alive and sending nothing, which no restartPolicy would ever catch.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            if self.path.split("?", 1)[0] != "/health":
                self.send_error(404)
                return
            age = heartbeat.age()
            alive = age < STALE_AFTER
            body = f"{'ok' if alive else 'stale'} age={age:.0f}s\n".encode()
            self.send_response(200 if alive else 503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            # Silence per-request logging: the probe hits this every 30s and would
            # otherwise bury the event lines we actually care about.
            pass

    server = ThreadingHTTPServer(("", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Health endpoint en :{port}/health")


def install_sigterm_handler() -> None:
    """Make SIGTERM unwind like Ctrl-C so the subscription is released.

    Python installs no SIGTERM handler, so as PID 1 in a container the default
    action kills the process outright: the `finally: unsubscribe()` never runs,
    the pod burns its whole termination grace period on every rollout, and the
    camera is left holding a subscription nobody will ever pull from again.
    Raising KeyboardInterrupt reuses the shutdown path Ctrl-C already exercises.
    """

    def handler(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handler)


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
    install_sigterm_handler()
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

    # Off unless HEALTH_PORT is set, so running this on a laptop or as a Windows
    # scheduled task behaves exactly as before and binds no port.
    heartbeat = Heartbeat()
    health_port = int(env_number("HEALTH_PORT", 0))
    if health_port:
        start_health_server(health_port, heartbeat)

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
                # A poll that returns -- with or without events -- is the proof the
                # loop is still alive and talking to the camera.
                heartbeat.beat()
            except OnvifError as exc:
                connected = False
                session.unsubscribe()
                log(f"⚠ {exc} — reintento en {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                # Beat on the way out too: reconnecting is healthy work, and a camera
                # rebooting for a couple of minutes must not trigger a pod restart.
                heartbeat.beat()
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
