#!/usr/bin/env python3
"""Hermes Discord capturer — the transcript recorder for the home channel.

Ruled by Ryan 19 Jul 2026 (spec 065c1cb0). Companion to hermes_mailwatch.py.

Deciding principle (Ryan): THE CAPTURER MUST OUTLIVE THE THINKER.
A gateway hook dies whenever the brain dies — capture would stop exactly when
the record matters most. This is an INDEPENDENT daemon: it reads channel
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
- ALL THREADS OF THE HOME CHANNEL, ACTIVE AND ARCHIVED (fix, 2 Sep 2026,
  task filed same date). Ryan's conversations with Hermes happen in per-message
  THREADS off the home channel (Discord auto-threads each top-level message).
  GET /channels/{home}/messages only ever returns the thread-STARTER message —
  every reply inside a thread lives in a separate channel-shaped object
  (the thread itself) and is invisible to a capturer that only polls the
  parent. This was the live bug found 2 Sep 2026: an entire session's replies
  (agent + Ryan) went uncaptured while direct top-level nudges were fine.
  FIX: track the home channel PLUS every thread parented to it (active via
  GET /guilds/{g}/threads/active, archived via
  GET /channels/{home}/threads/archived/public, paginated), each with its own
  go-forward cursor, re-discovering new/newly-archived threads on a timer.
- EMBED + COMPONENT TEXT (retested + actually implemented 2 Sep 2026 — task
  7c048c4e was marked done in the store but the deployed code never touched
  msg['embeds'] or msg['components']; that claim was false, corrected here).
  Hermes's own question cards / approval blocks render as embeds with the
  visible text in embed title/description/fields/footer, and buttons carry
  label text in components — msg['content'] alone is blank or near-blank for
  these. flatten_embeds()/flatten_components() pull that text into raw_text
  (delimited, full fidelity) and the raw embeds/components JSON (secret-masked)
  is preserved in raw_json for structure.
- FULL FIDELITY. raw_text stores the complete message content (+ flattened
  embed/component text), never truncated or summarized.
- REAL PROVENANCE. raw_json carries the actual Discord fields: message id,
  author id + name, timestamp, channel id, guild id, thread id/parent when
  applicable. No hand-built stubs.
- DEDUPE. source_object_id = the Discord message id. Inserts use
  resolution=ignore-duplicates so retries, reconnects, and the one-time
  backfill script (backfill_discord_threads.py) never double-file.
- VITALS FLOOD LAW. Transcript rows file as processing_status=processed (note:
  "transcript capture — handled live by agent"), NEVER pending. A full
  transcript at pending would flood the Wire's "to route" count with chat noise.
- CREDENTIALS LAW. If a message (or embed/component text) matches a secret
  pattern (API key, token, password), store it with the secret MASKED and
  note the redaction. The credentials-live-in-two-homes law outranks
  full-fidelity storage for secrets.
- ATTACHMENTS. Originals mirror to the raw-attachments bucket under
  discord/YYYYMM/... and are referenced on the event (mirrors mailwatch).
- FAILURES ARE VISIBLE. A failed capture is logged and that channel's cursor
  is NOT advanced past it, so the next cycle retries it. A failure on one
  tracked channel (e.g. a deleted/locked thread) never blocks polling of the
  others. Nothing is silently dropped.

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
  DISCORD_THREAD_REFRESH_SECONDS (default 300) — how often to re-discover
    active/newly-archived threads under the home channel.
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
UA = "DiscordBot (hermes-discord-capturer, 1.1)"
CAPTURE_BUCKET = "raw-attachments"      # capture bucket != vault (standing rule)
POLL_SECONDS = int(ENV.get("DISCORD_POLL_SECONDS", "5"))
THREAD_REFRESH_SECONDS = int(ENV.get("DISCORD_THREAD_REFRESH_SECONDS", "300"))
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


def mask_json_secrets(obj):
    """Mask secrets inside an arbitrary JSON-able structure (embeds/components).

    Full-fidelity structure is preserved by round-tripping through JSON text:
    serialize -> mask the text -> parse back. If the masked text somehow isn't
    valid JSON anymore (a mask marker landing awkwardly), fall back to storing
    the masked text itself rather than losing the redaction.
    """
    if not obj:
        return obj, []
    text = json.dumps(obj, ensure_ascii=False)
    masked_text, labels = mask_secrets(text)
    if not labels:
        return obj, []
    try:
        return json.loads(masked_text), labels
    except Exception:
        return {"_masked_raw": masked_text}, labels


# ---------- embed / component flattening (fixes task 7c048c4e for real) ----------

def flatten_embeds(embeds):
    """Pull visible text out of embeds into plain lines: title, description,
    each field name+value, footer, author. This is what Hermes's own question
    cards, approval blocks, and briefs render as — msg['content'] is blank for
    these, so without this the record shows an empty outbound message.
    """
    parts = []
    for e in (embeds or []):
        if e.get("author", {}).get("name"):
            parts.append("[EMBED AUTHOR] %s" % e["author"]["name"])
        if e.get("title"):
            parts.append("[EMBED TITLE] %s" % e["title"])
        if e.get("description"):
            parts.append("[EMBED DESC] %s" % e["description"])
        for f in (e.get("fields") or []):
            parts.append("[EMBED FIELD] %s: %s" % (f.get("name", ""), f.get("value", "")))
        footer = e.get("footer") or {}
        if footer.get("text"):
            parts.append("[EMBED FOOTER] %s" % footer["text"])
    return parts


def flatten_components(components):
    """Pull button/select-option labels out of action rows (Approve/Edit/Cancel
    etc.), recursing into nested component containers.
    """
    parts = []

    def walk(comps):
        for c in (comps or []):
            label = c.get("label")
            if label:
                parts.append("[BUTTON] %s" % label)
            for opt in (c.get("options") or []):
                if opt.get("label"):
                    parts.append("[OPTION] %s" % opt["label"])
            if c.get("components"):
                walk(c["components"])

    walk(components)
    return parts


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


def channel_guild_id(channel_id):
    """The guild a channel belongs to.

    REAL provenance, not a guess: GET /channels/{id}/messages does NOT echo
    guild_id on message objects (only the gateway MESSAGE_CREATE event does),
    but the channel object itself carries it. Every message in this channel
    belongs to this guild by definition, so we resolve it once at startup and
    stamp it on each row (threads inherit the same guild). A DM channel has no
    guild -> None.
    """
    try:
        ch = _discord_get("/channels/%s" % channel_id)
        gid = ch.get("guild_id")
        return str(gid) if gid else None
    except Exception as e:
        log("guild resolve failed (%s) — rows will carry guild_id=null" % e)
        return None


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


# ---------- thread discovery: ALL threads of the home channel, active + archived ----------

def list_active_threads(guild_id, parent_channel_id):
    """Every active thread in the guild, filtered to this parent channel.

    GET /guilds/{g}/threads/active is guild-wide (there is no per-channel
    active-threads endpoint), so we filter client-side on parent_id.
    """
    if not guild_id:
        return []
    try:
        data = _discord_get("/guilds/%s/threads/active" % guild_id)
        threads = data.get("threads") or []
        return [t for t in threads if str(t.get("parent_id")) == str(parent_channel_id)]
    except Exception as e:
        log("active-thread discovery failed (%s)" % e)
        return []


def list_archived_public_threads(parent_channel_id):
    """Every archived PUBLIC thread parented to this channel, paginated.

    Archived threads age out of the active-threads endpoint (auto-archive is
    1440 min per the thread's metadata) — without this call, a thread that
    goes quiet for a day drops off discovery entirely even though Discord
    still serves its full history.
    """
    out = []
    before = None
    for _ in range(20):  # hard cap: 20 pages * 100 = 2000 threads, plenty
        q = "?limit=100"
        if before:
            q += "&before=%s" % urllib.parse.quote(before)
        try:
            data = _discord_get("/channels/%s/threads/archived/public%s" % (parent_channel_id, q))
        except Exception as e:
            log("archived-thread discovery failed (%s)" % e)
            break
        threads = data.get("threads") or []
        out.extend(threads)
        if not data.get("has_more") or not threads:
            break
        before = threads[-1].get("thread_metadata", {}).get("archive_timestamp")
        if not before:
            break
    return out


def discover_tracked_channels(home_channel_id, guild_id):
    """Home channel + every thread (active and archived) parented to it.

    Returns a dict {channel_id: thread_or_none} — thread_or_none is the
    Discord thread object (for logging/name) or None for the home channel
    itself.
    """
    tracked = {str(home_channel_id): None}
    for t in list_active_threads(guild_id, home_channel_id):
        tracked[str(t["id"])] = t
    for t in list_archived_public_threads(home_channel_id):
        tracked[str(t["id"])] = t
    return tracked


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

def build_row(msg, bot_id, attachments, guild_id=None, home_channel_id=None):
    author = msg.get("author") or {}
    author_id = str(author.get("id", ""))
    is_hermes = author_id == bot_id
    # display name: prefer server nick-ish global_name, fall back to username
    author_name = author.get("global_name") or author.get("username") or "unknown"
    sender = "hermes" if is_hermes else author_name

    content = msg.get("content") or ""
    channel_id = str(msg.get("channel_id", ""))
    is_thread = bool(home_channel_id) and channel_id != str(home_channel_id)

    # Embed + component text (task 7c048c4e, actually implemented this pass).
    embed_parts = flatten_embeds(msg.get("embeds"))
    component_parts = flatten_components(msg.get("components"))
    extra_parts = embed_parts + component_parts
    full_text = content
    if extra_parts:
        full_text = (content + "\n" if content else "") + "\n".join(extra_parts)

    masked_text, redactions = mask_secrets(full_text)
    masked_embeds, embed_redactions = mask_json_secrets(msg.get("embeds"))
    masked_components, comp_redactions = mask_json_secrets(msg.get("components"))
    all_redactions = redactions + [l for l in embed_redactions if l not in redactions] \
        + [l for l in comp_redactions if l not in redactions]

    note = PROCESSED_NOTE
    if all_redactions:
        note = "%s; secret(s) masked before storage: %s" % (
            PROCESSED_NOTE, ", ".join(all_redactions))

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
        "channel_id": channel_id,
        "guild_id": (str(msg["guild_id"]) if msg.get("guild_id") else guild_id),
        "message_type": msg.get("type"),
        "reply_to": (msg.get("referenced_message") or {}).get("id"),
        "is_thread": is_thread,
        "thread_id": channel_id if is_thread else None,
        "parent_channel_id": str(home_channel_id) if (home_channel_id and is_thread) else None,
    }
    if masked_embeds:
        raw_json["embeds"] = masked_embeds
    if masked_components:
        raw_json["components"] = masked_components
    if all_redactions:
        raw_json["redactions"] = all_redactions

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


# ---------- cursor: per-channel, go-forward only per channel, survives restart ----------
#
# File format is JSON: {"channels": {"<channel_id>": "<last_msg_id>", ...}}.
# Back-compat: a pre-existing cursor file from before multi-thread support was
# a bare message-id string (the home channel's cursor only) — migrate it in
# place under the home channel's id on first load rather than losing it.

def load_cursors(home_channel_id):
    try:
        with open(CURSOR_FILE) as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "channels" in data:
            return {str(k): str(v) for k, v in data["channels"].items()}
    except Exception:
        pass
    # legacy bare-string cursor: it was always the home channel's cursor
    return {str(home_channel_id): raw}


def save_cursors(cursors):
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"channels": cursors}, f)
    os.replace(tmp, CURSOR_FILE)


# ---------- the dumb transport loop ----------

def handle_message(msg, bot_id, guild_id=None, home_channel_id=None):
    """Capture one message. Returns True on success (cursor may advance)."""
    attachments = capture_attachments(msg)   # capture originals first
    row, is_hermes = build_row(msg, bot_id, attachments, guild_id, home_channel_id)
    result = store(row)
    who = "hermes" if is_hermes else row["sender"]
    tag = "" if str(msg.get("channel_id")) == str(home_channel_id) else " [thread %s]" % msg.get("channel_id")
    if result == "duplicate":
        log("already filed %s%s | %s | %s" % (msg["id"], tag, who, (row["raw_text"] or "")[:60]))
    else:
        log("filed %s%s | %s | %s" % (msg["id"], tag, who, (row["raw_text"] or "")[:60]))
    return True


def poll_channel(channel_id, cursors, bot_id, guild_id, home_channel_id, is_new):
    """Poll one channel, advancing its cursor as messages file successfully.

    is_new=True means this channel was JUST discovered this cycle (e.g. a
    fresh thread). Per the go-forward-only policy (mirrors the home channel's
    original anchor rule), a newly discovered channel anchors at its CURRENT
    latest message id rather than walking its full history live — the daemon
    fix is go-forward; recovering the already-existing backlog for threads
    that predate this fix is the separate one-time backfill script
    (backfill_discord_threads.py), which is explicitly allowed to walk full
    history because it's a bounded one-time job, not an ever-growing live poll.
    """
    cursor = cursors.get(channel_id)
    if cursor is None:
        if is_new:
            latest = fetch_latest_id(channel_id)
            cursors[channel_id] = latest  # may be None if channel is empty
            log("new channel tracked, anchored go-forward at %s: %s" % (latest, channel_id))
            return
        cursor = None  # known channel with no messages yet ever seen

    batch = fetch_after(channel_id, cursor, limit=100)
    for msg in batch:
        try:
            handle_message(msg, bot_id, guild_id, home_channel_id)
        except Exception as e:
            log("capture FAILED for %s in channel %s (%s) — cursor held, will retry"
                % (msg.get("id"), channel_id, e))
            raise
        cursor = str(msg["id"])
        cursors[channel_id] = cursor
        save_cursors(cursors)


def main():
    channel_id = _need("DISCORD_CHANNEL_ID")
    _need("SUPABASE_URL"); _need("SUPABASE_SERVICE_ROLE_KEY")
    bot_id, bot_name = whoami()
    guild_id = channel_guild_id(channel_id)
    log("discord capturer starting | bot=%s (%s) | home_channel=%s | guild=%s | poll=%ss | thread_refresh=%ss"
        % (bot_name, bot_id, channel_id, guild_id, POLL_SECONDS, THREAD_REFRESH_SECONDS))

    cursors = load_cursors(channel_id)
    if channel_id not in cursors:
        # GO-FORWARD ONLY on first ever run: the lost days are accepted as
        # unrecoverable for the home channel itself (original spec). Start at
        # the newest message so we never backfill history live.
        latest = fetch_latest_id(channel_id)
        cursors[channel_id] = latest
        save_cursors(cursors)
        log("no cursor on file for home channel — anchoring go-forward at latest id %s" % latest)

    tracked = {channel_id: None}
    last_thread_refresh = 0

    while True:
        try:
            now = time.time()
            if now - last_thread_refresh >= THREAD_REFRESH_SECONDS:
                discovered = discover_tracked_channels(channel_id, guild_id)
                new_ids = set(discovered) - set(tracked)
                if new_ids:
                    log("thread discovery: %d new thread(s) of %d tracked total"
                        % (len(new_ids), len(discovered)))
                tracked = discovered
                last_thread_refresh = now

            for cid in list(tracked.keys()):
                is_new = cid not in cursors
                try:
                    poll_channel(cid, cursors, bot_id, guild_id, channel_id, is_new)
                except urllib.error.HTTPError as e:
                    body = ""
                    try:
                        body = e.read().decode()[:200]
                    except Exception:
                        pass
                    if e.code == 429:
                        log("rate limited (429) on %s — backing off 10s | %s" % (cid, body))
                        time.sleep(10)
                    elif e.code == 404:
                        log("channel %s gone (404) — dropping from tracked set" % cid)
                        tracked.pop(cid, None)
                    else:
                        log("discord/store HTTP %s on %s (%s) — will retry next cycle" % (e.code, cid, body))
                except Exception as e:
                    log("cycle error on channel %s (%s) — will retry next cycle" % (cid, e))
        except Exception as e:
            log("outer cycle error (%s) — retry in %ss" % (e, POLL_SECONDS))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
