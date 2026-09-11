import imaplib
import email
import re
import json
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from flask import Flask, request, Response

app = Flask(__name__)

DB_FILE = "/tmp/payments.db"
KOLKATA = ZoneInfo("Asia/Kolkata")


def json_response(data, status=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        mimetype="application/json"
    )


# ---------------- DATABASE ----------------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS used_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id TEXT UNIQUE,
            utr TEXT UNIQUE,
            amount TEXT,
            first_used_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def normalize(value):
    if not value:
        return ""

    return re.sub(r"[^a-zA-Z0-9]", "", value).upper()


def payment_used(txn_id, utr):
    txn_id = normalize(txn_id)
    utr = normalize(utr)

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id
        FROM used_payments
        WHERE txn_id = ? OR utr = ?
        LIMIT 1
        """,
        (txn_id, utr)
    )

    row = cur.fetchone()
    conn.close()

    return row is not None


def save_payment(txn_id, utr, amount):
    txn_id = normalize(txn_id)
    utr = normalize(utr)

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO used_payments
            (txn_id, utr, amount, first_used_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                txn_id,
                utr,
                amount,
                datetime.now(timezone.utc).isoformat()
            )
        )

        conn.commit()
        return True

    except sqlite3.IntegrityError:
        conn.rollback()
        return False

    finally:
        conn.close()


# ---------------- EMAIL ----------------

def decode_text(value):
    if not value:
        return ""

    try:
        parts = email.header.decode_header(value)
        result = ""

        for part, encoding in parts:
            if isinstance(part, bytes):
                result += part.decode(
                    encoding or "utf-8",
                    errors="ignore"
                )
            else:
                result += str(part)

        return result

    except Exception:
        return str(value)


def get_email_body(msg):
    body = ""

    if msg.is_multipart():

        for part in msg.walk():

            content_type = part.get_content_type()

            if content_type == "text/plain":

                try:
                    payload = part.get_payload(
                        decode=True
                    )

                    if payload:
                        body += payload.decode(
                            "utf-8",
                            errors="ignore"
                        )

                except Exception:
                    pass

    else:

        try:
            payload = msg.get_payload(
                decode=True
            )

            if payload:
                body = payload.decode(
                    "utf-8",
                    errors="ignore"
                )

        except Exception:
            body = ""

    return body


# ---------------- KOLKATA DATE CHECK ----------------

def is_today_in_kolkata(date_header):
    """
    Check whether email date is today's date
    according to Asia/Kolkata timezone.
    """

    if not date_header:
        return False

    try:
        email_dt = parsedate_to_datetime(date_header)

        if email_dt.tzinfo is None:
            email_dt = email_dt.replace(
                tzinfo=timezone.utc
            )

        email_kolkata = email_dt.astimezone(
            KOLKATA
        )

        today_kolkata = datetime.now(
            KOLKATA
        ).date()

        return email_kolkata.date() == today_kolkata

    except Exception:
        return False


# ---------------- PAYMENT PARSER ----------------

def extract_payment_data(text):

    clean = " ".join(text.split())

    # Amount
    amount = None

    amount_patterns = [
        r"₹\s*([\d,]+(?:\.\d{1,2})?)",
        r"INR\s*([\d,]+(?:\.\d{1,2})?)",
        r"Rs\.?\s*([\d,]+(?:\.\d{1,2})?)"
    ]

    for pattern in amount_patterns:

        match = re.search(
            pattern,
            clean,
            re.IGNORECASE
        )

        if match:
            amount = match.group(1).replace(
                ",",
                ""
            )
            break

    # Transaction ID
    txn_id = None

    txn_patterns = [
        r"transaction\s*id\s*[:\-]?\s*([A-Za-z0-9]+)",
        r"txn\s*id\s*[:\-]?\s*([A-Za-z0-9]+)",
        r"transaction\s*number\s*[:\-]?\s*([A-Za-z0-9]+)"
    ]

    for pattern in txn_patterns:

        match = re.search(
            pattern,
            clean,
            re.IGNORECASE
        )

        if match:
            txn_id = match.group(1)
            break

    # UTR
    utr = None

    utr_patterns = [
        r"UTR\s*[:\-]?\s*([A-Za-z0-9]+)",
        r"UPI\s*transaction\s*id\s*[:\-]?\s*([A-Za-z0-9]+)",
        r"reference\s*id\s*[:\-]?\s*([A-Za-z0-9]+)"
    ]

    for pattern in utr_patterns:

        match = re.search(
            pattern,
            clean,
            re.IGNORECASE
        )

        if match:
            utr = match.group(1)
            break

    return {
        "amount": amount,
        "txn_id": txn_id,
        "utr": utr
    }


# ---------------- API ----------------

@app.route("/api", methods=["GET"])
def verify_payment():

    gmail = request.args.get(
        "Gmail",
        ""
    ).strip()

    gpass = request.args.get(
        "GPASS",
        ""
    ).strip()

    requested_id = request.args.get(
        "ID",
        ""
    ).strip()

    if not gmail or not gpass:

        return json_response({
            "status": "error",
            "first_time": False,
            "message": "Gmail and password are required."
        }, 400)

    if not requested_id:

        return json_response({
            "status": "error",
            "first_time": False,
            "message": "ID is required."
        }, 400)

    requested_normalized = normalize(
        requested_id
    )

    mail = None

    try:

        # Gmail connection
        mail = imaplib.IMAP4_SSL(
            "imap.gmail.com",
            993
        )

        mail.login(
            gmail,
            gpass
        )

        mail.select("INBOX")

        # --------------------------------
        # SEARCH CURRENT DAY ONLY
        # Gmail IMAP date is based on
        # mailbox/server date, so we still
        # perform an exact Kolkata date
        # check below.
        # --------------------------------

        today_kolkata = datetime.now(
            KOLKATA
        )

        today_str = today_kolkata.strftime(
            "%d-%b-%Y"
        )

        status, data = mail.search(
            None,
            'ON',
            today_str
        )

        if status != "OK" or not data or not data[0]:

            return json_response({
                "status": "not_found",
                "first_time": False,
                "message": "Today's matching payment not found."
            })

        message_ids = data[0].split()

        # Latest emails first
        message_ids = message_ids[-20:]
        message_ids.reverse()

        for message_id in message_ids:

            status, msg_data = mail.fetch(
                message_id,
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

            sender = decode_text(
                msg.get("From", "")
            )

            body = get_email_body(msg)

            full_text = (
                subject + " " + body
            )

            lower_text = full_text.lower()

            # --------------------------------
            # IMPORTANT:
            # EXACT KOLKATA DATE CHECK
            # --------------------------------

            timestamp = msg.get(
                "Date",
                ""
            )

            if not is_today_in_kolkata(
                timestamp
            ):
                continue

            # --------------------------------
            # ONLY RECEIVED PAYMENT
            # --------------------------------

            if (
                "you have successfully received"
                not in lower_text
                and
                "successfully received"
                not in lower_text
            ):
                continue

            # Ignore outgoing payment
            if (
                "you have successfully paid"
                in lower_text
            ):
                continue

            payment = extract_payment_data(
                full_text
            )

            amount = payment["amount"]
            txn_id = payment["txn_id"]
            utr = payment["utr"]

            if not txn_id and not utr:
                continue

            txn_normalized = normalize(
                txn_id
            )

            utr_normalized = normalize(
                utr
            )

            # --------------------------------
            # REQUESTED ID MUST MATCH
            # --------------------------------

            if (
                requested_normalized != txn_normalized
                and
                requested_normalized != utr_normalized
            ):
                continue

            # --------------------------------
            # DUPLICATE PAYMENT CHECK
            # --------------------------------

            if payment_used(
                txn_id,
                utr
            ):

                return json_response({
                    "status": "already_used",
                    "first_time": False,
                    "message": "This payment has already been used.",
                    "amount": (
                        "₹" + amount
                        if amount else None
                    ),
                    "txn_id": txn_id,
                    "utr": utr,
                    "sender": sender,
                    "timestamp": timestamp
                })

            # --------------------------------
            # SAVE FIRST-TIME PAYMENT
            # --------------------------------

            saved = save_payment(
                txn_id,
                utr,
                amount
            )

            if not saved:

                return json_response({
                    "status": "already_used",
                    "first_time": False,
                    "message": "This payment has already been used.",
                    "amount": (
                        "₹" + amount
                        if amount else None
                    ),
                    "txn_id": txn_id,
                    "utr": utr,
                    "sender": sender,
                    "timestamp": timestamp
                })

            # --------------------------------
            # VERIFIED
            # --------------------------------

            return json_response({
                "status": "verified",
                "first_time": True,
                "amount": (
                    "₹" + amount
                    if amount else None
                ),
                "currency": "INR",
                "sender": sender,
                "txn_id": txn_id,
                "utr": utr,
                "reference_id": utr or txn_id,
                "timestamp": timestamp,
                "verification_date": today_kolkata.strftime(
                    "%Y-%m-%d"
                ),
                "timezone": "Asia/Kolkata"
            })

        return json_response({
            "status": "not_found",
            "first_time": False,
            "message": "Matching payment not found in today's Kolkata date."
        })

    except imaplib.IMAP4.error:

        return json_response({
            "status": "error",
            "first_time": False,
            "message": "Gmail login failed."
        }, 401)

    except Exception:

        return json_response({
            "status": "error",
            "first_time": False,
            "message": "Verification failed."
        }, 500)

    finally:

        try:
            if mail:
                mail.logout()
        except Exception:
            pass


# ---------------- HOME ----------------

@app.route("/")
def home():

    return json_response({
        "status": "online",
        "service": "Payment Verification API",
        "timezone": "Asia/Kolkata",
        "rule": "Only today's Kolkata-date emails are verified."
    })


# ---------------- START ----------------

init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        )
    )
