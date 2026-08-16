"""Shared test fixtures.

Most notably: the admin surfaces are role-gated (§17), so integration tests
that exercise /review, /ops or alert management have to hold a session. The
`sign_in` fixture creates a throwaway account of the requested role and logs
the given TestClient in as them.
"""

import os
import uuid

import pytest

DSN = os.environ.get("TEST_DATABASE_URL")

TEST_PASSWORD = "integration-test-password"


@pytest.fixture()
def sign_in():
    """Factory: `sign_in(client, "analyst")` -> the account's email address.

    Accounts are created directly in the database (there is deliberately no
    public sign-up endpoint) and removed afterwards.
    """
    import psycopg

    from tenderza.auth.passwords import hash_password

    created: list[str] = []

    def _sign_in(client, role: str = "admin", email: str | None = None) -> str:
        address = email or f"{role}-{uuid.uuid4().hex[:8]}@tests.example.com"
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (email, name, password_hash, role)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        role = EXCLUDED.role
                RETURNING id
                """,
                (address, f"test {role}", hash_password(TEST_PASSWORD), role),
            )
            created.append(str(cur.fetchone()[0]))
            conn.commit()

        resp = client.post("/auth/login",
                           json={"email": address, "password": TEST_PASSWORD})
        assert resp.status_code == 200, resp.text
        return address

    yield _sign_in

    # Best-effort cleanup. A test account that reviewed a queue item or owns an
    # alert is referenced by foreign keys that exist precisely so attribution
    # survives (§9) -- deleting the user would be deleting evidence. Those get
    # disabled instead, which also kills any session they left behind.
    for user_id in created:
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            try:
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                conn.commit()
            except psycopg.errors.ForeignKeyViolation:
                conn.rollback()
                cur.execute(
                    "UPDATE users SET disabled_at = now() WHERE id = %s",
                    (user_id,),
                )
                conn.commit()
