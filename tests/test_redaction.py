"""Provider-token, key-block and escaped-JSON redaction, and its limits."""

from __future__ import annotations

import time
import unittest

from muninn.redaction import REDACTED as R
from muninn.redaction import redact

# Built at run time so no secret scanner sees a whole token in the source.
A36 = "A1b2C3d4" * 5  # 40 alphanumerics
SLACK = "xo" + "xb-" + "1234567890-abcdefghij"
GOOGLE = "AI" + "za" + "A" * 35
STRIPE = "sk" + "_live_" + A36[:24]
WHSEC = "whsec" + "_" + A36[:24]
GITLAB = "glpat" + "-" + A36[:20]
NPM = "npm" + "_" + A36[:36]
HF = "hf" + "_" + A36[:34]
GOOGLE_OAUTH = "ya29" + "." + A36[:30]
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u"
BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"


class ProviderTokenTests(unittest.TestCase):
    """Each fixed-prefix format is blanked wherever it appears."""

    def test_bare_tokens_are_redacted(self) -> None:
        for token in (
            SLACK,
            GOOGLE,
            STRIPE,
            WHSEC,
            GITLAB,
            NPM,
            HF,
            GOOGLE_OAUTH,
            JWT,
        ):
            with self.subTest(token=token[:8]):
                self.assertEqual(
                    redact(f"saw {token} here"), (f"saw {R} here", True)
                )

    def test_ordinary_look_alikes_are_untouched(self) -> None:
        for text in (
            "task-1 and sk_test",
            "eyJ alone",
            "eyJhbGciOiJIUzI1NiJ9 header only",
            "version 1.2.3 and v1.20.300.4",
            "xoxb-short",
            "hf_model and npm_config",
            "AIza-too-short",
            "the whsec_ prefix",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), (text, False))


class BoundaryTests(unittest.TestCase):
    """A prefix inside a longer word or identifier is not a token."""

    def test_prefixes_inside_identifiers_are_untouched(self) -> None:
        for text in (
            "disk" + "_live_" + A36[:24],
            "task" + "_test_" + A36[:24],
            "prefi" + "xoxb-1234567890-abcdefghij",
            "task-abcdefghijklmnopqrstuvwx",
            "highp_abcdefghijklmnopqrstuvwx",
            "mynpm" + "_" + A36[:36],
        ):
            with self.subTest(text=text[:12]):
                self.assertEqual(redact(text), (text, False))

    def test_real_tokens_after_punctuation_are_still_redacted(self) -> None:
        for token in (
            STRIPE,
            SLACK,
            NPM,
            "sk-" + A36 + "xyz",
            "AKIA" + "B" * 16,
        ):
            for pre, post in (("(", ")"), ('"', '"'), (" ", "."), ("_", "")):
                with self.subTest(token=token[:6], pre=pre):
                    got, changed = redact(f"x{pre}{token}{post}y")
                    self.assertTrue(changed)
                    self.assertNotIn(token, got)

    def test_tokens_after_a_json_escape_are_redacted(self) -> None:
        # Tool-call bodies are json.dumps text: a newline is backslash + "n".
        for token in (
            "ghp_" + A36[:30],
            "sk-" + A36,
            "AKIA" + "B" * 16,
            STRIPE,
        ):
            for esc in ("\\n", "\\t", "\\r"):
                with self.subTest(token=token[:6], esc=esc):
                    text = '{"content": "line' + esc + token + '"}'
                    got, changed = redact(text)
                    self.assertTrue(changed)
                    self.assertNotIn(token, got)


class KeyAndHeaderTests(unittest.TestCase):
    """Key blocks, AWS secret keys and Basic credentials."""

    def test_pgp_private_key_block_is_redacted(self) -> None:
        block = (
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
            f"{BODY}\n-----END PGP PRIVATE KEY BLOCK-----"
        )
        self.assertEqual(redact(f"k:\n{block}\nok"), (f"k:\n{R}\nok", True))

    def test_pgp_public_key_block_is_kept(self) -> None:
        text = f"-----BEGIN PGP PUBLIC KEY BLOCK-----\n{BODY}\n-----END"
        self.assertEqual(redact(text), (text, False))

    def test_aws_secret_access_key(self) -> None:
        got, changed = redact("AWS_SECRET_ACCESS_KEY=abc/def+ghi next")
        self.assertEqual(got, f"AWS_SECRET_ACCESS_KEY={R} next")
        self.assertTrue(changed)

    def test_basic_authorization_loses_the_credential(self) -> None:
        got, _ = redact("Authorization: Basic dXNlcjpwYXNzd29yZA==")
        self.assertEqual(got, f"Authorization: {R}")


class BasicProseTests(unittest.TestCase):
    """The word Basic before an ordinary word is prose, not a credential."""

    def test_prose_words_after_basic_are_kept(self) -> None:
        for text in (
            "authorization: basic configuration",
            "Authorization: Basic Configuration is described below",
            "auth = basic authentication",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), (text, False))

    def test_base64_looking_credentials_are_redacted(self) -> None:
        for cred in (
            "dXNlcjpwYXNzd29yZA==",
            "dXNlcjpwYXNz",
            "QWxhZGRpbjoxMjM0",
        ):
            with self.subTest(cred=cred):
                self.assertEqual(
                    redact(f"Authorization: Basic {cred} tail"),
                    (f"Authorization: {R} tail", True),
                )

    def test_a_bare_basic_or_another_key_is_still_redacted(self) -> None:
        self.assertEqual(
            redact("authorization: basic"), (f"authorization: {R}", True)
        )
        got, changed = redact("password: basic configuration")
        self.assertEqual(
            (got, changed), (f"password: {R} configuration", True)
        )

    def test_other_auth_values_are_redacted(self) -> None:
        self.assertEqual(redact("auth=abc123def456"), (f"auth={R}", True))


class EscapedJsonTests(unittest.TestCase):
    """Secrets in JSON that is itself inside a JSON string."""

    def test_escaped_quotes_are_covered(self) -> None:
        text = r'{"out": "{\"password\": \"hunter2-x\", \"n\": 1}"}'
        got, changed = redact(text)
        self.assertTrue(changed)
        self.assertNotIn("hunter2", got)
        self.assertIn(R, got)
        self.assertIn(r"\"n\": 1", got)  # the neighbouring field survives

    def test_plain_json_with_a_backslash_in_the_value(self) -> None:
        got, _ = redact(r'{"secret": "a\nb\\c"} tail')
        self.assertEqual(got, f'{{"secret": "{R}"}} tail')

    def test_escaped_key_with_a_non_secret_name_is_kept(self) -> None:
        text = r"{\"name\": \"alice\"}"
        self.assertEqual(redact(text), (text, False))


class HostileInputTests(unittest.TestCase):
    """Pathological runs stay fast: no pattern backtracks badly."""

    def test_long_runs_finish_quickly(self) -> None:
        n = 40_000
        for text in (
            "eyJ." * n,
            "xox" * n,
            "\\" * n,
            "AIza" * n,
            '"password": "' + "a" * n,
            '\\"token\\": \\"' + "\\" * n,
            "Authorization: Basic " + "A" * n,
            "-----BEGIN PGP PRIVATE KEY BLOCK-----" * 200,
        ):
            with self.subTest(text=text[:16]):
                began = time.monotonic()
                redact(text)
                self.assertLess(time.monotonic() - began, 2.0)


if __name__ == "__main__":
    unittest.main()
