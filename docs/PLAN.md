# Platform Plan

An AI phone operator and an in-restaurant avatar kiosk, built as one product.
This document is the plan of record. It gets revised as we learn, but nothing
gets built that isn't in here first.

---

## 1. What this is

A restaurant answers its phone with an AI that can take orders, take
reservations, answer menu questions, and hand the result to the kitchen. Later,
the same agent greets walk-in guests through an avatar on a screen at the
reception.

The thing to hold onto: **it is one agent with two mouths.** The phone and the
kiosk are I/O adapters. Every piece of restaurant logic lives behind a tool
layer that neither adapter owns. Get that boundary right and Stage 2 is largely
a rendering job on a brain that already works.

**Pilot:** one US restaurant, tenant-ready schema, no tenant admin UI yet.
**POS:** unknown. Every kitchen write goes through a `KitchenSink` interface
with one implementation today. A Toast or Square adapter drops in later without
the agent changing.

---

## 2. The demo is the product, for now

This gets shown to prospects before it runs a single real dinner service. That
inverts the usual priorities and I want it stated plainly, because it changes
engineering decisions all the way down.

**What this means concretely:**

- **The demo must not fail live.** A prospect watching an AI stumble on their
  own phone call is worse than never having demoed. Resilience work that would
  normally be M6 moves earlier.
- **The wow is the handoff, not the chat.** Prospects have all talked to a
  chatbot. What they have not seen is their spoken words becoming a kitchen
  ticket in front of them while they are still on the phone. That moment is what
  we design the entire portal around.
- **They use their own phone.** Not a simulator, not a recording. They dial the
  number, it is their voice, and the skepticism drains out of the room.
- **It must reset in one click.** You will run this five times in a day.
- **It must survive bad wifi.** Conference and restaurant wifi is hostile.
  Tether to a phone, and have a fallback path.

### Demo choreography

The sequence we build toward, roughly ninety seconds:

1. Prospect dials the number from their own phone. Speaker on.
   (On trial, verify their number in advance. See open decision 2.)
2. Big screen: the call goes live. Waveform, running transcript.
3. They order naturally, including something awkward. "Actually make that two,
   and no pickles."
4. As they speak, items land on a ticket on screen. Each one appears at the
   moment the agent's tool fires, so the screen is visibly reacting to *them*.
5. They ask something the menu has to answer. "Is the grain bowl vegan?"
6. Agent reads the order back. They say yes.
7. The ticket clips to the kitchen rail. Audible. The kitchen side of the
   screen now has a live ticket with a timer running.
8. Their phone buzzes. SMS confirmation, under two seconds.
9. Response latency has been visible on screen the whole time.

Then the closer: hand them the phone again and invite them to try to break it.
Graceful failure in front of a prospect builds more trust than a clean run,
which is why M5 exists.

### Demo safety rules

- `DEMO_MODE=true` allowlists SMS recipients and disables real charges.
- Never scale to zero. A cold start mid-demo is a lost deal.
- Reset restores seed state, clears orders, un-86s everything.
- A recorded call replay exists as a fallback if telephony fails entirely.
- Nothing on the screen shows a raw error. Failure states are designed.

---

## 3. Architecture

```
   Caller ──PSTN──▶ Twilio ──WebSocket (μ-law 8k)──▶ ┌──────────────────┐
                                                      │  Media Bridge    │
   Kiosk ───────────browser mic (PCM 16k)───────────▶ │  (adapter layer) │
                                                      └────────┬─────────┘
                                                               │
                                                      ┌────────▼─────────┐
                                                      │  Agent Core      │
                                                      │  session state   │
                                                      │  prompt assembly │
                                                      │  tool dispatch   │
                                                      └────────┬─────────┘
                                                               │
                    ┌──────────────┬─────────────────┬─────────┴────────┐
                    ▼              ▼                 ▼                  ▼
              Realtime model   Postgres           Redis            KitchenSink
              (speech↔speech)  orders, menu    live session      InternalKDS
                                                                 (POS later)
                                                               │
                                                      ┌────────▼─────────┐
                                                      │ Portal + KDS     │
                                                      │ live over WS     │
                                                      └──────────────────┘
```

