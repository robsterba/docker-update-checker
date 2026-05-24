import json
import requests
import smtplib
from email.message import EmailMessage
from typing import Optional

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
