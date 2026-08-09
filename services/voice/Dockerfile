FROM python:3.12-slim

# scipy needs a compiler toolchain only at wheel-install time on some arches.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get purge -y build-essential && apt-get autoremove -y

COPY app ./app

EXPOSE 8000
# One worker on purpose. Call state lives in the process until Redis lands in
# M2; multiple workers would drop callers onto a process that has never heard
# of them.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
