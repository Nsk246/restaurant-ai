# ---------------------------------------------------------------- portal ---
# The portal is built inside the image rather than copied from the working
# tree. `web/portal/dist` is gitignored, so a platform that builds from the
# repo (Railway, Fly remote builder) would otherwise ship an API with no UI
# and 404 on every page load.
FROM node:20-slim AS portal

WORKDIR /build
COPY web/portal/package.json web/portal/package-lock.json ./
RUN npm ci --silent
COPY web/portal ./
RUN npm run build

# ------------------------------------------------------------------ api ---
FROM python:3.12-slim

# psql is needed at runtime by the release command that migrates the database
# before a new version takes traffic.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential postgresql-client && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY services/voice/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get purge -y build-essential && apt-get autoremove -y

COPY services/voice/app ./app
# Migrations, seed and assertions travel with the image so the release
# command can run them.
COPY db ./db
COPY --from=portal /build/dist ./web/portal/dist

EXPOSE 8000

# One worker on purpose. Call state lives in the process until Redis lands,
# so a second worker would drop callers onto a process that has never heard
# of them. --proxy-headers is what makes X-Forwarded-* visible; without it
# Twilio signature validation fails behind any load balancer.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} \
     --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
