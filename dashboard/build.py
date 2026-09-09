"""Render the private lead dashboard from the lead store.

Reads leads/data.json at the datarail-site repo root -- outside public/, so the
raw store is never uploaded to the webspace -- and writes the rendered page to
public/leads/index.html, which is.

The page is a single self-contained file with the lead data inlined, so it
needs no second authenticated fetch to render, and the source JSON never has to
be served at all. The rendered page sits behind Apache basic auth; see the
.htaccess in public/leads/.

It is deliberately read-only. Marking a lead as worked would mean writing back
from a static page, which means an endpoint, which means an attack surface on
the one thing holding client data. Status changes are made by editing the JSON
in the repo, and git remembers who changed what.

    python dashboard/build.py --site ../datarail-site
"""

from __future__ import annotations

import argparse
import html
import json
import os
from datetime import datetime, timezone

# The house palette, taken from /consult and /library so the dashboard looks
# like part of the same site rather than a bolted-on admin tool.
STYLE = """
:root {
  --bg: #05070b;
  --bg-soft: #0b0f16;
  --bg-raised: #10151e;
  --fg: #f5f5f7;
  --muted: #9ca3af;
  --dim: #7d8694;
  --accent: #42f5ff;
  --accent-strong: #42ffb3;
  --warm: #e0b25c;
  --danger: #ff6b81;
  --line: #1c232f;
  --font-main: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  --radius: 10px;
  --shell: 1100px;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: var(--font-main);
  background: radial-gradient(circle at 50% -6%, #0d1622 0, var(--bg) 58%, #03050a 100%);
  background-attachment: fixed;
  color: var(--fg);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.shell { max-width: var(--shell); margin: 0 auto; padding: 0 20px; }
a { color: var(--accent); }
h1, h2, h3 { margin: 0; line-height: 1.15; font-weight: 600; letter-spacing: -0.02em; }
.label {
  font-family: var(--font-mono); font-size: 0.67rem;
  text-transform: uppercase; letter-spacing: 0.18em; color: var(--dim);
}
header.top { border-bottom: 1px solid var(--line); padding: 28px 0 22px; margin-bottom: 26px; }
header.top h1 { font-size: 1.7rem; margin-top: 6px; }
.sub { color: var(--muted); font-size: 0.9rem; margin-top: 8px; }

.stats { display: flex; flex-wrap: wrap; gap: 10px; margin: 22px 0 26px; }
.stat {
  flex: 1 1 130px; background: var(--bg-soft); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 13px 15px;
}
.stat b { display: block; font-size: 1.5rem; font-weight: 600; letter-spacing: -0.03em; }
.stat span { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.14em; color: var(--dim); }

.filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
.filters button {
  font: inherit; font-size: 0.78rem; cursor: pointer; padding: 7px 15px;
  border-radius: 999px; border: 1px solid var(--line);
  background: transparent; color: var(--muted); transition: all 150ms ease-out;
}
.filters button:hover { color: var(--fg); border-color: var(--dim); }
.filters button[aria-pressed="true"] { background: var(--accent); color: #05070b; border-color: var(--accent); font-weight: 600; }

.lead {
  background: var(--bg-soft); border: 1px solid var(--line);
  border-radius: var(--radius); margin-bottom: 12px; overflow: hidden;
}
.lead > summary {
  cursor: pointer; padding: 15px 18px; display: grid; gap: 3px 14px;
  grid-template-columns: 1fr auto auto; align-items: center; list-style: none;
}
.lead > summary::-webkit-details-marker { display: none; }
.lead > summary:hover { background: var(--bg-raised); }
.who { font-weight: 600; }
.who small { font-weight: 400; color: var(--dim); font-family: var(--font-mono); font-size: 0.76rem; }
.gist { grid-column: 1 / 2; color: var(--muted); font-size: 0.87rem; }
.score { font-family: var(--font-mono); font-size: 0.95rem; color: var(--accent); }
.pill {
  font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.13em;
  padding: 4px 10px; border-radius: 999px; border: 1px solid currentColor; white-space: nowrap;
}
.s-qualified { color: var(--accent-strong); }
.s-qualifying { color: var(--warm); }
.s-new { color: var(--dim); }
.s-unqualified, .s-lost { color: var(--danger); }
.s-won { color: var(--accent-strong); }

.body { padding: 4px 18px 20px; border-top: 1px solid var(--line); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin: 16px 0; }
.field span { display: block; font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.14em; color: var(--dim); margin-bottom: 3px; }
.field p { margin: 0; font-size: 0.9rem; }
.field p.none { color: var(--dim); font-style: italic; }
.call { margin: 16px 0 0; padding: 12px 16px; border-radius: 8px; background: var(--bg-raised); border-left: 2px solid var(--dim); }
.call .label { display: block; margin-bottom: 3px; }
.call strong { font-size: 1.02rem; }
.call.booked { border-left-color: var(--accent-strong); }
.call.booked strong { color: var(--accent-strong); }
.call.offered { border-left-color: var(--warm); color: var(--muted); font-size: 0.88rem; }
.questions { background: var(--bg-raised); border-left: 2px solid var(--warm); padding: 11px 15px; border-radius: 0 6px 6px 0; margin: 14px 0; }
.questions ul { margin: 6px 0 0; padding-left: 18px; font-size: 0.87rem; color: var(--muted); }
.thread { margin-top: 18px; }
.msg { border-left: 2px solid var(--line); padding: 3px 0 3px 14px; margin-bottom: 14px; }
.msg.out { border-left-color: var(--accent); }
.msg .meta { font-family: var(--font-mono); font-size: 0.7rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.1em; }
.msg pre { margin: 6px 0 0; white-space: pre-wrap; word-wrap: break-word; font-family: var(--font-main); font-size: 0.87rem; color: var(--muted); }
.empty { text-align: center; padding: 70px 20px; color: var(--dim); }
footer { border-top: 1px solid var(--line); margin-top: 40px; padding: 22px 0 50px; color: var(--dim); font-size: 0.8rem; }
@media (max-width: 620px) {
  .lead > summary { grid-template-columns: 1fr auto; }
  .score { display: none; }
}
"""

