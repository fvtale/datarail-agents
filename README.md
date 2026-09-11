# datarail-agents

The autonomous receptionists for DataRail.

| Agent | Answers | Status |
| --- | --- | --- |
| **Email** | contact@datarail.org | Built. Dry run until you switch it on |
| **Voice** | 718 838-9901 | [Specified](docs/voice-receptionist.md), waiting on a GPU |

Both write to one lead store in the [datarail-site](https://github.com/fvtale/datarail-site)
repository, which renders a private dashboard at `datarail.org/leads/`. The
public product page for all of this is `datarail.org/agents`.

---

## What the email agent does

Every 30 minutes, on a GitHub Actions cron:

1. Reads unread mail from the IONOS mailbox over IMAP.
2. Throws out anything structurally machine-sent — `no-reply@` addresses,
   `List-Unsubscribe`, `Auto-Submitted`, bulk `Precedence` — without spending a
   token on it.
3. Classifies what is left. Only **genuine** human enquiries earn a reply;
   spam, newsletters, automated mail and personal mail are filed in silence.
4. Drafts a reply and extracts what the message revealed about the enquiry.
5. Offers three genuinely-free times for the intro call, read live from Google
   Calendar, spread across different days.
6. Vets the draft. Anything naming a price, promising a date, guaranteeing an
   outcome or leaking model scaffolding is **blocked and held for a human**.
7. Sends, threaded onto the original conversation.
8. When the client picks a time, re-checks it is still free, books it, and
   attaches an `.ics` invitation.
9. Scores the lead, writes it to the store, rebuilds the dashboard, pushes.

Every message ends in one of four places, and the run log says which:

| Outcome | Where the mail goes |
| --- | --- |
| `replied` | `Agent/Handled` |
| `listing` | `Agent/Listings`, once proposed to [Glyph](#listings-for-glyph) |
| `review` | `Agent/Review` — **this is the folder to actually read** |
| `ignored` | `Agent/Ignored` |
| `error` | left in INBOX, unread, retried next run |

---

## Setup

### 1. The mailbox

In the IONOS control panel, confirm `contact@datarail.org` is a real mailbox
with a password you control, not just an alias that forwards. The agent needs
to both read and send as that address.

Defaults assume IONOS: IMAP `imap.ionos.com:993`, SMTP `smtp.ionos.com:465`,
both implicit TLS, authenticating with the full address as the username.

### 2. Secrets

**Settings → Secrets and variables → Actions → Secrets:**

| Secret | Value |
| --- | --- |
| `OPENAI_API_KEY` | An OpenAI API key |
| `DATARAIL_MAIL_PASSWORD` | The mailbox password |
| `DATARAIL_SITE_TOKEN` | A fine-grained PAT with **Contents: read and write** on `fvtale/datarail-site`, and nothing else |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Optional. The whole downloaded service-account key file, pasted in. Without it the agent still answers and qualifies, it just never offers a time |
| `GLYPH_DEPLOY_KEY` | Optional. The private half of an SSH deploy key with **write access on `fvtale/glyph`**. Without it, listing mail waits unread in the inbox — see [Listings for Glyph](#listings-for-glyph) |

`DATARAIL_SITE_TOKEN` is needed because the default `GITHUB_TOKEN` cannot reach
another repository. Scope it to that one repo only.

A name collision worth knowing about: the Glyph repository *also* has a secret
called `DATARAIL_SITE_TOKEN`, and there it holds an SSH deploy key, not a PAT.
The two are not interchangeable — this workflow passes its value as `token:`,
Glyph's passes its as `ssh-key:`. Do not copy one into the other.

### 2b. Google Calendar, for booking

Four steps, once:

1. In the [Google Cloud console](https://console.cloud.google.com), create a
   project and **enable the Google Calendar API**.
2. Create a **service account**, then create a **JSON key** for it and
   download it. Paste the entire file into `GOOGLE_SERVICE_ACCOUNT_JSON`.
3. Open that file and copy the `client_email` — something like
   `datarail-agent@your-project.iam.gserviceaccount.com`.
4. In Google Calendar → Settings → the calendar you want booked → **Share with
   specific people** → add that address with **Make changes to events**.

Step 4 is the one people miss. A service account has no calendar of its own;
it can only reach yours because you shared it. If `doctor` reports the calendar
returned an error, that share is almost always why.

Then set `GOOGLE_CALENDAR_ID` to the calendar's ID (your email address for the
main one, or the ID shown in that calendar's settings). It defaults to
`primary`, which for a service account is its own empty calendar — so set it.

Booking behaviour is tunable by variable: `DATARAIL_TIMEZONE`
(default `America/New_York`), `BOOKING_SLOT_MINUTES` (30),
`BOOKING_BUFFER_MINUTES` (15), `BOOKING_MIN_NOTICE_HOURS` (12),
`BOOKING_HORIZON_DAYS` (14), `BOOKING_SLOTS_TO_OFFER` (3).

**Availability is all hours by default** — your calendar's own free/busy is the
only constraint, so a 4am slot can be offered if you are free at 4am. To keep
it to working hours set `BOOKING_EARLIEST_HOUR` and `BOOKING_LATEST_HOUR`
(e.g. `9` and `17`); nothing else changes.

The agent creates the event with **no attendees** and emails the `.ics` itself.
Google blocks service accounts from adding attendees without domain-wide
delegation, and this way the invitation arrives from `contact@datarail.org`
rather than from Google.

**Settings → Secrets and variables → Actions → Variables** (all optional):

| Variable | Default | What it does |
| --- | --- | --- |
| `DATARAIL_AGENT_LIVE` | unset | **The switch.** Nothing is sent unless this is exactly `true` |
| `OPENAI_MODEL` | `gpt-5` | Drafting model |
| `OPENAI_CLASSIFIER_MODEL` | `gpt-5-mini` | Triage model — high volume, so keep it cheap |
| `DATARAIL_DISCLOSE_AGENT` | `true` | Whether replies say they were written by an assistant |
| `DATARAIL_MAX_SENDS_PER_RUN` | `10` | Hard ceiling per run |
| `DATARAIL_MAX_LISTINGS_PER_RUN` | `5` | Glyph proposals per run. Each is a public pull request, so a burst of junk waits in the inbox instead |

Leaving any of these unset is fine. Actions passes an unset variable as an
empty string, and `core/config.py` reads empty as "use the default" — it used
not to, which would have blanked the mailbox address on the first real run.

### 3. Check it before it talks

**Actions → Email receptionist → Run workflow**, tick **doctor**.

This verifies the API key, that the configured models actually exist for that
key, that the mailbox accepts the credentials, and that the knowledge base
loads. It touches no mail and sends nothing.

Model names move between generations. If doctor reports a model is unavailable,
set `OPENAI_MODEL` to one the key can reach — that is the whole fix.

### 4. Watch it dry-run

Run the workflow again without **live**. It does everything except send, and
prints each reply it *would* have sent. Read a few. This is the cheapest chance
to catch a voice that is wrong before a client sees it.

Nothing is filed or marked during a dry run, so the same mail is still waiting
when you go live.

### 5. Protect the dashboard

Follow the instructions at the top of `datarail-site/public/leads/.htaccess`.
Until you do, `datarail.org/leads/` returns 500 — it fails closed, not open.

The raw lead store lives at `leads/data.json` in the datarail-site repo root,
**outside `public/`**, so it is never uploaded to the webspace at all. The
dashboard inlines its own copy of the data.

The workflow checks `datarail.org/leads/` after each push and fails the job if
it returns anything other than 401 or 403.

### 6. Go live

Set the repository variable `DATARAIL_AGENT_LIVE` to `true`. That is also what
starts the 30-minute schedule: until then, scheduled runs skip themselves and
the agent only runs when you start it by hand. A dry run leaves mail unread on
purpose, so on a schedule it would re-read and re-draft the same messages every
half hour — paying for each call, duplicating each lead's history, and pushing
datarail-site each time.

To stop it: set it to anything else, or delete it. No deploy, no code change,
effective on the next run.

---

## The guardrails

The agent sends to real people with nobody reading first, so the limits are in
code, outside the model, in `core/policy.py`. A rule that only exists as a
polite request in a prompt is not a rule.

- **Never names a price.** DataRail quotes in writing after a free intro call —
  that is DataRail's own stated policy on `/consult`, not something invented
  here. Drafts containing an amount, a rate, or rate language are blocked.
- **Never promises or guarantees.** No delivery dates, no accepting terms.
- **Never invents.** Answers only from `knowledge/datarail.md`. If a fact is not
  in that file the agent says it will check.
- **Says what it is.** Replies carry a disclosure line by default. The EU AI Act
  requires telling people they are dealing with an AI system, several US states
  have bot-disclosure rules, and DataRail labels things honestly everywhere
  else on the domain.
- **Fails quiet.** Anything unclassifiable gets no reply. A classifier returning
  nonsense means silence, not a guess.
- **Cannot loop.** Never replies to itself, to `no-reply@` addresses, or to
  anything carrying automation headers. Sends `Auto-Submitted: auto-replied` so
  other well-behaved responders do not reply back.
- **Has a ceiling.** 10 sends per run, 2 per conversation per day.
- **Leaves a record.** Every decision, including every decision *not* to reply
  and the reason, goes to `leads/runlog.json`.

Held drafts are never retried automatically. A model that just produced an
unsafe draft is not the thing to ask for a safer one.

---

## Listings for Glyph

[Glyph](https://github.com/fvtale/glyph) is DataRail's calendar of New York
literary events. Its submit page asks venues and organisers to email their
dates to this same address with the subject "Glyph listing", so the
receptionist is also Glyph's listings desk.

A message the classifier labels `listing` never reaches the reply path — a
venue sending its dates must not get a consulting pitch back, and it is not a
lead. Instead:

1. The drafting model reads the events out of it. It is given the subject and
   body but **not the sender**, so it cannot leak an address it never saw.
2. `core/listings.py` keeps only the fields the listing contract allows,
   scrubs email addresses and phone numbers, and names each listing the way
   Glyph does.
3. `email_agent/glyph.py` pushes them to Glyph as a branch `listings/<ref>`, one
   file per listing, using `GLYPH_DEPLOY_KEY`. The ref is a hash of the
   Message-ID, so a retried run finds the branch already there and never opens
   a second pull request for one email.
4. The mail moves to `Agent/Listings`, where the reviewer can check it.
5. Glyph's own workflow judges the branch against the listing contract and
   opens the pull request. Merging it publishes the listing.

The receptionist never judges its own proposals. Glyph does, with the same
code its board uses.

**Glyph is a public repository**, and its pull requests are public from the
moment they open. So the commit — and therefore the PR — is built from fixed
phrases and the listing fields alone. The sender's address and the model's
free-text notes go to the private run log in datarail-site, never to Glyph.

Listing mail with nothing listable in it — a question, a removal request —
goes to `Agent/Review` for a person.

To switch it on:

1. Generate an SSH keypair with no passphrase.
2. Public half: `fvtale/glyph` → Settings → Deploy keys → Add, **tick "Allow
   write access"**.
3. Private half: this repository's `GLYPH_DEPLOY_KEY` secret.
4. On `fvtale/glyph`: Settings → Actions → General → Workflow permissions →
   tick **Allow GitHub Actions to create and approve pull requests**, so Glyph's
   workflow can open the PR.
5. Run the workflow with **doctor** ticked. The Glyph line proves the key
   authenticates, without pushing anything.

It follows the same dry-run rule as everything else: without `--live`,
listings are drafted and printed but nothing is pushed and no mail is moved.

---

## Editing what it knows

`knowledge/datarail.md` is the agent's entire world. Change what DataRail
offers, and change it there.

It is curated by hand rather than scraped from datarail.org on purpose: the
site carries a speculative concept build and campaign marketing prose, and an
agent that swallowed the whole site would eventually tell a client DataRail
runs a gelato shop.

---

## Running it locally

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
export DATARAIL_MAIL_PASSWORD=...
python -m datarail_agents.email_agent.run --doctor --site ../datarail-site
python -m datarail_agents.email_agent.run --site ../datarail-site   # dry run
```

`--live` is the only way to send. The environment variable can silence the
agent but cannot switch it on, so a mis-set repository variable fails quiet
rather than loud.

---

## Layout

```
datarail_agents/
  core/
    config.py      settings from the environment; nothing defaults to sending
    brain.py       the only file that imports the OpenAI SDK
    policy.py      the guardrails — plain rules, no model calls
    leads.py       the shared lead store
    listings.py    shapes and scrubs Glyph listings — plain rules, no model calls
    knowledge.py   loads knowledge/*.md
  email_agent/
    mailbox.py     IMAP and SMTP; all the network code
    glyph.py       pushes listing proposals to Glyph; all the git code
    run.py         one pass over unread mail
  voice/           empty until the GPU arrives — see docs/
knowledge/
  datarail.md      everything the agents may say about DataRail
dashboard/
  build.py         renders the private lead dashboard
docs/
  voice-receptionist.md
```

Nothing here is built or run on the author's machine — there is no local Python
toolchain. **CI is the only thing that executes this code before it answers a
real client.** A red CI run is a broken receptionist.
