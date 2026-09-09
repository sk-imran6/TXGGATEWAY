from flask import Flask, request, jsonify
import imaplib
import email
from email.header import decode_header

app = Flask(__name__)

@app.route("/api")
def check_gmail():
    gmail = request.args.get("Gmail")
    app_pass = request.args.get("GPASS")

    if not gmail or not app_pass:
        return jsonify({
            "status": "error",
            "message": "Gmail and GPASS are required"
        }), 400

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail, app_pass)
        mail.select("INBOX")

        status, messages = mail.search(None, "UNSEEN")
        email_ids = messages[0].split()

        emails = []

        for mail_id in email_ids[-10:]:
            status, data = mail.fetch(mail_id, "(RFC822)")

            msg = email.message_from_bytes(data[0][1])

            subject = decode_header(msg.get("Subject", ""))[0][0]
            if isinstance(subject, bytes):
                subject = subject.decode(errors="ignore")

            emails.append({
                "from": msg.get("From"),
                "subject": subject,
                "date": msg.get("Date")
            })

        mail.logout()

        return jsonify({
            "status": "success",
            "new_emails": len(emails),
            "emails": emails
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
