"""Tests for notifier.py: payload building, gating, throttling, backends."""


import pytest

import config
import notifier
from notifier import (
    _should_batch_notification,
    build_notification_payload,
    notify_email,
    notify_mqtt,
    notify_pull_result,
    notify_webhook,
    send_notification,
    send_notification_direct,
)


@pytest.fixture(autouse=True)
def reset_notifier_state():
    notifier._pending_notifications.clear()
    notifier._last_notification_time = 0.0
    yield
    notifier._pending_notifications.clear()
    notifier._last_notification_time = 0.0


class TestBuildNotificationPayload:
    def test_payload_shape(self):
        payload = build_notification_payload(
            "pull_result", "Title", "Message", status="success", extra={"image": "redis:7"}
        )
        assert payload["event_type"] == "pull_result"
        assert payload["title"] == "Title"
        assert payload["message"] == "Message"
        assert payload["status"] == "success"
        assert payload["host"]  # hostname is filled in
        assert payload["app"] == "docker-update-checker"
        assert payload["extra"] == {"image": "redis:7"}
        assert payload["time"]

    def test_extra_defaults_to_empty_dict(self):
        assert build_notification_payload("t", "T", "M")["extra"] == {}


class TestBatchingDecision:
    def test_batchable_types_batch_when_enabled(self, monkeypatch):
        monkeypatch.setattr(notifier, "NOTIFY_SUMMARY_ENABLED", True)
        monkeypatch.setattr(notifier, "NOTIFY_BATCH_WINDOW", 300)
        assert _should_batch_notification("pull_result")
        assert _should_batch_notification("recreate_result")
        assert _should_batch_notification("bulk_complete")

    def test_immediate_types_never_batch(self, monkeypatch):
        monkeypatch.setattr(notifier, "NOTIFY_SUMMARY_ENABLED", True)
        monkeypatch.setattr(notifier, "NOTIFY_BATCH_WINDOW", 300)
        assert not _should_batch_notification("updates_found")
        assert not _should_batch_notification("test")

    def test_no_batching_when_disabled_or_window_zero(self, monkeypatch):
        monkeypatch.setattr(notifier, "NOTIFY_SUMMARY_ENABLED", False)
        monkeypatch.setattr(notifier, "NOTIFY_BATCH_WINDOW", 300)
        assert not _should_batch_notification("pull_result")
        monkeypatch.setattr(notifier, "NOTIFY_SUMMARY_ENABLED", True)
        monkeypatch.setattr(notifier, "NOTIFY_BATCH_WINDOW", 0)
        assert not _should_batch_notification("pull_result")


class TestSendNotificationDirect:
    def test_disabled_notifications_do_nothing(self, monkeypatch):
        monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
        calls = []
        monkeypatch.setattr(notifier, "notify_webhook", lambda p: calls.append(p))
        send_notification_direct("test", "t", "m")
        assert calls == []

    def test_throttling_suppresses_rapid_notifications(self, monkeypatch):
        monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
        monkeypatch.setattr(config, "NOTIFY_BACKEND", "webhook")
        monkeypatch.setattr(notifier, "NOTIFY_MAX_FREQUENCY", 600)
        calls = []
        monkeypatch.setattr(notifier, "notify_webhook", lambda p: calls.append(p))

        send_notification_direct("test", "first", "m")
        send_notification_direct("test", "second", "m")
        assert len(calls) == 1

    def test_unsupported_backend_raises(self, monkeypatch):
        monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
        monkeypatch.setattr(config, "NOTIFY_BACKEND", "carrier-pigeon")
        with pytest.raises(RuntimeError, match="Unsupported NOTIFY_BACKEND"):
            send_notification_direct("test", "t", "m")


class TestSendNotification:
    def test_disabled_returns_without_sending(self, monkeypatch):
        monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
        calls = []
        monkeypatch.setattr(notifier, "notify_webhook", lambda p: calls.append(p))
        send_notification("pull_result", "t", "m")
        assert calls == []

    def test_batchable_events_are_queued_not_sent(self, monkeypatch):
        monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
        monkeypatch.setattr(config, "NOTIFY_BACKEND", "webhook")
        monkeypatch.setattr(notifier, "NOTIFY_SUMMARY_ENABLED", True)
        monkeypatch.setattr(notifier, "NOTIFY_BATCH_WINDOW", 3600)
        calls = []
        monkeypatch.setattr(notifier, "notify_webhook", lambda p: calls.append(p))

        send_notification("pull_result", "t", "m", status="success")
        assert calls == []  # queued, not delivered
        assert "pull_result" in notifier._pending_notifications
        # cancel the pending timer thread so it does not fire during the run
        if notifier._batch_timer is not None:
            notifier._batch_timer.cancel()
            with notifier._batch_lock:
                notifier._batch_timer = None


