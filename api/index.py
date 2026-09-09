import imaplib
import email
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime
from flask import Flask, request, jsonify

app = Flask(__name__)


def decode_text(value):
    if not value:
        return ""

    try:
        parts = decode_header(value)
        result = ""

        for text, encoding in parts:
            if isinstance(text, bytes):
                result += text.decode(encoding or "utf-8", errors="ignore")
            else:
                result += text

        return result
    except Exception:
        return str(value)


def get_body(msg):
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in disposition:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="ignore"
                        )
                except Exception:
                    pass

    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="ignore"
                )
        except Exception:
            pass

    return body


def extract_amount(text):
    patterns = [
        r"(?:₹|Rs\.?|INR)\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"(?:amount|credited|received)[^\d]{0,30}([0-9,]+(?:\.[0-9]{1,2})?)"
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            amount = match.group(1).replace(",", "")
            return f"₹{float(amount):.2f}"

    return "₹0.00"


def extract_sender(text, from_header):
    # Try common payment-email sender fields
    patterns = [
        r"(?:sender|from|received from)\s*[:\-]?\s*([A-Za-z][A-Za-z .]{1,40})",
        r"(?:paid by|payer)\s*[:\-]?\s*([A-Za-z][A-Za-z .]{1,40})"
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            name = re.sub(r"\s+", " ", name)
            return name[:50]

    # Fallback: name from From header
    decoded = decode_text(from_header)

    if "<" in decoded:
        decoded = decoded.split("<")[0].strip()

    return decoded or "Unknown"


def extract_transaction_id(text, requested_id):
    patterns = [
        r"(?:UTR|Transaction\s*ID|Txn\s*ID|Reference\s*ID|Reference)"
        r"\s*[:\-]?\s*([A-Za-z0-9]+)"
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)

        for value in matches:
            if requested_id.lower() == value.lower():
                return value

    # If the requested ID was found in the email, use it
    if requested_id.lower() in text.lower():
        return requested_id

    return requested_id


def iso_timestamp(date_header):
    try:
        dt = parsedate_to_datetime(date_header)
        return dt.isoformat()
    except Exception:
        return date_header or ""


@app.route("/api", methods=["GET"])
def verify_payment():

    gmail = request.args.get("Gmail")
    gpass = request.args.get("GPASS")
    requested_id = request.args.get("ID")

    if not gmail or not gpass or not requested_id:
        return jsonify({
            "status": "error",
            "message": "Missing parameters! Required: Gmail, GPASS, ID"
        }), 400

    try:
        # Connect to Gmail IMAP
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)

        # Login using Gmail + App Password
        mail.login(gmail, gpass)

        mail.select("INBOX", readonly=True)

        # Get latest emails
        status, data = mail.search(None, "ALL")

        if status != "OK":
            mail.logout()
            return jsonify({
                "status": "error",
                "message": "Unable to search Gmail inbox."
            }), 500

        email_ids = data[0].split()

        # Check latest 50 emails, newest first
        email_ids = email_ids[-50:]
        email_ids.reverse()

        for mail_id in email_ids:

            status, msg_data = mail.fetch(
                mail_id,
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

            msg = email.message_from_bytes(raw_email)

            subject = decode_text(msg.get("Subject", ""))
            from_header = decode_text(msg.get("From", ""))
            date_header = msg.get("Date", "")

            body = get_body(msg)

            full_text = (
                subject + "\n" +
                from_header + "\n" +
                body
            )

            # Case-insensitive ID matching
            if requested_id.lower() not in full_text.lower():
                continue

            amount = extract_amount(full_text)
            sender = extract_sender(body, from_header)
            txn_id = extract_transaction_id(
                full_text,
                requested_id
            )

            timestamp = iso_timestamp(date_header)

            mail.logout()

            return jsonify({
                "status": "verified",
                "amount": amount,
                "currency": "INR",
                "sender": sender,
                "txn_id": txn_id,
                "utr": txn_id,
                "reference_id": txn_id,
                "timestamp": timestamp
            })

        mail.logout()

        return jsonify({
            "status": "not_found",
            "message": f"ID {requested_id} not found in latest emails."
        })

    except imaplib.IMAP4.error:
        return jsonify({
            "status": "error",
            "message": "Gmail login failed. Check Gmail and App Password."
        }), 401

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": "Unable to check Gmail."
        }), 500


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "message": "Gmail verification API is running."
    })