### Component decisions

| Layer | Choice | Why |
|---|---|---|
| API + bridge | Python 3.12, FastAPI, uvicorn | Long-lived WebSockets, and it's the stack you're fastest in |
| Database | Postgres 16 | Concurrency, real constraints, tenant isolation. Not SQLite this time |
| Session state | Redis | A pod restart mid-call must not drop the caller |
| Frontend | React 19, TypeScript, Vite | Your stack. Portal and KDS share a design system |
| Realtime transport | Twilio Media Streams | Bidirectional, low overhead |
| Speech model | Provider-flagged, benchmarked in M1 | This choice carries into the kiosk. Decide on numbers, not habit |
| Hosting | Fly.io, always-on machine, `iad` | WebSockets, no cold start, near Twilio's Ashburn edge |
| Payments | Stripe payment links via SMS | No card numbers over voice. Keeps us out of PCI scope entirely |

### The tool layer

The agent never writes free text into the database. Every action is a
constrained tool call. This is the single most important accuracy decision in
the system.

```
get_menu()                      → sellable items only, 86'd items absent
check_item(query)               → resolves spoken name via aliases
start_order(type)               → returns draft order id
add_item(item_id, qty, mods[])  → item_id is a uuid from the menu, never a string
remove_item(line_id)
set_customer(name, phone)
quote_order()                   → totals + prep time estimate
confirm_order(idempotency_key)  → draft → confirmed
fire_order(order_id)            → confirmed → fired, hits KitchenSink
check_availability(datetime, party_size)
create_reservation(...)
get_hours(date)
transfer_to_human(reason)
```

Two rules that fall out of this:

- The model cannot invent a dish, because `add_item` only accepts uuids that
  came from the injected menu snapshot.
- The model cannot sell what's 86'd, because unavailable items never enter the
  snapshot in the first place.

### Measured latency, August 2026

A real call on Railway: 1614ms total response, of which 1633ms was the model
round trip. Transport measured effectively zero, so the network is not the
problem and there is nothing to win by moving regions or hosts. Two earlier
hypotheses about transport were both wrong.

Isolating the model with `tools/sweep_latency.py`, one variable at a time:

    bare prompt, no tools              755ms
    full menu, no tools               1008ms

The 4,810-character menu therefore costs roughly 250ms on every turn. Tool
declarations and the tool round trip account for the rest.

Levers, in measured order of size: shrink the injected menu, reduce the
model's end-of-speech wait, cut the tools offered per turn.

### Latency budget

Target: **under 900ms** from caller stopping to agent starting to speak.

```
  network in (Twilio → us)       ~40ms
  model round trip              ~400-600ms
  tool call (when one fires)    ~50-150ms   ← must stay under 1200ms or we bail
  network out                    ~40ms
```

Tool calls are the risk. Mitigations: keep the menu in context rather than
querying it per turn, cache the snapshot in Redis per session, and if a tool
exceeds `TOOL_TIMEOUT_MS` the agent speaks a filler line rather than going
silent. Silence is what makes a voice agent feel broken.

### Measured latency, August 2026

A real call on Railway: 1614ms total response, of which 1633ms was the model
round trip. Transport measured effectively zero, so the network is not the
problem and there is nothing to win by moving regions or hosts.

Isolating the model with the sweep tool, one variable at a time:

    bare prompt, no tools              755ms
    full menu, no tools               1008ms

So the 4,810-character menu costs roughly 250ms on every turn. Tool
declarations and the tool round trip account for the rest.

The levers, in order of measured size: shrink the injected menu, reduce the
model's end-of-speech wait, cut the number of tools offered per turn.

### Known optimization, not yet needed

The menu snapshot for 18 seeded items is 8.3KB, about 2,100 tokens. Roughly 40%
of that is uuids at 36 characters each. If the pilot menu is large, swap the
snapshot ids for short stable codes. Not worth doing until a real menu tells us
it matters.

---

## 4. Repo and development environment

### Structure