class TestTriggerGating:
    def test_updates_found_notifies_for_available_updates(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "send_notification", lambda *a, **k: sent.append(a))
        monkeypatch.setattr(config, "NOTIFY_ON_UPDATES_FOUND", True)
        from notifier import notify_updates_found

        notify_updates_found({
            "redis:7": {"status": "update_available", "image": "redis:7", "stacks": ["media"]},
            "nginx:1": {"status": "up_to_date", "image": "nginx:1", "stacks": []},
        })
        assert len(sent) == 1

    def test_updates_found_silent_when_no_updates(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "send_notification", lambda *a, **k: sent.append(a))
        monkeypatch.setattr(config, "NOTIFY_ON_UPDATES_FOUND", True)
        from notifier import notify_updates_found

        notify_updates_found({"nginx:1": {"status": "up_to_date", "image": "nginx:1", "stacks": []}})
        assert sent == []

    def test_pull_result_success_gated(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "send_notification", lambda *a, **k: sent.append(a))
        monkeypatch.setattr(config, "NOTIFY_ON_PULL_SUCCESS", False)
        notify_pull_result("redis:7", ok=True, message="pulled")
        assert sent == []
        monkeypatch.setattr(config, "NOTIFY_ON_PULL_SUCCESS", True)
        notify_pull_result("redis:7", ok=True, message="pulled")
        assert len(sent) == 1

    def test_pull_result_error_gated(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "send_notification", lambda *a, **k: sent.append(a))
        monkeypatch.setattr(config, "NOTIFY_ON_PULL_ERROR", False)
        notify_pull_result("redis:7", ok=False, message="boom")
        assert sent == []
        monkeypatch.setattr(config, "NOTIFY_ON_PULL_ERROR", True)
        notify_pull_result("redis:7", ok=False, message="boom")
        assert len(sent) == 1


class TestWebhookBackend:
    def test_missing_url_raises(self, monkeypatch):
        monkeypatch.setattr(notifier, "NOTIFY_WEBHOOK_URL", "")
        with pytest.raises(RuntimeError, match="not configured"):
            notify_webhook({"a": 1})

    def test_post_json_payload(self, requests_mock, monkeypatch):
        monkeypatch.setattr(notifier, "NOTIFY_WEBHOOK_URL", "http://hooks.local/hook")
        monkeypatch.setattr(notifier, "NOTIFY_WEBHOOK_METHOD", "POST")
        mock = requests_mock.post("http://hooks.local/hook", json={"ok": True})
        notify_webhook({"event_type": "test"})
        assert mock.call_count == 1
        assert mock.last_request.json() == {"event_type": "test"}

    def test_put_method_used_when_configured(self, requests_mock, monkeypatch):
        monkeypatch.setattr(notifier, "NOTIFY_WEBHOOK_URL", "http://hooks.local/hook")
        monkeypatch.setattr(notifier, "NOTIFY_WEBHOOK_METHOD", "PUT")
        mock = requests_mock.put("http://hooks.local/hook", json={"ok": True})
        notify_webhook({})
        assert mock.call_count == 1

    def test_http_error_raises(self, requests_mock, monkeypatch):
        import requests

        monkeypatch.setattr(notifier, "NOTIFY_WEBHOOK_URL", "http://hooks.local/hook")
        monkeypatch.setattr(notifier, "NOTIFY_WEBHOOK_METHOD", "POST")
        requests_mock.post("http://hooks.local/hook", status_code=500)
        with pytest.raises(requests.exceptions.HTTPError):
            notify_webhook({})


class TestMqttEmailBackends:
    def test_mqtt_requires_host_and_topic(self, monkeypatch):
        monkeypatch.setattr(notifier, "NOTIFY_MQTT_HOST", "")
        monkeypatch.setattr(notifier, "NOTIFY_MQTT_TOPIC", "")
        with pytest.raises(RuntimeError, match="MQTT"):
            notify_mqtt({})

    def test_email_requires_settings(self, monkeypatch):
        monkeypatch.setattr(notifier, "NOTIFY_EMAIL_HOST", "")
        monkeypatch.setattr(notifier, "NOTIFY_EMAIL_FROM", "")
        monkeypatch.setattr(notifier, "NOTIFY_EMAIL_TO", "")
        with pytest.raises(RuntimeError, match="incomplete"):
            notify_email({})
