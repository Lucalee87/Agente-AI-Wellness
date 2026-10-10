import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DATABASE_PATH": os.path.join(_tmp, "test.db"),
    "DISABLE_DIALER": "1",
    "VOICE_PROVIDER": "dry_run",
    "VAPI_SERVER_SECRET": "vapi-secret",
    "META_VERIFY_TOKEN": "meta-verify",
    "META_APP_SECRET": "meta-secret",
    "GOOGLE_LEAD_KEY": "google-key",
    "GENERIC_WEBHOOK_TOKEN": "generic-token",
    "ADMIN_PASSWORD": "admin-pass",
    "NOTIFY_WEBHOOK_URL": "",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.connect(str(tmp_path / "test.db"))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
