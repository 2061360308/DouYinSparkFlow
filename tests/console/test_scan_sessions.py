import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from spark_console.db import create_schema
from spark_console.models import DouyinAccount, ScanStatus, User
from spark_console.services import Conflict, NotFound, ValidationError
from spark_console.services.scan_sessions import (
    MAX_QR_PNG_BYTES,
    PNG_SIGNATURE,
    ScanSessionService,
)


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ScanSessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        create_schema(self.engine)
        self.session = Session(self.engine)
        self.clock = MutableClock(datetime(2026, 8, 25, 4, 0, tzinfo=timezone.utc))
        self.service = ScanSessionService(self.session, now=self.clock)
        self.owner = User(username="owner", password_hash="hash", role="user")
        self.other = User(username="other", password_hash="hash", role="user")
        self.session.add_all([self.owner, self.other])
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _claim_and_publish(self):
        scan = self.service.start(self.owner.id)
        claimed = self.service.claim_next()
        self.assertEqual(scan.id, claimed.id)
        return self.service.publish_qr(scan.id, PNG_SIGNATURE + b"fixture")

    def _account(self) -> DouyinAccount:
        account = DouyinAccount(
            owner_user_id=self.owner.id,
            display_name="scan result",
            encrypted_cookies=b"ciphertext",
            cookie_nonce=b"n" * 12,
        )
        self.session.add(account)
        self.session.flush()
        return account

    def test_start_reserves_global_slot_for_exactly_five_minutes(self):
        scan = self.service.start(self.owner.id)

        self.assertEqual(self.owner.id, scan.owner_user_id)
        self.assertEqual("global", scan.slot)
        self.assertEqual(ScanStatus.QUEUED, scan.status)
        self.assertEqual(self.clock.value + timedelta(minutes=5), scan.expires_at)

    def test_second_start_reports_slot_busy_without_damaging_active_session(self):
        active = self.service.start(self.owner.id)

        with self.assertRaisesRegex(Conflict, "^slot_busy$"):
            self.service.start(self.other.id)

        retained = self.session.get(type(active), active.id)
        self.assertEqual("global", retained.slot)
        self.assertEqual(ScanStatus.QUEUED, retained.status)

    def test_owner_only_access_and_cancel_hide_session_from_other_users(self):
        scan = self.service.start(self.owner.id)

        self.assertEqual(scan.id, self.service.get_owned(self.owner.id, scan.id).id)
        with self.assertRaises(NotFound):
            self.service.get_owned(self.other.id, scan.id)
        with self.assertRaises(NotFound):
            self.service.cancel_owned(self.other.id, scan.id)

        self.assertEqual("global", scan.slot)
        self.assertEqual(ScanStatus.QUEUED, scan.status)

    def test_claim_publish_confirm_and_complete_follow_legal_transitions(self):
        awaiting = self._claim_and_publish()
        self.assertEqual(ScanStatus.AWAITING_SCAN, awaiting.status)
        self.assertTrue(awaiting.qr_png.startswith(PNG_SIGNATURE))

        confirming = self.service.mark_confirming(awaiting.id)
        self.assertEqual(ScanStatus.CONFIRMING, confirming.status)
        account = self._account()
        completed = self.service.complete(confirming.id, account.id)

        self.assertEqual(ScanStatus.SUCCEEDED, completed.status)
        self.assertEqual(account.id, completed.account_id)
        self.assertIsNone(completed.qr_png)
        self.assertIsNone(completed.slot)
        self.assertIsNone(completed.error_code)
        self.assertEqual(self.clock.value, completed.finished_at)

    def test_claim_next_returns_none_when_no_queued_session_exists(self):
        self.assertIsNone(self.service.claim_next())
        scan = self.service.start(self.owner.id)
        self.assertEqual(scan.id, self.service.claim_next().id)
        self.assertIsNone(self.service.claim_next())

    def test_invalid_transition_is_rejected_without_mutating_session(self):
        scan = self.service.start(self.owner.id)

        with self.assertRaisesRegex(Conflict, "^invalid_transition$"):
            self.service.mark_confirming(scan.id)

        self.assertEqual(ScanStatus.QUEUED, scan.status)
        self.assertEqual("global", scan.slot)

    def test_publish_qr_requires_png_signature_and_enforces_one_mib_limit(self):
        scan = self.service.start(self.owner.id)
        self.service.claim_next()

        for invalid in (
            b"not-a-png",
            PNG_SIGNATURE + b"x" * (MAX_QR_PNG_BYTES - len(PNG_SIGNATURE) + 1),
        ):
            with self.subTest(size=len(invalid)):
                with self.assertRaises(ValidationError):
                    self.service.publish_qr(scan.id, invalid)

        self.assertEqual(ScanStatus.LOADING_QR, scan.status)
        self.assertIsNone(scan.qr_png)

    def test_cancel_clears_qr_and_releases_global_slot(self):
        scan = self._claim_and_publish()

        cancelled = self.service.cancel_owned(self.owner.id, scan.id)

        self.assertEqual(ScanStatus.CANCELLED, cancelled.status)
        self.assertEqual("cancelled", cancelled.error_code)
        self.assertIsNone(cancelled.qr_png)
        self.assertIsNone(cancelled.slot)
        self.service.start(self.other.id)

    def test_failure_clears_qr_and_releases_global_slot(self):
        scan = self._claim_and_publish()

        failed = self.service.fail(scan.id, "qr_load_failed")

        self.assertEqual(ScanStatus.FAILED, failed.status)
        self.assertEqual("qr_load_failed", failed.error_code)
        self.assertIsNone(failed.qr_png)
        self.assertIsNone(failed.slot)
        self.service.start(self.other.id)

    def test_failure_rejects_non_public_error_codes(self):
        scan = self.service.start(self.owner.id)

        with self.assertRaises(ValidationError):
            self.service.fail(scan.id, "browser stack trace")

        self.assertEqual(ScanStatus.QUEUED, scan.status)
        self.assertIsNone(scan.error_code)

    def test_expire_stale_clears_qr_releases_slot_and_is_idempotent(self):
        scan = self._claim_and_publish()
        self.clock.value += timedelta(minutes=5)

        self.assertEqual(1, self.service.expire_stale())

        self.assertEqual(ScanStatus.EXPIRED, scan.status)
        self.assertEqual("login_timeout", scan.error_code)
        self.assertIsNone(scan.qr_png)
        self.assertIsNone(scan.slot)
        self.assertEqual(self.clock.value, scan.finished_at)
        self.assertEqual(0, self.service.expire_stale())
        self.service.start(self.other.id)

    def test_worker_startup_cleanup_expires_abandoned_queued_session(self):
        abandoned = self.service.start(self.owner.id)
        self.clock.value += timedelta(minutes=6)

        cleaned = ScanSessionService(self.session, now=self.clock).expire_stale()

        self.assertEqual(1, cleaned)
        self.assertEqual(ScanStatus.EXPIRED, abandoned.status)
        self.assertIsNone(abandoned.slot)

    def test_public_status_has_only_owner_safe_fields(self):
        scan = self._claim_and_publish()
        self.clock.value += timedelta(seconds=61)

        public = self.service.public_status(scan, self.clock.value)

        self.assertEqual(
            {"id", "status", "remaining_seconds", "error", "message", "account_id"},
            set(public),
        )
        self.assertEqual(scan.id, public["id"])
        self.assertEqual("awaiting_scan", public["status"])
        self.assertEqual(239, public["remaining_seconds"])
        self.assertIsNone(public["error"])
        self.assertIsNone(public["account_id"])
        self.assertIsInstance(public["message"], str)
        serialized = repr(public)
        self.assertNotIn("owner_user_id", serialized)
        self.assertNotIn("qr_png", serialized)
        self.assertNotIn("slot", serialized)


if __name__ == "__main__":
    unittest.main()
