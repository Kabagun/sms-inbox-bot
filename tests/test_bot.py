import json
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread, Timer
from unittest.mock import Mock, patch

from sms_inbox_bot.__main__ import Settings, main, poll_until_stopped, serve
from sms_inbox_bot.clients import ApiError, Router, Sms, Stream, Telegram
from sms_inbox_bot.delivery import CONTINUATION, Delivery, DeliveryReceipts, sms_parts
from sms_inbox_bot.menu import HELP, SUBSCRIPTIONS, Menu, stream_key


RECIPIENT = 123456
ROUTER_TOKEN = "synthetic-router-token"
TELEGRAM_TOKEN = "12345:synthetic-token"
APP = "/internal/v1/app"
STUB_BOT = f"/bot{TELEGRAM_TOKEN}"
RECEIVED_AT = "2026-01-01T12:00:00Z"


@contextmanager
def fake_api(respond):
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            size = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(size)) if size else None
            status, response = respond(
                self.command, self.path, self.headers.get("Authorization"), payload
            )
            body = json.dumps(response).encode() if response is not None else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_DELETE = handle_request

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.work = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "SMS_ROUTER_APP_TOKEN": ROUTER_TOKEN,
            "TELEGRAM_RECIPIENT_ID": str(RECIPIENT),
            "SMS_INBOX_STATE_DIR": self.work,
        }, clear=True))

    def test_configuration_uses_loopback_and_hides_tokens(self):
        settings = Settings.load()
        self.assertEqual(settings.router_url, "http://127.0.0.1:18090")
        self.assertEqual(settings.recipient_id, RECIPIENT)
        self.assertNotIn(TELEGRAM_TOKEN, repr(settings))
        self.assertNotIn(ROUTER_TOKEN, repr(settings))
        with patch.dict(os.environ, {"TELEGRAM_RECIPIENT_ID": "-12345"}):
            with self.assertRaisesRegex(ValueError, "positive numeric Telegram user ID"):
                Settings.load()

    def test_running_worker_locks_its_state_directory(self):
        lock_path = Path(self.work) / "worker.sqlite3"
        with closing(sqlite3.connect(lock_path)) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            with (
                patch("sms_inbox_bot.__main__.signal.signal"),
                patch("sms_inbox_bot.__main__.os.umask"),
                patch("sms_inbox_bot.__main__.serve") as run,
                self.assertLogs("sms_inbox_bot", logging.ERROR) as logs,
            ):
                self.assertEqual(main(), 1)
            run.assert_not_called()
            self.assertIn("Check for an existing worker", logs.output[0])


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.router = Mock(spec=Router)
        self.telegram = Mock(spec=Telegram)
        self.menu = Menu(self.router, self.telegram, RECIPIENT)
        self.chat = {"type": "private", "id": RECIPIENT}
        self.user = {"id": RECIPIENT}
        self.stream = Stream("example/stream-" + "a" * 80, "example", False)
        self.router.streams.return_value = [self.stream]

    def message(self, text, *, chat=None, user=None):
        self.menu.handle({"message": {
            "chat": self.chat if chat is None else chat,
            "from": self.user if user is None else user,
            "text": text,
        }})

    def callback(self, data, *, chat=None, user=None):
        self.menu.handle({"callback_query": {
            "id": "synthetic-query",
            "message": {"message_id": 42, "chat": self.chat if chat is None else chat},
            "from": self.user if user is None else user,
            "data": data,
        }})

    def test_private_recipient_is_required_for_messages_and_callbacks(self):
        data = f"sub:1:{stream_key(self.stream.id)}"
        for chat, user in (
            (self.chat, {"id": 999}),
            ({"type": "group", "id": RECIPIENT}, self.user),
            ({"type": "private", "id": 999}, self.user),
        ):
            with self.subTest(chat=chat, user=user):
                self.message("/start", chat=chat, user=user)
                self.message(SUBSCRIPTIONS, chat=chat, user=user)
                self.message(HELP, chat=chat, user=user)
                self.callback(data, chat=chat, user=user)
        self.assertEqual(self.router.mock_calls, [])
        self.assertEqual(self.telegram.mock_calls, [])

    def test_start_shows_main_keyboard_and_toggle_edits_original_message(self):
        self.message("/start")
        keyboard = self.telegram.call.call_args.kwargs["reply_markup"]
        self.assertEqual(keyboard["keyboard"], [[{"text": SUBSCRIPTIONS}, {"text": HELP}]])
        self.assertNotIn("is_persistent", keyboard)

        self.message(SUBSCRIPTIONS)
        args = self.telegram.call.call_args.kwargs
        data = args["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(data.encode()), 64)
        self.router.subscribe.return_value = Stream(self.stream.id, "example", True)
        self.callback(data)
        self.router.subscribe.assert_called_once_with(self.stream.id, True)
        edit, answer = self.telegram.call.call_args_list[-2:]
        self.assertEqual(edit.args, ("editMessageText",))
        self.assertEqual(edit.kwargs["message_id"], 42)
        self.assertEqual(edit.kwargs["chat_id"], RECIPIENT)
        enabled = edit.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertIn("Включено", enabled["text"])
        self.assertTrue(enabled["callback_data"].startswith("sub:0:"))
        self.assertEqual(answer.args, ("answerCallbackQuery",))

        self.router.streams.return_value = [self.router.subscribe.return_value]
        self.callback(data)
        self.router.subscribe.assert_called_once()
        self.router.subscribe.reset_mock()
        self.router.subscribe.return_value = self.stream
        self.callback(enabled["callback_data"])
        self.router.subscribe.assert_called_once_with(self.stream.id, False)

    def test_help_sends_only_subscription_instructions(self):
        self.message(HELP)

        self.router.streams.assert_not_called()
        self.telegram.call.assert_called_once_with(
            "sendMessage",
            chat_id=RECIPIENT,
            text=(
                "В «Подписках» включите нужные потоки. Новые SMS из них "
                "будут приходить сюда целиком."
            ),
        )

    def test_revoked_stream_is_removed_without_a_subscription_request(self):
        self.router.streams.return_value = []
        self.callback(f"sub:1:{stream_key(self.stream.id)}")
        self.router.subscribe.assert_not_called()
        edit = self.telegram.call.call_args_list[-2]
        self.assertEqual(edit.kwargs["reply_markup"], {"inline_keyboard": []})


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / "receipts.sqlite3"
        self.router = Mock(spec=Router)
        self.telegram = Mock(spec=Telegram)

    def poll(self):
        with closing(DeliveryReceipts(self.path)) as receipts:
            Delivery(self.router, self.telegram, RECIPIENT, receipts).poll(Event())

    def test_partial_message_resumes_and_ack_retries_across_restarts(self):
        text = ("Demo & <text> 🧪\n" * 850) + "Synthetic ending"
        sms = Sms("item-1", "example", "DEMO", text, RECEIVED_AT)
        parts = sms_parts(sms)
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts))
        self.assertEqual(
            parts[0].split("\n\n", 1)[1]
            + "".join(part.removeprefix(CONTINUATION) for part in parts[1:]),
            text,
        )
        self.router.inbox.return_value = [sms]
        self.telegram.send_sms.side_effect = [None, ApiError("Telegram", "connection failed")]
        with self.assertRaises(ApiError):
            self.poll()
        self.router.acknowledge.assert_not_called()

        self.telegram.send_sms.side_effect = None
        self.router.acknowledge.side_effect = ApiError("Router", "HTTP 503")
        with self.assertRaises(ApiError):
            self.poll()
        self.assertEqual(
            [call.args for call in self.telegram.send_sms.call_args_list],
            [(RECIPIENT, parts[0]), (RECIPIENT, parts[1])]
            + [(RECIPIENT, part) for part in parts[1:]],
        )

        self.router.acknowledge.side_effect = None
        self.telegram.reset_mock()
        self.poll()
        self.telegram.send_sms.assert_not_called()
        self.assertEqual(self.router.acknowledge.call_count, 2)

    def test_unconfirmed_telegram_response_never_acknowledges(self):
        def respond(_method, _path, _auth, _payload):
            return 200, {"ok": True, "result": {}}

        self.router.inbox.return_value = [Sms("item-2", "example", "DEMO", "Example", RECEIVED_AT)]
        with fake_api(respond) as base:
            self.telegram = Telegram(TELEGRAM_TOKEN, base)
            with self.assertRaisesRegex(ApiError, "missing message confirmation"):
                self.poll()
        self.router.acknowledge.assert_not_called()


