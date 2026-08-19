# Tapo TC71 -> RTSP -> ffmpeg -> JPG -> Telegram, como daemon para Kubernetes.
#
# python:3.12-slim y no alpine: es el patron del cluster, y el ffmpeg de Debian
# es la misma linea que el del nodo, con lo que el comportamiento RTSP ya esta
# probado. Sin deps de pip que compilar, musl no aportaria nada.
FROM python:3.12-slim

# ffmpeg captura el frame por RTSP; tzdata hace que TZ=Europe/Madrid tenga efecto
# (los pies de foto usan datetime.now() naive, sin tzdata saldrian en UTC).
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia explicita de los tres modulos: nada de "COPY . .", para que ningun .env
# ni fichero suelto acabe en la imagen aunque falle el .dockerignore.
COPY tapo_telegram.py onvif_events.py tapo_watch.py ./

RUN useradd --system --uid 10001 --no-create-home guardian
USER 10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Madrid \
    HEALTH_PORT=8080

EXPOSE 8080

# ENTRYPOINT/CMD separados: permite lanzar un pod de debug sobrescribiendo solo
# los args, por ejemplo `--once --debug`, sin repetir el interprete.
ENTRYPOINT ["python", "-u", "tapo_watch.py"]
CMD ["--output", "/tmp/snapshot.jpg"]
