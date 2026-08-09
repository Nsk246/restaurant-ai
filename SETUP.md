# Setup

From zip to a phone call you can answer.

## 1. Files and repo

```bash
unzip restaurant-ai.zip && cd restaurant-ai
gh auth status || gh auth login          # bootstrap.sh needs this
./bootstrap.sh restaurant-ai
```

Creates the repo, commits, pushes, and tries to enable branch protection.
Protection on private repos needs a paid plan; the script continues either way.

## 2. Codespaces

Open the repo on GitHub, then Code, Codespaces, Create codespace on main.

Setup runs automatically and applies migrations, seeds the pilot restaurant,
and runs the 13 schema assertions. Watch for `--- all schema assertions
passed ---` in the terminal.

## 3. Verify before touching anything

```bash
make test
```

Expect 13 schema assertions and 26 Python tests. If either fails, stop here.
Nothing downstream is worth debugging on a broken foundation.

## 4. Run it with no keys at all

```bash
make api
curl localhost:8000/health
```

Returns `{"ok":true,"provider":"mock"}`. The mock provider means the whole
bridge runs with no Gemini key, no Twilio account, and no phone.

## 5. Your keys

```bash
cp .env.example .env
```

Fill in `GEMINI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`. Set
`REALTIME_PROVIDER=gemini`. Leave `PUBLIC_BASE_URL` until step 6.

## 6. Deploy

You can try pointing Twilio at the Codespaces forwarded URL first, and it may
work. If the media stream fails with Twilio error 31920, that is the relay
rejecting the WebSocket upgrade, and it is not fixable from inside the
container. That happened on The Operator. Deploying is the reliable path.

```bash
fly launch --no-deploy --copy-config --name restaurant-ai-voice
fly secrets set \
  GEMINI_API_KEY=... \
  TWILIO_ACCOUNT_SID=... \
  TWILIO_AUTH_TOKEN=... \
  REALTIME_PROVIDER=gemini \
  PUBLIC_BASE_URL=restaurant-ai-voice.fly.dev
fly deploy
curl https://restaurant-ai-voice.fly.dev/health
```

## 7. Point Twilio at it

Twilio Console, Phone Numbers, your number, Voice Configuration:

- A call comes in: Webhook
- URL: `https://restaurant-ai-voice.fly.dev/twilio/voice`
- Method: HTTP POST

Save.

## 8. Call it

Dial your Twilio number from the phone you signed up with, which is verified
automatically. You will hear the trial notice, then the agent.

Watch the logs:

```bash
fly logs
```

M1's gate is p50 under 900ms with barge-in interrupting cleanly. Talk over the
agent mid-sentence and confirm it stops immediately rather than finishing its
thought.

## The Pass

    make portal     build it once
    make api        serve API and portal on :8000

Open port 8000. Front of house is above the line, the kitchen rail below it.
During a call the feed shows speech with tool marks inline, and the chit fills
as each `add_item` fires rather than at the end of the call.

The 86 board takes an item off the menu immediately: the next call will not be
offered it at all, rather than being told not to mention it. Reset clears
orders and calls and un-86s everything, but leaves the menu alone, so a
prospect's own menu survives between demos.

For frontend work, run the portal on its own with hot reload:

    make portal-dev     :5173, proxies /api and /ws to :8000

### Tickets without a phone call

    cd services/voice && python tools/seed_demo_calls.py --count 5

Runs real orders through the real tool layer, so what lands is exactly what a
call produces: the readback gate, the fired ticket, the closed conversation
with latency numbers. Useful for rehearsing and for working on the portal.

`Reset` in the top right clears it all again.

## Making a real call

1. Get a key at aistudio.google.com. Native-audio Live models need a billed
   project, not just a free key.
2. In `.env`: `GEMINI_API_KEY=...` and `REALTIME_PROVIDER=gemini`.
3. Deploy, because the Codespaces relay is unreliable for the media stream:

       fly launch --no-deploy --copy-config --name restaurant-ai-voice
       fly secrets set GEMINI_API_KEY=... TWILIO_ACCOUNT_SID=... \
         TWILIO_AUTH_TOKEN=... REALTIME_PROVIDER=gemini \
         DATABASE_URL=... PUBLIC_BASE_URL=restaurant-ai-voice.fly.dev
       fly deploy

   `DATABASE_URL` must point at a database Fly can reach. `fly postgres create`
   then `fly postgres attach`, and apply migrations and seed against it.
