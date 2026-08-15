import os
import tempfile
import unittest
from pathlib import Path

from src.infrastructure import db as core_db


class InitDbMultiUserSchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # init_db() must create the file from scratch
        self._orig_db_path = core_db.DB_PATH
        core_db.DB_PATH = Path(self.db_path)

    def tearDown(self) -> None:
        core_db.DB_PATH = self._orig_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_creates_multi_user_tables_on_fresh_install(self) -> None:
        core_db.init_db()

        with core_db.get_connection() as conn:
            table_names = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertIn("engineers", table_names)
            self.assertIn("topic_catalog", table_names)
            self.assertIn("engineer_topic_progress", table_names)

            session_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            self.assertIn("engineer_id", session_columns)

    def test_init_db_is_idempotent_on_fresh_multi_user_schema(self) -> None:
        core_db.init_db()
        core_db.init_db()  # must not raise on second run

        with core_db.get_connection() as conn:
            table_names = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertIn("engineer_topic_progress", table_names)


if __name__ == "__main__":
    unittest.main()
