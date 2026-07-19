#!/usr/bin/env python3
"""Hermes intake toolkit — the universal pipe, agent side.

Doctrine (mirrors SOUL.md / Intake Doctrine):
  Raw Event -> Normalized Object -> Artifact -> Action -> PWA.
  1. CAPTURE FIRST. Every input becomes a raw_event before interpretation.
  2. EXPLAIN ROUTING. Every processed event carries a plain-language note.
  3. HIGH BAR FOR ARTIFACTS. Most events end as no_action. That is success.
  4. NEVER LOSE THE THREAD. Every artifact points at its source raw_event.
  5. FAILURES ARE VISIBLE. Any tool/sync failure -> status 'failed' + reason.

Stdlib only. Config via environment (same .env as mailwatch):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

Use as a library (import hermes_intake) or as a CLI:

  # 1) capture an input the moment it arrives (BEFORE thinking about it)
  python3 hermes_intake.py capture --source telegram --object-id "tg-<msg id>" \
      --sender "Ryan" --text "full message text" [--attach /path/to/file ...]
  -> prints JSON {"id": "<raw_event uuid>", "duplicate": false, ...}

  # 2) after routing, record the outcome and the reason
  python3 hermes_intake.py mark --id <raw_event uuid> --status processed \
      --note "Created task: follow up W-Sensors onboarding (dated request from Christer)."
  # statuses: processed | no_action | failed

  # 3) create an artifact born from that event (keeps the thread)
  python3 hermes_intake.py artifact --type task --desk front_desk \
      --title "Follow up W-Sensors onboarding" --body "..." \
      --source-event <raw_event uuid> [--project <project uuid>] \
      [--link entity:<uuid>:mentions] [--link raw_event:<uuid>:derived_from]

  # 4) retrieval: "pull me the june 2026 report"
  python3 hermes_intake.py search --q "june 2026 sales report"

  # 5) store a file permanently (returns storage path for raw_json/metadata)
  python3 hermes_intake.py upload --file /path/to/report.pdf

  Valid vocabularies (schema truth — do not guess, do not infer from examples):
    sources:        telegram | discord | gmail | calendar | pwa | file | web
    relationships:  mentions | belongs_to | supersedes | caused_by | assigned_to | derived_from
    entity types:   person | company | project | asset | place | product | account
    desks:          front_desk | concierge | trip_radar | war_room | workshop

  # 6) propose/link entities (exact + alias match only; never guess)
  python3 hermes_intake.py entity-find --name "Christer"
  python3 hermes_intake.py entity-add --type person --name "Christer" --alias "christer@..."
"""
import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV = os.environ
BASE = ENV.get("SUPABASE_URL", "").rstrip("/")
KEY = ENV.get("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = "raw-attachments"
SOURCES = ["telegram", "discord", "gmail", "calendar", "pwa", "file", "web"]
MARKS = ["processed", "no_action", "failed"]


def _die(msg, code=2):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(code)


def _req(method, path, body=None, headers=None, raw=False):
    if not BASE or not KEY:
        _die("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing from environment")
    h = {
        "apikey": KEY,
        "Authorization": f"Bearer {KEY}",
    }
    if not raw:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    data = None
    if body is not None:
        data = body if raw else json.dumps(body).encode()
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            out = resp.read().decode() or "null"
            return json.loads(out) if out.strip().startswith(("[", "{", "n")) else out
    except urllib.error.HTTPError as e:
        _die(f"{method} {path} -> {e.code}: {e.read().decode()[:400]}")


def dedupe_hash(text, sender, when_iso):
    """Content hash: normalized text + sender + calendar day. Advisory."""
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    norm = re.sub(r"^(fwd?|re):\s*", "", norm)
    day = (when_iso or "")[:10]
    return hashlib.sha256(f"{norm}|{(sender or '').lower()}|{day}".encode()).hexdigest()


# ————— capture —————

def capture(source, object_id, sender=None, text=None, raw_json=None, attachments=None):
    if source not in SOURCES:
        _die(f"source must be one of {SOURCES}")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    row = {
        "source": source,
        "source_object_id": object_id,
        "sender": sender,
        "raw_text": text,
        "raw_json": raw_json or {},
        "attachments": attachments or [],
        "dedupe_hash": dedupe_hash(text, sender, now),
        "processing_status": "pending",
    }
    res = _req(
        "POST", "/rest/v1/raw_events", body=row,
        headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
    )
    if res:  # inserted
        ev = res[0]
        # advisory cross-source duplicate check
        twins = _req(
            "GET",
            "/rest/v1/raw_events?select=id,source,received_at"
            f"&dedupe_hash=eq.{ev['dedupe_hash']}&id=neq.{ev['id']}",
        ) or []
        return {"id": ev["id"], "duplicate": False,
                "possible_twins": twins,
                "hint": "If a twin exists, prefer ONE artifact linked to BOTH events."}
    # conflict: same source object already captured — fetch it
    q = urllib.parse.quote(object_id, safe="")
    ex = _req("GET", f"/rest/v1/raw_events?select=id&source=eq.{source}&source_object_id=eq.{q}")
    return {"id": ex[0]["id"] if ex else None, "duplicate": True,
            "hint": "Already on the wire. Do not create anything new from this."}


# ————— mark (the explanation is mandatory) —————

def mark(event_id, status, note):
    if status not in MARKS:
        _die(f"status must be one of {MARKS}")
    if not note or len(note.strip()) < 8:
        _die("note is mandatory: one plain sentence explaining the routing (or the failure)")
    body = {
        "processing_status": status,
        "processing_note": note.strip(),
        "processed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    res = _req("PATCH", f"/rest/v1/raw_events?id=eq.{event_id}", body=body,
               headers={"Prefer": "return=representation"})
    return {"ok": bool(res), "id": event_id, "status": status}


# ————— artifacts born from events —————

def artifact(a_type, title, body=None, desk="front_desk", status="new",
             project=None, source_event=None, metadata=None, links=None, due=None):
    row = {"type": a_type, "title": title, "body": body, "desk": desk,
           "status": status, "metadata": metadata or {}}
    if due:
        row["due_at"] = due  # ISO 8601; feeds Today (due 48h) and Inbox (overdue)
    if project:
        row["project_id"] = project
    if source_event:
        row["source_raw_event_id"] = source_event
    res = _req("POST", "/rest/v1/artifacts", body=row,
               headers={"Prefer": "return=representation"})
    art = res[0]
    made = []
    all_links = list(links or [])
    if source_event:
        all_links.append(("raw_event", source_event, "derived_from"))
    for (ltype, lid, rel) in all_links:
        _req("POST", "/rest/v1/artifact_links",
             body={"artifact_id": art["id"], "linked_type": ltype,
                   "linked_id": lid, "relationship": rel},
             headers={"Prefer": "resolution=ignore-duplicates"})
        made.append(f"{rel}->{ltype}:{lid[:8]}")
    return {"id": art["id"], "title": art["title"], "links": made}


# ————— retrieval —————

def search(q):
    qq = urllib.parse.quote(q)
    arts = _req("GET", "/rest/v1/artifacts?select=id,type,title,status,metadata,source_raw_event_id,created_at"
                       f"&search_vector=plfts.{qq}&order=created_at.desc&limit=10") or []
    raw = []
    if len(arts) < 3:  # fallback: the originals are searchable too
        raw = _req("GET", "/rest/v1/raw_events?select=id,source,sender,received_at,attachments"
                          f"&raw_text=plfts.{qq}&order=received_at.desc&limit=5") or []
    return {"artifacts": arts, "raw_events": raw}


# ————— file vault —————

def upload(path):
    name = os.path.basename(path)
    day = dt.date.today().isoformat()
    key = f"{day}/{name}"
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        blob = f.read()
    _req("POST", f"/storage/v1/object/{BUCKET}/{urllib.parse.quote(key)}",
         body=blob, raw=True,
         headers={"Content-Type": ctype, "x-upsert": "true"})
    return {"bucket": BUCKET, "path": key, "bytes": len(blob),
            "hint": "Store this path in raw_event attachments and artifact metadata."}


def download_url(key, expires=3600):
    res = _req("POST", f"/storage/v1/object/sign/{BUCKET}/{urllib.parse.quote(key)}",
               body={"expiresIn": expires})
    return {"url": f"{BASE}/storage/v1{res['signedURL']}" if isinstance(res, dict) else res}


# ————— entities: exact + alias only, never guess —————

def entity_find(name):
    n = urllib.parse.quote(name)
    exact = _req("GET", f"/rest/v1/entities?select=*&canonical_name=ilike.{n}") or []
    alias = _req(
        "GET",
        "/rest/v1/entities?select=*&aliases=cs."
        + urllib.parse.quote("{" + json.dumps(name) + "}", safe=""),
    ) or []
    seen, out = set(), []
    for e in exact + alias:
        if e["id"] not in seen:
            seen.add(e["id"]); out.append(e)
    return {"matches": out,
            "hint": "No match => ASK RYAN before creating the entity (approval layer)."}


def entity_add(e_type, name, aliases=None, metadata=None):
    res = _req("POST", "/rest/v1/entities",
               body={"type": e_type, "canonical_name": name,
                     "aliases": aliases or [], "metadata": metadata or {}},
               headers={"Prefer": "return=representation"})
    return {"id": res[0]["id"], "canonical_name": name}


# ————— CLI —————

def main():
    p = argparse.ArgumentParser(description="Hermes intake toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture")
    c.add_argument("--source", required=True); c.add_argument("--object-id", required=True)
    c.add_argument("--sender"); c.add_argument("--text")
    c.add_argument("--json", dest="raw_json"); c.add_argument("--attach", action="append")

    m = sub.add_parser("mark")
    m.add_argument("--id", required=True); m.add_argument("--status", required=True)
    m.add_argument("--note", required=True)

    a = sub.add_parser("artifact")
    a.add_argument("--type", required=True); a.add_argument("--title", required=True)
    a.add_argument("--body"); a.add_argument("--desk", default="front_desk")
    a.add_argument("--status", default="new"); a.add_argument("--project")
    a.add_argument("--source-event"); a.add_argument("--metadata")
    a.add_argument("--due", help="ISO 8601 deadline, e.g. 2026-07-10T18:00:00+08:00")
    a.add_argument("--link", action="append",
                   help="linked_type:uuid:relationship (e.g. entity:...:mentions)")

    s = sub.add_parser("search"); s.add_argument("--q", required=True)
    u = sub.add_parser("upload"); u.add_argument("--file", required=True)
    d = sub.add_parser("sign"); d.add_argument("--path", required=True)
    ef = sub.add_parser("entity-find"); ef.add_argument("--name", required=True)
    ea = sub.add_parser("entity-add")
    ea.add_argument("--type", required=True); ea.add_argument("--name", required=True)
    ea.add_argument("--alias", action="append")

    args = p.parse_args()

    if args.cmd == "capture":
        atts = []
        for f in (args.attach or []):
            atts.append(upload(f))
        out = capture(args.source, args.object_id, args.sender, args.text,
                      json.loads(args.raw_json) if args.raw_json else {},
                      [{"bucket": x["bucket"], "path": x["path"]} for x in atts])
    elif args.cmd == "mark":
        out = mark(args.id, args.status, args.note)
    elif args.cmd == "artifact":
        links = []
        for l in (args.link or []):
            t, i, r = l.split(":", 2); links.append((t, i, r))
        out = artifact(args.type, args.title, args.body, args.desk, args.status,
                       args.project, args.source_event,
                       json.loads(args.metadata) if args.metadata else {}, links,
                       args.due)
    elif args.cmd == "search":
        out = search(args.q)
    elif args.cmd == "upload":
        out = upload(args.file)
    elif args.cmd == "sign":
        out = download_url(args.path)
    elif args.cmd == "entity-find":
        out = entity_find(args.name)
    elif args.cmd == "entity-add":
        out = entity_add(args.type, args.name, args.alias)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
