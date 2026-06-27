"""
WSO Academy Support Bot API
Hosted on Railway.
- Answers tickets via private notes
- Logs everything to Postgres
- Analytics API for dashboard
"""

from flask import Flask, request, jsonify
import requests
import json
import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from anthropic import Anthropic
from datetime import datetime

app = Flask(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

GITHUB_RAW = "https://raw.githubusercontent.com/wsochad/wso-kb/refs/heads/main/wso-kb"
FRESHWORKS_DOMAIN = os.environ["FRESHWORKS_DOMAIN"]
FRESHWORKS_API_KEY = os.environ["FRESHWORKS_API_KEY"]
DATABASE_URL = os.environ.get("DATABASE_URL")

FLAG_REASONS = {
    "jobtestprep": "jobTestPrep",
    "job test prep": "jobTestPrep",
    "linkedin premium": "linkedInPremium",
    "activation link": "linkedInPremium",
    "billing": "billing",
    "refund": "billing",
    "payment": "billing",
    "switch track": "trackSwitch",
    "change track": "trackSwitch",
    "track switch": "trackSwitch",
    "change email": "emailChange",
    "update email": "emailChange",
    "cohort": "cohortChange",
    "defer": "cohortChange",
}


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            ticket_id TEXT,
            student_email TEXT,
            student_name TEXT,
            subject TEXT,
            templates_used TEXT[],
            flag_human BOOLEAN DEFAULT FALSE,
            flag_reason TEXT,
            reply_preview TEXT,
            note_status INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ticket_resolutions (
            id SERIAL PRIMARY KEY,
            ticket_id TEXT UNIQUE,
            resolved_at TIMESTAMPTZ,
            time_to_resolve_hours FLOAT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pr_runs (
            id SERIAL PRIMARY KEY,
            pr_number INTEGER,
            pr_url TEXT,
            branch TEXT,
            items_found INTEGER DEFAULT 0,
            new_templates TEXT[],
            trigger_updates TEXT[],
            style_upgrades TEXT[],
            merged BOOLEAN DEFAULT FALSE,
            merged_by TEXT,
            merged_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS templates (
            id SERIAL PRIMARY KEY,
            filename TEXT UNIQUE,
            topic TEXT,
            created_by TEXT DEFAULT 'human',
            times_used INTEGER DEFAULT 0,
            last_used TIMESTAMPTZ,
            flag_rate FLOAT DEFAULT 0,
            added_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            id SERIAL PRIMARY KEY,
            date DATE UNIQUE,
            tickets_processed INTEGER DEFAULT 0,
            tickets_flagged INTEGER DEFAULT 0,
            flag_rate FLOAT DEFAULT 0,
            new_templates_added INTEGER DEFAULT 0,
            prs_opened INTEGER DEFAULT 0,
            prs_merged INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("Database initialized")


def detect_flag_reason(subject, description, templates_used):
    combined = (subject + " " + description).lower()
    for keyword, reason in FLAG_REASONS.items():
        if keyword in combined:
            return reason
    return "lowConfidence"


def log_ticket(ticket_id, student_email, student_name, subject,
               templates_used, flag_human, flag_reason, reply_preview, note_status):
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO tickets
            (ticket_id, student_email, student_name, subject,
             templates_used, flag_human, flag_reason, reply_preview, note_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (ticket_id, student_email, student_name, subject,
              templates_used, flag_human, flag_reason,
              reply_preview[:500] if reply_preview else "", note_status))

        for fname in templates_used:
            cur.execute("""
                INSERT INTO templates (filename, topic, times_used, last_used)
                VALUES (%s, %s, 1, NOW())
                ON CONFLICT (filename) DO UPDATE SET
                    times_used = templates.times_used + 1,
                    last_used = NOW()
            """, (fname, fname.replace('.md', '').replace('-', ' ').title()))

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB log_ticket error: {e}")


def log_resolution(ticket_id, resolved_at_str):
    try:
        conn = get_db()
        cur = conn.cursor()

        resolved_at = datetime.fromisoformat(resolved_at_str.replace('Z', '+00:00'))

        cur.execute("SELECT created_at FROM tickets WHERE ticket_id = %s LIMIT 1",
                    (ticket_id,))
        row = cur.fetchone()

        hours = None
        if row and row["created_at"]:
            delta = resolved_at - row["created_at"].replace(tzinfo=None)
            hours = round(delta.total_seconds() / 3600, 2)

        cur.execute("""
            INSERT INTO ticket_resolutions (ticket_id, resolved_at, time_to_resolve_hours)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticket_id) DO UPDATE SET
                resolved_at = EXCLUDED.resolved_at,
                time_to_resolve_hours = EXCLUDED.time_to_resolve_hours
        """, (ticket_id, resolved_at, hours))

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB log_resolution error: {e}")


def log_pr(pr_number, pr_url, branch, report):
    try:
        conn = get_db()
        cur = conn.cursor()

        new_templates = [t.get("topic", "") for t in report.get("new_templates_needed", [])]
        trigger_updates = [t.get("template_file", "") for t in report.get("trigger_improvements", [])]
        style_upgrades = [t.get("template_file", "") for t in report.get("style_upgrades", [])]
        items_found = len(new_templates) + len(trigger_updates) + len(style_upgrades)

        cur.execute("""
            INSERT INTO pr_runs
            (pr_number, pr_url, branch, items_found,
             new_templates, trigger_updates, style_upgrades)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (pr_number, pr_url, branch, items_found,
              new_templates or [], trigger_updates or [], style_upgrades or []))

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB log_pr error: {e}")


def snapshot_daily_stats():
    try:
        today = datetime.now().date()
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                COUNT(*) as processed,
                COUNT(*) FILTER (WHERE flag_human) as flagged
            FROM tickets
            WHERE DATE(created_at) = %s
        """, (today,))
        row = dict(cur.fetchone())
        processed = row["processed"] or 0
        flagged = row["flagged"] or 0
        flag_rate = round((flagged / processed * 100), 1) if processed > 0 else 0

        cur.execute("""
            SELECT COUNT(*) as prs FROM pr_runs WHERE DATE(created_at) = %s
        """, (today,))
        prs_opened = dict(cur.fetchone())["prs"] or 0

        cur.execute("""
            INSERT INTO daily_stats
            (date, tickets_processed, tickets_flagged, flag_rate, prs_opened)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (date) DO UPDATE SET
                tickets_processed = EXCLUDED.tickets_processed,
                tickets_flagged = EXCLUDED.tickets_flagged,
                flag_rate = EXCLUDED.flag_rate,
                prs_opened = EXCLUDED.prs_opened
        """, (today, processed, flagged, flag_rate, prs_opened))

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Daily snapshot error: {e}")


# ── GitHub ────────────────────────────────────────────────────────────────────

def fetch_github_file(path):
    url = f"{GITHUB_RAW}/{path}"
    headers = {"Authorization": f"token {os.environ.get('GITHUB_TOKEN', '')}"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.text


# ── Claude ────────────────────────────────────────────────────────────────────

def route_to_templates(subject, description, index_content):
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": f"""You are a routing agent for WSO Academy support.

KB INDEX:
{index_content}

STUDENT TICKET:
Subject: {subject}
Description: {description}

Return ONLY a JSON array of 1-3 most relevant template filenames.
Example: ["13-heyreach.md", "27-mentor-request.md"]
No explanation. Just the JSON array."""}]
    )
    raw = resp.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


def generate_reply(subject, description, first_name, templates_text, style_guide):
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": f"""You are WSO Academy support.

STYLE GUIDE:
{style_guide}

RELEVANT TEMPLATES:
{templates_text}

STUDENT TICKET:
Subject: {subject}
Description: {description}
Student first name: {first_name}

Write a personalized reply following the templates and style guide exactly.
Address the student by first name.
Match the length and tone of the template exactly -- do not add extra paragraphs or context the student did not ask for.
If this needs human review (billing, JobTestPrep, LinkedIn Premium activation, track switch, email change, cohort change),
start your reply with [FLAG FOR HUMAN] then write the appropriate holding reply.
Do not include a subject line. Just the reply body."""}]
    )
    return resp.content[0].text.strip()


def markdown_to_html(text):
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'^- (.+)$', r'<br>• \1', text, flags=re.MULTILINE)
    text = text.replace('\n\n', '<br><br>')
    text = text.replace('\n', '<br>')
    return text


def post_private_note(ticket_id, note_body):
    url = f"https://{FRESHWORKS_DOMAIN}/api/v2/tickets/{ticket_id}/notes"
    resp = requests.post(
        url,
        json={"body": note_body, "private": True},
        headers={"Content-Type": "application/json"},
        auth=(FRESHWORKS_API_KEY, "X")
    )
    return resp.status_code, resp.json()


# ── Core route ────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/suggest-reply", methods=["POST"])
def suggest_reply():
    data = request.json
    ticket_id = data.get("ticket_id")
    subject = data.get("subject", "")
    description = data.get("description", "")
    first_name = data.get("first_name", "there")
    student_email = data.get("student_email", "")
    student_name = data.get("student_name", "")

    print(f"[{datetime.now()}] Ticket {ticket_id}: {subject}")

    try:
        index_content = fetch_github_file("index.md")
        style_guide = fetch_github_file("style-guide.md")

        template_files = route_to_templates(subject, description, index_content)
        print(f"Templates: {template_files}")

        templates_content = []
        for fname in template_files:
            try:
                content = fetch_github_file(f"templates/{fname}")
                templates_content.append(f"=== {fname} ===\n{content}")
            except Exception as e:
                print(f"Could not fetch {fname}: {e}")

        reply = generate_reply(subject, description, first_name,
                               "\n\n".join(templates_content), style_guide)

        flag_human = reply.startswith("[FLAG FOR HUMAN]")
        reply = reply.replace("[FLAG FOR HUMAN]", "").strip()

        flag_reason = None
        if flag_human:
            flag_reason = detect_flag_reason(subject, description, template_files)

        reply_html = markdown_to_html(reply)
        prefix = "🚨 <strong>FLAG FOR HUMAN:</strong><br><br>" if flag_human else "🤖 <strong>Suggested reply:</strong><br><br>"
        full_note = prefix + reply_html

        status_code, _ = post_private_note(ticket_id, full_note)
        print(f"Note posted: {status_code}")

        log_ticket(ticket_id, student_email, student_name, subject,
                   template_files, flag_human, flag_reason, reply, status_code)

        snapshot_daily_stats()

        return jsonify({
            "success": True,
            "ticket_id": ticket_id,
            "templates_used": template_files,
            "flag_human": flag_human,
            "flag_reason": flag_reason,
            "note_status": status_code
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Webhooks ──────────────────────────────────────────────────────────────────

@app.route("/ticket-resolved", methods=["POST"])
def ticket_resolved():
    """Freshdesk calls this when a ticket is resolved."""
    data = request.json or {}
    ticket_id = str(data.get("ticket_id") or data.get("id") or "")
    resolved_at = data.get("resolved_at") or data.get("updated_at") or datetime.now().isoformat()

    if not ticket_id:
        return jsonify({"error": "no ticket_id"}), 400

    log_resolution(ticket_id, resolved_at)
    snapshot_daily_stats()

    print(f"Ticket {ticket_id} resolved at {resolved_at}")
    return jsonify({"success": True, "ticket_id": ticket_id})


@app.route("/analytics/log-pr", methods=["POST"])
def log_pr_route():
    """Improvement agent calls this after opening a PR."""
    data = request.json or {}
    log_pr(
        data.get("pr_number"),
        data.get("pr_url"),
        data.get("branch"),
        data.get("report", {})
    )
    return jsonify({"success": True})


# ── Analytics API ─────────────────────────────────────────────────────────────

@app.route("/analytics/overview", methods=["GET"])
def analytics_overview():
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                COUNT(*) as total_tickets,
                COUNT(*) FILTER (WHERE DATE(created_at) = CURRENT_DATE) as today,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days') as this_week,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '30 days') as this_month,
                ROUND(AVG(CASE WHEN flag_human THEN 1.0 ELSE 0.0 END) * 100, 1) as flag_rate
            FROM tickets
        """)
        ticket_stats = dict(cur.fetchone())

        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE merged) as merged,
                COALESCE(SUM(items_found), 0) as total_improvements
            FROM pr_runs
        """)
        pr_stats = dict(cur.fetchone())

        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE created_by = 'agent') as by_agent,
                COUNT(*) FILTER (WHERE created_by = 'human') as by_human
            FROM templates
        """)
        template_stats = dict(cur.fetchone())

        cur.execute("""
            SELECT ROUND(AVG(time_to_resolve_hours)::numeric, 1) as avg_hours
            FROM ticket_resolutions
            WHERE time_to_resolve_hours IS NOT NULL
        """)
        resolution_stats = dict(cur.fetchone())

        cur.execute("""
            SELECT filename, topic, times_used, last_used, flag_rate
            FROM templates ORDER BY times_used DESC LIMIT 10
        """)
        top_templates = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT flag_reason, COUNT(*) as count
            FROM tickets
            WHERE flag_human = TRUE AND flag_reason IS NOT NULL
            GROUP BY flag_reason ORDER BY count DESC
        """)
        flag_breakdown = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT date, tickets_processed, flag_rate, prs_opened
            FROM daily_stats
            ORDER BY date DESC LIMIT 30
        """)
        daily_trend = [dict(r) for r in cur.fetchall()]

        cur.close()
        conn.close()

        return jsonify({
            "tickets": ticket_stats,
            "prs": pr_stats,
            "templates": template_stats,
            "resolutions": resolution_stats,
            "top_templates": top_templates,
            "flag_breakdown": flag_breakdown,
            "daily_trend": daily_trend
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analytics/tickets", methods=["GET"])
def analytics_tickets():
    try:
        limit = int(request.args.get("limit", 50))
        flag_only = request.args.get("flagged") == "true"
        conn = get_db()
        cur = conn.cursor()

        if flag_only:
            cur.execute("""
                SELECT ticket_id, student_email, student_name, subject,
                       templates_used, flag_human, flag_reason,
                       reply_preview, note_status, created_at
                FROM tickets WHERE flag_human = TRUE
                ORDER BY created_at DESC LIMIT %s
            """, (limit,))
        else:
            cur.execute("""
                SELECT ticket_id, student_email, student_name, subject,
                       templates_used, flag_human, flag_reason,
                       reply_preview, note_status, created_at
                FROM tickets ORDER BY created_at DESC LIMIT %s
            """, (limit,))

        tickets = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify(tickets)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analytics/prs", methods=["GET"])
def analytics_prs():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM pr_runs ORDER BY created_at DESC LIMIT 30")
        prs = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify(prs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analytics/templates", methods=["GET"])
def analytics_templates():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM templates ORDER BY times_used DESC")
        templates = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify(templates)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analytics/student/<email>", methods=["GET"])
def student_history(email):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT t.ticket_id, t.subject, t.templates_used,
                   t.flag_human, t.flag_reason, t.reply_preview, t.created_at,
                   r.resolved_at, r.time_to_resolve_hours
            FROM tickets t
            LEFT JOIN ticket_resolutions r ON t.ticket_id = r.ticket_id
            WHERE t.student_email = %s
            ORDER BY t.created_at DESC
        """, (email,))
        history = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify(history)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analytics/resolutions", methods=["GET"])
def analytics_resolutions():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT ticket_id, resolved_at, time_to_resolve_hours, created_at
            FROM ticket_resolutions
            ORDER BY resolved_at DESC LIMIT 50
        """)
        resolutions = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify(resolutions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Boot ──────────────────────────────────────────────────────────────────────

try:
    init_db()
except Exception as e:
    print(f"DB init warning: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
