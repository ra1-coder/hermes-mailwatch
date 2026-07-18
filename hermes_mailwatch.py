#!/usr/bin/env python3
"""Hermes mail watcher — real-time inbox daemon.

Design contract (do not violate):
- This process is DUMB. It holds an IMAP IDLE connection (real-time push)
  and falls back to 60s polling if IDLE misbehaves. Idle cost: zero.
- The model is invoked ONLY when at least one new message exists —
  one triage call per message, never on a timer.
- Everyone gets read; only Ryan gets obeyed. A message is an instruction
  only if from PRINCIPAL_ADDR *and* DKIM passed. Nobody ever gets a reply.
- A message is marked \\Seen only after a successful store write.

Stdlib only. Config via environment (see EnvironmentFile in the unit):
  GMAIL_USER, GMAIL_APP_PASSWORD,
  ANTHROPIC_API_KEY, HERMES_TRIAGE_MODEL (optional),
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
  DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID
"""
import base64
import email
import email.header
import email.utils
import html.parser
import imaplib
import json
import os
import re
import socket
import time
import urllib.request

PRINCIPAL_ADDR = "ryananthony.t@gmail.com"
CATEGORIES = ["instruction", "action", "personal", "receipt", "news", "noise"]
IDLE_MINUTES = 24  # re-issue IDLE before Gmail's ~29-minute cutoff
BODY_LIMIT = 4000      # stored in the database (capture first)
TRIAGE_LIMIT = 2000    # sent to the model (category + one sentence needs no more)
CAPTURE_BUCKET = "raw-attachments"   # capture bucket != vault (standing rule)
IMG_STORE_MAX = 15 * 1024 * 1024     # capture anything reasonable
IMG_VISION_MAX = 4 * 1024 * 1024     # API image ceiling with base64 headroom
IMG_VISION_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
VISION_COUNT = 3                     # at most this many images shown to triage

ENV = os.environ
MODEL = ENV.get("HERMES_TRIAGE_MODEL", "claude-haiku-4-5-20251001")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ---------- normalize ----------

class _HTMLText(html.parser.HTMLParser):
    SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li", "blockquote"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup):
    p = _HTMLText()
    try:
        p.feed(markup)
    except Exception:
        return markup
    return "".join(p.parts)


def decode_hdr(value):
    if not value:
        return ""
    out = []
    for chunk, enc in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", "replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def best_body(msg):
    plain, htm = None, None
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, "replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and htm is None:
            htm = text
    text = plain if plain is not None else html_to_text(htm or "")
    # CAPTURE FIRST: store full fidelity — forwarded content and quoted
    # threads are often the entire point of the email. Cleaning is a
    # triage-time concern (see triage_view), never a storage-time one.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:BODY_LIMIT]


def triage_view(text):
    # For the model only: drop quoted thread + signature to save tokens,
    # but keep forwarded content — it usually carries the payload.
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .{6,80} wrote:\s*$", line):
            break
        if line.strip() == "--":
            break
        lines.append(line)
    return "\n".join(lines).strip() or text


def normalize(raw):
    msg = email.message_from_bytes(raw)
    from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
    from_addr = from_addr.lower()
    auth = " ".join(msg.get_all("Authentication-Results", []) or []).lower()
    # Authority requires DKIM specifically: SPF authenticates the sending
    # server, not the From header, and must never grant principal status.
    dkim_ok = re.search(r"\bdkim=pass\b", auth) is not None
    try:
        received_at = email.utils.parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        received_at = None
    # Attachment awareness: the payload of an email is often a photo or a
    # file, not text. Record what rode along so capture and triage know
    # the words may not be the whole message.
    attachments = []
    images = []
    for part in msg.walk():
        fn = part.get_filename()
        is_image = part.get_content_maintype() == "image"
        if fn:
            attachments.append(decode_hdr(fn))
        elif is_image:
            attachments.append("inline-" + (part.get_content_subtype() or "image"))
        if is_image:
            try:
                data = part.get_payload(decode=True)
            except Exception:
                data = None
            if data and len(data) <= IMG_STORE_MAX:
                name = decode_hdr(fn) if fn else (
                    "inline-%d.%s" % (len(images) + 1, part.get_content_subtype() or "img"))
                images.append({
                    "name": name,
                    "mime": part.get_content_type(),
                    "data": data,
                })
    body = best_body(msg)
    if attachments:
        body = (body + "\n\n[ATTACHMENTS: %s]" % ", ".join(attachments))[:BODY_LIMIT]
    return {
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "from_addr": from_addr or "unknown",
        "from_name": decode_hdr(from_name) or None,
        "subject": decode_hdr(msg.get("Subject", "")) or None,
        "body_text": body,
        "attachments": attachments,
        "received_at": received_at,
        "is_ryan": from_addr == PRINCIPAL_ADDR and dkim_ok,
        "_images": images,  # bytes ride outside the stored row
    }


