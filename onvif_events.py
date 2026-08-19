#!/usr/bin/env python3
"""Minimal ONVIF event client for Tapo cameras (standard library only).

Implements the WS-BaseNotification PullPoint flow the TC71 speaks:

    CreatePullPointSubscription -> PullMessages (long poll) -> Renew -> Unsubscribe

Only the handful of calls the camera actually needs are implemented, with no WSDL
parsing, which is why this needs no third-party SOAP stack.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PWD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
B64 = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)

ACTION_PULL = (
    "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
)
ACTION_RENEW = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"
ACTION_UNSUBSCRIBE = (
    "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"
)


class OnvifError(RuntimeError):
    """Transport failures and SOAP faults alike."""


@dataclass(frozen=True)
class Event:
    """One property change reported by a detector."""

    topic: str      # tns1:RuleEngine/CellMotionDetector/Motion
    name: str       # Motion
    item: str       # IsMotion
    value: bool     # True while the detector is firing
    utc_time: str


def _local(tag: str) -> str:
    """Strip the {namespace} prefix ElementTree puts on every tag."""
    return tag.rsplit("}", 1)[-1]


def _findall(root: ET.Element, name: str) -> list[ET.Element]:
    """Find descendants by local name, ignoring namespace prefixes.

    Cameras are inconsistent about which prefixes they use, so matching on the
    local name is far more robust than fully qualified paths.
    """
    return [el for el in root.iter() if _local(el.tag) == name]


def _int_child(parent: ET.Element, name: str, default: int = 0) -> int:
    found = _findall(parent, name)
    if not found or not (found[0].text or "").strip():
        return default
    try:
        return int(found[0].text.strip())
    except ValueError:
        return default


def parse_events(root: ET.Element) -> list[Event]:
    """Extract the Is* boolean properties out of a PullMessages response."""
    events: list[Event] = []
    for message in _findall(root, "NotificationMessage"):
        topics = _findall(message, "Topic")
        topic = (topics[0].text or "").strip() if topics else ""

        stamp = ""
        for el in _findall(message, "Message"):
            if el.get("UtcTime"):
                stamp = el.get("UtcTime", "")
                break

        for data in _findall(message, "Data"):
            for item in _findall(data, "SimpleItem"):
                name = item.get("Name", "")
                if not name.startswith("Is"):
                    continue
                raw = (item.get("Value") or "").strip().lower()
                events.append(
                    Event(
                        topic=topic,
                        name=topic.rsplit("/", 1)[-1] if topic else name[2:],
                        item=name,
                        value=raw in {"true", "1"},
                        utc_time=stamp,
                    )
                )
    return events


class PullPointSession:
    """A PullPoint subscription against one camera."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 2020,
        pull_timeout: int = 30,
    ) -> None:
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.events_url = f"http://{host}:{port}/onvif/service"
        self._user = user
        self._password = password
        self._pull_timeout = pull_timeout
        self._skew = timedelta(0)
        self._subscription: str | None = None

    # -- transport ---------------------------------------------------------

    def _security_header(self) -> str:
        nonce = secrets.token_bytes(16)
        created = (datetime.now(timezone.utc) + self._skew).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self._password.encode()).digest()
        ).decode()
        return (
            f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{WSSE}">'
            "<wsse:UsernameToken>"
            f"<wsse:Username>{escape(self._user)}</wsse:Username>"
            f'<wsse:Password Type="{PWD_DIGEST}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{B64}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f'<wsu:Created xmlns:wsu="{WSU}">{created}</wsu:Created>'
            "</wsse:UsernameToken></wsse:Security>"
        )

    def _soap(
        self,
        url: str,
        body: str,
        action: str | None = None,
        timeout: int = 15,
    ) -> ET.Element:
        addressing = ""
        if action:
            addressing = (
                f'<wsa:Action s:mustUnderstand="1">{action}</wsa:Action>'
                f'<wsa:To s:mustUnderstand="1">{escape(url)}</wsa:To>'
            )
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:wsa="http://www.w3.org/2005/08/addressing">'
            f"<s:Header>{addressing}{self._security_header()}</s:Header>"
            f"<s:Body>{body}</s:Body>"
            "</s:Envelope>"
        )
        request = Request(
            url,
            data=envelope.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            # SOAP faults come back as HTTP 400/500 with the detail in the body.
            raw = exc.read()
            if not raw.strip():
                raise OnvifError(
                    f"La cámara respondió HTTP {exc.code} sin cuerpo "
                    "(revisa TAPO_USER y TAPO_PASSWORD)."
                ) from exc
        except URLError as exc:
            raise OnvifError(f"No se pudo contactar con la cámara: {exc.reason}") from exc
        except OSError as exc:
            raise OnvifError(f"Error de red hablando con la cámara: {exc}") from exc

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise OnvifError(f"Respuesta ONVIF ilegible: {exc}") from exc

        faults = _findall(root, "Fault")
        if faults:
            reasons = [el.text for el in _findall(faults[0], "Text") if el.text]
            detail = reasons[0].strip() if reasons else "fault sin detalle"
            raise OnvifError(f"La cámara rechazó la petición ONVIF: {detail}")
        return root

    # -- session lifecycle -------------------------------------------------

    def sync_clock(self) -> None:
        """Align our WS-Security timestamp with the camera clock.

        Digest auth is rejected when Created drifts too far from device time, and
        IP cameras do drift. GetSystemDateAndTime needs no authentication, so this
        works even when the clocks are already too far apart to authenticate.
        """
        root = self._soap(
            self.device_url,
            '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
        )
        utc = _findall(root, "UTCDateTime")
        if not utc:
            return
        date = _findall(utc[0], "Date")
        time_ = _findall(utc[0], "Time")
        if not date or not time_:
            return
        try:
            camera = datetime(
                _int_child(date[0], "Year"),
                _int_child(date[0], "Month", 1),
                _int_child(date[0], "Day", 1),
                _int_child(time_[0], "Hour"),
                _int_child(time_[0], "Minute"),
                _int_child(time_[0], "Second"),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return
        self._skew = camera - datetime.now(timezone.utc)

    def subscribe(self, termination: str = "PT10M") -> None:
        root = self._soap(
            self.events_url,
            '<CreatePullPointSubscription xmlns="http://www.onvif.org/ver10/events/wsdl">'
            f"<InitialTerminationTime>{termination}</InitialTerminationTime>"
            "</CreatePullPointSubscription>",
        )
        refs = _findall(root, "SubscriptionReference")
        addresses = _findall(refs[0], "Address") if refs else []
        address = (addresses[0].text or "").strip() if addresses else ""
        if not address:
            raise OnvifError("La cámara no devolvió dirección de suscripción.")
        # The camera hands back its own endpoint; every later call must go there.
        self._subscription = address

    def pull(self, limit: int = 10) -> list[Event]:
        """Long-poll for events. Blocks until something happens or the poll expires."""
        if not self._subscription:
            raise OnvifError("No hay suscripción activa.")
        root = self._soap(
            self._subscription,
            '<PullMessages xmlns="http://www.onvif.org/ver10/events/wsdl">'
            f"<Timeout>PT{self._pull_timeout}S</Timeout>"
            f"<MessageLimit>{limit}</MessageLimit>"
            "</PullMessages>",
            action=ACTION_PULL,
            # The camera holds the connection for the whole poll window, so the
            # socket timeout has to outlast it.
            timeout=self._pull_timeout + 15,
        )
        return parse_events(root)

    def renew(self, termination: str = "PT10M") -> None:
        if not self._subscription:
            raise OnvifError("No hay suscripción activa.")
        self._soap(
            self._subscription,
            '<Renew xmlns="http://docs.oasis-open.org/wsn/b-2">'
            f"<TerminationTime>{termination}</TerminationTime>"
            "</Renew>",
            action=ACTION_RENEW,
        )

    def unsubscribe(self) -> None:
        if not self._subscription:
            return
        try:
            self._soap(
                self._subscription,
                '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>',
                action=ACTION_UNSUBSCRIBE,
                timeout=5,
            )
        except OnvifError:
            # Shutting down: a camera that already dropped the subscription is fine.
            pass
        finally:
            self._subscription = None

    def __enter__(self) -> PullPointSession:
        self.sync_clock()
        self.subscribe()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.unsubscribe()
