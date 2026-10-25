"""Autonomous quote demo: a typed quote request runs hands-off through intake, clarification by e-mail, partner
research, real rate requests, replies, pricing and the offer e-mail, and every step is drawn live on a flow map.

Separate from the app's own modules so it merges cleanly; it reuses Huolitsija's quoting agent (parse_request), global
quoting (preview / quote_any), partner discovery and published-contact lookup, core.email (SMTP2GO), the inbound
pipeline (mail.tm poller -> dispatch_raw -> partner_network.ingest_message -> partner_parser.parse_reply) and the
audit log. Off unless DEMO_AUTOQUOTE_ENABLED=true (see conf.py)."""
