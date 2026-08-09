# Running it

From a cold Codespace to a working phone call. Written down because the order
matters and a couple of the steps are not obvious.

## Deployed (Railway)

The deployed instance is the one to demo from. A tunnel URL changes on every
restart and takes the Twilio console with it.

**First time:**

1. Railway, New Project, Deploy from GitHub repo, pick `restaurant-ai`.
   It reads `railway.json` and builds from `services/voice/Dockerfile`.
2. In the project, Add, Database, PostgreSQL. Railway sets `DATABASE_URL`
   on the service automatically.
3. Service, Variables, add:

       GEMINI_API_KEY=...
       TWILIO_ACCOUNT_SID=...
       TWILIO_AUTH_TOKEN=...
       REALTIME_PROVIDER=gemini

   Leave `PUBLIC_BASE_URL` unset. Railway provides its own hostname and the
   app uses it, which is one fewer value to copy wrong.
4. Settings, Networking, Generate Domain.
5. Twilio, +15722281712, Voice Configuration, Webhook POST to
   `https://your-app.up.railway.app/twilio/voice`. This one never changes.

**Every deploy after that** is a `git push`. The pre-deploy command migrates,
seeds, and runs the schema assertions; Railway aborts the release if any of
it fails, so a broken migration never reaches a live phone line.

Check it came up:

    curl -s https://your-app.up.railway.app/health

Wants `{"ok":true,"provider":"gemini","database":"up"}`.

## Every session (local)

**1. Start the services.** Postgres and Redis do not survive a container stop.

    cd /workspaces/restaurant-ai
    bash .devcontainer/start.sh
    make test

Expect 13 schema assertions and 100 Python tests. If the order tests skip
rather than pass, Postgres is not up.

**2. Start the app.** Always from the repo root, never from `services/voice`:
the app reads `.env` from the repo root, and launching elsewhere used to load
nothing and silently fall back to defaults.

    make api

Check it in another terminal:

    curl -s localhost:8000/health

Want `{"ok":true,"provider":"gemini","database":"up"}`. If it says `mock`, the
Gemini key did not load; the startup log prints which `.env` it read and
whether it existed.

**3. Start the tunnel.** Twilio needs a public URL.

    ngrok http 8000

Copy the `https://xxxx.ngrok-free.app` host it prints.

**4. Point the app at the tunnel.** The URL changes every time ngrok restarts,
and it is what builds the `wss://` stream address handed to Twilio. Get it
wrong and the call connects, then goes silent.

    sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=xxxx.ngrok-free.app|" .env

Restart `make api`, because `--reload` watches Python files and not `.env`.

Verify the stream URL before dialling:

    curl -s -X POST localhost:8000/twilio/voice \
      -d "To=%2B15722281712&CallSid=CAtest"

Want `wss://xxxx.ngrok-free.app/ws/twilio/CAtest`. An empty host or a double
slash means `PUBLIC_BASE_URL` is wrong.

**5. Point Twilio at the tunnel.** Console, Phone Numbers, Active Numbers,
+15722281712, Voice Configuration. Webhook, HTTP POST:

    https://xxxx.ngrok-free.app/twilio/voice

Save. This has to be redone whenever the ngrok URL changes.

**6. Open the portal** at port 8000 and call.

## Putting a real menu in

Open the portal, press **Menu**, then **Import**. Paste whatever the
restaurant has: a copy-pasted PDF, an email, a transcribed photo. It proposes
structured items with prices and sections; correct anything wrong, then press
import. Nothing is written until you do.

Tick **Replace the current menu** to put a real menu in over the sample one.
That deactivates the existing items rather than deleting them, so past orders
still resolve their line items.

The **Edit** tab does day-to-day work: change a price inline, 86 an item,
add a special. The code beside each dish is what the agent emits in a tool
call, and renaming deliberately does not change it.

## Tickets without a phone call

    cd services/voice && python tools/seed_demo_calls.py --count 8

Real orders through the real tool layer. `Reset` in the portal clears them.

## Is Gemini the problem?

    cd services/voice && python tools/probe_gemini.py

Tests the model on its own, so a failure comes with a message instead of dead
air on a call.

## Where things stand

Working: inbound calls, tenant routing by dialled number, menu injection,
ordering with modifiers and kitchen notes, enforced readback, idempotent
confirmation, kitchen rail, 86 board, demo reset.

**Open, in priority order:**

1. **Latency.** Last measured p50 1935ms against a 900ms target. The lever is
   `GEMINI_END_OF_SPEECH_MS`, the silence the model waits through before
   deciding the caller finished. It is added to every turn. Try 400, 300, 250,
   one call each, and stop one notch above where it starts cutting you off
   mid-sentence. Read the `CALL DONE` line in the log after each call.
2. **Some turns got no reply.** May be fixed: the outbound pacing loop was
   drifting ~64ms per turn and compounding. Re-check across a longer call.
3. **Twilio signature validation is off.** `TWILIO_VALIDATE_SIGNATURE=false`.
   The URL matched and the token matched itself, so the likely cause is the
   token belonging to a different account than the one owning the number.
   Check the Account SID in the console against `.env` before any real demo.
4. **Hosting.** ngrok URLs change on every restart, so steps 4 and 5 have to
   be repeated each session. Fly works and is deployable (`fly.toml`,
   `db/release.sh`) but its free trial stops machines after five minutes,
   which caused a long evening of phantom failures. It needs a card.

## Things that cost hours, so they do not again

- **Launch from the repo root.** `.env` resolves there. Now absolute in
  `config.py`, and the startup log says which file it read.
- **Stop uvicorn before replacing files.** `rm -rf services` while it is
  running kills the reloader with a `FileNotFoundError` about `getcwd`.
- **`.env` changes need a full restart.** `--reload` only watches Python.
- **A trial Fly machine stops after five minutes.** It looks exactly like the
  database crashing, the DNS breaking, and the app dying mid-call.