SCRIPT = """
const buttons = document.querySelectorAll('.filters button');
buttons.forEach(function (button) {
  button.addEventListener('click', function () {
    const want = button.dataset.filter;
    buttons.forEach(function (other) {
      other.setAttribute('aria-pressed', String(other === button));
    });
    document.querySelectorAll('.lead').forEach(function (lead) {
      lead.hidden = !(want === 'all' || lead.dataset.status === want);
    });
  });
});
"""


def esc(value) -> str:
    return html.escape(str(value or ""))


def _field(label: str, value: str) -> str:
    if (value or "").strip():
        return '<div class="field"><span>' + esc(label) + "</span><p>" + esc(value) + "</p></div>"
    return '<div class="field"><span>' + esc(label) + '</span><p class="none">not established</p></div>'


def _when(iso: str) -> str:
    try:
        moment = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return esc(iso)
    return moment.strftime("%d %b %Y, %H:%M UTC")


def render(payload: dict) -> str:
    leads = payload.get("leads", [])

    counts = {}
    for lead in leads:
        status = lead.get("status", "new")
        counts[status] = counts.get(status, 0) + 1

    booked = sum(
        1 for lead in leads if (lead.get("booking") or {}).get("status") == "booked"
    )
    stats = [
        ("Total", len(leads)),
        ("Calls booked", booked),
        ("Qualified", counts.get("qualified", 0)),
        ("Qualifying", counts.get("qualifying", 0)),
        ("New", counts.get("new", 0)),
    ]
    stat_html = "".join(
        '<div class="stat"><b>' + str(value) + "</b><span>" + esc(label) + "</span></div>"
        for label, value in stats
    )

    order = ["all", "qualified", "qualifying", "new", "unqualified", "won", "lost"]
    filter_html = "".join(
        '<button data-filter="' + key + '" aria-pressed="' + ("true" if key == "all" else "false") + '">'
        + esc(key.capitalize()) + "</button>"
        for key in order
        if key == "all" or counts.get(key)
    )

    if not leads:
        body = (
            '<div class="empty"><p>No leads yet.</p>'
            "<p>The receptionist writes here as enquiries arrive.</p></div>"
        )
    else:
        body = "".join(_render_lead(lead) for lead in leads)

    generated = _when(payload.get("generated_at", ""))

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        # Private page: keep it out of search indexes even though basic auth
        # should already prevent a crawler ever seeing it.
        '<meta name="robots" content="noindex, nofollow">\n'
        "<title>Leads &mdash; DataRail</title>\n"
        "<style>" + STYLE + "</style>\n"
        "</head>\n<body>\n"
        '<div class="shell">\n'
        '<header class="top">\n'
        '<div class="label">DataRail &middot; private</div>\n'
        "<h1>Leads</h1>\n"
        '<p class="sub">Captured by the email and voice receptionists. '
        "Rebuilt " + generated + ".</p>\n"
        "</header>\n"
        '<div class="stats">' + stat_html + "</div>\n"
        '<div class="filters">' + filter_html + "</div>\n"
        + body
        + "\n<footer>Read-only. Edit <code>leads/data.json</code> in "
        "datarail-site to change a status; git keeps the history.</footer>\n"
        "</div>\n"
        "<script>" + SCRIPT + "</script>\n"
        "</body>\n</html>\n"
    )


