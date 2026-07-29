import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from app.config import get_settings


class SessionOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "locks.db")
        get_settings.cache_clear()
        from app.database import get_connection, initialize_database, utc_now

        initialize_database()
        now = utc_now()
        with get_connection() as connection:
            self.session_ids = [
                connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted,
                        status, created_at, updated_at
                    ) VALUES (?, '+90 ***', 'enc', 'session', 'active', ?, ?)
                    """,
                    (f"Session {number}", now, now),
                ).lastrowid
                for number in (1, 2)
            ]

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def test_same_session_waits_and_lock_state_is_visible(self):
        from app.session_operation import (
            acquire_session_operation,
            get_session_operation,
        )

        async def scenario():
            first = await acquire_session_operation(
                self.session_ids[0], "invite_job", "job:1", "JOB-1 üye ekleme"
            )
            waiting = asyncio.create_task(
                acquire_session_operation(
                    self.session_ids[0],
                    "activity_scan",
                    "scan:2",
                    "Tarama #2",
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
            self.assertEqual(get_session_operation(self.session_ids[0])["operation_key"], "job:1")

            await first.release()
            second = await asyncio.wait_for(waiting, timeout=1)
            self.assertEqual(get_session_operation(self.session_ids[0])["operation_key"], "scan:2")
            await second.release()
            self.assertIsNone(get_session_operation(self.session_ids[0]))

        asyncio.run(scenario())

    def test_different_sessions_can_hold_independent_locks(self):
        from app.session_operation import (
            acquire_session_operation,
            get_session_operation,
        )

        async def scenario():
            first, second = await asyncio.gather(
                acquire_session_operation(
                    self.session_ids[0], "invite_job", "job:1", "JOB-1"
                ),
                acquire_session_operation(
                    self.session_ids[1], "group_access", "group:1", "Grup hazırlama"
                ),
            )
            self.assertIsNotNone(get_session_operation(self.session_ids[0]))
            self.assertIsNotNone(get_session_operation(self.session_ids[1]))
            await first.release()
            await second.release()

        asyncio.run(scenario())

    def test_cancelled_waiter_does_not_poison_the_session_lock(self):
        from app.session_operation import acquire_session_operation

        async def scenario():
            first = await acquire_session_operation(
                self.session_ids[0], "invite_job", "job:1", "JOB-1"
            )
            waiting = asyncio.create_task(
                acquire_session_operation(
                    self.session_ids[0], "activity_scan", "scan:1", "Tarama"
                )
            )
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            await first.release()
            third = await asyncio.wait_for(
                acquire_session_operation(
                    self.session_ids[0], "group_list", "groups:list", "Gruplar"
                ),
                timeout=1,
            )
            await third.release()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
