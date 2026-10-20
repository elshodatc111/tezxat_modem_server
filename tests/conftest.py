import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp(prefix="smsmodem-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "secret123"
os.environ["SECRET_KEY"] = "test-secret"
# Serverdagi .env (demo modem, MySQL sozlamalari) testlarga aralashmasin
os.environ["CREATE_DEMO_MODEM"] = "0"
os.environ["DB_SKIP_LOCKED"] = "1"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    from app import db
    from app import models  # noqa: F401
    from app import worker

    db.Base.metadata.drop_all(db.engine)
    db.Base.metadata.create_all(db.engine)
    worker._MOCKS.clear()
    yield