```
restaurant-ai/
├── .devcontainer/          Codespaces definition
├── .github/workflows/      CI
├── db/
│   ├── migrations/         schema, applied in filename order
│   ├── seed/               pilot restaurant, idempotent
│   └── tests/              assertions that guarantees hold
├── services/
│   └── voice/              FastAPI: bridge, agent core, tools, kitchen sink
├── web/
│   ├── portal/             owner + live call view
│   ├── kds/                kitchen display
│   └── shared/             design tokens, generated API types
├── docs/
│   ├── PLAN.md             this file
│   ├── DEMO.md             the runbook, printed and rehearsed
│   └── ADR/                one file per architectural decision
├── docker-compose.yml
└── Makefile
```

One repo, private, `main` protected. Squash merges. Conventional commits, since
the changelog doubles as the demo history.

### Codespaces

The `.devcontainer` gives a working environment in one click: Python 3.12,
Node 20, Postgres and Redis as compose services, and a `postCreateCommand` that
runs migrations, seed, and the schema assertions so a fresh Codespace is a
working database.

**Correction to an earlier version of this plan.** I previously wrote that
Codespaces public port forwarding is a reliable Twilio webhook target. It is
not, and we have already been burned by it. On The Operator, the Codespaces
relay returned HTTP 404 on unauthenticated WebSocket upgrade requests, which
surfaced as Twilio error 31920 and blocked the media stream entirely. Identical
code worked in one Codespace and failed in another, which points at relay
assignment rather than anything in our control.

So the rule is:

- HTTP webhooks to a public Codespaces port usually work, and are fine for
  iterating on `/twilio/voice`.
- The **media stream WebSocket should not depend on Codespaces.** Deploy to Fly
  and point Twilio there, even during development. The devcontainer prints this
  warning on setup so it is not rediscovered at 2am.

Demos never run from Codespaces regardless: the machine sleeps on idle and the
extra relay hop costs latency we cannot afford.

### CI

On every PR: spin a Postgres service container, apply migrations, run the
schema assertions, then ruff and mypy on Python, tsc and eslint on the web. The
schema tests already exist and already caught two real bugs, so they earn their
place in the gate.

### Deployment

Fly.io, one always-on machine in `iad`, no autostop. Managed Postgres and
Redis. A `staging` app and a `demo` app, where `demo` only ever runs a tagged
release you have personally rehearsed against.

---

## 5. Design direction

### Grounding

The reflexive palette for a restaurant product is cream paper, a high-contrast
serif, and a terracotta accent. It is also the single most recognizable
AI-generated design idiom right now. Prospects who have seen any recent product
site will read it as generic. So we go somewhere else, and we take the material
world of a working restaurant literally.

### The thesis: The Pass

The pass is the steel counter where front of house meets back of house. It is
the physical place a ticket crosses from the person taking the order to the
person cooking it. That is exactly what this product is.

So the portal is laid out as a pass. Front of house lives above the line, the
kitchen lives below it, and the ticket crosses the band in the middle. This is
the one bold structural move. Everything else stays quiet.

Two visual registers, deliberately different, sharing one token set:

- **Front of house** is composed and legible, read at desk distance by an owner.
- **Back of house** is operational and high contrast, read at six feet by
  someone with their hands full. Bigger type, fewer words, timers that shift
  colour as they age.

That contrast is itself a demo asset. "This is what your host sees. This is
what your line sees."

### Tokens

Colour. A steel field with heat-lamp light, not paper with clay.

```
--steel-900  #12171B   deep brushed steel, primary background
--steel-700  #1C242A   raised surfaces, panels
--steel-500  #38454E   dividers, inactive rails
--lamp-400   #FFA51E   heat lamp amber. Live states, active call, hot ticket
--ember-500  #E0472B   late ticket, error, 86'd
--service-400 #52A96B  ready, served, confirmed
--chit-100   #F1EDE2   ticket paper. Only ever a ticket surface, never a page
```

The cream appears exactly once, as the chit. That is the difference between a
material and a mood: it is an object sitting on a steel field, not the field
itself.

Type.

```
Display   Archivo Expanded    wide industrial signage, not editorial serif
UI        Archivo             same family, keeps the system tight
Data      Martian Mono        chits, order numbers, latency, timers
```

Martian Mono does the heavy lifting on the kitchen side, where everything is
data being read fast. Archivo Expanded appears rarely and large.

