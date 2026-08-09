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

warnings.filterwarnings("ignore", category=DeprecationWarning)
