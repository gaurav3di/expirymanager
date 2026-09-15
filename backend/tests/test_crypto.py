"""The location-bound envelope, asserted where the location is actually assembled.

`test_field_crypto.py` proves the envelope keeps its promises when a test passes the table, the
column and the row id by hand. That is the right test for `security/crypto.py` and it would pass
unchanged if `SqliteTokenStore` encrypted under one row id and decrypted under another, because
nothing in it ever asks the store what it used.

So this file never names an AAD. It writes a real credential and a real token through the real
stores into a real migrated database, then moves the ciphertext bytes around inside that database
the way an attacker with file access would, and asserts that every relocation fails to read. The
properties are SECURITY.md section 13's, bound to the code that constructs them.

Every credential in this file is synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from expirymanager.brokers.fyers.tokens import (
    SqliteCredentialStore,
    SqliteTokenStore,
)
from expirymanager.security import crypto
from expirymanager.security.kek import KeyFileKekProvider, KeyringKekProvider
from expirymanager.security.keys import InMemoryCryptoKeyStore, KeyManager

# Synthetic throughout. Real credentials live outside the repository and are never read by
# application code or copied into it.
CREDENTIAL_ID = "cred-synthetic-1"
SYNTHETIC_APP_ID = "TESTAPP01-100"
SYNTHETIC_APP_SECRET = "synthetic-app-secret-not-a-real-value"
SYNTHETIC_ACCESS_TOKEN = "header.synthetic-payload.signature"
REDIRECT_URI = "http://127.0.0.1:8000/"


@pytest.fixture
def key_manager(tmp_path):
    manager = KeyManager(InMemoryCryptoKeyStore(), KeyFileKekProvider(tmp_path / "master.key"))
    manager.ensure_dek()
    return manager


class FakeKeyring:
    """An in-process stand in for the OS keyring, so no test touches the developer's own."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def _write_credential(engine, key_manager) -> None:
    now = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO broker_credential (credential_id, broker, label, app_id,"
                " app_secret_enc, redirect_uri, plan, key_ver, is_active, created_at, updated_at)"
                " VALUES (:cid, 'fyers', 'Test', :app_id, :secret, :redirect, 'standard',"
                " :key_ver, 1, :now, :now)"
            ),
            {
                "cid": CREDENTIAL_ID,
                "app_id": SYNTHETIC_APP_ID,
                "secret": key_manager.encrypt_field(
                    SYNTHETIC_APP_SECRET,
                    table="broker_credential",
                    column="app_secret_enc",
                    row_id=CREDENTIAL_ID,
                ),
                "redirect": REDIRECT_URI,
                "key_ver": key_manager.active_version,
                "now": now,
            },
        )


def _save_token(engine, key_manager, *, generation: int = 1):
    return SqliteTokenStore(engine, key_manager).save(
        credential_id=CREDENTIAL_ID,
        access_token=SYNTHETIC_ACCESS_TOKEN,
        refresh_token=None,
        generation=generation,
        access_expires_at=(datetime.now(UTC) + timedelta(hours=12)).isoformat(),
        refresh_expires_at=None,
    )


def _column(engine, sql: str, params: dict | None = None):
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).scalar_one()


class TestTheStoresRoundTripThroughTheRealTables:
    def test_a_credential_and_a_token_read_back_as_they_were_written(
        self, registry_engine, key_manager
    ):
        _write_credential(registry_engine, key_manager)
        record = _save_token(registry_engine, key_manager)

        credentials = SqliteCredentialStore(registry_engine, key_manager).load_active()
        assert credentials is not None
        assert credentials.app_secret == SYNTHETIC_APP_SECRET
        assert credentials.app_id == SYNTHETIC_APP_ID

        loaded = SqliteTokenStore(registry_engine, key_manager).load_active(CREDENTIAL_ID)
        assert loaded is not None
        assert loaded[1] == SYNTHETIC_ACCESS_TOKEN
        assert loaded[0].token_id == record.token_id

    def test_no_stored_column_holds_the_plaintext(self, registry_engine, key_manager):
        """The bytes on disk are asserted, not the accessor that reads them."""
        _write_credential(registry_engine, key_manager)
        _save_token(registry_engine, key_manager)

        secret_bytes = _column(
            registry_engine, "SELECT app_secret_enc FROM broker_credential"
        )
        token_bytes = _column(registry_engine, "SELECT access_token_enc FROM broker_token")
        assert SYNTHETIC_APP_SECRET.encode() not in bytes(secret_bytes)
        assert SYNTHETIC_ACCESS_TOKEN.encode() not in bytes(token_bytes)
        assert bytes(secret_bytes)[:3] == crypto.MAGIC
        assert bytes(token_bytes)[:3] == crypto.MAGIC
        # The fingerprint stored beside the token is a digest, not the token.
        fingerprint = _column(registry_engine, "SELECT token_fingerprint FROM broker_token")
        assert SYNTHETIC_ACCESS_TOKEN not in str(fingerprint)


