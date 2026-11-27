from collections import Counter
from hashlib import sha256
from html import escape

from .clients import ApiError, Router, Stream, Telegram, copy_button


SUBSCRIPTIONS = "Подписки"
HELP = "Помощь"
PUBLIC_HELP_URL = "https://github.com/Kabagun/sms-inbox-bot#send-a-message"
MAIN_KEYBOARD = {
    "keyboard": [[{"text": SUBSCRIPTIONS}, {"text": HELP}]],
    "resize_keyboard": True,
}


def help_view(router_public_origin: str | None) -> dict:
    origin = router_public_origin or "https://sms.example.com"
    endpoint = escape(f"{origin}/v1/sms/<device-token>")
    guide = f"{router_public_origin}/" if router_public_origin else PUBLIC_HELP_URL
    example = '{"service":"example","sender":"EXAMPLE","text":"Ваш код: 4837"}'
    return {
        "text": (
            "<b>Как отправить сообщение</b>\n\n"
            f"<code>POST {endpoint}</code>\n"
            "<code>Content-Type: application/json</code>\n\n"
            f"<pre>{escape(example, quote=False)}</pre>\n\n"
            "<code>service</code> — имя зарегистрированного потока. "
            "<code>text</code> — полный текст сообщения. "
            "<code>sender</code> — зарегистрированный отправитель. "
            "Если у потока один отправитель, это поле можно опустить.\n\n"
            "Получите персональный адрес с токеном у владельца потока. "
            "Чтобы сообщения приходили сюда, включите поток в «Подписках»."
        ),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "Инструкция", "url": guide},
                copy_button("Копировать ссылку", guide),
            ]],
        },
    }


def stream_key(stream_id: str) -> str:
    return sha256(stream_id.encode()).hexdigest()[:32]


def subscription_view(streams: list[Stream]) -> dict:
    if not streams:
        return {"text": "Нет доступных подписок.", "reply_markup": {"inline_keyboard": []}}

    service_counts = Counter(stream.service for stream in streams)
    buttons = []
    for stream in streams:
        name = stream.service
        if service_counts[name] > 1:
            name = f"{name} ({stream.id})"
        status = "Включено" if stream.enabled else "Выключено"
        target = "0" if stream.enabled else "1"
        buttons.append([{
            "text": f"{name} · {status}",
            "callback_data": f"sub:{target}:{stream_key(stream.id)}",
        }])
    return {"text": SUBSCRIPTIONS, "reply_markup": {"inline_keyboard": buttons}}


class Menu:
    def __init__(
        self, router: Router, telegram: Telegram, recipient_id: int, *,
        router_public_origin: str | None = None,
    ) -> None:
        self.router = router
        self.telegram = telegram
        self.recipient_id = recipient_id
        self.router_public_origin = router_public_origin

    def _is_recipient(self, sender: dict, chat: dict) -> bool:
        return (
            chat.get("type") == "private"
            and chat.get("id") == self.recipient_id
            and sender.get("id") == self.recipient_id
        )

    def handle(self, update: dict) -> None:
        if "message" in update:
            self._handle_message(update["message"])
        elif "callback_query" in update:
            self._handle_callback(update["callback_query"])

    def _handle_message(self, message: dict) -> None:
        if not self._is_recipient(message.get("from", {}), message["chat"]):
            return
        text = message.get("text", "")
        if text.partition(" ")[0] == "/start":
            self.telegram.call(
                "sendMessage", chat_id=self.recipient_id,
                text="В «Подписках» можно выбрать, какие SMS получать здесь.",
                reply_markup=MAIN_KEYBOARD,
            )
        elif text == SUBSCRIPTIONS:
            try:
                view = subscription_view(self.router.streams())
            except ApiError:
                view = {"text": "Подписки сейчас недоступны. Попробуйте позже."}
            self.telegram.call("sendMessage", chat_id=self.recipient_id, **view)
        elif text == HELP:
            self.telegram.call(
                "sendMessage", chat_id=self.recipient_id,
                **help_view(self.router_public_origin),
            )

    def _handle_callback(self, query: dict) -> None:
        message = query.get("message", {})
        if not self._is_recipient(query.get("from", {}), message.get("chat", {})):
            return
        query_id = query["id"]
        if message.get("date") == 0:
            self._answer(query_id, "Откройте «Подписки» снова.")
            return

        action, _, data = query.get("data", "").partition(":")
        target, _, key = data.partition(":")
        if action != "sub" or target not in {"0", "1"} or len(key) != 32:
            self._answer(query_id, "Откройте «Подписки» снова.")
            return
        try:
            streams = self.router.streams()
            for index, stream in enumerate(streams):
                if stream_key(stream.id) == key:
                    enabled = target == "1"
                    if stream.enabled != enabled:
                        streams[index] = self.router.subscribe(stream.id, enabled)
                    break
        except ApiError:
            self._answer(query_id, "Не удалось обновить подписку. Откройте список снова.")
            return

        self.telegram.call(
            "editMessageText", chat_id=self.recipient_id, message_id=message["message_id"],
            **subscription_view(streams),
        )
        self._answer(query_id)

    def _answer(self, query_id: str, text: str = "") -> None:
        self.telegram.call("answerCallbackQuery", callback_query_id=query_id, text=text)
