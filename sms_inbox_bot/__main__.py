import logging
import os
import re
import signal
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.parse import urlsplit

from .clients import ApiError, Router, Telegram
from .delivery import Delivery, DeliveryReceipts
from .menu import Menu


LOG = logging.getLogger("sms_inbox_bot")
UPDATE_TIMEOUT = 20
INBOX_INTERVAL = 2


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(repr=False)
    router_token: str = field(repr=False)
    recipient_id: int
    router_url: str
    state_dir: Path
    router_public_origin: str | None = None
    router_device_url: str | None = field(default=None, repr=False)
    help_service: str = "example"
    help_sender: str = "EXAMPLE"

    @classmethod
    def load(cls) -> "Settings":
        telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not re.fullmatch(r"[1-9][0-9]*:[a-zA-Z0-9_-]+", telegram_token):
            raise ValueError("Set TELEGRAM_BOT_TOKEN to a Telegram bot token.")
        router_token = os.environ.get("SMS_ROUTER_APP_TOKEN", "")
        if not re.fullmatch(r"[!-~]+", router_token):
            raise ValueError("Set SMS_ROUTER_APP_TOKEN to a router app token.")
        recipient = os.environ.get("TELEGRAM_RECIPIENT_ID", "")
        if not re.fullmatch(r"[1-9][0-9]{0,15}", recipient) or int(recipient) >= 2**52:
            raise ValueError("Set TELEGRAM_RECIPIENT_ID to a positive numeric Telegram user ID.")

        router_url = os.environ.get("SMS_ROUTER_BASE_URL", "http://127.0.0.1:18090")
        try:
            url = urlsplit(router_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname or url.port == 0
                or url.username is not None or url.query or url.fragment
                or any(character.isspace() for character in router_url)
            ):
                raise ValueError
        except ValueError:
            raise ValueError("Set SMS_ROUTER_BASE_URL to a valid HTTP(S) base URL.") from None
        public_origin = os.environ.get("SMS_ROUTER_PUBLIC_ORIGIN", "").removesuffix("/")
        if public_origin:
            try:
                public_url = urlsplit(public_origin)
                if (
                    public_url.scheme != "https" or not public_url.hostname
                    or public_url.port == 0 or public_url.username is not None
                    or public_url.path or public_url.query or public_url.fragment
                    or len(public_origin) > 255
                    or re.search(r"[\s?#<>\\]", public_origin)
                ):
                    raise ValueError
            except ValueError:
                raise ValueError("Set SMS_ROUTER_PUBLIC_ORIGIN to a public HTTPS origin.") from None
        device_url = os.environ.get("SMS_ROUTER_DEVICE_URL", "")
        if device_url:
            try:
                device = urlsplit(device_url)
                if (
                    device.scheme != "https" or not device.hostname or device.port == 0
                    or device.username is not None or device.query or device.fragment
                    or not re.fullmatch(r"/v1/sms/[A-Za-z0-9_-]{43}", device.path)
                    or len(device_url) > 256
                    or re.search(r"[\s?#<>\\]", device_url)
                ):
                    raise ValueError
            except ValueError:
                raise ValueError("Set SMS_ROUTER_DEVICE_URL to a personal HTTPS ingestion URL.") from None
        help_service = os.environ.get("SMS_ROUTER_HELP_SERVICE", "example")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", help_service):
            raise ValueError("Set SMS_ROUTER_HELP_SERVICE to the registered service name.")
        help_sender = os.environ.get("SMS_ROUTER_HELP_SENDER", "EXAMPLE")
        if (
            not 1 <= len(help_sender) <= 128 or not help_sender.strip()
            or not help_sender.isprintable()
        ):
            raise ValueError("Set SMS_ROUTER_HELP_SENDER to the registered sender.")
        return cls(
            telegram_token, router_token, int(recipient), router_url,
            Path(os.environ.get("SMS_INBOX_STATE_DIR", "state")),
            public_origin or None,
            device_url or None,
            help_service,
            help_sender,
        )


def poll_until_stopped(
    stop: Event, operation: Callable[[], bool], idle_seconds: float
) -> None:
    backoff = 1
    while not stop.is_set():
        try:
            more_work = operation()
        except ApiError as error:
            delay = max(backoff, error.retry_after)
            LOG.warning("%s. Retrying in %s seconds.", error, delay)
            stop.wait(delay)
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1
            if not more_work:
                stop.wait(idle_seconds)


def serve(settings: Settings, stop: Event) -> None:
    router = Router(settings.router_url, settings.router_token)
    telegram = Telegram(settings.telegram_token)
    menu = Menu(
        router, telegram, settings.recipient_id,
        router_public_origin=settings.router_public_origin,
        device_url=settings.router_device_url,
        help_service=settings.help_service,
        help_sender=settings.help_sender,
    )
    offset = None

    def poll_updates() -> bool:
        nonlocal offset
        options = {"offset": offset} if offset is not None else {}
        updates = telegram.call(
            "getUpdates", request_timeout=UPDATE_TIMEOUT + 5, **options,
            allowed_updates=["message", "callback_query"],
            timeout=UPDATE_TIMEOUT,
        )
        if not isinstance(updates, list):
            raise ApiError("Telegram getUpdates", "invalid update list")
        for update in updates:
            if stop.is_set():
                break
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                raise ApiError("Telegram getUpdates", "invalid update")
            try:
                menu.handle(update)
            except ApiError as error:
                if error.retryable:
                    raise
                LOG.warning("%s. Skipping the Telegram update.", error)
            offset = update["update_id"] + 1
        return bool(updates)

    def run_delivery() -> None:
        with closing(DeliveryReceipts(settings.state_dir / "receipts.sqlite3")) as receipts:
            delivery = Delivery(router, telegram, settings.recipient_id, receipts)
            poll_until_stopped(stop, lambda: delivery.poll(stop), idle_seconds=INBOX_INTERVAL)

    with ThreadPoolExecutor(max_workers=1) as workers:
        worker = workers.submit(run_delivery)
        worker.add_done_callback(lambda _: stop.set())
        try:
            poll_until_stopped(stop, poll_updates, idle_seconds=0.1)
        finally:
            stop.set()
        worker.result()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        settings = Settings.load()
    except ValueError as error:
        LOG.error("%s", error)
        return 2

    stop = Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    os.umask(0o077)
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(settings.state_dir / "worker.sqlite3", timeout=0)) as lock:
            try:
                lock.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError:
                LOG.error("Could not lock the state directory. Check for an existing worker.")
                return 1
            LOG.info("SMS inbox bot started.")
            serve(settings, stop)
    except Exception as error:
        LOG.error("SMS inbox bot stopped with an unexpected %s.", type(error).__name__)
        return 1
    LOG.info("SMS inbox bot stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
