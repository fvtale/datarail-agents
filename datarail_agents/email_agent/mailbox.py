"""IMAP and SMTP against the IONOS mailbox.

Everything that touches the network lives here, so the agent logic in run.py
can be tested without a mail server.

Two things this module is careful about, because both are ways an autonomous
mail agent embarrasses you:

  Threading   -- replies carry In-Reply-To and References so they land in the
                 client's existing conversation rather than starting a new one.
  Idempotence -- handled mail is moved out of INBOX, so a rerun after a crash
                 cannot reply to the same message twice.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import smtplib
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import EmailMessage
from collections.abc import Iterator

from ..core.config import MailboxConfig

# IMAP responses are bytes; IONOS folder names are ASCII, so plain decoding is
# safe here.
_ENCODING = "utf-8"


def _decode(value) -> str:
    """Turn a possibly RFC 2047-encoded header into readable text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode(_ENCODING, errors="replace")
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - malformed headers are common in the wild
        return str(value)


@dataclass
class InboundMessage:
    uid: str
    message_id: str
    sender_name: str
    sender_email: str
    subject: str
    body: str
    date: str
    in_reply_to: str = ""
    references: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def thread_id(self) -> str:
        """A stable id for the conversation this message belongs to.

        The first entry in References is the root of the thread. Falling back
        through In-Reply-To to the message's own id means a first contact is
        its own thread root, which is what we want.
        """
        if self.references:
            first = self.references.split()
            if first:
                return first[0].strip()
        if self.in_reply_to:
            return self.in_reply_to.strip()
        return self.message_id


def _body_of(message) -> str:
    """Pull the best available plain text out of a parsed message.

    Prefers text/plain. Falls back to text/html with tags stripped, because a
    surprising number of real enquiries are sent as HTML only.
    """
    plain_parts = []
    html_parts = []

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if part.get_content_type() == "text/plain":
                plain_parts.append(text)
            elif part.get_content_type() == "text/html":
                html_parts.append(text)
    else:
        payload = message.get_payload(decode=True)
        if payload is not None:
            charset = message.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if message.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        return "\n".join(plain_parts).strip()
    if html_parts:
        return _strip_html("\n".join(html_parts)).strip()
    return ""


def _strip_html(html: str) -> str:
    """Crude tag removal. Good enough to read; never rendered anywhere."""
    import re

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&rsquo;": "'", "&mdash;": "--",
    }
    for entity, char in replacements.items():
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


