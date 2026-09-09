# Voice receptionist — specification

Status: **designed, not built.** Blocked on a GPU enclosure, not on decisions.

This document exists so that when the hardware arrives the build is assembly
rather than research. It records what was decided, what was measured, and — in
the last section — what could still sink it.

---

## The shape of it

```
     caller
       │  PSTN
       ▼
  TextNow 718 838-9901
       │  call forwarding
       ▼
  wholesale DID  ──SIP/RTP──►  home box (Starlink, CGNAT)
                                 │
                                 ├─ Asterisk          softswitch, trunk registration
                                 ├─ agent bridge      audio ⇄ turn-taking ⇄ tools
                                 ├─ Whisper  (GPU)    speech → text
                                 ├─ Piper/Kokoro (GPU) text → speech
                                 └─ OpenAI            what to say next
                                        │
                                        ▼
                             leads/data.json in datarail-site
                             (the same store the email agent writes)
```

Everything above the PSTN is ours. The trunk is bought, not built, because
handing a call to the public phone network requires a licensed carrier — that
is a regulatory fact, not a software limitation.

---

## Why TextNow is not in the audio path

TextNow is a closed consumer application. There is no public API, no webhooks,
and no SIP credentials exposed to subscribers, so a server cannot answer a
TextNow call or read its SMS. Confirmed before this design was chosen.

**718 838-9901 stays the published number** and forwards to a programmable DID.
The caller dials the number on `/contact`; TextNow hands the call to the trunk;
the agent answers.

Two things to verify on the account before relying on this:

1. **Call forwarding availability.** It is a paid-tier feature. If it is not on
   the current plan, the fallback is to publish the new DID on `/contact` and
   retire TextNow from the site.
2. **Texts do not forward.** Only voice. Inbound SMS to 718 838-9901 will keep
   landing in the TextNow app and will not reach the agent. If SMS matters,
   the DID handles it natively and the published number has to change.

Porting 718 838-9901 off TextNow would solve both at once and remove the
forwarding hop, but port-out is slow and not guaranteed. Worth starting early
if the number has any recognition attached; not worth blocking on.

---

## The Starlink problem, and what is being done about it

Running the softswitch at home behind Starlink was a deliberate choice. It is
also the riskiest part of this design, so the mitigations are listed rather than
assumed.

| Problem | Why it bites | Mitigation |
| --- | --- | --- |
| **CGNAT** — no public IPv4, no inbound connections | The trunk cannot send an INVITE to an address that does not exist | Register-based trunk with a short keepalive. Registration opens a NAT pinhole the provider sends inbound calls back through. `qualifyfreq` low enough that the binding never ages out |
| **Return audio** takes a different path than signalling | One-way audio, the classic SIP-behind-NAT failure | Symmetric RTP (`comedia` / `nat=force_rport,comedia`). Never trust the SDP address; send to wherever the audio actually came from |
| **Handover jitter** — Starlink re-points on roughly a 15s cadence | Latency spikes and packet loss exactly where RTP hurts | Adaptive jitter buffer, Opus with in-band FEC and DTX where the trunk supports it, PLC enabled. Accept audible artefacts on some calls |
| **Whole-link outage** | The business line is down | The trunk's failover routes to voicemail or forwards onward. Configure this at the provider, not at home — a failover that lives on the box that just died is not a failover |
| **Dynamic IP** | IP-authenticated trunks break | Registration auth rather than IP auth. Rules this out as a problem, and is required for CGNAT anyway |

**If it proves too flaky**, the exit is already designed: move Asterisk to a
$5/mo VPS with a public IP and have the home GPU dial out to it over WireGuard.
The agent code does not change — only where the softswitch runs. Keep trunk
configuration in one file so this stays a relocation.

---

## Trunk providers

Wanted: register-based auth (mandatory under CGNAT), a 718 DID, Opus support,
and per-provider failover routing.