### Layout

```
┌──────────────────────────────────────────────────────────────┐
│  BROADWAY KITCHEN                    ● LIVE   0:42    812ms  │  status rail
├──────────────────────────────────────────────────────────────┤
│                                                              │
│   FRONT OF HOUSE                              ┌────────────┐ │
│   ╭──────────────────────────────╮            │ ░░░░░░░░░░ │ │
│   │ ~~~~~~ waveform ~~~~~~~~~~~~ │            │  T-014     │ │
│   ╰──────────────────────────────╯            │            │ │
│                                               │ 2  HOT CHIX│ │
│   caller  "two hot chicken, no pickles"       │    no pickl│ │
│   agent   "got it, two hot chicken"           │ 1  MAC     │ │
│           ▸ add_item  ▸ add_item              │            │ │
│   caller  "and a mac and cheese"              │ ░░░░░░░░░░ │ │
│   agent   "anything to drink?"                └────────────┘ │
│           ▸ add_item                            the chit,    │
│                                                 filling live │
├══════════════════════════════════════════════════════════════┤  ← the pass
│   BACK OF HOUSE                                              │
│   ┌────────┐ ┌────────┐ ┌────────┐                           │
│   │ T-011  │ │ T-012  │ │ T-013  │                           │
│   │ 12:04  │ │ 06:20  │ │ 01:55  │   timers, oldest first    │
│   │ ●LATE  │ │ ●FIRING│ │ ●NEW   │                           │
│   └────────┘ └────────┘ └────────┘                           │
└──────────────────────────────────────────────────────────────┘
```

### Signature element

**The chit fills as the caller speaks.** A line lands on the ticket at the exact
moment `add_item` fires, not when the call ends. Tool calls render as small
marks in the transcript so you can see the machine thinking. On confirmation the
chit clips down to the rail below the pass with a sound.

That is the whole demo in one interaction, and it is impossible to fake, which
is the point.

Motion is spent almost entirely here. Everywhere else: state changes only, and
`prefers-reduced-motion` respected throughout.

### Copy

Interface language comes from the restaurant, not from software. Orders are
**fired**, not submitted. Items are **86'd**, not disabled. The kitchen view is
the **rail**. An empty rail says "No tickets on the rail" and nothing else.

Errors state what happened and what to do, in the product's voice. A dropped
call reads "Call ended before the order was confirmed. Nothing was fired."

---

## 6. Stage 1: the voice operator

**M0 — Data foundation. Done.**
Schema, order state machine, menu snapshot, seed, 13 passing assertions.

**M1 — Media bridge. Done.**
Twilio inbound webhook with signature validation, tenant resolution by dialled
number, bidirectional media stream, μ-law 8k resampling, audible round trip.
Then barge-in, which is where most voice agents feel broken. Then a latency
harness that records per-turn timings to `conversations`.

Ends with: you dial a number and have a conversation. No ordering yet.
Gate: p50 under 900ms on a real cellular call, barge-in interrupts cleanly.

**M2 — Tools and ordering. Done.**
Tool layer against the M0 schema, session state in Redis, menu snapshot
injection, order construction, mandatory spoken readback before confirm,
idempotent confirmation.

Gate: twenty scripted orders, including modifiers and mid-order changes,
land in the database exactly right.

**M3 — Portal, rail, and confirmation. Done.**
The Pass design built for real. Live call view, chit filling on tool calls,
kitchen rail with timers, 86 toggle, SMS confirmation through the notification
queue.

Gate: the full demo choreography runs end to end.

**M3.5 — Menu management. Done.**
The menu was seed SQL, so a price change was a deploy. Now: create, edit,
deactivate, attach modifier groups, and import a pasted menu.

Import is deliberately two steps. Parsing a menu is inexact, so a parse
produces a preview to correct and only an explicit commit writes. `replace`
deactivates the existing menu for onboarding a real restaurant over the
sample data, and deactivates rather than deletes so past orders still
resolve their line items.

This exists because the highest-leverage demo change is seeding a prospect's
own menu, and that needs a door which is not me writing SQL.

**M4 — Reservations, hours, and questions.**
Capacity checking, reservation creation, hours including exceptions, allergen
and dietary answers grounded in menu tags.