class Mailbox:
    """A connected IMAP session plus the ability to send.

    Used as a context manager so the connection is always closed, including on
    the paths where the agent raises.
    """

    def __init__(self, config: MailboxConfig):
        self.config = config
        self._imap: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> Mailbox:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def connect(self) -> None:
        self._imap = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port)
        self._imap.login(self.config.username, self.config.password)
        self._ensure_folder(self.config.processed_folder)
        self._ensure_folder(self.config.ignored_folder)
        self._ensure_folder(self.config.review_folder)

    def close(self) -> None:
        if self._imap is None:
            return
        try:
            self._imap.close()
        except Exception:  # noqa: BLE001 - already closed or never selected
            pass
        try:
            self._imap.logout()
        except Exception:  # noqa: BLE001
            pass
        self._imap = None

    def _ensure_folder(self, folder: str) -> None:
        """Create a destination folder if it does not exist.

        IMAP servers disagree about the error for an existing folder, so the
        result is not checked -- a genuine failure surfaces later when the move
        is attempted, and a move failure is handled without losing mail.
        """
        try:
            self._imap.create(self._quote(folder))
        except Exception:  # noqa: BLE001
            pass
        try:
            self._imap.subscribe(self._quote(folder))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _quote(folder: str) -> str:
        return '"' + folder.replace('"', '\\"') + '"'

    def unread(self, limit: int = 50) -> Iterator[InboundMessage]:
        """Yield unseen messages from INBOX, oldest first.

        Fetched with BODY.PEEK so reading does not mark anything as seen. The
        agent decides what counts as handled; IMAP should not decide for it.
        """
        self._imap.select("INBOX")
        status, data = self._imap.uid("SEARCH", None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return

        uids = data[0].split()[:limit]
        for raw_uid in uids:
            uid = raw_uid.decode(_ENCODING)
            status, fetched = self._imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            raw_bytes = fetched[0][1]
            if not isinstance(raw_bytes, (bytes, bytearray)):
                continue
            parsed = email.message_from_bytes(bytes(raw_bytes))
            name, address = email.utils.parseaddr(_decode(parsed.get("From")))
            yield InboundMessage(
                uid=uid,
                message_id=_decode(parsed.get("Message-ID")).strip(),
                sender_name=name.strip(),
                sender_email=address.strip().lower(),
                subject=_decode(parsed.get("Subject")).strip(),
                body=_body_of(parsed),
                date=_decode(parsed.get("Date")).strip(),
                in_reply_to=_decode(parsed.get("In-Reply-To")).strip(),
                references=_decode(parsed.get("References")).strip(),
                headers={key.lower(): _decode(value) for key, value in parsed.items()},
            )

    def count_unread(self) -> int:
        """How many unread messages are waiting, without fetching any of them.

        Used by doctor, which wants to prove the connection works and not to
        download the inbox to do it.
        """
        self._imap.select("INBOX")
        status, data = self._imap.uid("SEARCH", None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return 0
        return len(data[0].split())

    def mark_seen(self, uid: str) -> None:
        self._imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")

    def move(self, uid: str, folder: str) -> bool:
        """Move a message out of INBOX. Returns whether it worked.

        Tries the MOVE extension first, then falls back to copy-and-delete for
        servers without it. A failed move is not fatal -- the caller marks the
        message seen either way, so it will not be picked up again.
        """
        try:
            status, _ = self._imap.uid("MOVE", uid, self._quote(folder))
            if status == "OK":
                return True
        except Exception:  # noqa: BLE001 - server lacks MOVE
            pass

        try:
            status, _ = self._imap.uid("COPY", uid, self._quote(folder))
            if status != "OK":
                return False
            self._imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            self._imap.expunge()
            return True
        except Exception:  # noqa: BLE001
            return False

    def send_reply(
        self,
        *,
        to_address: str,
        to_name: str,
        subject: str,
        body: str,
        in_reply_to: str,
        references: str,
        ics: str = "",
    ) -> str:
        """Send one reply, threaded onto the original. Returns its Message-ID.

        `ics`, when given, is attached as a text/calendar REQUEST so Gmail,
        Outlook and Apple Mail all offer to add the call to the recipient's
        calendar. The invite comes from here rather than from Google because a
        service account cannot add attendees without domain-wide delegation.
        """
        message = EmailMessage()
        message["From"] = email.utils.formataddr(
            ("DataRail", self.config.address)
        )
        message["To"] = email.utils.formataddr((to_name or "", to_address))
        message["Subject"] = subject
        message["Date"] = email.utils.formatdate(localtime=True)
        message_id = email.utils.make_msgid(domain="datarail.org")
        message["Message-ID"] = message_id

        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            # Appending to the existing chain is what keeps a long exchange in
            # one conversation rather than fanning into several.
            chain = (references + " " + in_reply_to).strip() if references else in_reply_to
            message["References"] = chain

        # Tell other well-behaved auto-responders not to answer this. It costs
        # one header and prevents the loop where two robots write to each other
        # until someone notices the bill.
        message["Auto-Submitted"] = "auto-replied"

        message.set_content(body)

        if ics:
            message.add_attachment(
                ics.encode("utf-8"),
                maintype="text",
                subtype="calendar",
                filename="invite.ics",
                # METHOD=REQUEST is what turns a downloadable file into an
                # accept/decline prompt in the recipient's mail client.
                params={"method": "REQUEST", "name": "invite.ics"},
            )

        with smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port) as server:
            server.login(self.config.username, self.config.password)
            server.send_message(message)

        return message_id
