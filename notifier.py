import json
import logging
import time
import requests
import smtplib
import socket
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Optional
from collections import defaultdict

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
    NOTIFY_MAX_FREQUENCY,
    NOTIFY_BATCH_WINDOW,
    NOTIFY_SUMMARY_ENABLED,
    STATUS_UPDATE_AVAILABLE,
)

# Notification throttling state
_throttle_lock = threading.Lock()
_last_notification_time = 0.0

# Notification batching state for summary notifications
_batch_lock = threading.Lock()
_pending_notifications = defaultdict(list)  # event_type -> list of notifications
_last_batch_time = 0.0
_batch_timer = None


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


def _should_batch_notification(event_type: str) -> bool:
    """Determine if this notification type should be batched into summaries."""
    # Batch pull and recreate success notifications, but send errors immediately
    batchable_types = {"pull_result", "recreate_result", "bulk_complete"}
    return (NOTIFY_SUMMARY_ENABLED and 
            event_type in batchable_types and
            NOTIFY_BATCH_WINDOW > 0)


def _send_batched_notifications():
    """Send summary notifications for all batched events."""
    global _pending_notifications, _last_batch_time, _batch_timer
    
    with _batch_lock:
        if not _pending_notifications:
            return
            
        # Group notifications by type for summary
        notifications_by_type = dict(_pending_notifications)
        _pending_notifications.clear()
        _last_batch_time = time.time()
        
    # Send summary for each notification type
    for event_type, notifications in notifications_by_type.items():
        if event_type == "pull_result":
            successes = [n for n in notifications if n.get("status") == "success"]
            errors = [n for n in notifications if n.get("status") == "error"]
            
            if successes:
                image_names = [n.get("extra", {}).get("image", n.get("title", "")) for n in successes]
                send_notification_direct(
                    event_type="pull_summary",
                    title=f"{len(successes)} images pulled successfully",
                    message=f"Successfully pulled: {', '.join(image_names[:5])}{'...' if len(image_names) > 5 else ''}",
                    status="success",
                    extra={"count": len(successes), "images": image_names, "type": "summary"}
                )
                
            if errors:
                for error_notif in errors:
                    # Send individual error notifications immediately
                    send_notification_direct(**error_notif)
                    
        elif event_type == "recreate_result":
            successes = [n for n in notifications if n.get("status") == "success"]
            errors = [n for n in notifications if n.get("status") == "error"]
            
            if successes:
                targets = [n.get("extra", {}).get("target", n.get("title", "")) for n in successes]
                send_notification_direct(
                    event_type="recreate_summary",
                    title=f"{len(successes)} stacks recreated successfully",
                    message=f"Successfully recreated: {', '.join(targets[:5])}{'...' if len(targets) > 5 else ''}",
                    status="success",
                    extra={"count": len(successes), "targets": targets, "type": "summary"}
                )
                
            if errors:
                for error_notif in errors:
                    send_notification_direct(**error_notif)
                    
        elif event_type == "bulk_complete":
            for notif in notifications:
                send_notification_direct(**notif)


def _schedule_batch_timer():
    """Schedule the batch notification timer if not already running."""
    global _batch_timer
    
    with _batch_lock:
        if _batch_timer is not None:
            return
            
        def batch_wrapper():
            global _batch_timer
            _send_batched_notifications()
            with _batch_lock:
                _batch_timer = None
                
        _batch_timer = threading.Timer(NOTIFY_BATCH_WINDOW, batch_wrapper)
        _batch_timer.daemon = True
        _batch_timer.start()


def send_notification_direct(
    event_type: str,
    title: str,
    message: str,
    status: str = "info",
    extra: Optional[dict] = None
) -> None:
    """Send a notification directly without batching logic."""
    from config import NOTIFY_ENABLED, NOTIFY_BACKEND
    
    if not NOTIFY_ENABLED:
        return

    # Apply notification throttling if configured
    if NOTIFY_MAX_FREQUENCY > 0:
        global _last_notification_time
        with _throttle_lock:
            elapsed = time.time() - _last_notification_time
            if elapsed < NOTIFY_MAX_FREQUENCY:
                log.debug(f"Notification throttled: {title} (elapsed: {elapsed:.1f}s < {NOTIFY_MAX_FREQUENCY}s)")
                return
            _last_notification_time = time.time()

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


def send_notification(
    event_type: str,
    title: str,
    message: str,
    status: str = "info",
    extra: Optional[dict] = None
) -> None:
    """Send a notification using the configured backend.
    
    notifications may be batched into summary notifications for certain event types.
    
    Raises:
        RuntimeError: If NOTIFY_ENABLED is False or backend is not configured
    """
    from config import NOTIFY_ENABLED, NOTIFY_BACKEND
    
    if not NOTIFY_ENABLED:
        return

    # Check if this notification should be batched
    if _should_batch_notification(event_type):
        with _batch_lock:
            notification_data = {
                "event_type": event_type,
                "title": title,
                "message": message,
                "status": status,
                "extra": extra or {}
            }
            _pending_notifications[event_type].append(notification_data)
            _schedule_batch_timer()
        return
    
    # Send directly for non-batchable notifications
    send_notification_direct(event_type, title, message, status, extra)


def notify_updates_found(results: dict) -> None:
    """Notify when updates are found during a check."""
    from config import NOTIFY_ON_UPDATES_FOUND
    
    if not NOTIFY_ON_UPDATES_FOUND:
        return

    updates = [r for r in results.values() if r["status"] == STATUS_UPDATE_AVAILABLE]
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