**M5 — Holding up when the caller goes off script. Done.**
Not a showcase of failures. This is what lets a prospect talk normally
instead of reading from a card.

- **No dead air.** Two deadlines on every tool call. If one has not answered
  in 450ms the agent says three or four words and waits, rather than leaving
  silence that sounds like a dropped line. The real answer still arrives.
- **Stops guessing.** Consecutive dead ends are counted; after two the tool
  result carries a hint telling the model to apologise and fetch a person. A
  success resets the counter, so stumbling once early does not haunt the call.
- **Actually transfers.** `<Connect><Stream>` occupies the call for its whole
  duration, so a transfer is a Twilio REST redirect performed once the media
  stream ends. `answerOnBridge` is set, or the caller hears silence while the
  phone is still ringing. A failed transfer is logged loudly, because it drops
  someone who just asked for a human.
- **Knows when it is closed.** Hours and one-off exceptions live in rows, so a
  holiday closure is data. A closed answer always says when the restaurant
  next opens.

**M6 — Demo hardening.****M6 — Demo hardening.**
Reset, demo mode, allowlists, recorded fallback, observability, an order
accuracy eval set built from recorded calls that can be replayed against
changes. Rehearse the runbook five times.

---

## 7. Stage 2: the avatar

Only starts once Stage 1 is demoing reliably. The agent core is reused
unchanged, which is the entire payoff of the architecture.

**A1 — Kiosk adapter.** Browser audio in and out against the same agent core
and the same tools, no avatar yet. Proves the abstraction held. Kiosk audio is
wideband and has no telephony hop, so this should be *faster* than the phone.

**A2 — Avatar and lip sync.** VRM render, audio-driven visemes, idle and
listening states. Lip sync is driven from output audio amplitude and formants
client-side, which means it does not constrain the speech model choice.

**A3 — Presence.** Camera-triggered greeting on approach, attention and gaze,
returning guest recognition if the pilot wants it. This one has real privacy
implications and needs a decision with the restaurant, not just with us.

**A4 — Dine-in flow.** Table assignment, order to the same rail, the kitchen
sees phone and kiosk tickets side by side. That shared rail is the proof that
it was one product all along.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| Order accuracy on 8kHz narrowband audio | Constrained tools, alias matching, mandatory readback, recorded eval set |
| Latency creeping past the point it feels human | Measured from M1, gated at every milestone, filler audio on slow tools |
| Live demo failure | Always-on deploy, one-click reset, recorded fallback, rehearsed runbook |
| POS integration blocked by vendor partnership gates | `KitchenSink` interface, own KDS ships first, adapters later |
| Noisy restaurant background on the kiosk | Directional mic, push-to-talk fallback, deferred to A1 testing |
| Prospect asks about their own POS in the room | Answer honestly: interface exists, adapter is scoped work |

---

## 9. Open decisions

1. **Speech provider.** Benchmark Gemini Live against one alternative on real
   phone-quality audio during M1. It carries into the kiosk, so decide on
   measured latency and narrowband accuracy, not familiarity.
2. **Twilio account. Decided: stay on trial through early demos.**
   Trial restrictions we are accepting for now:
   - A Twilio trial notice plays before our TwiML runs. Callers hear it first.
   - Inbound calls only connect from numbers verified in the account, max five.
   - Outbound SMS only reaches those same verified numbers.
   - Trial accounts expire after 30 days. **Set a calendar reminder.**

   Workaround for scheduled demos: collect the prospect's mobile in advance and
   verify it. They then dial from their own phone and receive the confirmation
   SMS, which keeps the demo choreography intact.

   Upgrade becomes mandatory when demos stop being scheduled, when more than
   five people need to call, or when SMS goes to real customers. US SMS also
   requires A2P 10DLC registration on a paid account, and carrier approval takes
   days, so start that at least a week before it is needed.
3. **Demo restaurant identity.** A real prospect's menu is far more persuasive
   than a fictional one. Worth asking whoever you demo to for their PDF menu
   ahead of time and seeding it.
4. **Avatar privacy posture.** Camera recognition needs an explicit stance
   before A3, not after.
