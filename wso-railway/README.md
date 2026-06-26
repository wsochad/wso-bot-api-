# WSO Academy Support Bot API

Hosted on Railway. Called by Zapier on every new Freshworks ticket.

## Endpoints

GET  /health         — health check
POST /suggest-reply  — generate and post private note on ticket

## POST /suggest-reply payload

```json
{
  "ticket_id": "12345",
  "subject": "How do I access HeyReach?",
  "description": "Full ticket description here",
  "first_name": "James"
}
```

## Environment variables (set in Railway)

ANTHROPIC_API_KEY
FRESHWORKS_DOMAIN    (e.g. wallstreetoasis-help.freshdesk.com)
FRESHWORKS_API_KEY

## Local development

pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
export FRESHWORKS_DOMAIN=...
export FRESHWORKS_API_KEY=...
python main.py
