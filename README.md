# Tapo TC71 → Telegram

Pequeña prueba independiente de Home Assistant/Kubernetes:

`Tapo TC71 → RTSP → ffmpeg → JPG → Telegram Bot API`

Dos modos de uso:

- **`tapo_telegram.py`** — una foto puntual, cuando tú la pides.
- **`tapo_watch.py`** — proceso en segundo plano que envía una foto **cada vez que la cámara
  detecta algo**, suscribiéndose a los eventos ONVIF.

Sin dependencias de terceros: solo la biblioteca estándar de Python y el ejecutable `ffmpeg`.

## 1. Configurar la TC71

En la app Tapo, crea una **Cuenta de cámara** para acceso RTSP:

`Cámara → Ajustes → Ajustes avanzados → Cuenta de la cámara`

TP-Link documenta para estas cámaras el acceso RTSP en el puerto 554 con `/stream1` (principal) y `/stream2` (substream).

## 2. Instalar FFmpeg

En Windows, instala FFmpeg y asegúrate de que `ffmpeg.exe` está en el `PATH`.

Comprueba:

```powershell
ffmpeg -version
```

## 3. Crear el bot de Telegram

1. En Telegram abre `@BotFather`.
2. Ejecuta `/newbot`.
3. Guarda el token.
4. Abre tu bot y envíale `/start`.

## 4. Configurar `.env`

Copia:

```text
.env.example → .env
```

Rellena:

- `TAPO_IP`: IP local de la TC71.
- `TAPO_USER`: usuario de la Cuenta de cámara.
- `TAPO_PASSWORD`: contraseña de esa cuenta.
- `TELEGRAM_BOT_TOKEN`: token de BotFather.

Luego obtén el chat ID:

```powershell
python tapo_telegram.py --get-chat-id
```

Verás algo parecido a:

```text
chat_id=123456789    Eduardo
```

Pon ese número en `TELEGRAM_CHAT_ID`.

### Varios destinos

`TELEGRAM_CHAT_ID` admite varios destinos separados por comas, y envía la misma foto a todos:

```text
TELEGRAM_CHAT_ID=123456789,-987654321
```

Para enviar a un **grupo**: añade el bot al grupo, escribe cualquier mensaje allí y vuelve a
lanzar `--get-chat-id`.

Los IDs de grupo **llevan signo negativo**, y olvidarlo es el error más fácil de cometer: sin
el `-`, Telegram busca un usuario con ese número y responde `chat not found`.

Si un grupo se convierte en supergrupo — al hacerlo público, activar Temas o crecer mucho —
su ID cambia a un `-100…` y hay que volver a obtenerlo.

## 5. Primera prueba

```powershell
python tapo_telegram.py
```

Deberías obtener `snapshot.jpg` y recibirlo en Telegram.

Para probar el substream:

```powershell
python tapo_telegram.py --stream 2
```

Para poner un texto:

```powershell
python tapo_telegram.py --caption "🚨 Movimiento detectado en salón"
```

## 6. Vigilancia automática

`tapo_watch.py` se suscribe a los eventos ONVIF de la cámara y envía una foto cada vez que
salta un detector — la misma fuente que dispara las notificaciones push de la app Tapo.

Requiere la **Cuenta de cámara** del paso 1: el puerto ONVIF 2020 se abre junto con el RTSP.

```powershell
python tapo_watch.py
```

Para probar un único evento y salir:

```powershell
python tapo_watch.py --once
```

Para ver todo lo que emite la cámara sin filtrar, útil para afinar `WATCH_EVENTS`:

```powershell
python tapo_watch.py --debug
```

### Detectores disponibles en la TC71

| Nombre en `WATCH_EVENTS` | Qué detecta |
| --- | --- |
| `Motion` | Movimiento genérico |
| `People` | Persona |
| `Intrusion` | Intrusión en zona |
| `LineCross` | Cruce de línea |
| `Tamper` | Manipulación de la cámara |
| `TPSmartEvent` | Evento inteligente de TP-Link |

Los detectores por zona (`Intrusion`, `LineCross`) solo disparan si has definido las zonas en
la app Tapo.

### Ajustes

- `WATCH_EVENTS` (`Motion,People`): qué detectores disparan el envío.
- `MOTION_COOLDOWN` (`60`): segundos mínimos entre envíos. No lo bajes demasiado — Telegram
  limita a unos 20 mensajes por minuto en un grupo.
- `ONVIF_PORT` (`2020`).

El proceso solo envía en el **flanco de subida** del detector. Los detectores emiten también el
evento de fin, y sin ese filtro llegaría el doble de fotos.

Ante caída de red, reinicio de la cámara o suscripción caducada, reconecta solo con espera
exponencial. Un fallo puntual de ffmpeg o de Telegram se registra pero no detiene la vigilancia.

## 7. Dejarlo corriendo en Windows

Tarea del Programador que arranca al iniciar sesión, sin ventana de consola y guardando el log:

```powershell
schtasks /Create /TN "TapoWatch" /TR "cmd /c pythonw.exe G:\ws\tapo_notify\tapo_watch.py >> G:\ws\tapo_notify\watch.log 2>&1" /SC ONLOGON /F
```

`pythonw.exe` evita la ventana; la redirección es imprescindible porque sin ella los mensajes
se pierden por completo.

Gestionarla:

```powershell
schtasks /Run /TN "TapoWatch"
```

```powershell
schtasks /End /TN "TapoWatch"
```

```powershell
schtasks /Delete /TN "TapoWatch" /F
```

Antes de programarla, prueba siempre en primer plano con `python tapo_watch.py`: así ves los
errores de configuración al momento en vez de tener que ir al log.

## Seguridad

- No compartas `.env`.
- No abras hacia Internet ni el puerto RTSP 554 ni el ONVIF 2020. Ambos van sin cifrar.
- Usa una cuenta de cámara específica para RTSP en vez de reutilizar la contraseña de tu TP-Link ID.
