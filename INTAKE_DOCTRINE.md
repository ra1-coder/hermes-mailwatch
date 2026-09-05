# Intake Doctrine — the spine

Everything that enters my world follows one pipe, no exceptions:
Raw Event → Normalized Object → Artifact → Action → Control Room.
The toolkit is `/opt/hermes/hermes_intake.py`. I use its CLI for every step.

## 1. Capture first, think second

The moment anything arrives — a Telegram message, a file, a forwarded email,
a note — I run `capture` BEFORE interpreting it. The original is preserved
exactly as it arrived; files go into the vault via `--attach`. If capture
says `duplicate: true`, I stop: it is already on the wire.

## 2. High bar for artifacts

Most raw events should become nothing. Casual chat, acknowledgments, noise:
I mark them `no_action` and move on. I create an artifact only when there is
a concrete reason — a date, a request, a decision, a named commitment, a
document worth retrieving later. Ryan's attention is the scarce resource;
storage is not.

## 2b. The standing-law check, wired into intake itself (25 Jul 2026)

Before filing ANYTHING from Ryan — before choosing `no_action` vs an
artifact, before choosing a type or a desk — I run two checks, in order,
against the current head rules (fetch fresh from the ledger; never trust an
id cached in a doc):

1. **THE PLAIN SPEECH LAW** (ledger `rule`, head as of 20 Jul 2026: `ccd4fab9`).
   If Ryan's words carry an unstated purpose that changes how the item should
   be filed or settled (what a receipt/payment is for, who it's between,
   what the finish line is), I ask the plain question NOW, before marking the
   event `processed`/`no_action` — I do not file first and ask later. A
   `no_action` mark is itself a filing decision the law applies to.
2. **THE LOOSE-END ROUTING TEST — person, not date** (ledger `rule`, head as
   of 25 Jul 2026: `f199709d`, supersedes `6bd5b65e`). Before routing an
   intention to Front Desk, I ask: is there a PERSON on the other side of
   this, unsettled, in either direction? If yes, it is a `loose_end`
   (concierge), regardless of whether it carries a date — a due date is not
   the test. Only a solo errand with no counterparty stays Front Desk.

Failure mode this closes: a receipt was marked `no_action` without asking
its plain-language purpose question six minutes after the law took effect,
and a person-directed thank-you was routed to Front Desk on a date-based
reflex instead of the person-test. Both were live misses, corrected
25 Jul 2026 (see ledger `rule` `f199709d` metadata and the reopened
`raw_events` row for the receipt). This section exists so the check runs
inline during filing, not as a later audit.

**Amendment, 4 Sep 2026 (task 215ba9b6):** check 2 above is superseded in
its phrasing by THE WAITING TEST (ledger `rule` `498125c6`, amended
4 Sep 2026) — "is someone waiting on you to deliver something?" — same
routing outcome (person + unsettled = loose_end), sharper question. A third
miss on 3 Sep 2026 ("Pay Uncle Jo ₱620" filed front_desk, nine days after
the rule existed) showed that a prose-only check in this file is a check
that can be silently skipped — the rule existed and was simply not
consulted at filing time. THE NAME TRIGGER closes that hole as CODE, not
doctrine: `hermes_intake.py artifact` now mechanically scans every filing's
title+body for a named person and hard-blocks non-loose_end filings until
the Waiting Test is answered (`--waiting-test-ack`) or the trigger is
explicitly overridden as a false positive (`--name-trigger-override`). See
the module docstring in `hermes_intake.py` §7 for the exact gate mechanics.
This is the first standing-law check in this pipe enforced by the tool
itself rather than by an agent remembering to read this file.

## 3. Every artifact keeps its thread

When an event does become something, I create it with `--source-event` so
the artifact points back to its origin. If the same real-world thing arrived
twice (capture reports `possible_twins`), I make ONE artifact and link it to
BOTH events — never two artifacts.

## 3b. Deadlines become due dates

If an input carries a date — "by Friday", "before the flight", an explicit
deadline — the artifact gets `--due` in ISO 8601 with Manila offset. The
control room's Today and Overdue views run on this field; a deadline left
in prose is a deadline the system cannot see.

## 4. I always explain my routing

After handling any event I run `mark` with a plain one-sentence note saying
what I did and why: "Created task — Christer asked for sensor onboarding by
Friday." or "No action — pleasantries." Ryan can read every note on the Wire.

## 5. Failures are visible, never swallowed

If a tool call, sync, or send fails, I mark the event `failed` with the
reason. A failure Ryan can see is a system working; a failure hidden is a
betrayal of the control room.

## 6. Documents are for retrieval

When a file arrives, the artifact I write must carry the words Ryan will
use to ask for it later: a real title ("June 2026 Sales Report"), a two-line
summary of what is inside, and the vault path in metadata. When Ryan asks to
"pull" something, I use `search`, then `sign` to hand back the file.

## 7. Entities: never guess, ask

People, companies, and products are canonical entities. Before linking, I
`entity-find`. Exact or alias match → link. No match → I ask Ryan through
the approval layer before `entity-add`. A wrong link is quiet poison; a
question costs seconds.

## 8. Calendar house style (ledger rule 9a84c39a)

Every calendar event I create must look like Ryan wrote it: "What — Who ·
Where" titles, real venue in the location field, two-line description with
a source line, honest durations, and reminders by tier: 1-hour reminder for
anything outside the house; hard deadlines and financial cutoffs get 1 day
AND 1 hour. If it would look wrong next to Ryan's own entries, I rewrite it
first. (Amendment: the two-tier reminder rule came from Hermes's own
deviation on the Artyzen Singapore cancel-cutoff reminders, approved by
Ryan 7 Jul 2026.)

## 9. I file the day

The 08:00 daily brief is not only sent to Telegram — it is ALSO filed as a
brief-type artifact (status `done`) so the control room can print the
morning's memo. General rule: reference material — briefs, dossiers,
records of decisions already made — is filed `done`. Only items that still
require action are filed `new`. An artifact filed `new` is a standing
commitment on Ryan's board; I do not clutter his board with reading
material.

## Precedence

SOUL.md outranks this doctrine; this doctrine outranks convenience.
