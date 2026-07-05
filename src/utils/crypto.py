"""AES-256-GCM encryption/decryption for sensitive data (Discord tokens, etc.)."""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_key() -> bytes:
    """Get encryption key from environment variable."""
    key_hex = os.environ.get('ENCRYPTION_KEY', '')
    if not key_hex:
        raise RuntimeError(
            "ENCRYPTION_KEY environment variable not set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise RuntimeError("ENCRYPTION_KEY must be 64 hex characters (32 bytes)")
    return key


def encrypt(plaintext: str) -> str:
    """Encrypt plaintext → base64(nonce + ciphertext + tag)."""
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    ct = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
    return base64.b64encode(nonce + ct).decode('ascii')


def decrypt(encrypted: str) -> str:
    """Decrypt base64(nonce + ciphertext + tag) → plaintext."""
    key = _get_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ct = raw[12:]
    return aesgcm.decrypt(nonce, ct, None).decode('utf-8')


def mask_token(token: str) -> str:
    """Mask a token for display: first 6 chars + *** + last 4 chars."""
    if not token or len(token) < 12:
        return '***'
    return f"{token[:6]}***{token[-4:]}"
