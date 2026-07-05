# Hackathon Scout

A tiny agent that watches the web for **healthcare / biotech / pharma hackathons** and pings you the moment something new appears.

## How it works

```
Devpost API  ─┐                                                  ┌─ Telegram
MLH calendar ─┤   dedupe → keyword filter → date filter          ├─ Discord
Aggregators  ─┼→  (drop past / reg-closed) → new only?  ────────►┼─ E-mail digest
Web sweep    ─┘                    ↓                             └─ Action logs
                     seen.json  ·  events.json ──────────────────► Notion database
                     (notified)    (full catalog: /all, reminders)
```