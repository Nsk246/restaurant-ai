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
