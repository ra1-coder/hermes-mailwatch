# Hermes mail watcher

Real-time inbox daemon (IMAP IDLE, 60s-poll fallback). Design contract:
the transport is dumb and free; the model is invoked only when mail exists.
Everyone gets read; only the verified principal gets obeyed.

Install (as root or the hermes user):

    mkdir -p /opt/hermes
    curl -o /opt/hermes/hermes_mailwatch.py https://raw.githubusercontent.com/ra1-coder/hermes-mailwatch/main/hermes_mailwatch.py
    curl -o /etc/systemd/system/hermes-mailwatch.service https://raw.githubusercontent.com/ra1-coder/hermes-mailwatch/main/hermes-mailwatch.service
    # ensure /opt/hermes/.env contains: GMAIL_USER, GMAIL_APP_PASSWORD,
    # ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
    # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    systemctl daemon-reload && systemctl enable --now hermes-mailwatch
    journalctl -u hermes-mailwatch -f   # watch it connect