class TestRelocatingACiphertextFails:
    def test_a_credential_secret_pasted_into_the_token_column_cannot_be_read(
        self, registry_engine, key_manager
    ):
        """The AAD binds a ciphertext to its table and column, so a swap is not readable.

        Done by moving the bytes inside the database rather than by calling decrypt with a
        different string, so what is under test is the AAD the store itself builds.
        """
        _write_credential(registry_engine, key_manager)
        _save_token(registry_engine, key_manager)

        secret_bytes = _column(
            registry_engine, "SELECT app_secret_enc FROM broker_credential"
        )
        with registry_engine.begin() as connection:
            connection.execute(
                text("UPDATE broker_token SET access_token_enc = :blob"),
                {"blob": secret_bytes},
            )

        with pytest.raises(crypto.DecryptionError):
            SqliteTokenStore(registry_engine, key_manager).load_active(CREDENTIAL_ID)

    def test_a_token_ciphertext_moved_to_another_token_row_cannot_be_read(
        self, registry_engine, key_manager
    ):
        """The row id is in the AAD, which is why broker_token has a text primary key."""
        _write_credential(registry_engine, key_manager)
        first = _save_token(registry_engine, key_manager, generation=1)
        original = _column(
            registry_engine,
            "SELECT access_token_enc FROM broker_token WHERE token_id = :t",
            {"t": first.token_id},
        )

        # A second login supersedes the first and writes a new row with its own id.
        second = _save_token(registry_engine, key_manager, generation=2)
        assert second.token_id != first.token_id

        with registry_engine.begin() as connection:
            connection.execute(
                text("UPDATE broker_token SET access_token_enc = :blob WHERE token_id = :t"),
                {"blob": original, "t": second.token_id},
            )

        with pytest.raises(crypto.DecryptionError):
            SqliteTokenStore(registry_engine, key_manager).load_active(CREDENTIAL_ID)

    def test_a_truncated_or_mislabelled_envelope_is_refused_before_any_crypto(
        self, registry_engine, key_manager
    ):
        _write_credential(registry_engine, key_manager)
        _save_token(registry_engine, key_manager)
        good = bytes(_column(registry_engine, "SELECT access_token_enc FROM broker_token"))

        for damaged in (good[: len(good) // 2], b"EM2" + good[3:], good[:2]):
            with registry_engine.begin() as connection:
                connection.execute(
                    text("UPDATE broker_token SET access_token_enc = :blob"),
                    {"blob": damaged},
                )
            with pytest.raises(crypto.CryptoError):
                SqliteTokenStore(registry_engine, key_manager).load_active(CREDENTIAL_ID)

        # The empty blob is the one case that is not damage. A logout destroys the ciphertext in
        # place rather than merely marking the row revoked, so an empty column means there is no
        # token, and reporting that as a crypto failure would send the user to a repair screen
        # instead of to the login button.
        with registry_engine.begin() as connection:
            connection.execute(text("UPDATE broker_token SET access_token_enc = X''"))
        assert SqliteTokenStore(registry_engine, key_manager).load_active(CREDENTIAL_ID) is None


class TestSwitchingTheKekProvider:
    def test_a_rewrap_leaves_every_stored_ciphertext_byte_identical(
        self, registry_engine, tmp_path, monkeypatch
    ):
        """Only the wrapping of the DEK changes. The field ciphertexts are not touched.

        That is what makes switching from a key file to the OS keyring a safe operation on a store
        holding years of encrypted credentials: nothing is re-encrypted, so nothing can be lost
        halfway through.
        """
        keyring = FakeKeyring()
        monkeypatch.setattr("expirymanager.security.kek.keyring", keyring, raising=False)

        manager = KeyManager(
            InMemoryCryptoKeyStore(), KeyFileKekProvider(tmp_path / "master.key")
        )
        manager.ensure_dek()
        _write_credential(registry_engine, manager)
        _save_token(registry_engine, manager)

        before_secret = bytes(
            _column(registry_engine, "SELECT app_secret_enc FROM broker_credential")
        )
        before_token = bytes(
            _column(registry_engine, "SELECT access_token_enc FROM broker_token")
        )

        new_provider = KeyringKekProvider()
        new_provider.provision()
        manager.rewrap(new_provider)

        after_secret = bytes(
            _column(registry_engine, "SELECT app_secret_enc FROM broker_credential")
        )
        after_token = bytes(
            _column(registry_engine, "SELECT access_token_enc FROM broker_token")
        )
        assert after_secret == before_secret
        assert after_token == before_token

        # And both still read, through the same stores, under the new provider.
        credentials = SqliteCredentialStore(registry_engine, manager).load_active()
        assert credentials is not None and credentials.app_secret == SYNTHETIC_APP_SECRET
        loaded = SqliteTokenStore(registry_engine, manager).load_active(CREDENTIAL_ID)
        assert loaded is not None and loaded[1] == SYNTHETIC_ACCESS_TOKEN
