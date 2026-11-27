import http.client
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ApiError(Exception):
    def __init__(
        self, operation: str, reason: str, *, retry_after: int = 0, retryable: bool = True
    ) -> None:
        super().__init__(f"{operation}: {reason}")
        self.retry_after = retry_after
        self.retryable = retryable


def retryable_status(code: int) -> bool:
    return code in {408, 429} or code >= 500


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(
    operation: str,
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict | None = None,
    timeout: int = 10,
) -> tuple[int, object]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            contents = response.read()
    except (URLError, OSError, http.client.HTTPException):
        raise ApiError(operation, "connection failed") from None

    if not contents:
        return status, None
    try:
        return status, json.loads(contents)
    except ValueError:
        if 200 <= status < 300:
            raise ApiError(operation, "invalid JSON response") from None
        return status, None


@dataclass(frozen=True)
class Stream:
    id: str
    service: str
    enabled: bool


@dataclass(frozen=True)
class Sms:
    id: str
    service: str
    sender: str
    text: str
    received_at: str


def read_stream(value: dict) -> Stream:
    stream = Stream(value["id"], value["service"], value["enabled"])
    if not all(isinstance(field, str) for field in (stream.id, stream.service)):
        raise TypeError
    if type(stream.enabled) is not bool:
        raise TypeError
    return stream


class Router:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = f"{base_url.rstrip('/')}/internal/v1/app"
        self.token = token

    def _request(
        self, method: str, path: str, payload: dict | None = None, *, expected: int = 200
    ) -> object:
        collection = path[1:].partition("/")[0].partition("?")[0]
        operation = f"Router {method} {collection}"
        status, response = request_json(
            operation, method, self.base_url + path, token=self.token, payload=payload
        )
        if status != expected:
            raise ApiError(operation, f"HTTP {status}")
        return response

    def streams(self) -> list[Stream]:
        response = self._request("GET", "/streams")
        try:
            return [read_stream(value) for value in response["streams"]]
        except (KeyError, TypeError):
            raise ApiError("Router streams", "invalid stream list") from None

    def subscribe(self, stream_id: str, enabled: bool) -> Stream:
        response = self._request(
            "PUT", f"/streams/{quote(stream_id, safe='')}/subscription",
            {"enabled": enabled},
        )
        try:
            stream = read_stream(response)
        except (KeyError, TypeError):
            raise ApiError("Router subscription", "invalid stream") from None
        if stream.id != stream_id or stream.enabled != enabled:
            raise ApiError("Router subscription", "unexpected stream state")
        return stream

    def inbox(self, limit: int) -> list[Sms]:
        response = self._request("GET", f"/inbox?{urlencode({'limit': limit})}")
        try:
            messages = []
            for item in response["messages"]:
                fields = [item[key] for key in ("id", "service", "sender", "text", "received_at")]
                if not all(isinstance(field, str) for field in fields):
                    raise TypeError
                messages.append(Sms(*fields))
            return messages
        except (KeyError, TypeError):
            raise ApiError("Router inbox", "invalid message list") from None

    def acknowledge(self, message_id: str) -> None:
        self._request("DELETE", f"/inbox/{quote(message_id, safe='')}", expected=204)


class Telegram:
    def __init__(
        self, token: str, base_url: str = "https://api.telegram.org"
    ) -> None:
        self.base_url = f"{base_url.rstrip('/')}/bot{token}"

    def call(self, method: str, *, request_timeout: int = 10, **payload) -> object:
        operation = f"Telegram {method}"
        status, response = request_json(
            operation, "POST", f"{self.base_url}/{method}",
            payload=payload, timeout=request_timeout,
        )
        if not isinstance(response, dict):
            raise ApiError(
                operation, f"unexpected response (HTTP {status})",
                retryable=status == 200 or retryable_status(status),
            )
        if status == 200 and response.get("ok") is True and "result" in response:
            return response["result"]

        code = response.get("error_code")
        if type(code) is not int:
            code = status
        description = response.get("description")
        if (
            method == "editMessageText"
            and code == 400
            and isinstance(description, str)
            and "message is not modified" in description.lower()
        ):
            return None
        parameters = response.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else 0
        raise ApiError(
            operation, f"API error {code}",
            retry_after=retry_after if type(retry_after) is int and retry_after > 0 else 0,
            retryable=retryable_status(code) or retryable_status(status),
        )

    def send_sms(
        self, recipient_id: int, text: str, *,
        entities: list[dict] | None = None, copy_code: str | None = None,
    ) -> None:
        options = {}
        if entities:
            options["entities"] = entities
        if copy_code:
            options["reply_markup"] = {
                "inline_keyboard": [[copy_button("Копировать код", copy_code)]],
            }
        result = self.call(
            "sendMessage",
            chat_id=recipient_id,
            text=text,
            link_preview_options={"is_disabled": True},
            protect_content=True,
            **options,
        )
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise ApiError("Telegram sendMessage", "missing message confirmation")


def copy_button(label: str, text: str) -> dict:
    return {"text": label, "copy_text": {"text": text}}