4. Twilio Console, your number, Voice Configuration: Webhook, POST, to
   `https://restaurant-ai-voice.fly.dev/twilio/voice`.
5. Call from the phone you signed up with. Trial notice first, then the agent.

What to try on the call, in order of how much it tells you:

- "What's on the menu?" Confirms the snapshot reached the model.
- "Two hot chicken, extra hot, no pickles." Confirms tool calls with modifiers
  and a kitchen note.
- "Actually make that one." Confirms mid-order correction.
- Interrupt it mid-sentence. Confirms barge-in.
- "Is the grain bowl vegan?" Confirms it answers from tags, not invention.

Then check what landed:

    psql "$DATABASE_URL" -c "select order_number, status, total_cents from orders order by created_at desc limit 3;"
    psql "$DATABASE_URL" -c "select p50_response_ms, p95_response_ms, turn_count from conversations order by started_at desc limit 1;"

p50 under 900ms is the M1 gate. Cost is roughly half a cent per minute of
audio in and under two cents per minute out, so a test call costs pennies.

## Troubleshooting

**Silence on the call, no errors in logs.** Almost always TwiML using
`<Start><Stream>` instead of `<Connect><Stream>`. `<Start>` is listen-only.
There is a test for this.

**Codespace stuck at "Retrieving" or "Setting up your codespace".** Fixed. The
devcontainer previously used a docker-compose stack plus three features, which
forced Codespaces to build a container on every create. It is now a single
prebuilt Python image with Postgres and Redis apt-installed by setup.sh, which
builds in well under a minute. If you still have a stuck Codespace, delete it
rather than rebuilding: half-created containers carry their broken state
forward.

**Setup fails with a Yarn GPG error.** The base image carries a Yarn apt source
with no signing key, so `apt-get update` fails and takes the script with it.
Fixed: setup.sh now removes that source and tolerates partial update failures.
To unstick an existing container: `sudo rm -f /etc/apt/sources.list.d/yarn.list`
then re-run `bash .devcontainer/setup.sh`.

**Setup prompts for a sudo password.** Devcontainers give the `vscode` user
passwordless sudo to root only; `sudo -u postgres` targets a third user and
falls outside that rule, so it prompts for a password that was never set.
Fixed: privileged Postgres commands now go through root via `sudo su postgres`.

**"This database already has a schema but no migration history."** The
migration runner is newer than your database. If the schema matches
`db/migrations`, adopt it with `bash db/migrate.sh --baseline "$DATABASE_URL"`,
then `make test` to confirm. If unsure, `make reset` rebuilds from scratch.

**`npm: not found`.** The devcontainer image is Python-only; Node is installed
by `setup.sh`. On a container created before that change:
`curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs`

**`GET / 404` from the API.** The portal has not been built. `make portal`.

**Tests wiped my demo data.** They no longer can. Tests run against
`operator_test`, created and migrated automatically by `make test`, because
the API tests call `/api/demo/reset` and pointing them at the development
database destroyed whatever was on the rail.

**Gemini session fails to open.** Model ids churn on the developer tier. The
error names the model it tried; check the current list at
https://ai.google.dev/gemini-api/docs/models and set `GEMINI_LIVE_MODEL`.

**Twilio 31920.** The WebSocket upgrade was rejected. Deploy to Fly and repoint.

**Agent replies once then goes deaf.** The provider's receive iterator ended at
a turn boundary. `test_receive_loop_survives_turn_boundaries` covers this.

**Agent gets cut off by background noise.** Raise `BARGE_RMS_THRESHOLD` or
`BARGE_SUSTAIN_FRAMES` in `services/voice/app/telephony/bridge.py`.

## Known gaps, closing in M2

The seeded phone number is a placeholder, so replace `+16155550111` in
`db/seed/001_pilot.sql` with your real Twilio number. The webhook does not yet
resolve the tenant from the dialled number, and the agent has no tools and no
menu. That is M2.