class HttpTests(unittest.TestCase):
    def test_route_paths_and_bearer_token(self):
        stream = {"id": "test/stream", "service": "example", "enabled": False}
        calls = []

        def respond(method, path, auth, payload):
            calls.append((method, path, auth, payload))
            responses = {
                ("GET", f"{APP}/streams"): (200, {"streams": [stream]}),
                ("PUT", f"{APP}/streams/test%2Fstream/subscription"): (
                    200, {**stream, "enabled": True},
                ),
                ("DELETE", f"{APP}/inbox/test%2Fmessage"): (204, None),
            }
            return responses.get((method, path), (404, None))

        with fake_api(respond) as base:
            router = Router(base, ROUTER_TOKEN)
            self.assertEqual(router.streams(), [Stream("test/stream", "example", False)])
            self.assertTrue(router.subscribe("test/stream", True).enabled)
            router.acknowledge("test/message")
        self.assertEqual([call[2] for call in calls], [f"Bearer {ROUTER_TOKEN}"] * 3)
        self.assertEqual(calls[1][3], {"enabled": True})

    def test_rate_limit_delay_is_honored_without_logging_api_response(self):
        def respond(_method, _path, _auth, _payload):
            return 429, {
                "ok": False, "error_code": 429, "parameters": {"retry_after": 13},
                "description": "Synthetic string that should not be logged",
            }

        with fake_api(respond) as base:
            telegram = Telegram(TELEGRAM_TOKEN, base)
            stop = Mock(spec=Event)
            stop.is_set.side_effect = [False, True]
            with self.assertLogs("sms_inbox_bot", logging.WARNING) as logs:
                poll_until_stopped(
                    stop, lambda: telegram.send_sms(RECIPIENT, "Synthetic SMS"), 0
                )
        stop.wait.assert_called_once_with(13)
        self.assertNotIn("Synthetic", logs.output[0])
        self.assertNotIn(TELEGRAM_TOKEN, logs.output[0])

    def test_service_runs_menu_and_delivery_over_http(self):
        stop = Event()
        calls = []
        completed = set()
        sms = {
            "id": "sample-message",
            "stream_id": "sample-stream",
            "service": "example",
            "sender": "DEMO",
            "text": "A synthetic SMS with markup <demo> and emoji 🧪.",
            "received_at": RECEIVED_AT,
        }

        def respond(method, path, auth, payload):
            calls.append((method, path, auth, payload))
            if path == f"{STUB_BOT}/getUpdates":
                if "menu" not in completed:
                    chat = {"id": RECIPIENT, "type": "private"}
                    user = {"id": RECIPIENT}
                    updates = [
                        {
                            "update_id": 1,
                            "callback_query": {
                                "id": "expired-query", "message": {"chat": chat},
                                "from": user, "data": "expired-data",
                            },
                        },
                        {
                            "update_id": 2,
                            "message": {"text": "/start", "from": user, "chat": chat},
                        },
                    ]
                else:
                    updates = []
                return 200, {"ok": True, "result": updates}
            if path == f"{STUB_BOT}/answerCallbackQuery":
                return 400, {"ok": False, "error_code": 400}
            if path == f"{STUB_BOT}/sendMessage":
                if "reply_markup" in payload:
                    completed.add("menu")
                if completed == {"menu", "delivery"}:
                    stop.set()
                return 200, {"ok": True, "result": {"message_id": 2}}
            if method == "GET" and path == f"{APP}/inbox?limit=25":
                return 200, {"messages": [] if "delivery" in completed else [sms]}
            if method == "DELETE" and path == f"{APP}/inbox/sample-message":
                completed.add("delivery")
                if completed == {"menu", "delivery"}:
                    stop.set()
                return 204, None
            return 404, None

        with fake_api(respond) as base, tempfile.TemporaryDirectory() as work:
            settings = Settings(TELEGRAM_TOKEN, ROUTER_TOKEN, RECIPIENT, base, Path(work))
            with patch("sms_inbox_bot.__main__.Telegram", return_value=Telegram(TELEGRAM_TOKEN, base)):
                timeout = Timer(5, stop.set)
                timeout.start()
                try:
                    with self.assertLogs("sms_inbox_bot", logging.WARNING) as logs:
                        serve(settings, stop)
                finally:
                    timeout.cancel()
                    timeout.join()

        self.assertEqual(completed, {"menu", "delivery"})
        self.assertIn("Skipping the Telegram update", logs.output[0])
        router_calls = [call for call in calls if call[1].startswith(APP)]
        self.assertTrue(all(call[2] == f"Bearer {ROUTER_TOKEN}" for call in router_calls))
        updates = [call[3] for call in calls if call[1] == f"{STUB_BOT}/getUpdates"]
        self.assertEqual(updates[0]["timeout"], 20)
        sends = [
            (index, payload) for index, (_, path, _, payload) in enumerate(calls)
            if path == f"{STUB_BOT}/sendMessage" and "reply_markup" not in payload
        ]
        self.assertEqual(len(sends), 1)
        index, payload = sends[0]
        self.assertIn(sms["text"], payload["text"])
        self.assertEqual(payload["chat_id"], RECIPIENT)
        self.assertNotIn("parse_mode", payload)
        self.assertLess(index, next(i for i, call in enumerate(calls) if call[0] == "DELETE"))


if __name__ == "__main__":
    unittest.main()
