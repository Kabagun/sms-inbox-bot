# SMS Inbox Bot

SMS Inbox Bot delivers incoming messages from SMS Router to one private Telegram chat. The **Подписки** button lists the streams granted to the bot's router app. Tap a stream to turn delivery on or off.

The **Помощь** button shows the JSON format for submitting messages and offers a shareable setup link.

## Send a message

Get a personal device URL and the registered stream name from the SMS Router stream owner. Use HTTPS and send a JSON request to the device URL. The hostname and token below are placeholders.

```http
POST https://sms.example.com/v1/sms/<device-token>
Content-Type: application/json
```

```json
{
  "service": "example",
  "sender": "EXAMPLE",
  "text": "Your verification code: 4837"
}
```

Use the exact registered `service` and put the full message in `text`. The `sender` field must match a registered sender. You can omit it when the stream has one sender, or omit `service` when the sender matches exactly one registered stream for this device. SMS Router also accepts optional `received_at` (timezone-qualified ISO 8601) and `device_id` (a deduplication input).

The stream must be shared with the bot's router app and enabled under **Подписки** to receive new messages in Telegram. Share the setup page or this guide. Send credential-bearing device URLs only to their designated users.

## Configure

Use Python 3.11 or newer. The bot uses the standard library and requires no package installation.

Create a Telegram bot, then open its private chat from the recipient account and tap **Start**. Register a dedicated app with SMS Router and grant it the streams this bot should offer. Supply the following settings through the process environment or an environment file outside the checkout.

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token for this Telegram bot |
| `TELEGRAM_RECIPIENT_ID` | Positive numeric Telegram user ID of the recipient |
| `SMS_ROUTER_APP_TOKEN` | Bearer token issued to the dedicated router app |
| `SMS_ROUTER_BASE_URL` | Router base URL, defaults to `http://127.0.0.1:18090`. Use HTTPS when connecting over an untrusted network. |
| `SMS_ROUTER_PUBLIC_ORIGIN` | Optional public HTTPS origin of SMS Router, used for the endpoint example and the shareable setup link. Uses this document's guide when unset. |
| `SMS_INBOX_STATE_DIR` | Writable directory for delivery receipts, defaults to `state/` in the working directory |

## Run

From the checkout, with the settings loaded into the environment, run:

```sh
python -m sms_inbox_bot
```

Run a single instance per Telegram bot and router app. Use a dedicated state directory for that pair and keep it across restarts. Telegram updates use long polling, so the bot token must have no webhook configured.

On a Linux host managed by systemd, point `WorkingDirectory` at the checkout and `ExecStart` at the host's Python 3.11 or newer with `-m sms_inbox_bot`. Use `EnvironmentFile` to load the credentials from a file readable only by the service manager, and set `Restart=on-failure`. Give the service account write access to its state directory.

## Delivery behavior

Only updates from the configured Telegram user in their private chat are handled. The router's bearer token controls which streams and inbox items the bot can access.

The bot sends the full SMS with the service and sender highlighted above the received time. When it recognizes one numeric code, **Копировать код** copies that code. Long messages are split into multiple Telegram messages, with the copy button attached to the last part. The bot acknowledges the inbox item after Telegram confirms every part. Local receipts store item IDs and progress without retaining SMS text. If delivery fails, the bot retries with backoff while the item is available under the router's retention policy. Telegram's rate-limit delay is honored.

If Telegram accepts a message and the response is lost or the process stops before saving the receipt, the next attempt may deliver that part again. Keep the router's inbox IDs stable for pending items.

## Verify locally

```sh
python -m unittest discover -s tests -v
```

The tests use loopback HTTP doubles for both APIs and synthetic SMS content and credentials. The router double covers the application endpoints for listing streams, updating subscriptions, reading the inbox, and acknowledging deliveries.
