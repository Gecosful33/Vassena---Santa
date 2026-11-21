import base64
import hashlib
import json

import pytest
from cryptography.exceptions import InvalidTag

from casecal import crypto
from casecal.crypto import (
    AAD_FAMILY,
    AAD_OWNER,
    EnvelopeError,
    PassphraseError,
    build_envelope,
    check_passphrase,
    decrypt_json,
    derive_key,
    encrypt_json,
    generate_passphrase,
    normalize_passphrase,
    open_envelope,
    site_salt,
)

FAMILY = "famiglia-di-prova-2026"   # the page's one passphrase
OWNER = "proprieta-di-prova-2026"   # what used to open the figures, before the single access
FAST = 100_000  # the smallest count open_envelope accepts; keeps the suite quick
CALENDAR = {"title": "Calendario", "properties": [{"label": "Santa"}]}
FIGURES = {"properties": [{"id": "santa", "kpis": [{"net_cents": 658404}]}]}


def envelope(**overrides):
    values = dict(
        passphrase=FAMILY,
        salt=site_salt("test-site"),
        family_payload=CALENDAR,
        owner_payload=FIGURES,
        iterations=FAST,
    )
    values.update(overrides)
    return build_envelope(**values)


def two_passphrase_envelope():
    """A page as it was published when the figures had a passphrase of their own."""
    salt = site_salt("test-site")
    family_key, owner_key = derive_key(FAMILY, salt, FAST), derive_key(OWNER, salt, FAST)
    figures = {**FIGURES, "family_key": base64.b64encode(family_key).decode()}
    return {
        "v": 1,
        "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": FAST, "salt": base64.b64encode(salt).decode()},
        "family": encrypt_json(family_key, CALENDAR, aad=AAD_FAMILY),
        "owner": encrypt_json(owner_key, figures, aad=AAD_OWNER),
    }


def test_key_derivation_is_plain_pbkdf2_hmac_sha256():
    salt = site_salt("x")
    expected = hashlib.pbkdf2_hmac("sha256", b"correct horse", salt, FAST, 32)
    assert derive_key("correct horse", salt, FAST) == expected
    assert crypto.KDF_ITERATIONS >= 600_000


def test_passphrases_are_normalised_before_derivation():
    assert normalize_passphrase("  abc \n") == "abc"
    assert normalize_passphrase("202６") == "2026"  # full-width digit, as typed by an IME
    salt = site_salt("x")
    assert derive_key(" abc ", salt, FAST) == derive_key("abc", salt, FAST)
    assert derive_key("abc", salt, FAST) != derive_key("Abc", salt, FAST)


def test_the_salt_is_stable_per_site():
    assert site_salt("a/b") == site_salt("a/b")
    assert site_salt("a/b") != site_salt("a/c")
    assert len(site_salt("a/b")) == 16


def test_blob_round_trip_and_authentication():
    key = derive_key(FAMILY, site_salt("x"), FAST)
    blob = encrypt_json(key, {"città": "è già"}, aad=AAD_FAMILY)
    assert decrypt_json(key, blob, aad=AAD_FAMILY) == {"città": "è già"}
    with pytest.raises(InvalidTag):
        decrypt_json(derive_key(OWNER, site_salt("x"), FAST), blob, aad=AAD_FAMILY)
    with pytest.raises(InvalidTag):
        decrypt_json(key, blob, aad=AAD_OWNER)
    raw = bytearray(base64.b64decode(blob["ct"]))
    raw[5] ^= 1
    with pytest.raises(InvalidTag):
        decrypt_json(key, {"iv": blob["iv"], "ct": base64.b64encode(bytes(raw)).decode()}, aad=AAD_FAMILY)


def test_ciphertext_length_does_not_track_the_amount_of_data():
    key = derive_key(FAMILY, site_salt("x"), FAST)
    few = encrypt_json(key, {"stays": [["2026-08-01", "2026-08-07", "b"]] * 3}, aad=AAD_FAMILY)
    more = encrypt_json(key, {"stays": [["2026-08-01", "2026-08-07", "b"]] * 40}, aad=AAD_FAMILY)
    assert len(few["ct"]) == len(more["ct"])
    assert few["iv"] != more["iv"]