| Provider | DID | Per minute | Notes |
| --- | --- | --- | --- |
| VoIP.ms | ~$0.85/mo | ~$0.009 in | Per-POP registration, good NAT handling, failover in the portal. Best documented for exactly this setup |
| Flowroute | ~$1.25/mo | ~$0.004 in | Cheaper per minute, more carrier-grade, thinner hand-holding |
| Telnyx | ~$1/mo | ~$0.0045 in | Best API, but the pull toward using their managed voice features defeats the point |

Start with VoIP.ms. The per-minute difference is noise at this volume, and NAT
traversal documentation is worth more than a fraction of a cent.

---

## Latency budget

A caller notices a machine somewhere past 800ms of silence. Target 600ms at the
95th percentile, measured from end-of-speech to first audio out.

| Stage | Budget | Notes |
| --- | --- | --- |
| Endpointing (deciding they stopped) | 150ms | The single biggest lever. Semantic VAD beats a fixed silence timer |
| Whisper on the 5070 | 120ms | `faster-whisper`, `distil-large-v3`, int8. Streaming, not batch |
| OpenAI round trip | 250ms | Over Starlink. Stream the response; do not wait for the full completion |
| TTS first chunk | 80ms | Piper is fast enough. Start speaking on the first sentence, not the last |
| Jitter buffer + network | 100ms | Starlink's contribution, and the least controllable |

Two things buy more than any model choice: **start speaking before the sentence
is finished**, and **barge-in** — if the caller talks over the agent, stop
immediately. A receptionist that will not be interrupted is worse than a menu.

---

## Hardware

The RTX 5070's 12GB is comfortable for speech and nothing else:

| Component | VRAM | Note |
| --- | --- | --- |
| `distil-large-v3` int8 | ~1.5GB | Streaming transcription |
| Piper or Kokoro | <1GB | Kokoro is markedly better; Piper is faster |
| Headroom | ~9GB | Room for a second concurrent call |

Reasoning stays on OpenAI. An 8B model would fit in the headroom, but lead
qualification is exactly where a small model gets vague and starts repeating
itself, and this agent is answering a business line.

**To buy:** a Thunderbolt or OCuLink eGPU enclosure. OCuLink is meaningfully
faster and cheaper; Thunderbolt is more convenient. Bandwidth barely matters
here — the models load once and stay resident — so optimise for whatever the
host machine actually has a port for.

---

## Code layout when it is built

```
datarail_agents/voice/
  bridge.py       Asterisk ARI / AudioSocket, one process per call
  turns.py        VAD, endpointing, barge-in
  stt.py          faster-whisper, streaming
  tts.py          Piper/Kokoro, streams first sentence early
  script.py       the qualification flow
  carrier/
    asterisk/     pjsip.conf, extensions.conf, the trunk in ONE file
```

Reuse without modification:

- `core/brain.py` — same OpenAI wrapper
- `core/policy.py` — **especially** the never-quote rule; saying a number aloud
  is worse than writing one, because there is no draft to hold back
- `core/leads.py` — same store, keyed on phone instead of email
- `knowledge/datarail.md` — same facts, same prohibitions

The voice agent needs one policy gate the email agent does not: **spoken
replies cannot be held for review.** Gate 2 runs before the text is synthesised,
and on failure the agent says a fixed safe line and flags the lead, rather than
improvising a second attempt.

---

## Open risks

1. **Starlink may simply not be good enough.** Unknown until real calls are
   made. Measure jitter and loss for a week before pointing the published
   number at it. The VPS exit exists for this.
2. **TextNow forwarding may not be on the plan.** Check before the DID is
   bought. Cheap to find out, and it changes which number goes on `/contact`.
3. **Barge-in over a satellite link is genuinely hard.** Echo cancellation plus
   variable latency means the agent may hear itself and stop. Budget real time
   for this; it is the part most likely to feel broken.
4. **A phone call is a worse place to be wrong than an inbox.** There is no
   draft, no held-for-review folder, no chance to catch it. The safe-line
   fallback needs to be genuinely good, because it is what a caller will hear
   whenever anything is uncertain.
