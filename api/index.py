import imaplib
import email
import re
import json

from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from flask import Flask, request, Response

app = Flask(__name__)


def json_response(data, status=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        mimetype="application/json"
    )


def decode_text(value):
    if not value:
        return ""

    try:
        result = ""

        for text, encoding in decode_header(value):
            if isinstance(text, bytes):
                result += text.decode(
                    encoding or "utf-8",
                    errors="ignore"
                )
            else:
                result += str(text)

        return result.strip()

    except Exception:
        return str(value)


def get_body(msg):
    parts = []

    if msg.is_multipart():

        for part in msg.walk():

            content_type = part.get_content_type()
            disposition = str(
                part.get("Content-Disposition", "")
            ).lower()

            if content_type == "text/plain" and \
               "attachment" not in disposition:

                try:
                    payload = part.get_payload(
                        decode=True
                    )

                    if payload:
                        parts.append(
                            payload.decode(
                                part.get_content_charset()
                                or "utf-8",
                                errors="ignore"
                            )
                        )

                except Exception:
                    pass

    else:

        try:
            payload = msg.get_payload(decode=True)

            if payload:
                parts.append(
                    payload.decode(
                        msg.get_content_charset()
                        or "utf-8",
                        errors="ignore"
                    )
                )

        except Exception:
            pass

    return "\n".join(parts)


def extract_amount(text):

    patterns = [

        # ₹10 / ₹10.00
        r"₹\s*([0-9,]+(?:\.[0-9]{1,2})?)",

        # Rs 10 / Rs. 10
        r"Rs\.?\s*([0-9,]+(?:\.[0-9]{1,2})?)",

        # INR 10
        r"INR\s*([0-9,]+(?:\.[0-9]{1,2})?)",

        # Amount: 10
        r"(?:amount|credited|received)"
        r"\s*[:\-]?\s*₹?\s*"
        r"([0-9,]+(?:\.[0-9]{1,2})?)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            try:
                amount = float(
                    match.group(1).replace(",", "")
                )

                return f"₹{amount:.2f}"

            except Exception:
                pass

    return "₹0.00"


def extract_sender(msg, body):

    # First priority: From header
    from_header = decode_text(
        msg.get("From", "")
    )

    name, address = parseaddr(from_header)

    if name:
        name = name.strip()

        # Remove unwanted trailing words
        name = re.sub(
            r"\s+(?:at|via|on|from)\s*$",
            "",
            name,
            flags=re.IGNORECASE
        )

        if name:
            return name

    # Fallback patterns
    patterns = [
        r"(?:sender|paid by|payer|received from)"
        r"\s*[:\-]?\s*([A-Za-z][A-Za-z .]{1,50})"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            body,
            re.IGNORECASE
        )

        if match:

            name = match.group(1).strip()

            name = re.sub(
                r"\s+(?:at|via|on|from)\s*$",
                "",
                name,
                flags=re.IGNORECASE
            )

            return name

    return "Unknown"


def extract_timestamp(date_header):

    try:
        dt = parsedate_to_datetime(
            date_header
        )

        return dt.isoformat()

    except Exception:
        return date_header or ""


@app.route("/api", methods=["GET"])
def verify_payment():

    gmail = request.args.get("Gmail")
    gpass = request.args.get("GPASS")
    requested_id = request.args.get("ID")

    # -----------------------------
    # Parameter validation
    # -----------------------------

    if not gmail or not gpass or not requested_id:

        return json_response({
            "status": "error",
            "message":
                "Missing parameters! Required: Gmail, GPASS, ID"
        }, 400)

    try:

        # -----------------------------
        # Gmail connection
        # -----------------------------

        mail = imaplib.IMAP4_SSL(
            "imap.gmail.com",
            993,
            timeout=10
        )

        mail.login(
            gmail,
            gpass
        )

        mail.select(
            "INBOX",
            readonly=True
        )

        # -----------------------------
        # FAST SERVER-SIDE SEARCH
        # -----------------------------

        status, data = mail.uid(
            "search",
            None,
            "TEXT",
            requested_id
        )

        if status != "OK":

            mail.logout()

            return json_response({
                "status": "error",
                "message": "Gmail search failed."
            }, 500)

        uid_list = data[0].split()

        # Newest first
        uid_list = uid_list[-10:]
        uid_list.reverse()

        # -----------------------------
        # Fetch only matching emails
        # -----------------------------

        for uid in uid_list:

            status, msg_data = mail.uid(
                "fetch",
                uid,
                "(RFC822)"
            )

            if status != "OK":
                continue

            raw_email = None

            for item in msg_data:

                if isinstance(item, tuple):

                    raw_email = item[1]
                    break

            if not raw_email:
                continue

            msg = email.message_from_bytes(
                raw_email
            )

            subject = decode_text(
                msg.get("Subject", "")
            )

            body = get_body(msg)

            # -----------------------------
            # EXACT ID CHECK
            # -----------------------------

            full_text = (
                subject +
                "\n" +
                body
            )

            if requested_id.lower() not in \
               full_text.lower():

                continue

            # -----------------------------
            # Extract information
            # -----------------------------

            amount = extract_amount(
                full_text
            )

            sender = extract_sender(
                msg,
                body
            )

            timestamp = extract_timestamp(
                msg.get("Date", "")
            )

            # Requested ID is trusted because
            # exact ID was found in this email
            txn_id = requested_id

            mail.logout()

            return json_response({

                "status": "verified",

                "amount": amount,

                "currency": "INR",

                "sender": sender,

                "txn_id": txn_id,

                "utr": txn_id,

                "reference_id": txn_id,

                "timestamp": timestamp

            })

        # -----------------------------
        # Not found
        # -----------------------------

        mail.logout()

        return json_response({

            "status": "not_found",

            "message":
                f"ID {requested_id} "
                f"not found in latest emails."

        })

    except imaplib.IMAP4.error:

        return json_response({

            "status": "error",

            "message":
                "Gmail login failed. "
                "Check Gmail and App Password."

        }, 401)

    except Exception:

        return json_response({

            "status": "error",

            "message":
                "Unable to check Gmail."

        }, 500)


@app.route("/", methods=["GET"])
def home():

    return json_response({

        "status": "online",

        "message":
            "Gmail verification API is running."

    })


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000
  )