def _render_lead(lead: dict) -> str:
    contact = lead.get("contact", {}) or {}
    name = contact.get("name") or contact.get("email") or contact.get("phone") or "Unknown"
    status = lead.get("status", "new")
    reach = contact.get("email") or contact.get("phone") or ""
    company = contact.get("company")

    booking = lead.get("booking") or {}
    booking_html = _render_booking(booking)

    questions = lead.get("open_questions") or []
    questions_html = ""
    if questions:
        questions_html = (
            '<div class="questions"><div class="label">Still open</div><ul>'
            + "".join("<li>" + esc(item) + "</li>" for item in questions)
            + "</ul></div>"
        )

    messages = []
    for item in lead.get("interactions", []):
        direction = item.get("direction", "in")
        who = "They wrote" if direction == "in" else "Agent replied"
        messages.append(
            '<div class="msg ' + esc(direction) + '"><div class="meta">'
            + who + " &middot; " + _when(item.get("at", "")) + "</div>"
            + ("<strong>" + esc(item.get("subject")) + "</strong>" if item.get("subject") else "")
            + "<pre>" + esc(item.get("body")) + "</pre></div>"
        )

    return (
        '<details class="lead" data-status="' + esc(status) + '">\n'
        "<summary>\n"
        '<div class="who">' + esc(name)
        + (" <small>" + esc(company) + "</small>" if company else "")
        + "<br><small>" + esc(reach) + "</small></div>\n"
        '<div class="score">' + str(lead.get("score", 0)) + "</div>\n"
        '<div class="pill s-' + esc(status) + '">' + esc(status) + "</div>\n"
        '<div class="gist">' + esc(lead.get("summary") or "No summary yet.") + "</div>\n"
        "</summary>\n"
        '<div class="body">\n'
        + booking_html
        + '<div class="grid">'
        + _field("Need", lead.get("need", ""))
        + _field("Scope", lead.get("scope", ""))
        + _field("Budget", lead.get("budget", ""))
        + _field("Timeline", lead.get("timeline", ""))
        + _field("Decision maker", lead.get("decision_maker", ""))
        + _field("Source", lead.get("source", ""))
        + "</div>\n"
        + questions_html
        + '<div class="thread"><div class="label">Conversation</div>'
        + ("".join(messages) or '<p class="none">Nothing recorded.</p>')
        + "</div>\n"
        "</div>\n</details>\n"
    )


def _render_booking(booking: dict) -> str:
    """The intro call, which is the thing you most want to see at a glance."""
    status = booking.get("status", "none")

    if status == "booked":
        slot = booking.get("slot") or {}
        return (
            '<div class="call booked"><span class="label">Intro call booked</span>'
            "<strong>" + _when(slot.get("start", "")) + "</strong></div>"
        )

    if status == "offered":
        offered = booking.get("offered_slots") or []
        times = ", ".join(_when(item.get("start", "")) for item in offered[:3])
        return (
            '<div class="call offered"><span class="label">Times offered, '
            "awaiting reply</span>" + esc(times) + "</div>"
        )

    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the private lead dashboard.")
    parser.add_argument(
        "--site",
        default=os.environ.get("DATARAIL_SITE_ROOT", "site"),
        help="Path to a datarail-site checkout.",
    )
    args = parser.parse_args(argv)

    # Source: private, never uploaded. Output: served, behind basic auth.
    data_path = os.path.join(args.site, "leads", "data.json")
    leads_dir = os.path.join(args.site, "public", "leads")

    if os.path.exists(data_path):
        with open(data_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = {
            "leads": [],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    os.makedirs(leads_dir, exist_ok=True)
    out_path = os.path.join(leads_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(render(payload))

    print("Wrote " + out_path + " (" + str(len(payload.get("leads", []))) + " leads)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
