import pytest

from app.services.crypto_engine import AesGcmPiiVault, key_from_hex


def _vault() -> AesGcmPiiVault:
    key = key_from_hex("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    return AesGcmPiiVault(key)


def test_key_from_hex_rejects_wrong_length():
    with pytest.raises(ValueError):
        AesGcmPiiVault(key_from_hex("abcd"))


def test_encrypt_decrypt_roundtrip():
    vault = _vault()
    blob = vault.encrypt("Sujith Perera", reference_id="ref-123")
    assert vault.decrypt(blob, reference_id="ref-123") == "Sujith Perera"


def test_decrypt_fails_with_wrong_reference_id():
    """AAD binds the ciphertext to its reference_id — decrypting under a
    different one must fail closed, not silently return garbage."""
    vault = _vault()
    blob = vault.encrypt("Sujith Perera", reference_id="ref-123")
    with pytest.raises(Exception):
        vault.decrypt(blob, reference_id="ref-456")


def test_decrypt_rejects_truncated_blob():
    vault = _vault()
    with pytest.raises(ValueError):
        vault.decrypt(b"short", reference_id="ref-123")