# ---------- capture: files land before any thinking ----------

def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "file"


def capture_files(m):
    """Upload every image to the capture bucket; return the file records.

    Capture-first doctrine: the original is preserved before triage runs.
    Uploads are idempotent (x-upsert) so a crash-and-retry never duplicates.
    A failed upload is recorded visibly on the row, never swallowed.
    """
    files = []
    if not m.get("_images"):
        return files
    stamp = time.strftime("%Y%m")
    key = _safe(m.get("message_id") or ("ts-%d" % int(time.time())))
    for i, img in enumerate(m["_images"], 1):
        path = "mail/%s/%s/%02d-%s" % (stamp, key, i, _safe(img["name"]))
        req = urllib.request.Request(
            ENV["SUPABASE_URL"].rstrip("/") + "/storage/v1/object/%s/%s" % (CAPTURE_BUCKET, path),
            data=img["data"],
            headers={
                "Content-Type": img["mime"],
                "apikey": ENV["SUPABASE_SERVICE_ROLE_KEY"],
                "Authorization": "Bearer " + ENV["SUPABASE_SERVICE_ROLE_KEY"],
                "x-upsert": "true",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if 200 <= r.status < 300:
                    files.append({"name": img["name"], "path": path,
                                  "mime": img["mime"], "size": len(img["data"])})
                else:
                    files.append({"name": img["name"], "error": "HTTP %s" % r.status})
        except Exception as e:
            log("capture failed for %s: %s" % (img["name"], e))
            files.append({"name": img["name"], "error": str(e)[:200]})
    return files


# ---------- the only model call ----------

def triage(m):
    prompt = (
        "Triage one inbound email for a personal chief-of-staff system. "
        "Categories: instruction (a directive from the principal), action "
        "(someone needs something / a deadline / a decision), personal (a "
        "human writing personally), receipt (order, payment, booking, "
        "notification of record), news (newsletter, digest, periodical), "
        "noise (marketing, spam remnants, automated filler).\n"
        "Respond with ONLY a JSON object: {\"category\": \"...\", "
        "\"summary\": \"one tight factual sentence\"}\n\n"
        "FROM: %s <%s>\nSUBJECT: %s\nVERIFIED_PRINCIPAL: %s\nBODY:\n%s"
        % (m["from_name"] or "", m["from_addr"], m["subject"] or "",
           m["is_ryan"], triage_view(m["body_text"])[:TRIAGE_LIMIT])
    )
    content = [{"type": "text", "text": prompt}]
    shown = 0
    for img in m.get("_images", []):
        if shown >= VISION_COUNT:
            break
        if img["mime"] in IMG_VISION_TYPES and len(img["data"]) <= IMG_VISION_MAX:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": img["mime"],
                "data": base64.b64encode(img["data"]).decode()}})
            shown += 1
    if shown:
        content[0]["text"] += (
            "\n\nIMAGES: %d attached and shown below. If an image is the "
            "payload (a receipt, a document photo, a screenshot), read it and "
            "summarize from what it shows." % shown)
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": MODEL,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": content}],
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": ENV["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in data.get("content", []))
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        out = json.loads(text)
        cat = out.get("category")
        if cat not in CATEGORIES:
            cat = "action"
        if cat == "instruction" and not m["is_ryan"]:
            cat = "action"  # authority is verified, never inferred
        return cat, str(out.get("summary", ""))[:300]
    except Exception as e:
        log("triage failed (%s) — filing as action" % e)
        return "action", "[triage failed] " + (m["body_text"][:120] or m["subject"] or "").strip()


# ---------- store + notify ----------

