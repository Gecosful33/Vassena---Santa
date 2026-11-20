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
    check_passphrases,
    decrypt_json,
    derive_key,
    encrypt_json,
    generate_passphrase,
    normalize_passphrase,
    open_envelope,
    site_salt,
)

FAMILY = "famiglia-di-prova-2026"
OWNER = "proprieta-di-prova-2026"
FAST = 100_000  # the smallest count open_envelope accepts; keeps the suite quick


def envelope(**overrides):
    values = dict(
        family_passphrase=FAMILY,
        owner_passphrase=OWNER,
        salt=site_salt("test-site"),
        family_payload={"title": "Calendario", "properties": [{"label": "Santa"}]},
        owner_payload={"properties": [{"id": "santa", "kpis": [{"net_cents": 658404}]}]},
        iterations=FAST,
    )
    values.update(overrides)
    return build_envelope(**values)


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


def test_the_family_passphrase_opens_only_the_calendar():
    family, owner = open_envelope(envelope(), family_passphrase=FAMILY)
    assert family["properties"] == [{"label": "Santa"}]
    assert owner is None
    # ... and it is useless against the owner blob.
    family, owner = open_envelope(envelope(), owner_passphrase=FAMILY)
    assert (family, owner) == (None, None)


def test_the_owner_passphrase_opens_both_tiers():
    family, owner = open_envelope(envelope(), owner_passphrase=OWNER)
    assert family["title"] == "Calendario"
    assert owner["properties"][0]["kpis"] == [{"net_cents": 658404}]


def test_wrong_passphrases_open_nothing():
    assert open_envelope(envelope(), family_passphrase="x" * 20, owner_passphrase="y" * 20) == (None, None)
    assert open_envelope(envelope()) == (None, None)


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
            open_envelope(broken, family_passphrase=FAMILY)


def test_weak_passphrases_are_rejected():
    strong_a, strong_b = "nebbia-quercia-sentiero-42", "lanterna-golfo-ardesia-17"
    check_passphrases(strong_a, strong_b, forbidden=["utente", "Segreta1!"])
    with pytest.raises(PassphraseError, match="FAMILY_PASSPHRASE"):
        check_passphrases("corta", strong_b)
    with pytest.raises(PassphraseError, match="OWNER_PASSPHRASE"):
        check_passphrases(strong_a, "aaaaaaaaaaaaaaaaaaaa")
    with pytest.raises(PassphraseError, match="diverse"):
        check_passphrases(strong_a, " " + strong_a + " ")
    with pytest.raises(PassphraseError, match="credenziali"):
        check_passphrases("la-mia-Segreta1!-di-casa", strong_b, forbidden=["utente", "Segreta1!"])
    with pytest.raises(PassphraseError):
        check_passphrases("", strong_b)


def test_error_messages_never_echo_the_passphrase():
    with pytest.raises(PassphraseError) as caught:
        check_passphrases("segretissima", "lanterna-golfo-ardesia-17")
    assert "segretissima" not in str(caught.value)


def test_generated_passphrases_pass_the_policy_and_differ():
    first, second = generate_passphrase(), generate_passphrase()
    assert first != second
    check_passphrases(first, second)
    assert len(first.replace("-", "")) == 20
