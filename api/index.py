import imaplib
import email
import re
import json

from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from flask import Flask, request, Response

app = Flask(__name__)


# =========================================================
# JSON RESPONSE
# =========================================================

def json_response(data, status=200):
    return Response(
        json.dumps(
            data,
            ensure_ascii=False
        ),
        status=status,
        mimetype="application/json"
    )


# =========================================================
# DECODE EMAIL TEXT
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
# GET EMAIL BODY
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

            payload = msg.get_payload(
                decode=True
            )

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


# =========================================================
# EXTRACT AMOUNT
# =========================================================

def extract_amount(text):

    patterns = [

        # ₹50 / ₹50.00
        r"₹\s*([0-9,]+(?:\.[0-9]{1,2})?)",

        # Rs 50 / Rs. 50
        r"Rs\.?\s*([0-9,]+(?:\.[0-9]{1,2})?)",

        # INR 50
        r"INR\s*([0-9,]+(?:\.[0-9]{1,2})?)",

        # Amount / credited / received
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
                    match.group(1).replace(
                        ",",
                        ""
                    )
                )

                return f"₹{amount:.2f}"

            except Exception:
                pass

    return "₹0.00"


# =========================================================
# EXTRACT SENDER
# =========================================================

def extract_sender(msg, body):

    # ---------------------------------------------
    # First priority: From header
    # ---------------------------------------------

    from_header = decode_text(
        msg.get(
            "From",
            ""
        )
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


    # ---------------------------------------------
    # Received email sender
    # ---------------------------------------------

    patterns = [

        r"successfully\s+received"
        r".*?"
        r"\bfrom\s+"
        r"([A-Za-z][A-Za-z .'-]{1,60})",

        r"(?:sender|paid by|payer|received from)"
        r"\s*[:\-]?\s*"
        r"([A-Za-z][A-Za-z .'-]{1,60})"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            body,
            re.IGNORECASE | re.DOTALL
        )

        if match:

            name = match.group(1).strip()

            name = re.split(
                r"\n|Transaction ID|Date|Updated Balance|UTR|Purpose",
                name,
                flags=re.IGNORECASE
            )[0].strip()

            name = re.sub(
                r"\s+(?:at|via|on|from)\s*$",
                "",
                name,
                flags=re.IGNORECASE
            )

            if name:

                return name

    return "Unknown"


# =========================================================
# EXTRACT TIMESTAMP
# =========================================================

def extract_timestamp(date_header):

    try:

        dt = parsedate_to_datetime(
            date_header
        )

        return dt.isoformat()

    except Exception:

        return date_header or ""


# =========================================================
# EXTRACT TRANSACTION ID
# =========================================================

def extract_transaction_id(text):

    patterns = [

        r"Transaction\s+ID\s*:\s*([A-Za-z0-9]+)",

        r"Transaction\s+Id\s*:\s*([A-Za-z0-9]+)"
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
# EXTRACT UTR
# =========================================================

def extract_utr(text):

    match = re.search(
        r"\bUTR\s*:\s*([A-Za-z0-9]+)",
        text,
        re.IGNORECASE
    )

    if match:

        return match.group(1).strip()

    return ""


# =========================================================
# PAYMENT VERIFY API
# =========================================================

@app.route(
    "/api",
    methods=["GET"]
)
def verify_payment():

    gmail = request.args.get(
        "Gmail"
    )

    gpass = request.args.get(
        "GPASS"
    )

    requested_id = request.args.get(
        "ID"
    )


    # =====================================================
    # PARAMETER VALIDATION
    # =====================================================

    if (
        not gmail
        or not gpass
        or not requested_id
    ):

        return json_response({

            "status": "error",

            "message":
                "Missing parameters! "
                "Required: Gmail, GPASS, ID"

        }, 400)


    requested_id = requested_id.strip()


    if not requested_id:

        return json_response({

            "status": "error",

            "message":
                "Transaction ID cannot be empty."

        }, 400)


    mail = None


    try:

        # =================================================
        # CONNECT TO GMAIL
        # =================================================

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


        # =================================================
        # SEARCH TRANSACTION ID
        # =================================================

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

                "message":
                    "Gmail search failed."

            }, 500)


        uid_list = data[0].split()


        # =================================================
        # NEWEST EMAILS FIRST
        # =================================================

        uid_list = uid_list[-10:]

        uid_list.reverse()


        # =================================================
        # CHECK MATCHING EMAILS
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


            # =============================================
            # PARSE EMAIL
            # =============================================

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


            # =============================================
            # EXACT REQUESTED ID CHECK
            # =============================================

            if (
                requested_id.lower()
                not in
                full_text.lower()
            ):

                continue


            # =============================================
            # IMPORTANT SECURITY CHECK
            #
            # ONLY "SUCCESSFULLY RECEIVED"
            # IS VALID.
            #
            # "SUCCESSFULLY PAID" IS REJECTED.
            # =============================================

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


            # ---------------------------------------------
            # PAID EMAIL = REJECT
            # ---------------------------------------------

            if (
                paid_check
                and not received_check
            ):

                continue


            # ---------------------------------------------
            # MUST BE RECEIVED EMAIL
            # ---------------------------------------------

            if not received_check:

                continue


            # =============================================
            # EXTRACT TRANSACTION ID
            # =============================================

            txn_id = extract_transaction_id(
                full_text
            )


            if not txn_id:

                txn_id = requested_id


            # =============================================
            # REQUESTED ID MUST MATCH EMAIL TRANSACTION ID
            # =============================================

            if (
                txn_id.lower()
                !=
                requested_id.lower()
            ):

                continue


            # =============================================
            # EXTRACT AMOUNT
            # =============================================

            received_section = full_text


            amount_match = re.search(

                r"successfully\s+received"
                r".*?"
                r"(?:₹|Rs\.?|INR)\s*"
                r"([0-9,]+(?:\.[0-9]{1,2})?)",

                received_section,

                re.IGNORECASE
                | re.DOTALL
            )


            if amount_match:

                try:

                    amount_value = float(
                        amount_match.group(1)
                        .replace(",", "")
                    )

                    amount = (
                        f"₹{amount_value:.2f}"
                    )

                except Exception:

                    amount = extract_amount(
                        full_text
                    )

            else:

                amount = extract_amount(
                    full_text
                )


            # =============================================
            # EXTRACT SENDER
            # =============================================

            sender_match = re.search(

                r"successfully\s+received"
                r".*?"
                r"\bfrom\s+"
                r"([A-Za-z][A-Za-z .'-]{1,60})",

                full_text,

                re.IGNORECASE
                | re.DOTALL
            )


            if sender_match:

                sender = (
                    sender_match
                    .group(1)
                    .strip()
                )


                sender = re.split(

                    r"\n"
                    r"|Transaction ID"
                    r"|Date"
                    r"|Updated Balance"
                    r"|UTR"
                    r"|Purpose",

                    sender,

                    flags=re.IGNORECASE
                )[0].strip()


            else:

                sender = extract_sender(
                    msg,
                    body
                )


            # =============================================
            # EXTRACT UTR
            # =============================================

            utr = extract_utr(
                full_text
            )


            # If UTR does not exist,
            # use transaction ID
            if not utr:

                utr = txn_id


            # =============================================
            # TIMESTAMP
            # =============================================

            timestamp = extract_timestamp(

                msg.get(
                    "Date",
                    ""
                )
            )


            # =============================================
            # LOGOUT
            # =============================================

            mail.logout()


            # =============================================
            # VERIFIED RESPONSE
            # =============================================

            return json_response({

                "status":
                    "verified",

                "amount":
                    amount,

                "currency":
                    "INR",

                "sender":
                    sender,

                "txn_id":
                    txn_id,

                "utr":
                    utr,

                "reference_id":
                    txn_id,

                "timestamp":
                    timestamp

            })


        # =================================================
        # NO VALID RECEIVED PAYMENT FOUND
        # =================================================

        mail.logout()


        return json_response({

            "status":
                "not_found",

            "message":
                f"ID {requested_id} "
                f"not found in latest emails."

        })


    # =====================================================
    # GMAIL LOGIN ERROR
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

            "message":
                "Gmail login failed. "
                "Check Gmail and App Password."

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

            "message":
                "Unable to check Gmail."

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
            "Gmail verification API is running."

    })


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000
      )
