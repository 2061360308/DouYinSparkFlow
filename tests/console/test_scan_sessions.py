import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from spark_console.db import create_schema
from spark_console.crypto import CookieCipher
from spark_console.models import DouyinAccount, DouyinLoginSession, ScanStatus, User
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
        self.session = Session(self.engine, expire_on_commit=False)
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

    def test_owner_can_queue_one_normalized_browser_click_for_worker(self):
        scan = self._claim_and_publish()

        self.assertTrue(
            hasattr(self.service, "queue_click"),
            "scan service must expose browser interaction queueing",
        )
        self.service.queue_click(self.owner.id, scan.id, 0.25, 0.75)

        self.assertEqual(
            {"kind": "click", "x": 0.25, "y": 0.75},
            self.service.claim_interaction(scan.id),
        )
        self.assertIsNone(self.service.claim_interaction(scan.id))
        with self.assertRaises(NotFound):
            self.service.queue_click(self.other.id, scan.id, 0.5, 0.5)

    def test_browser_click_rejects_coordinates_outside_the_viewport(self):
        scan = self._claim_and_publish()

        self.assertTrue(
            hasattr(self.service, "queue_click"),
            "scan service must expose browser interaction queueing",
        )
        for x, y in ((-0.01, 0.5), (1.01, 0.5), (0.5, -0.01), (0.5, 1.01)):
            with self.subTest(x=x, y=y):
                with self.assertRaises(ValidationError):
                    self.service.queue_click(self.owner.id, scan.id, x, y)

    def test_verification_code_is_encrypted_until_worker_claims_and_clears_it(self):
        scan = self._claim_and_publish()
        cipher = CookieCipher(b"i" * 32)

        self.assertTrue(
            hasattr(self.service, "queue_text"),
            "scan service must expose encrypted browser text queueing",
        )
        queued = self.service.queue_text(
            self.owner.id, scan.id, "123456", cipher
        )

        self.assertNotIn(b"123456", queued.ciphertext)
        self.assertEqual(
            {"kind": "text", "text": "123456"},
            self.service.claim_interaction(scan.id, cipher),
        )
        self.assertIsNone(queued.ciphertext)
        self.assertIsNone(queued.nonce)

    def test_verification_code_accepts_only_four_to_eight_digits(self):
        scan = self._claim_and_publish()
        cipher = CookieCipher(b"i" * 32)

        self.assertTrue(
            hasattr(self.service, "queue_text"),
            "scan service must expose encrypted browser text queueing",
        )
        for value in ("", "123", "123456789", "12a456", "1234\n"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.service.queue_text(self.owner.id, scan.id, value, cipher)

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

    def test_stale_worker_cannot_publish_after_owner_cancels(self):
        scan = self.service.start(self.owner.id)
        self.session.commit()

        with Session(self.engine, expire_on_commit=False) as worker_session:
            worker = ScanSessionService(worker_session, now=self.clock)
            worker_scan = worker.claim_next()
            worker_session.commit()
            self.assertEqual(ScanStatus.LOADING_QR, worker_scan.status)

            self.service.cancel_owned(self.owner.id, scan.id)
            self.session.commit()

            with self.assertRaisesRegex(Conflict, "^transition_conflict$"):
                worker.publish_qr(scan.id, PNG_SIGNATURE + b"late")

        with Session(self.engine) as verification:
            persisted = verification.get(DouyinLoginSession, scan.id)
            self.assertEqual(ScanStatus.CANCELLED, persisted.status)
            self.assertIsNone(persisted.slot)
            self.assertIsNone(persisted.qr_png)

    def test_stale_worker_cannot_complete_after_session_fails(self):
        account = self._account()
        scan = self.service.start(self.owner.id)
        self.session.commit()

        with Session(self.engine, expire_on_commit=False) as worker_session:
            worker = ScanSessionService(worker_session, now=self.clock)
            worker.claim_next()
            worker_scan = worker.publish_qr(scan.id, PNG_SIGNATURE + b"fixture")
            worker_session.commit()
            self.assertEqual(ScanStatus.AWAITING_SCAN, worker_scan.status)

            self.service.fail(scan.id, "automation_failed")
            self.session.commit()

            with self.assertRaisesRegex(Conflict, "^transition_conflict$"):
                worker.complete(scan.id, account.id)

        with Session(self.engine) as verification:
            persisted = verification.get(DouyinLoginSession, scan.id)
            self.assertEqual(ScanStatus.FAILED, persisted.status)
            self.assertEqual("automation_failed", persisted.error_code)
            self.assertIsNone(persisted.account_id)

    def test_transition_requires_persisted_global_slot(self):
        scan = self.service.start(self.owner.id)
        self.service.claim_next()
        self.session.commit()

        with Session(self.engine) as concurrent:
            concurrent_scan = concurrent.get(DouyinLoginSession, scan.id)
            concurrent_scan.slot = None
            concurrent.commit()

        with self.assertRaisesRegex(Conflict, "^transition_conflict$"):
            self.service.publish_qr(scan.id, PNG_SIGNATURE + b"late")

        with Session(self.engine) as verification:
            persisted = verification.get(DouyinLoginSession, scan.id)
            self.assertEqual(ScanStatus.LOADING_QR, persisted.status)
            self.assertIsNone(persisted.slot)
            self.assertIsNone(persisted.qr_png)

    def test_claim_next_expires_queued_session_at_deadline(self):
        scan = self.service.start(self.owner.id)
        self.session.commit()
        self.clock.value += timedelta(minutes=5)

        self.assertIsNone(self.service.claim_next())

        with Session(self.engine) as verification:
            persisted = verification.get(DouyinLoginSession, scan.id)
            self.assertEqual(ScanStatus.EXPIRED, persisted.status)
            self.assertEqual("login_timeout", persisted.error_code)
            self.assertIsNone(persisted.slot)

    def test_every_active_transition_expires_session_at_deadline(self):
        cases = (
            ("publish", ScanStatus.LOADING_QR),
            ("confirm", ScanStatus.AWAITING_SCAN),
            ("complete", ScanStatus.AWAITING_SCAN),
            ("fail", ScanStatus.LOADING_QR),
            ("cancel", ScanStatus.QUEUED),
        )
        for case, expected_source in cases:
            with self.subTest(case=case):
                scan = self.service.start(self.owner.id)
                if case != "cancel":
                    self.service.claim_next()
                if case in {"confirm", "complete"}:
                    self.service.publish_qr(scan.id, PNG_SIGNATURE + b"fixture")
                self.session.commit()
                self.assertEqual(expected_source, scan.status)
                self.clock.value += timedelta(minutes=5)

                try:
                    if case == "publish":
                        result = self.service.publish_qr(
                            scan.id, PNG_SIGNATURE + b"late"
                        )
                    elif case == "confirm":
                        result = self.service.mark_confirming(scan.id)
                    elif case == "complete":
                        result = self.service.complete(scan.id, self._account().id)
                    elif case == "fail":
                        result = self.service.fail(scan.id, "automation_failed")
                    else:
                        result = self.service.cancel_owned(self.owner.id, scan.id)

                    self.assertEqual(ScanStatus.EXPIRED, result.status)
                finally:
                    self.service.expire_stale()

                self.session.commit()
                with Session(self.engine) as verification:
                    persisted = verification.get(DouyinLoginSession, scan.id)
                    self.assertEqual(ScanStatus.EXPIRED, persisted.status)
                    self.assertEqual("login_timeout", persisted.error_code)
                    self.assertIsNone(persisted.slot)
                    self.assertIsNone(persisted.qr_png)
                    self.assertIsNone(persisted.account_id)

    def test_public_status_sanitizes_unknown_persisted_error_code(self):
        scan = self.service.start(self.owner.id)
        scan.status = ScanStatus.FAILED
        scan.slot = None
        scan.error_code = "internal-driver-diagnostic"
        self.session.flush()

        public = self.service.public_status(scan, self.clock.value)

        self.assertEqual("automation_failed", public["error"])
        self.assertEqual("自动化登录失败，请重试", public["message"])
        self.assertNotIn("internal-driver-diagnostic", repr(public))


if __name__ == "__main__":
    unittest.main()
