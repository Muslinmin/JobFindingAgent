import pytest
import aiosqlite
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.database import get_db, create_tables


@pytest.fixture
async def async_client(tmp_path):
    db_path = str(tmp_path / "test.db")

    async with aiosqlite.connect(db_path) as conn:
        await create_tables(conn)

    async def override_get_db():
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
