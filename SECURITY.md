# Security Policy

## Reporting a vulnerability

Please report vulnerabilities through GitHub's **Report a vulnerability** flow
on this repository's Security tab. Do not open a public issue containing bot
tokens, chat IDs, message contents, or exploit details.

## Deployment guidance

- Keep `.env` private and owner-readable only (`chmod 600 .env`).
- Set `TELEGRAM_ALLOWED_CHAT_IDS` explicitly; an empty list denies all chats.
- Treat every allowlisted chat as a trusted operator of the same OpenCode
  instance; this bridge is not a multi-tenant isolation boundary.
- Keep the OpenCode server bound to loopback and use a session with only the
  permissions you intend Telegram prompts to exercise.
- Use one bridge poller per Telegram bot token.
- Review local service logs before sharing them; they may contain session IDs
  or operational metadata even though message bodies and tokens are omitted.
