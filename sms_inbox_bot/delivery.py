import re
import sqlite3
from pathlib import Path
from threading import Event

from .clients import Router, Sms, Telegram


INBOX_LIMIT = 25
TELEGRAM_TEXT_LIMIT = 4096
CONTINUATION = "SMS (продолжение)\n"
CODE_LABEL = r"\b(?:код|code|otp|pin)\b"
CODE_NUMBER = r"(?<![\w+])(?<![0-9][.\-–—])([0-9]{4,8})(?!\w|[.\-–—][0-9])"
CODE_AFTER_LABEL = re.compile(
    rf"{CODE_LABEL}(?:[ \t]+(?:для[ \t]+)?(?:подтверждения|авторизации|входа|доступа|проверки|is))?"
    rf"[ \t]*[:=–—-]?[ \t]*(?:\r?\n[ \t]*)?{CODE_NUMBER}",
    re.IGNORECASE,
)
CODE_BEFORE_LABEL = re.compile(
    rf"{CODE_NUMBER}[ \t]*[:=–—-]?[ \t]*(?:(?:это|ваш|your)[ \t]+){{0,2}}{CODE_LABEL}",
    re.IGNORECASE,
)


def extract_code(text: str) -> str | None:
    if re.fullmatch("[0-9]{4,8}", text.strip()):
        return text.strip()
    candidates = set(CODE_AFTER_LABEL.findall(text)) | set(CODE_BEFORE_LABEL.findall(text))
    return candidates.pop() if len(candidates) == 1 else None


def sms_header_entities(part: str) -> list[dict]:
    title, _, tail = part.partition("\n")
    sender, _, _ = tail.partition("\n")
    title_length = len(title.encode("utf-16-le")) // 2
    entities = [{"type": "bold", "offset": 0, "length": title_length}]
    if sender.startswith("Отправитель: "):
        entities.append({
            "type": "bold",
            "offset": title_length + 1,
            "length": len(sender.encode("utf-16-le")) // 2,
        })
    return entities


def sms_parts(sms: Sms) -> list[str]:
    text = f"SMS · {sms.service}\nОтправитель: {sms.sender}\nПолучено: {sms.received_at}\n\n{sms.text}"
    parts = []
    start = 0
    units = 0
    prefix = ""
    for position, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > TELEGRAM_TEXT_LIMIT:
            parts.append(prefix + text[start:position])
            start = position
            prefix = CONTINUATION
            units = len(prefix)
        units += width
    parts.append(prefix + text[start:])
    return parts


class DeliveryReceipts:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS receipts "
                "(message_id TEXT PRIMARY KEY, sent_parts INTEGER NOT NULL)"
            )

    def sent_parts(self, message_id: str) -> int:
        row = self.db.execute(
            "SELECT sent_parts FROM receipts WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row[0] if row else 0

    def record_part(self, message_id: str, count: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO receipts VALUES (?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET sent_parts = excluded.sent_parts",
                (message_id, count),
            )

    def remove(self, message_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM receipts WHERE message_id = ?", (message_id,))

    def prune(self, pending: set[str]) -> None:
        rows = self.db.execute("SELECT message_id FROM receipts").fetchall()
        obsolete = [(message_id,) for (message_id,) in rows if message_id not in pending]
        with self.db:
            self.db.executemany("DELETE FROM receipts WHERE message_id = ?", obsolete)

    def close(self) -> None:
        self.db.close()


class Delivery:
    def __init__(
        self, router: Router, telegram: Telegram, recipient_id: int, receipts: DeliveryReceipts
    ) -> None:
        self.router = router
        self.telegram = telegram
        self.recipient_id = recipient_id
        self.receipts = receipts

    def poll(self, stop: Event) -> bool:
        messages = self.router.inbox(INBOX_LIMIT)
        if len(messages) < INBOX_LIMIT:
            # A short page includes all pending items, including after a lost DELETE response.
            self.receipts.prune({sms.id for sms in messages})
        for sms in messages:
            parts = sms_parts(sms)
            sent = self.receipts.sent_parts(sms.id)
            code = extract_code(sms.text)
            for part_index in range(sent, len(parts)):
                if stop.is_set():
                    return False
                part = parts[part_index]
                self.telegram.send_sms(
                    self.recipient_id,
                    part,
                    entities=sms_header_entities(part) if part_index == 0 else None,
                    copy_code=code if part_index == len(parts) - 1 else None,
                )
                self.receipts.record_part(sms.id, part_index + 1)
            self.router.acknowledge(sms.id)
            self.receipts.remove(sms.id)
        return len(messages) >= INBOX_LIMIT
