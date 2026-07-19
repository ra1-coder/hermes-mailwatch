#!/usr/bin/env python3
"""Hermes Discord capturer — the transcript recorder for the home channel.

Ruled by Ryan 19 Jul 2026 (spec 065c1cb0). Companion to hermes_mailwatch.py.

Deciding principle (Ryan): THE CAPTURER MUST OUTLIVE THE THINKER.
A gateway hook dies whenever the brain dies — capture would stop exactly when
the record matters most. This is an INDEPENDENT daemon: it reads the channel's
message history over the REST API and files every message — both directions —
before the agent reasons over it. It keeps filing even while the gateway is
down; the backlog is processed on return. It touches NOTHING about how Hermes
thinks: no model, runtime, prompt, or agent-config. Capture is a plain database
write — ZERO model calls per captured message.

Design contract (do not violate):
- CAPTURE FIRST, THINK SECOND. Every message becomes a raw_event. The agent
  reasons over the live message separately; this only files the original.
- BOTH DIRECTIONS. Inbound (Ryan / others) AND Hermes's own outbound sends are
  captured. Hermes's messages land in channel history like any other; we tag
  sender=hermes by matching the bot's own user id.
- FULL FIDELITY. raw_text stores the complete message content, never truncated
  or summarized. (Truncation is a model-view concern, never a storage one.)
- REAL PROVENANCE. raw_json carries the actual Discord fields: message id,
  author id + name, timestamp, channel id, guild id. No hand-built stubs.
- DEDUPE. source_object_id = the Discord message id. Inserts use
  resolution=ignore-duplicates so retries and reconnects never double-file.
- VITALS FLOOD LAW. Transcript rows file as processing_status=processed (note:
  "transcript capture — handled live by agent"), NEVER pending. A full
  transcript at pending would flood the Wire's "to route" count with chat noise.
- CREDENTIALS LAW. If a message matches a secret pattern (API key, token,
  password), store it with the secret MASKED and note the redaction. The
  credentials-live-in-two-homes law outranks full-fidelity storage for secrets.
- ATTACHMENTS. Originals mirror to the raw-attachments bucket under
  discord/YYYYMM/... and are referenced on the event (mirrors mailwatch).
- FAILURES ARE VISIBLE. A failed capture is logged and the cursor is NOT
  advanced past it, so the next cycle retries it. Nothing is silently dropped.

Transport: stdlib only, urllib REST polling — same plumbing the mailwatch
daemon already uses to talk to Discord. No discord.py, no WebSocket library
(none is installed, and hand-rolling RFC 6455 would break stdlib discipline).
Polling channel history also captures outbound sends for free and is what makes
the daemon structurally independent of the gateway.

Config via environment (reuses mailwatch.env — no new secrets):
  DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID,
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Optional:
  DISCORD_POLL_SECONDS (default 5)
  DISCORD_CURSOR_FILE  (default /data/hermes-intake/.discord_cursor)
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ENV = os.environ
API = "https://discord.com/api/v10"
UA = "DiscordBot (hermes-discord-capturer, 1.0)"
CAPTURE_BUCKET = "raw-attachments"      # capture bucket != vault (standing rule)
POLL_SECONDS = int(ENV.get("DISCORD_POLL_SECONDS", "5"))
CURSOR_FILE = ENV.get("DISCORD_CURSOR_FILE", "/data/hermes-intake/.discord_cursor")
ATT_STORE_MAX = 25 * 1024 * 1024        # capture anything reasonable
PROCESSED_NOTE = "transcript capture — handled live by agent"

# Secret patterns — matches masked, never stored in the clear. Ordered broad->narrow.
# Each entry: (compiled regex, human label). The captured secret span is replaced
# with a fixed marker so the surrounding message stays legible.
SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "anthropic key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai key"),
    (re.compile(r"sb_secret_[A-Za-z0-9_\-]{20,}"), "supabase secret"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "github token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{30,}"), "github pat"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "slack token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws access key id"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "google api key"),
    # Discord bot token shape: id.timestamp.hmac
    (re.compile(r"[MNO][A-Za-z0-9_\-]{23,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{27,}"), "discord token"),
    # eyJ... JWT (three base64url segments)
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "jwt"),
    # generic bearer
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"), "bearer token"),
    # key/token/password/secret = value  (assignment form)
    (re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|pwd)\b\s*[:=]\s*\S{6,}"), "credential assignment"),
]


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _need(name):
    v = ENV.get(name)
    if not v:
        raise SystemExit("missing required env: %s" % name)
    return v


# ---------- secrets: mask before storage ----------

def mask_secrets(text):
    """Return (masked_text, labels). labels is empty when nothing matched.

    A message that contains a credential is stored with the secret replaced by
    a fixed marker. The rest of the message is preserved at full fidelity.
    """
    if not text:
        return text, []
    labels = []
    masked = text
    for rx, label in SECRET_PATTERNS:
        def _sub(m, _l=label):
            labels.append(_l)
            return "[REDACTED:%s]" % _l
        masked = rx.sub(_sub, masked)
    # de-dup labels, preserve order
    seen, ordered = set(), []
    for l in labels:
        if l not in seen:
            seen.add(l); ordered.append(l)
    return masked, ordered


# ---------- discord REST ----------

def _discord_get(path):
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": "Bot " + _need("DISCORD_BOT_TOKEN"),
                 "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def whoami():
    me = _discord_get("/users/@me")
    return str(me["id"]), me.get("username")


def fetch_after(channel_id, after_id, limit=100):
    """Oldest-first list of messages strictly after after_id.

    Discord returns newest-first; we reverse so we file in chronological order
    and can advance the cursor safely one message at a time.
    """
    q = "?limit=%d" % limit
    if after_id:
        q += "&after=%s" % after_id
    msgs = _discord_get("/channels/%s/messages%s" % (channel_id, q))
    return list(reversed(msgs))


def fetch_latest_id(channel_id):
    """Newest message id in the channel — the go-forward starting cursor."""
    msgs = _discord_get("/channels/%s/messages?limit=1" % channel_id)
    return str(msgs[0]["id"]) if msgs else None


# ---------- attachments: originals to the capture bucket ----------

def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "")[:80] or "file"


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _bucket_put(path, data, mime):
    req = urllib.request.Request(
        _need("SUPABASE_URL").rstrip("/") + "/storage/v1/object/%s/%s"
        % (CAPTURE_BUCKET, urllib.parse.quote(path)),
        data=data,
        headers={
            "Content-Type": mime or "application/octet-stream",
            "apikey": _need("SUPABASE_SERVICE_ROLE_KEY"),
            "Authorization": "Bearer " + _need("SUPABASE_SERVICE_ROLE_KEY"),
            "x-upsert": "true",   # idempotent: crash-and-retry never duplicates
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return 200 <= r.status < 300


def capture_attachments(msg):
    """Mirror every attachment to discord/YYYYMM/<msgid>/NN-name. Returns records.

    A failed mirror is recorded visibly on the row (with an error), never
    swallowed — but it does not fail the whole message capture: the text and
    provenance still get filed.
    """
    out = []
    atts = msg.get("attachments") or []
    if not atts:
        return out
    stamp = time.strftime("%Y%m")
    mid = _safe(str(msg["id"]))
    for i, a in enumerate(atts, 1):
        name = _safe(a.get("filename") or ("att-%d" % i))
        path = "discord/%s/%s/%02d-%s" % (stamp, mid, i, name)
        rec = {"name": a.get("filename"), "bucket": CAPTURE_BUCKET, "path": path,
               "mime": a.get("content_type"), "size": a.get("size"),
               "source_url": a.get("url")}
        try:
            size = a.get("size") or 0
            if size and size > ATT_STORE_MAX:
                rec["error"] = "over store ceiling (%d bytes) — reference kept" % size
                out.append(rec); continue
            data = _download(a["url"])
            if _bucket_put(path, data, a.get("content_type")):
                rec["size"] = len(data)
            else:
                rec["error"] = "bucket put non-2xx"
        except Exception as e:
            log("attachment mirror failed for %s: %s" % (name, e))
            rec["error"] = str(e)[:200]
        out.append(rec)
    return out


# ---------- store: one raw_event per message ----------

def build_row(msg, bot_id, attachments):
    author = msg.get("author") or {}
    author_id = str(author.get("id", ""))
    is_hermes = author_id == bot_id
    # display name: prefer server nick-ish global_name, fall back to username
    author_name = author.get("global_name") or author.get("username") or "unknown"
    sender = "hermes" if is_hermes else author_name

    content = msg.get("content") or ""
    masked_text, redactions = mask_secrets(content)

    note = PROCESSED_NOTE
    if redactions:
        note = "%s; secret(s) masked before storage: %s" % (
            PROCESSED_NOTE, ", ".join(redactions))

    raw_json = {
        "platform": "discord",
        "message_id": str(msg["id"]),
        "author_id": author_id,
        "author_name": author_name,
        "author_username": author.get("username"),
        "is_bot": bool(author.get("bot", False)),
        "direction": "outbound" if is_hermes else "inbound",
        "timestamp": msg.get("timestamp"),
        "edited_timestamp": msg.get("edited_timestamp"),
        "channel_id": str(msg.get("channel_id", "")),
        "guild_id": str(msg["guild_id"]) if msg.get("guild_id") else None,
        "message_type": msg.get("type"),
        "reply_to": (msg.get("referenced_message") or {}).get("id"),
    }
    if redactions:
        raw_json["redactions"] = redactions

    return {
        "source": "discord",
        "source_object_id": str(msg["id"]),
        "sender": sender,
        "raw_text": masked_text,
        "raw_json": raw_json,
        "attachments": attachments,
        "processing_status": "processed",   # VITALS FLOOD law: never pending
        "processing_note": note,
    }, is_hermes


def store(row):
    """Insert one raw_event. ignore-duplicates makes reconnects safe.

    Returns 'inserted', 'duplicate', or raises so the caller leaves the cursor
    where it is and retries next cycle.
    """
    req = urllib.request.Request(
        _need("SUPABASE_URL").rstrip("/") + "/rest/v1/raw_events",
        data=json.dumps(row).encode(),
        headers={
            "Content-Type": "application/json",
            "apikey": _need("SUPABASE_SERVICE_ROLE_KEY"),
            "Authorization": "Bearer " + _need("SUPABASE_SERVICE_ROLE_KEY"),
            "Prefer": "resolution=ignore-duplicates,return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if not (200 <= r.status < 300):
            raise RuntimeError("store non-2xx: %s" % r.status)
        body = r.read().decode().strip()
        return "duplicate" if body in ("", "[]") else "inserted"


# ---------- cursor: go-forward only, survives restart ----------

def load_cursor():
    try:
        with open(CURSOR_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_cursor(mid):
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(mid))
    os.replace(tmp, CURSOR_FILE)


# ---------- the dumb transport loop ----------

def handle_message(msg, bot_id):
    """Capture one message. Returns True on success (cursor may advance)."""
    attachments = capture_attachments(msg)   # capture originals first
    row, is_hermes = build_row(msg, bot_id, attachments)
    result = store(row)
    who = "hermes" if is_hermes else row["sender"]
    if result == "duplicate":
        log("already filed %s | %s | %s" % (msg["id"], who, (row["raw_text"] or "")[:60]))
    else:
        log("filed %s | %s | %s" % (msg["id"], who, (row["raw_text"] or "")[:60]))
    return True


def main():
    channel_id = _need("DISCORD_CHANNEL_ID")
    _need("SUPABASE_URL"); _need("SUPABASE_SERVICE_ROLE_KEY")
    bot_id, bot_name = whoami()
    log("discord capturer starting | bot=%s (%s) | channel=%s | poll=%ss"
        % (bot_name, bot_id, channel_id, POLL_SECONDS))

    cursor = load_cursor()
    if cursor is None:
        # GO-FORWARD ONLY: the lost days are accepted as unrecoverable. Start at
        # the newest message so we never backfill history.
        cursor = fetch_latest_id(channel_id)
        if cursor:
            save_cursor(cursor)
            log("no cursor on file — anchoring go-forward at latest id %s" % cursor)
        else:
            log("channel empty — will anchor on first message")

    while True:
        try:
            batch = fetch_after(channel_id, cursor, limit=100)
            for msg in batch:
                # File this message. If it fails, STOP advancing the cursor so
                # the next cycle retries from here — never silently drop.
                try:
                    handle_message(msg, bot_id)
                except Exception as e:
                    log("capture FAILED for %s (%s) — cursor held, will retry"
                        % (msg.get("id"), e))
                    raise  # break out to the outer sleep+retry
                cursor = str(msg["id"])
                save_cursor(cursor)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            if e.code == 429:
                log("rate limited (429) — backing off 10s | %s" % body)
                time.sleep(10)
            else:
                log("discord/store HTTP %s (%s) — retry in %ss" % (e.code, body, POLL_SECONDS))
                time.sleep(POLL_SECONDS)
            continue
        except Exception as e:
            log("cycle error (%s) — retry in %ss" % (e, POLL_SECONDS))
            time.sleep(POLL_SECONDS)
            continue
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