def test_every_encryption_uses_a_fresh_nonce():
    key = derive_key(FAMILY, site_salt("x"), FAST)
    first, second = (encrypt_json(key, {"a": 1}, aad=AAD_FAMILY) for _ in range(2))
    assert first["iv"] != second["iv"]
    assert first["ct"] != second["ct"]


def test_one_passphrase_opens_the_calendar_and_the_figures():
    family, owner = open_envelope(envelope(), passphrase=FAMILY)
    assert family == CALENDAR
    assert owner == FIGURES


def test_a_wrong_passphrase_opens_nothing():
    assert open_envelope(envelope(), passphrase="x" * 20) == (None, None)
    assert open_envelope(envelope(), passphrase="") == (None, None)
    # What once opened the figures alone is no key to a page of today.
    assert open_envelope(envelope(), passphrase=OWNER) == (None, None)


def test_the_two_blobs_cannot_be_swapped():
    good = envelope()
    swapped = {**good, "family": good["owner"], "owner": good["family"]}
    assert open_envelope(swapped, passphrase=FAMILY) == (None, None)


def test_a_page_published_with_two_passphrases_is_still_read():
    # The nightly job finds such a page online the first time it runs after the change.
    old = two_passphrase_envelope()
    family, owner = open_envelope(old, passphrase=FAMILY)
    assert family == CALENDAR
    assert owner is None  # the figures had their own passphrase
    family, owner = open_envelope(old, passphrase=FAMILY, legacy_owner_passphrase=OWNER)
    assert family == CALENDAR
    assert owner["properties"] == FIGURES["properties"]
    # The old second passphrase is never tried on the calendar.
    assert open_envelope(old, passphrase="x" * 20, legacy_owner_passphrase=OWNER)[0] is None


def test_the_envelope_holds_no_plaintext():
    text = json.dumps(envelope())
    for leaked in ("Santa", "Calendario", "658404", "kpis", FAMILY, OWNER):
        assert leaked not in text
    assert set(json.loads(text)) == {"v", "kdf", "family", "owner"}


def test_malformed_envelopes_are_refused():
    good = envelope()
    for broken in (
        {},
        {**good, "v": 2},
        {**good, "kdf": {**good["kdf"], "iterations": 10}},
        {**good, "kdf": {**good["kdf"], "iterations": 10**9}},
        {**good, "kdf": {**good["kdf"], "hash": "MD5"}},
        {**good, "kdf": {**good["kdf"], "salt": "not base64!"}},
        {k: v for k, v in good.items() if k != "owner"},
    ):
        with pytest.raises(EnvelopeError):
            open_envelope(broken, passphrase=FAMILY)


def test_weak_passphrases_are_rejected():
    strong = "nebbia-quercia-sentiero-42"
    check_passphrase(strong, forbidden=["utente", "Segreta1!"])
    with pytest.raises(PassphraseError, match="FAMILY_PASSPHRASE"):
        check_passphrase("corta")
    with pytest.raises(PassphraseError, match="ripetitiva"):
        check_passphrase("aaaaaaaaaaaaaaaaaaaa")
    with pytest.raises(PassphraseError, match="credenziali"):
        check_passphrase("la-mia-Segreta1!-di-casa", forbidden=["utente", "Segreta1!"])
    with pytest.raises(PassphraseError):
        check_passphrase("")


def test_error_messages_never_echo_the_passphrase():
    with pytest.raises(PassphraseError) as caught:
        check_passphrase("segretissima")
    assert "segretissima" not in str(caught.value)


def test_generated_passphrases_pass_the_policy_and_differ():
    first, second = generate_passphrase(), generate_passphrase()
    assert first != second
    check_passphrase(first)
    check_passphrase(second)
    assert len(first.replace("-", "")) == 20
