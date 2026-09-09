import imaplib
import email
import re
import json

from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from flask import Flask, request, Response

app = Flask(__name__)


# =========================================================
# CONFIG
# =========================================================

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Hosting environment variables থেকে এগুলো সেট করবে
# GMAIL_USER
# GMAIL_APP_PASSWORD


# =========================================================
# JSON RESPONSE
# =========================================================

def json_response(data, status=200):

    return Response(
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        status=status,
        mimetype="application/json"
    )


# =========================================================
# DECODE
# =========================================================

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


# =========================================================
# EMAIL BODY
# =========================================================

def get_body(msg):

    parts = []

    if msg.is_multipart():

        for part in msg.walk():

            content_type = part.get_content_type()

            disposition = str(
                part.get(
                    "Content-Disposition",
                    ""
                )
            ).lower()

            if (
                content_type == "text/plain"
                and "attachment" not in disposition
            ):

                payload = part.get_payload(
                    decode=True
                )

                if payload:

                    try:

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

        payload = msg.get_payload(
            decode=True
        )

        if payload:

            try:

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


# =========================================================
# AMOUNT
# =========================================================

def extract_amount(text):

    patterns = [

        r"₹\s*([0-9,]+(?:\.[0-9]{1,2})?)",

        r"Rs\.?\s*([0-9,]+(?:\.[0-9]{1,2})?)",

        r"INR\s*([0-9,]+(?:\.[0-9]{1,2})?)",

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

                value = float(
                    match.group(1).replace(
                        ",",
                        ""
                    )
                )

                return f"₹{value:.2f}"

            except Exception:
                pass

    return "₹0.00"


# =========================================================
# TRANSACTION ID
# =========================================================

def extract_transaction_id(text):

    patterns = [

        r"Transaction\s+ID\s*:\s*([A-Za-z0-9]+)",

        r"Transaction\s+Id\s*:\s*([A-Za-z0-9]+)",

        r"Transaction\s*ID\s*[-:]\s*([A-Za-z0-9]+)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            return match.group(1).strip()

    return ""


# =========================================================
# UTR
# =========================================================

def extract_utr(text):

    patterns = [

        r"\bUTR\s*:\s*([A-Za-z0-9]+)",

        r"\bUTR\s*[-:]\s*([A-Za-z0-9]+)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            return match.group(1).strip()

    return ""


# =========================================================
# SENDER
# =========================================================

def extract_sender(msg, body):

    from_header = decode_text(
        msg.get("From", "")
    )

    name, address = parseaddr(
        from_header
    )

    if name:

        name = name.strip()

        name = re.sub(
            r"\s+(?:at|via|on|from)\s*$",
            "",
            name,
            flags=re.IGNORECASE
        )

        if name:

            return name

    match = re.search(

        r"successfully\s+received"
        r".*?"
        r"\bfrom\s+"
        r"([A-Za-z][A-Za-z .'-]{1,60})",

        body,

        re.IGNORECASE
        | re.DOTALL
    )

    if match:

        name = match.group(1).strip()

        name = re.split(

            r"\n"
            r"|Transaction ID"
            r"|Date"
            r"|Updated Balance"
            r"|UTR"
            r"|Purpose",

            name,

            flags=re.IGNORECASE
        )[0].strip()

        if name:

            return name

    return "Unknown"


# =========================================================
# TIMESTAMP
# =========================================================

def extract_timestamp(value):

    try:

        return parsedate_to_datetime(
            value
        ).isoformat()

    except Exception:

        return value or ""


# =========================================================
# NORMALIZE ID
# =========================================================

def normalize_id(value):

    return str(
        value or ""
    ).strip().lower()


# =========================================================
# PAYMENT EXTRACTION
# =========================================================

def extract_payment(msg, full_text):

    received_check = re.search(

        r"you\s+have\s+successfully\s+received",

        full_text,

        re.IGNORECASE
    )

    paid_check = re.search(

        r"you\s+have\s+successfully\s+paid",

        full_text,

        re.IGNORECASE
    )

    # Paid email is never accepted
    if paid_check and not received_check:

        return None

    # Must be received email
    if not received_check:

        return None


    txn_id = extract_transaction_id(
        full_text
    )

    utr = extract_utr(
        full_text
    )


    # If one is missing, use the other
    if not txn_id and utr:

        txn_id = utr

    if not utr and txn_id:

        utr = txn_id


    if not txn_id and not utr:

        return None


    amount = extract_amount(
        full_text
    )


    sender = extract_sender(
        msg,
        full_text
    )


    timestamp = extract_timestamp(
        msg.get("Date", "")
    )


    return {

        "amount": amount,

        "currency": "INR",

        "sender": sender,

        "txn_id": txn_id,

        "utr": utr,

        "reference_id": txn_id,

        "timestamp": timestamp
    }


# =========================================================
# PAYMENT VERIFY
# =========================================================

@app.route(
    "/api",
    methods=["GET"]
)
def verify_payment():

    # -----------------------------------------------------
    # ID
    # -----------------------------------------------------

    requested_id = request.args.get(
        "ID",
        ""
    ).strip()


    if not requested_id:

        return json_response({

            "status": "error",

            "first_time": False,

            "message":
                "ID is required."

        }, 400)


    # -----------------------------------------------------
    # CREDENTIALS FROM ENVIRONMENT
    # -----------------------------------------------------

    import os

    gmail = os.environ.get(
        "GMAIL_USER"
    )

    gpass = os.environ.get(
        "GMAIL_APP_PASSWORD"
    )


    if not gmail or not gpass:

        return json_response({

            "status": "error",

            "first_time": False,

            "message":
                "Server configuration error."

        }, 500)


    mail = None


    try:

        # =================================================
        # CONNECT
        # =================================================

        mail = imaplib.IMAP4_SSL(
            IMAP_HOST,
            IMAP_PORT,
            timeout=8
        )

        mail.login(
            gmail,
            gpass
        )

        mail.select(
            "INBOX",
            readonly=True
        )


        # =================================================
        # SEARCH ID
        # =================================================

        status, data = mail.uid(

            "search",

            None,

            "TEXT",

            requested_id
        )


        if status != "OK":

            return json_response({

                "status": "error",

                "first_time": False,

                "message":
                    "Gmail search failed."

            }, 500)


        uid_list = data[0].split()


        # =================================================
        # NEWEST FIRST
        # =================================================

        uid_list = uid_list[-10:]

        uid_list.reverse()


        # =================================================
        # CHECK EMAILS
        # =================================================

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

                if isinstance(
                    item,
                    tuple
                ):

                    raw_email = item[1]

                    break


            if not raw_email:

                continue


            msg = email.message_from_bytes(
                raw_email
            )


            subject = decode_text(
                msg.get(
                    "Subject",
                    ""
                )
            )


            body = get_body(
                msg
            )


            full_text = (
                subject
                + "\n"
                + body
            )


            # =================================================
            # REQUESTED ID MUST EXIST
            # =================================================

            if normalize_id(requested_id) not in normalize_id(full_text):

                continue


            # =================================================
            # EXTRACT PAYMENT
            # =================================================

            payment = extract_payment(
                msg,
                full_text
            )


            if not payment:

                continue


            txn_id = payment["txn_id"]

            utr = payment["utr"]


            # =================================================
            # REQUESTED ID MUST MATCH UTR OR TXN
            # =================================================

            requested_normalized = normalize_id(
                requested_id
            )

            txn_normalized = normalize_id(
                txn_id
            )

            utr_normalized = normalize_id(
                utr
            )


            if (
                requested_normalized
                != txn_normalized
                and
                requested_normalized
                != utr_normalized
            ):

                continue


            # =================================================
            # SUCCESS
            #
            # NOTE:
            # first_time must be stored in persistent DB/KV.
            # =================================================

            mail.logout()


            return json_response({

                "status":
                    "verified",

                "first_time":
                    True,

                "amount":
                    payment["amount"],

                "currency":
                    payment["currency"],

                "sender":
                    payment["sender"],

                "txn_id":
                    payment["txn_id"],

                "utr":
                    payment["utr"],

                "reference_id":
                    payment["reference_id"],

                "timestamp":
                    payment["timestamp"]

            })


        # =================================================
        # NOT FOUND
        # =================================================

        mail.logout()


        return json_response({

            "status":
                "not_found",

            "first_time":
                False,

            "message":
                "Payment not found."

        })


    # =====================================================
    # LOGIN ERROR
    # =====================================================

    except imaplib.IMAP4.error:

        try:

            if mail:
                mail.logout()

        except Exception:
            pass


        return json_response({

            "status":
                "error",

            "first_time":
                False,

            "message":
                "Gmail authentication failed."

        }, 401)


    # =====================================================
    # OTHER ERROR
    # =====================================================

    except Exception:

        try:

            if mail:
                mail.logout()

        except Exception:
            pass


        return json_response({

            "status":
                "error",

            "first_time":
                False,

            "message":
                "Unable to verify payment."

        }, 500)


# =========================================================
# HOME
# =========================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return json_response({

        "status":
            "online",

        "message":
            "Payment verification API is running."

    })


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000
      )
