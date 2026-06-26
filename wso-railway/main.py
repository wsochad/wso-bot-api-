"""
WSO Academy Support Bot API
Hosted on Railway. Called by Zapier when a new ticket arrives.
Fetches KB from GitHub, calls Claude, returns suggested reply.
Logs everything to Railway's built-in logging.
"""

from flask import Flask, request, jsonify
import requests
import json
import os
from anthropic import Anthropic
from datetime import datetime

app = Flask(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

GITHUB_RAW = "https://raw.githubusercontent.com/wsochad/wso-kb/main/wso-kb"
FRESHWORKS_DOMAIN = os.environ["FRESHWORKS_DOMAIN"]
FRESHWORKS_API_KEY = os.environ["FRESHWORKS_API_KEY"]


def fetch_github_file(path):
    url = f"{GITHUB_RAW}/{path}"
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.text


def route_to_templates(ticket_subject, ticket_description, index_content):
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""You are a routing agent for WSO Academy support.

KB INDEX:
{index_content}

STUDENT TICKET:
Subject: {ticket_subject}
Description: {ticket_description}

Return ONLY a JSON array of 1-3 most relevant template filenames.
Example: ["13-heyreach.md", "27-mentor-request.md"]
No explanation. Just the JSON array."""
        }]
    )
    raw = resp.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


def generate_reply(ticket_subject, ticket_description, first_name, templates_content, style_guide):
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": f"""You are WSO Academy support.

STYLE GUIDE:
{style_guide}

RELEVANT TEMPLATES:
{templates_content}

STUDENT TICKET:
Subject: {ticket_subject}
Description: {ticket_description}
Student first name: {first_name}

Write a personalized reply following the templates and style guide exactly.
Address the student by first name.
If this needs human review (billing, JobTestPrep, LinkedIn Premium activation), 
start your reply with [FLAG FOR HUMAN] then write a warm holding reply.
Do not include a subject line. Just the reply body."""
        }]
    )
    return resp.content[0].text.strip()


def post_private_note(ticket_id, note_body):
    url = f"https://{FRESHWORKS_DOMAIN}/api/v2/tickets/{ticket_id}/notes"
    headers = {"Content-Type": "application/json"}
    body = {
        "body": note_body,
        "private": True
    }
    resp = requests.post(
        url,
        json=body,
        headers=headers,
        auth=(FRESHWORKS_API_KEY, "X")
    )
    return resp.status_code, resp.json()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/suggest-reply", methods=["POST"])
def suggest_reply():
    data = request.json
    ticket_id = data.get("ticket_id")
    ticket_subject = data.get("subject", "")
    ticket_description = data.get("description", "")
    first_name = data.get("first_name", "there")

    print(f"[{datetime.now()}] Processing ticket {ticket_id}: {ticket_subject}")

    try:
        # Step 1: fetch index and style guide
        index_content = fetch_github_file("index.md")
        style_guide = fetch_github_file("style-guide.md")
        print(f"Fetched index and style guide from GitHub")

        # Step 2: route to relevant templates
        template_files = route_to_templates(ticket_subject, ticket_description, index_content)
        print(f"Routed to templates: {template_files}")

        # Step 3: fetch those template files
        templates_content = []
        for fname in template_files:
            try:
                content = fetch_github_file(f"templates/{fname}")
                templates_content.append(f"=== {fname} ===\n{content}")
            except Exception as e:
                print(f"Could not fetch {fname}: {e}")

        templates_text = "\n\n".join(templates_content)

        # Step 4: generate reply
        reply = generate_reply(
            ticket_subject,
            ticket_description,
            first_name,
            templates_text,
            style_guide
        )
        print(f"Generated reply ({len(reply)} chars)")

        # Step 5: post as private note
        flag_human = reply.startswith("[FLAG FOR HUMAN]")
        note_prefix = "🤖 *Suggested reply — review and send if accurate:*\n\n"
        if flag_human:
            note_prefix = "🚨 *FLAG FOR HUMAN — bot is not confident. Review carefully:*\n\n"
            reply = reply.replace("[FLAG FOR HUMAN]", "").strip()

        full_note = note_prefix + reply
        status_code, note_resp = post_private_note(ticket_id, full_note)
        print(f"Posted private note: {status_code}")

        return jsonify({
            "success": True,
            "ticket_id": ticket_id,
            "templates_used": template_files,
            "flag_human": flag_human,
            "note_status": status_code
        })

    except Exception as e:
        print(f"Error processing ticket {ticket_id}: {str(e)}")
        return jsonify({
            "success": False,
            "ticket_id": ticket_id,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
