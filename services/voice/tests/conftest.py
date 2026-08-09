"""Test environment.

Settings are read once and cached, so these must be set before anything
imports `app.config`. Putting them here rather than in a test module keeps
import order out of the tests themselves.
"""

import os
import warnings

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("REALTIME_PROVIDER", "mock")
os.environ.setdefault("PUBLIC_BASE_URL", "https://demo.test")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")
# The app opens its own pool through its lifespan, on its own event loop.
# Sharing one pool across loops raises InterfaceError in ways that look like
# a database problem and are not.
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL", "postgresql://operator:operator@127.0.0.1:5432/operator"
    ),
)

warnings.filterwarnings("ignore", category=DeprecationWarning)
