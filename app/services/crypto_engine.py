import os
from typing import Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AesGcmPiiVault:
    """AES-256-GCM encrypt/decrypt for PII blobs bound to reference_id via AAD."""

    def __init__(self, master_key_32: bytes):
        if len(master_key_32) != 32:
            raise ValueError("PII master key must be 32 bytes (64 hex chars)")
        self._aes = AESGCM(master_key_32)

    def encrypt(self, plaintext: str, reference_id: str) -> bytes:
        nonce = os.urandom(12)
        aad = reference_id.encode("utf-8")
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return nonce + ct

    def decrypt(self, blob: bytes, reference_id: str) -> str:
        if len(blob) < 13:
            raise ValueError("Invalid ciphertext blob")
        nonce, ct = blob[:12], blob[12:]
        aad = reference_id.encode("utf-8")
        pt = self._aes.decrypt(nonce, ct, aad)
        return pt.decode("utf-8")


def key_from_hex(hex_str: str) -> bytes:
    h = hex_str.strip().replace(" ", "")
    return bytes.fromhex(h)
