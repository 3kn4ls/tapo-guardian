# Sesión de trabajo — despliegue en k3s

**Session ID:** `5409e450-67c8-4987-ba04-c92eb572b745`
**Fecha:** 2026-08-19
**Retomar con:** `claude --resume 5409e450-67c8-4987-ba04-c92eb572b745`

El plan de aquella sesión quedó en:
`/home/ecanals/.claude/plans/mossy-sauteeing-rivest.md`

## Dónde se quedó

Servicio **desplegado y funcionando 24/7** en el cluster. Último commit `068c92a`,
versión `v1.0.1`, árbol limpio y todo pusheado a `main`.

Verificado de extremo a extremo, no solo desplegado:

- Foto real entregada a los dos chats de Telegram desde dentro del pod.
- Un evento real de movimiento disparó captura y envío.
- Reinicio del pod → vuelve solo y se resuscribe en ~1s.
- 6 min en reposo → 0 reinicios (el umbral de liveness de 150s no da falsos positivos).
- Rootfs solo lectura, `/tmp` escribible, uid 10001, hora en CEST.

## Datos que costó averiguar

- **La cámara está en `192.168.1.111`**, no en la `.50` del `.env.example` (esa IP no
  existe en la red). Se encontró escaneando el /24 por el puerto 554.
- Emite `tns1:RuleEngine/CellMotionDetector/Motion`. Queda **sin confirmar si emite
  `People`**: nunca se vio ese topic durante las pruebas.
- ffmpeg pica a **78 MiB** capturando un frame del stream principal (medido), y tarda
  ~2,4s. De ahí el límite de 256Mi en vez del 128Mi habitual del cluster.
- Egress desde un pod real a ONVIF 2020, RTSP 554 y Telegram 443: OK, sin NetworkPolicies.

## Tres fallos que solo aparecieron al ejecutarlo en el cluster

1. **Contraseña en los logs.** No basta con no imprimir `rtsp_url`: ffmpeg devuelve la
   URL entera en su stderr y acababa en `kubectl logs`. Resuelto con `redact_rtsp()`.
2. **SIGTERM ignorado.** Como PID 1, el proceso moría sin ejecutar el `finally` que hace
   `unsubscribe()`. Medido: 30s / exit 137 antes, 0,33s / exit 130 después.
3. **Snapshot perdido.** El primer evento real falló con `Operation not permitted` (la
   cámara sirve pocas sesiones RTSP simultáneas). Añadido un reintento; después, 5
   capturas seguidas correctas.

## Pendiente / decisiones tuyas

- **ArgoCD está apagado entero**: sus 7 Deployments a 0 réplicas, no solo el
  image-updater. Por eso tus 6 aplicaciones salen en `Unknown`; es anterior a este
  trabajo. Mientras siga así, desplegar con `kubectl apply -f k8s/`.
  El `Application` está listo en `/home/ecanals/ws/argocd/tapo-guardian.yaml`.
  **Al levantarlo**, `selfHeal` revertirá ediciones manuales del ConfigMap.
- **Rotar el PAT de GitHub** de `/etc/rancher/k3s/registries.yaml`: quedó visible en la
  sesión.
- **Confirmar si el detector `People` funciona** de verdad, o quitarlo de `WATCH_EVENTS`.
  Para verlo: poner `--debug` temporalmente en los `args` del Deployment y leer los topics.
- Reservar `192.168.1.111` por DHCP en el router: si cambia, el watcher se queda
  reintentando contra una IP muerta.

## Comandos del día a día

```bash
kubectl -n tapo-guardian get pods
kubectl -n tapo-guardian logs -f deploy/tapo-guardian
./release.sh patch "descripción"     # sube versión, tag e imagen
kubectl apply -f k8s/                # desplegar (mientras ArgoCD esté apagado)
```
