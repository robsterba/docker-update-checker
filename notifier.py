import json
import logging
import requests
import smtplib
import socket
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Optional

from config import (
    NOTIFY_WEBHOOK_URL,
    NOTIFY_WEBHOOK_METHOD,
    NOTIFY_WEBHOOK_TIMEOUT,
    NOTIFY_MQTT_HOST,
    NOTIFY_MQTT_PORT,
    NOTIFY_MQTT_TOPIC,
    NOTIFY_MQTT_USERNAME,
    NOTIFY_MQTT_PASSWORD,
    NOTIFY_MQTT_RETAIN,
    NOTIFY_EMAIL_HOST,
    NOTIFY_EMAIL_PORT,
    NOTIFY_EMAIL_USERNAME,
    NOTIFY_EMAIL_PASSWORD,
    NOTIFY_EMAIL_FROM,
    NOTIFY_EMAIL_TO,
    NOTIFY_EMAIL_USE_TLS,
)


def notify_webhook(payload: dict):
    if not NOTIFY_WEBHOOK_URL:
        raise RuntimeError("NOTIFY_WEBHOOK_URL not configured")

    method = NOTIFY_WEBHOOK_METHOD if NOTIFY_WEBHOOK_METHOD in ("POST", "PUT") else "POST"
    headers = {"Content-Type": "application/json"}

    if method == "PUT":
        r = requests.put(NOTIFY_WEBHOOK_URL, json=payload, headers=headers, timeout=NOTIFY_WEBHOOK_TIMEOUT)
    else:
        r = requests.post(NOTIFY_WEBHOOK_URL, json=payload, headers=headers, timeout=NOTIFY_WEBHOOK_TIMEOUT)

    r.raise_for_status()


def notify_mqtt(payload: dict):
    if not NOTIFY_MQTT_HOST or not NOTIFY_MQTT_TOPIC:
        raise RuntimeError("NOTIFY_MQTT_HOST or NOTIFY_MQTT_TOPIC not configured")

    import paho.mqtt.client as mqtt

    client = mqtt.Client()
    if NOTIFY_MQTT_USERNAME:
        client.username_pw_set(NOTIFY_MQTT_USERNAME, NOTIFY_MQTT_PASSWORD or None)

    client.connect(NOTIFY_MQTT_HOST, NOTIFY_MQTT_PORT, 10)
    client.loop_start()
    result = client.publish(
        NOTIFY_MQTT_TOPIC,
        json.dumps(payload),
        qos=0,
        retain=NOTIFY_MQTT_RETAIN,
    )
    result.wait_for_publish()
    client.loop_stop()
    client.disconnect()


def notify_email(payload: dict):
    if not all([NOTIFY_EMAIL_HOST, NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO]):
        raise RuntimeError("Email notification settings incomplete")

    msg = EmailMessage()
    msg["Subject"] = f"[{payload.get('status', 'info').upper()}] {payload.get('title', 'Notification')}"
    msg["From"] = NOTIFY_EMAIL_FROM
    msg["To"] = NOTIFY_EMAIL_TO

    body = [
        payload.get("title", ""),
        "",
        payload.get("message", ""),
        "",
        f"Event Type: {payload.get('event_type', '')}",
        f"Status: {payload.get('status', '')}",
        f"Time: {payload.get('time', '')}",
        f"Host: {payload.get('host', '')}",
        "",
        json.dumps(payload.get("extra", {}), indent=2),
    ]
    msg.set_content("\n".join(body))

    with smtplib.SMTP(NOTIFY_EMAIL_HOST, NOTIFY_EMAIL_PORT, timeout=15) as server:
        if NOTIFY_EMAIL_USE_TLS:
            server.starttls()
        if NOTIFY_EMAIL_USERNAME:
            server.login(NOTIFY_EMAIL_USERNAME, NOTIFY_EMAIL_PASSWORD)
        server.send_message(msg)


