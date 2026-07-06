# Hermes mail watcher

Real-time inbox daemon (IMAP IDLE, 60s-poll fallback). Design contract:
the transport is dumb and free; the model is invoked only when mail exists.
Everyone gets read; only the verified principal gets obeyed.
Authority requires a DKIM pass on the principal address specifically —
SPF alone is never sufficient. (Check hardened after review by the
Hermes agent itself, 06 Jul 2026.)

Install (as root or the hermes user):

    mkdir -p /opt/hermes
    curl -o /opt/hermes/hermes_mailwatch.py https://raw.githubusercontent.com/ra1-coder/hermes-mailwatch/main/hermes_mailwatch.py
    curl -o /etc/systemd/system/hermes-mailwatch.service https://raw.githubusercontent.com/ra1-coder/hermes-mailwatch/main/hermes-mailwatch.service
    # ensure /opt/hermes/.env contains: GMAIL_USER, GMAIL_APP_PASSWORD,
    # ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
    # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    systemctl daemon-reload && systemctl enable --now hermes-mailwatch
    journalctl -u hermes-mailwatch -f   # watch it connect

## Container hosts (no systemd — PID 1 is tini or similar)

The .service file does not apply. Instead, in any writable workspace dir:

    curl -O https://raw.githubusercontent.com/ra1-coder/hermes-mailwatch/main/hermes_mailwatch.py
    curl -O https://raw.githubusercontent.com/ra1-coder/hermes-mailwatch/main/run_mailwatch.sh
    chmod +x run_mailwatch.sh
    # create mailwatch.env next to them with the seven variables
    nohup ./run_mailwatch.sh >/dev/null 2>&1 &
    tail -f mailwatch.log     # expect: "connected; processing backlog"

Survival: the daemon self-reconnects on every IMAP drop; run_mailwatch.sh
restarts it on any exit. If the host scheduler can run a watchdog, add:
pgrep -f hermes_mailwatch.py || nohup ./run_mailwatch.sh & (every 5 min).
Note the wrapper does not survive a container restart by itself — hook
run_mailwatch.sh into whatever startup mechanism the runtime provides.

## Env var mapping when names differ

- GMAIL_USER — not a secret; the bot inbox address.
- SUPABASE_URL — not a secret; the project URL.
- SUPABASE_SERVICE_ROLE_KEY — any key that can WRITE the mail table works.
  Verify empirically before trusting a differently-named key:
      curl -s -X POST "$SUPABASE_URL/rest/v1/mail" \
        -H "apikey: $KEY" -H "Authorization: Bearer $KEY" \
        -H "Content-Type: application/json" -H "Prefer: return=representation" \
        -d '{"from_addr":"envtest@local","subject":"env test","category":"noise","status":"archived"}'
  A JSON row back = the key writes (the row is archived, invisible in the
  PWA). An RLS/permission error = wrong key class; use the service key
  from the project's API settings.
- ANTHROPIC_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — the agent
  runtime already holds these to function; locate them in its own config
  rather than minting duplicates, or mint a dedicated Anthropic key for
  billing separation.
