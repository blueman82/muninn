"""Typed field limits and boundaries: tags, expiry, the withheld counts."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from muninn import obs, obs_stats, store
from muninn.knowledge_model import RefusedError
from muninn.knowledge_push import withheld
from muninn.knowledge_typed import (
    TypedRequest,
    parse_valid_until,
    validate,
)
from muninn.obs_log import clean_record
from tests.knowledge_support import KnowCase, kid
from tests.test_store_migrate import make_v1

NOW = datetime(2030, 1, 1, tzinfo=UTC).timestamp()


def tags(*names: str) -> tuple[str, ...]:
    """Validate a tag list and return the stored tags."""
    return validate(TypedRequest(tags=names), NOW).tags


class TagTests(unittest.TestCase):
    """Tags are bounded, deduplicated and sorted."""

    def refused(self, *names: str) -> None:
        """Assert the tag list is refused with ``bad_tags``."""
        with self.assertRaises(RefusedError) as caught:
            tags(*names)
        self.assertEqual(caught.exception.code, "bad_tags")

    def test_ten_tags_pass_and_eleven_do_not(self) -> None:
        ten = tuple(f"t{i}" for i in range(10))
        self.assertEqual(len(tags(*ten)), 10)
        self.refused(*ten, "t10")

    def test_the_count_is_checked_before_duplicates_are_dropped(self) -> None:
        ten = tuple(f"t{i}" for i in range(10))
        self.refused(*ten, "t0")  # 11 given, 10 distinct

    def test_length_boundary_is_32_characters(self) -> None:
        self.assertEqual(tags("a" * 32), ("a" * 32,))
        self.refused("a" * 33)

    def test_empty_and_malformed_tags_are_refused(self) -> None:
        for bad in ("", "Upper", "has space", "dot.dot", "ünï"):
            with self.subTest(bad):
                self.refused(bad)

    def test_duplicates_collapse_and_the_result_is_sorted(self) -> None:
        self.assertEqual(tags("b", "a", "b", "a_1"), ("a", "a_1", "b"))


class ValidUntilTests(unittest.TestCase):
    """The expiry must be a parseable moment strictly after now."""

    def refused(self, value: str) -> None:
        """Assert the value is refused with ``bad_valid_until``."""
        with self.assertRaises(RefusedError) as caught:
            parse_valid_until(value, NOW)
        self.assertEqual(caught.exception.code, "bad_valid_until")

    def test_exactly_now_is_refused_and_a_second_later_is_not(self) -> None:
        self.refused("2030-01-01T00:00:00+00:00")
        self.assertEqual(
            parse_valid_until("2030-01-01T00:00:01+00:00", NOW), NOW + 1
        )

    def test_a_naive_datetime_is_utc_and_an_offset_is_honoured(self) -> None:
        naive = parse_valid_until("2030-01-02T03:00:00", NOW)
        utc = parse_valid_until("2030-01-02T03:00:00+00:00", NOW)
        east = parse_valid_until("2030-01-02T03:00:00+02:00", NOW)
        self.assertEqual(naive, utc)
        self.assertEqual(utc - east, 2 * 3600)

    def test_a_date_alone_means_midnight_utc(self) -> None:
        self.assertEqual(parse_valid_until("2030-01-02", NOW), NOW + 24 * 3600)
        self.refused("2029-12-31")

    def test_garbage_is_refused(self) -> None:
        for bad in ("", "tomorrow", "2030-13-45", "12/31/2030", "soon"):
            with self.subTest(bad):
                self.refused(bad)


class WithheldCountTests(KnowCase):
    """``withheld`` counts by reason, per scope, as numbers only."""

    def expire(self, number: int) -> None:
        """Move one entry's expiry into the past."""
        self.rw.execute(
            "UPDATE knowledge SET valid_until = ? WHERE id = ?",
            (time.time() - 5, number),
        )

    def test_a_restricted_expired_entry_counts_as_expired_only(self) -> None:
        both = kid(
            self.add(
                text="both", sensitivity="restricted", valid_until="2099-01-01"
            )
        )
        self.expire(both)
        self.add(text="restricted only", sensitivity="restricted")
        got = withheld(self.ro(), [self.repo])
        self.assertEqual(got, {"expired": 1, "restricted": 1})

    def test_only_current_entries_in_the_given_scopes_count(self) -> None:
        other = self.add_scope("/other")
        self.add(text="here", sensitivity="restricted")
        far = kid(self.add(text="far", sensitivity="restricted"))
        self.rw.execute(
            "UPDATE knowledge SET scope_id = ? WHERE id = ?", (other, far)
        )
        old = kid(self.add(text="old", valid_until="2099-01-01"))
        self.expire(old)
        self.rw.execute(
            "UPDATE knowledge SET status = 'superseded' WHERE id = ?", (old,)
        )
        ro = self.ro()
        self.assertEqual(
            withheld(ro, [self.repo]), {"expired": 0, "restricted": 1}
        )
        self.assertEqual(
            withheld(ro, [other]), {"expired": 0, "restricted": 1}
        )
        self.assertEqual(
            withheld(ro, [self.repo, other]), {"expired": 0, "restricted": 2}
        )

    def test_no_scope_ids_count_nothing(self) -> None:
        self.add(text="private", sensitivity="restricted")
        self.assertEqual(
            withheld(self.ro(), []), {"expired": 0, "restricted": 0}
        )

    def test_the_counts_pass_the_call_log_allowlist(self) -> None:
        self.add(text="private", sensitivity="restricted")
        counts = withheld(self.ro(), [self.repo])
        self.assertTrue(
            obs.log_call(
                self.home, {"cmd": "hook session-start", "withheld": counts}
            )
        )
        line = (self.home / "calls.jsonl").read_text()
        self.assertIn('"withheld":{"expired":0,"restricted":1}', line)

    def test_stats_counts_an_expired_restricted_entry_as_expired(self) -> None:
        both = kid(
            self.add(
                text="both", sensitivity="restricted", valid_until="2099-01-01"
            )
        )
        self.expire(both)
        stale = kid(
            self.add(text="superseded stale", valid_until="2099-01-01")
        )
        self.expire(stale)
        self.rw.execute(
            "UPDATE knowledge SET status = 'superseded' WHERE id = ?",
            (stale,),
        )
        out = obs_stats.stats(self.ro(), self.home, {})
        self.assertEqual(out["knowledge"], {"expired": 1, "superseded": 1})
        self.assertEqual(out["knowledge_restricted"], 0)


class DetailAllowlistTests(unittest.TestCase):
    """Every message a store refusal carries survives the log allowlist."""

    def test_known_store_messages_are_logged_whole(self) -> None:
        messages = (
            "schema v1, need v2",
            "schema v7, need v2",
            "journal_mode is 'wal', need 'delete'",
            "cannot read the store (SQLITE_BUSY)",
            "cannot read the store (OperationalError)",
            "no store",
        )
        for message in messages:
            with self.subTest(message):
                self.assertEqual(
                    clean_record({"detail": message}), {"detail": message}
                )
        self.assertEqual(
            clean_record({"detail": "x" * 81}), {}
        )  # longer than any message is dropped, not cut

    def test_real_refusals_from_the_store_are_logged_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            make_v1(store.db_path(home))
            garbage = Path(tmp) / "garbage.sqlite"
            garbage.write_bytes(b"not a database" * 100)
            for path in (store.db_path(home), garbage):
                with self.assertRaises(store.StoreUnavailableError) as caught:
                    store.connect_ro(path)
                message = str(caught.exception)[:80]
                with self.subTest(message):
                    self.assertEqual(
                        clean_record({"detail": message}), {"detail": message}
                    )