# Notification helper functions
log = logging.getLogger(__name__)


def build_notification_payload(
    event_type: str,
    title: str,
    message: str,
    status: str = "info",
    extra: Optional[dict] = None
) -> dict[str, Any]:
    """Build a standardized notification payload."""
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "title": title,
        "message": message,
        "status": status,
        "host": socket.gethostname(),
        "app": "docker-update-checker",
        "extra": extra or {}
    }


def send_notification(
    event_type: str,
    title: str,
    message: str,
    status: str = "info",
    extra: Optional[dict] = None
) -> None:
    """Send a notification using the configured backend.
    
    Raises:
        RuntimeError: If NOTIFY_ENABLED is False or backend is not configured
    """
    from config import NOTIFY_ENABLED, NOTIFY_BACKEND
    
    if not NOTIFY_ENABLED:
        return

    payload = build_notification_payload(event_type, title, message, status, extra)

    try:
        if NOTIFY_BACKEND == "webhook":
            notify_webhook(payload)
        elif NOTIFY_BACKEND == "mqtt":
            notify_mqtt(payload)
        elif NOTIFY_BACKEND == "email":
            notify_email(payload)
        else:
            raise RuntimeError(f"Unsupported NOTIFY_BACKEND: {NOTIFY_BACKEND}")

        log.info(f"Notification sent: {NOTIFY_BACKEND}: {title}")
    except Exception as e:
        log.warning(f"Notification failed: {e}")
        raise


def notify_updates_found(results: dict) -> None:
    """Notify when updates are found during a check."""
    from config import NOTIFY_ON_UPDATES_FOUND
    
    if not NOTIFY_ON_UPDATES_FOUND:
        return

    updates = [r for r in results.values() if r["status"] == "update_available"]
    if not updates:
        return

    send_notification(
        event_type="updates_found",
        title=f"{len(updates)} image update(s) available",
        message="New container image updates were detected.",
        status="info",
        extra={
            "count": len(updates),
            "images": [r["image"] for r in updates],
            "stacks": sorted(list({s for r in updates for s in r.get("stacks", [])}))
        }
    )


def notify_pull_result(
    image_ref: str,
    ok: bool,
    message: str,
    stacks: Optional[list] = None
) -> None:
    """Notify about image pull results."""
    from config import NOTIFY_ON_PULL_SUCCESS, NOTIFY_ON_PULL_ERROR
    
    if ok and not NOTIFY_ON_PULL_SUCCESS:
        return
    if (not ok) and not NOTIFY_ON_PULL_ERROR:
        return

    send_notification(
        event_type="pull_result",
        title=f"Pull {'succeeded' if ok else 'failed'}: {image_ref}",
        message=message,
        status="success" if ok else "error",
        extra={"image": image_ref, "stacks": stacks or []}
    )


def notify_recreate_result(
    target: str,
    ok: bool,
    message: str,
    stack: Optional[str] = None
) -> None:
    """Notify about compose recreate results."""
    from config import NOTIFY_ON_RECREATE_SUCCESS, NOTIFY_ON_RECREATE_ERROR
    
    if ok and not NOTIFY_ON_RECREATE_SUCCESS:
        return
    if (not ok) and not NOTIFY_ON_RECREATE_ERROR:
        return

    send_notification(
        event_type="recreate_result",
        title=f"Recreate {'succeeded' if ok else 'failed'}: {target}",
        message=message,
        status="success" if ok else "error",
        extra={"target": target, "stack": stack}
    )


def notify_bulk_complete(
    target: str,
    message: str,
    extra: Optional[dict] = None
) -> None:
    """Notify when a bulk job completes."""
    from config import NOTIFY_ON_BULK_COMPLETE
    
    if not NOTIFY_ON_BULK_COMPLETE:
        return

    send_notification(
        event_type="bulk_complete",
        title=f"Bulk job complete: {target}",
        message=message,
        status="success",
        extra=extra or {}
    )
