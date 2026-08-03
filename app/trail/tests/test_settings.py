"""Secrets fail CLOSED — a published dev default must never boot with DEBUG off.

This source is public. `DEV_SECRET_KEY` and `DEV_INGEST_TOKEN` are therefore
known to everyone, and each one is a working attack if it ever reaches a
non-DEBUG process: the signing key forges sessions and CSRF tokens, and the
ingest token is the *only* authentication on `POST /ingest/` and
`GET /control/<vantage>/`.

Two layers of test, deliberately:

* the unit tests call `require_configured_secrets` directly — fast, and they
  name the exact rule;
* `SettingsModuleRefusesToImportTest` spawns a real interpreter and imports
  `config.settings` under a controlled environment, which is the only way to
  prove the module *calls* the check at import time rather than merely
  defining it.
"""
import os
import subprocess
import sys

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from config import settings as app_settings

REAL_KEY = "a-real-signing-key"
REAL_TOKEN = "a-real-ingest-token"


class RequireConfiguredSecretsTest(TestCase):
    """The rule itself: dev defaults are fine in DEBUG, fatal outside it."""

    def test_dev_signing_key_is_fatal_with_debug_off(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            app_settings.require_configured_secrets(
                False, app_settings.DEV_SECRET_KEY, REAL_TOKEN
            )
        self.assertIn("DJANGO_SIGNING_KEY", str(caught.exception))

    def test_dev_ingest_token_is_fatal_with_debug_off(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            app_settings.require_configured_secrets(
                False, REAL_KEY, app_settings.DEV_INGEST_TOKEN
            )
        self.assertIn("PING_TRAIL_INGEST_TOKEN", str(caught.exception))

    def test_both_dev_defaults_report_the_signing_key_first(self):
        """One clear instruction at a time — the operator fixes, re-runs, fixes."""
        with self.assertRaises(ImproperlyConfigured) as caught:
            app_settings.require_configured_secrets(
                False, app_settings.DEV_SECRET_KEY, app_settings.DEV_INGEST_TOKEN
            )
        self.assertIn("DJANGO_SIGNING_KEY", str(caught.exception))

    def test_dev_defaults_are_fine_with_debug_on(self):
        """Local dev must stay zero-setup: clone, compose up, it runs."""
        app_settings.require_configured_secrets(
            True, app_settings.DEV_SECRET_KEY, app_settings.DEV_INGEST_TOKEN
        )

    def test_real_secrets_are_fine_with_debug_off(self):
        app_settings.require_configured_secrets(False, REAL_KEY, REAL_TOKEN)

    def test_the_check_compares_against_the_values_actually_shipped(self):
        """A renamed constant must not quietly stop matching the shipped default."""
        self.assertEqual(app_settings.DEV_SECRET_KEY, "dev-only-change-me")
        self.assertEqual(app_settings.DEV_INGEST_TOKEN, "dev-ingest-token")


class SettingsModuleRefusesToImportTest(TestCase):
    """End to end: importing the settings module is what actually has to fail."""

    def _import_settings(self, **env_overrides):
        """Import `config.settings` in a fresh interpreter with a clean env."""
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DJANGO_", "PING_TRAIL_"))
        }
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, "-c", "import config.settings"],
            cwd=str(app_settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_debug_off_with_no_secrets_configured_refuses_to_import(self):
        result = self._import_settings(DJANGO_DEBUG="0")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DJANGO_SIGNING_KEY", result.stderr)

    def test_debug_off_with_a_signing_key_still_refuses_the_dev_ingest_token(self):
        result = self._import_settings(DJANGO_DEBUG="0", DJANGO_SIGNING_KEY=REAL_KEY)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("PING_TRAIL_INGEST_TOKEN", result.stderr)

    def test_debug_on_imports_with_no_configuration_at_all(self):
        result = self._import_settings(DJANGO_DEBUG="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_debug_default_is_on_so_a_bare_clone_runs(self):
        result = self._import_settings()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_debug_off_with_both_secrets_configured_imports_cleanly(self):
        result = self._import_settings(
            DJANGO_DEBUG="0",
            DJANGO_SIGNING_KEY=REAL_KEY,
            PING_TRAIL_INGEST_TOKEN=REAL_TOKEN,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_legacy_secret_key_env_var_also_satisfies_the_check(self):
        """DJANGO_SECRET_KEY is the documented alternative to DJANGO_SIGNING_KEY."""
        result = self._import_settings(
            DJANGO_DEBUG="0",
            DJANGO_SECRET_KEY=REAL_KEY,
            PING_TRAIL_INGEST_TOKEN=REAL_TOKEN,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