def store(m, category, summary, files):
    row = {k: v for k, v in m.items() if not k.startswith("_")}
    row["category"] = category
    row["summary"] = summary
    row["files"] = files
    req = urllib.request.Request(
        ENV["SUPABASE_URL"].rstrip("/") + "/rest/v1/mail",
        data=json.dumps(row).encode(),
        headers={
            "Content-Type": "application/json",
            "apikey": ENV["SUPABASE_SERVICE_ROLE_KEY"],
            "Authorization": "Bearer " + ENV["SUPABASE_SERVICE_ROLE_KEY"],
            "Prefer": "resolution=ignore-duplicates,return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if not (200 <= r.status < 300):
            return None
        body = r.read().decode().strip()
        # empty array => the unique message_id already existed (e.g. restart
        # after a crash between store and marking Seen). Never ping twice.
        return "duplicate" if body in ("", "[]") else "inserted"


def discord(text):
    tok = ENV.get("DISCORD_BOT_TOKEN")
    chan = ENV.get("DISCORD_CHANNEL_ID", "1525050183312740384")
    if not tok or not chan:
        log("discord ping skipped: DISCORD_BOT_TOKEN/DISCORD_CHANNEL_ID not set")
        return
    req = urllib.request.Request(
        "https://discord.com/api/v10/channels/%s/messages" % chan,
        data=json.dumps({"content": text[:1900]}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bot %s" % tok,
            "User-Agent": "DiscordBot (hermes-mailwatch, 1.0)",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        log("discord failed: %s" % e)



def handle_message(raw):
    m = normalize(raw)
    files = capture_files(m)   # capture first, think second
    category, summary = triage(m)
    ok = store(m, category, summary, files)
    if not ok:
        return False
    if ok == "duplicate":
        log("already filed, skipping ping | %s | %s" % (m["from_addr"], m["subject"]))
        return True
    log("filed %s | %s | %s" % (category, m["from_addr"], m["subject"]))
    if category in ("instruction", "action"):
        tag = "INSTRUCTION" if category == "instruction" else "ACTION"
        att = "\n📎 " + ", ".join(m["attachments"]) if m.get("attachments") else ""
        discord(
            "**POST · %s**\n**%s** — %s\n%s%s"
            % (tag, m["from_name"] or m["from_addr"],
               m["subject"] or "(no subject)", summary, att)
        )
    if category == "instruction":
        # HOOK(hermes): feed m["body_text"] into the agent loop here,
        # exactly as if it were a Discord message from Ryan.
        pass
    return True


# ---------- dumb transport loop ----------

def process_unseen(conn):
    typ, data = conn.search(None, "UNSEEN")
    if typ != "OK":
        return
    for num in data[0].split():
        typ, msgdata = conn.fetch(num, "(BODY.PEEK[])")
        if typ != "OK" or not msgdata or msgdata[0] is None:
            continue
        try:
            if handle_message(msgdata[0][1]):
                conn.store(num, "+FLAGS", "\\Seen")
        except Exception as e:
            log("handling failed, left unseen: %s" % e)


def idle_wait(conn):
    """Hand-rolled IMAP IDLE. Returns when the server pushes or the cycle ends."""
    tag = conn._new_tag().decode()
    conn.send(("%s IDLE\r\n" % tag).encode())
    conn.sock.settimeout(IDLE_MINUTES * 60)
    try:
        resp = conn.readline()
        if not resp.startswith(b"+"):
            raise RuntimeError("IDLE refused: %r" % resp)
        while True:
            line = conn.readline()
            if b"EXISTS" in line or b"RECENT" in line:
                break
            if not line:
                raise RuntimeError("connection dropped in IDLE")
    except socket.timeout:
        pass  # quiet cycle — re-issue IDLE
    finally:
        try:
            conn.send(b"DONE\r\n")
            conn.readline()
        except Exception:
            pass
        conn.sock.settimeout(None)


def main():
    log("hermes mail watcher starting (model on mail only: %s)" % MODEL)
    while True:
        try:
            conn = imaplib.IMAP4_SSL("imap.gmail.com")
            conn.login(ENV["GMAIL_USER"], ENV["GMAIL_APP_PASSWORD"].replace(" ", ""))
            conn.select("INBOX")
            log("connected; processing backlog")
            process_unseen(conn)
            while True:
                try:
                    idle_wait(conn)          # real-time push, zero cost
                except Exception as e:
                    log("IDLE degraded (%s) — 60s poll this cycle" % e)
                    time.sleep(60)
                process_unseen(conn)
        except Exception as e:
            log("connection lost (%s) — reconnecting in 30s" % e)
            time.sleep(30)


if __name__ == "__main__":
    main()
