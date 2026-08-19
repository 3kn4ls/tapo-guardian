# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Qué es

Envía snapshots de una cámara TP-Link Tapo TC71 a Telegram:

`Tapo TC71 → RTSP → ffmpeg → JPG → Telegram Bot API`

Prueba independiente, deliberadamente fuera de Home Assistant/Kubernetes.

## Restricción de diseño: sin dependencias de terceros

`requirements.txt` está vacío a propósito. Todo se hace con la biblioteca estándar
de Python (`urllib`, `xml.etree`, `subprocess`) más el ejecutable `ffmpeg` en el
`PATH`. **No añadas dependencias** (`requests`, `python-dotenv`, `onvif-zeep`,
`zeep`, `python-telegram-bot`) sin preguntar antes: `onvif_events.py` existe
precisamente para evitar una pila SOAP completa, y el cargador de `.env` y el
cliente multipart de Telegram están escritos a mano por la misma razón.

## Comandos

No hay tests, linter, build ni CI. La verificación es ejecutar los scripts:

```bash
python tapo_telegram.py                 # una foto puntual
python tapo_telegram.py --get-chat-id   # descubrir chat_ids (necesita /start previo al bot)
python tapo_telegram.py --stream 2      # substream
python tapo_watch.py                    # vigilancia continua por eventos ONVIF
python tapo_watch.py --once             # un solo evento y salir
python tapo_watch.py --debug            # registra todos los eventos, incluso los no vigilados
```

`--debug` es la herramienta para averiguar qué emite realmente la cámara antes de
tocar `WATCH_EVENTS`.

## Arquitectura

Tres módulos, con dependencia en una sola dirección:

- **`tapo_telegram.py`** — CLI de foto puntual y, a la vez, **biblioteca compartida**.
  `tapo_watch.py` importa de aquí `load_env_file`, `configure_console`, `require_env`,
  `parse_chat_ids`, `build_rtsp_url`, `run_ffmpeg` y `send_photo`. Cambiar cualquiera
  de esas firmas rompe el watcher.
- **`onvif_events.py`** — cliente ONVIF mínimo (WS-BaseNotification PullPoint) sin
  WSDL: `CreatePullPointSubscription → PullMessages → Renew → Unsubscribe`, con
  autenticación WS-Security PasswordDigest. No importa nada del proyecto.
- **`tapo_watch.py`** — bucle de vigilancia: une la sesión ONVIF con la captura y
  el envío.

El parseo XML busca por **nombre local** de etiqueta (`_local` / `_findall`), no por
rutas con namespace: las cámaras son inconsistentes con los prefijos.

## Invariantes que no deben romperse

Estas decisiones tienen una razón concreta detrás; el código las comenta, y varias
son sutiles:

- **Nunca imprimas `rtsp_url`**: lleva la contraseña de la cámara incrustada.
- **Solo flanco de subida.** Los detectores emiten también el evento de fin; el dict
  `active` en `tapo_watch.py` filtra esa segunda mitad. Sin él llegan fotos dobles.
- **`last_sent` se marca antes del intento de envío**, no después: una cámara o un
  bot que fallan no deben convertir el movimiento continuo en una tormenta de reintentos.
- **`dispatch()` nunca lanza excepción.** Un fallo de ffmpeg o de Telegram se registra
  y la vigilancia continúa.
- **Un destino malo no detiene a los demás**: se envía a todos los `chat_id` y el
  fallo se reporta al final.
- **`sync_clock()` antes de `subscribe()`**: el digest se rechaza si `Created` se aleja
  del reloj de la cámara, y las cámaras IP derivan. `GetSystemDateAndTime` no requiere
  autenticación, así que funciona incluso con los relojes ya desfasados.
- **El timeout de socket del `pull()` debe superar la ventana de long-poll**
  (`pull_timeout + 15`): la cámara mantiene la conexión abierta todo el intervalo.
- **`configure_console()`** fuerza UTF-8 en stdout/stderr. Sin ello, los emoji de
  estado revientan con `UnicodeEncodeError` en la consola de Windows y matan el
  proceso a mitad de envío.
- **`log()` usa `flush=True`**: el Programador de tareas de Windows redirige a
  fichero y un log con buffer no sirve para diagnosticar por qué enmudeció el watcher.
- **`MOTION_COOLDOWN`** existe por el límite de Telegram (~20 mensajes/minuto a un
  grupo). No lo bajes a la ligera.

## Configuración

Todo va por `.env` (nunca commiteado; ver `.env.example`), cargado con
`os.environ.setdefault`, así que **las variables de entorno reales tienen prioridad
sobre el fichero**.

Los IDs de grupo de Telegram llevan signo negativo, y los supergrupos empiezan por
`-100…`; olvidar el `-` produce `chat not found`.

## Idioma

Los mensajes de usuario, logs y comentarios de "por qué" están **en español**; los
docstrings de módulo y función están **en inglés**. Mantén esa convención.

## Despliegue

Objetivo real: Windows, mediante una tarea del Programador con `pythonw.exe` y
redirección de stdout a `watch.log` (ver README §7). Cualquier cambio en el arranque
o en el logging debe seguir funcionando sin consola conectada.

## Seguridad

Ni el RTSP (554) ni el ONVIF (2020) van cifrados: son solo para LAN. No propongas
exponerlos a Internet.
