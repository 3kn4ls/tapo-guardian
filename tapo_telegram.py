#!/usr/bin/env python3
"""Capture a snapshot from a TP-Link Tapo camera over RTSP and send it to Telegram.

Examples:
  python tapo_telegram.py
  python tapo_telegram.py --stream 2
  python tapo_telegram.py --get-chat-id
  python tapo_telegram.py --output snapshot.jpg --caption "Movimiento detectado"

TELEGRAM_CHAT_ID admite varios destinos separados por comas, por ejemplo:
  TELEGRAM_CHAT_ID=123456789,-987654321
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def load_env_file(path: Path) -> None:
    """Minimal .env loader; does not require python-dotenv."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def configure_console() -> None:
    """Make emoji-safe output regardless of the console codepage.

    Windows consoles default to a legacy codepage, and a scheduled task that
    redirects stdout to a file inherits the same. Printing the status emoji would
    then raise UnicodeEncodeError and kill the process midway through a send.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable {name} en .env")
    return value


def parse_chat_ids(raw: str) -> list[str]:
    """TELEGRAM_CHAT_ID admite varios destinos separados por comas.

    Los grupos llevan signo negativo (-100... en supergrupos). Se conserva el
    orden y se descartan duplicados para no enviar la misma foto dos veces.
    """
    unique: list[str] = []
    for chunk in raw.split(","):
        chat_id = chunk.strip()
        if chat_id and chat_id not in unique:
            unique.append(chat_id)
    if not unique:
        raise RuntimeError("TELEGRAM_CHAT_ID no contiene ningun destino valido.")
    return unique


def build_rtsp_url() -> str:
    host = require_env("TAPO_IP")
    user = quote(require_env("TAPO_USER"), safe="")
    password = quote(require_env("TAPO_PASSWORD"), safe="")
    stream = os.getenv("TAPO_STREAM", "1").strip()
    if stream not in {"1", "2"}:
        raise RuntimeError("TAPO_STREAM debe ser 1 o 2")
    return f"rtsp://{user}:{password}@{host}:554/stream{stream}"


def run_ffmpeg(rtsp_url: str, output: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "No encuentro ffmpeg en PATH. Instálalo y vuelve a ejecutar el script."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(output),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timeout conectando con la cámara por RTSP.") from exc

    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        details = (result.stderr or "").strip()
        raise RuntimeError(
            "No se pudo obtener el snapshot por RTSP. "
            + (f"ffmpeg: {details}" if details else "Revisa IP, usuario, contraseña y RTSP.")
        )


def telegram_request(token: str, method: str, data: bytes, content_type: str) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = Request(url, data=data, headers={"Content-Type": content_type}, method="POST")
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"No se pudo conectar con Telegram: {exc.reason}") from exc

    payload = json.loads(raw)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram devolvió un error: {payload}")
    return payload


def get_chat_ids(token: str) -> None:
    payload = telegram_request(token, "getUpdates", b"", "application/json")
    results = payload.get("result", [])
    if not results:
        print("No hay mensajes pendientes para el bot.")
        print("1) Abre Telegram y escribe /start al bot.")
        print("2) Vuelve a ejecutar: python tapo_telegram.py --get-chat-id")
        return

    seen: set[tuple[str, str]] = set()
    for update in results:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or "(sin nombre)"
        key = (str(chat_id), str(title))
        if key in seen:
            continue
        seen.add(key)
        print(f"chat_id={chat_id}\t{title}")


def send_photo(token: str, chat_id: str, photo: Path, caption: str) -> None:
    boundary = "----TapoTelegramBoundary7MA4YWxkTrZu0gW"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])

    field("chat_id", chat_id)
    if caption:
        field("caption", caption)

    image_bytes = photo.read_bytes()
    parts.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="photo"; filename="{photo.name}"\r\n'.encode(),
        b"Content-Type: image/jpeg\r\n\r\n",
        image_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])

    body = b"".join(parts)
    telegram_request(token, "sendPhoto", body, f"multipart/form-data; boundary={boundary}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--get-chat-id", action="store_true", help="Muestra los chat_id que han escrito al bot")
    parser.add_argument("--stream", choices=["1", "2"], help="Sobrescribe TAPO_STREAM")
    parser.add_argument("--output", default="snapshot.jpg", help="Ruta del JPG generado")
    parser.add_argument("--caption", default="📷 Snapshot Tapo TC71", help="Texto de la foto")
    args = parser.parse_args()

    configure_console()
    load_env_file(Path(__file__).with_name(".env"))
    token = require_env("TELEGRAM_BOT_TOKEN")

    if args.get_chat_id:
        get_chat_ids(token)
        return 0

    if args.stream:
        os.environ["TAPO_STREAM"] = args.stream

    chat_ids = parse_chat_ids(require_env("TELEGRAM_CHAT_ID"))
    output = Path(args.output).resolve()
    rtsp_url = build_rtsp_url()

    # Never print rtsp_url: it contains the camera password.
    print(f"Conectando con Tapo {os.getenv('TAPO_IP')} por RTSP...")
    run_ffmpeg(rtsp_url, output)
    print(f"Snapshot creado: {output}")

    # One bad destination must not stop the others: send to all, report at the end.
    failed: list[str] = []
    for chat_id in chat_ids:
        try:
            send_photo(token, chat_id, output, args.caption)
        except RuntimeError as exc:
            failed.append(chat_id)
            print(f"❌ {chat_id}: {exc}", file=sys.stderr)
        else:
            print(f"✅ Foto enviada a {chat_id}.")

    if failed:
        raise RuntimeError(
            f"Fallaron {len(failed)} de {len(chat_ids)} envios: {', '.join(failed)}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"❌ {exc}", file=sys.stderr)
        raise SystemExit(1)
