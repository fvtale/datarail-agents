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
5. Vets the draft. Anything naming a price, promising a date, guaranteeing an
   outcome or leaking model scaffolding is **blocked and held for a human**.
6. Sends, threaded onto the original conversation.
7. Scores the lead, writes it to the store, rebuilds the dashboard, pushes.

Every message ends in one of four places, and the run log says which:

| Outcome | Where the mail goes |
| --- | --- |
| `replied` | `Agent/Handled` |
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

`DATARAIL_SITE_TOKEN` is needed because the default `GITHUB_TOKEN` cannot reach
another repository. Scope it to that one repo only.

**Settings → Secrets and variables → Actions → Variables** (all optional):

| Variable | Default | What it does |
| --- | --- | --- |
| `DATARAIL_AGENT_LIVE` | unset | **The switch.** Nothing is sent unless this is exactly `true` |
| `OPENAI_MODEL` | `gpt-5` | Drafting model |
| `OPENAI_CLASSIFIER_MODEL` | `gpt-5-mini` | Triage model — high volume, so keep it cheap |
| `DATARAIL_DISCLOSE_AGENT` | `true` | Whether replies say they were written by an assistant |
| `DATARAIL_MAX_SENDS_PER_RUN` | `10` | Hard ceiling per run |

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

Set the repository variable `DATARAIL_AGENT_LIVE` to `true`.

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
    knowledge.py   loads knowledge/*.md
  email_agent/
    mailbox.py     IMAP and SMTP; all the network code
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
