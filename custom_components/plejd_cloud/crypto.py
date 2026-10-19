"""Plejd BLE mesh crypto.

Reverse-engineered from the Plejd Android app (`Plejd.Shared` `BleCrypto`). The
mesh payload cipher is an AES-128-ECB keystream XOR; login uses a SHA-256
challenge/response; device commissioning uses a 64-bit Diffie-Hellman key exchange.
See docs/reverse_engineering.md.
"""

from __future__ import annotations

import hashlib
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ADDRESS_LEN = 6
KEY_LEN = 16

# DH parameters from BleCrypto.cs in Plejd.Shared (DhCommonBase=2, DhCommonMod=15734018190158744081).
_DH_MOD: int = 15734018190158744081
_DH_BASE: int = 2

# Unprovisioned devices advertise using this well-known default mesh crypto key.
DEFAULT_MESH_KEY: bytes = bytes.fromhex("00112233445566778899aabbccddeeff")


def keystream_block(address: bytes, key: bytes) -> bytes:
    """AES-128-ECB over (address ++ address ++ address[:4]); the 16-byte keystream."""
    if len(address) != ADDRESS_LEN:
        raise ValueError(f"address must be {ADDRESS_LEN} bytes, got {len(address)}")
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes, got {len(key)}")
    buf = address + address + address[:4]
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(buf) + encryptor.finalize()


def encrypt_decrypt(address: bytes, data: bytes, key: bytes) -> bytes:
    """Encrypt or decrypt a Datavector payload (the operation is symmetric).

    `address` is the device's BLE MAC reversed (6 bytes); `key` is the 16-byte site
    crypto key. out[i] = data[i] XOR keystream[i % 16]. Rejects malformed inputs so
    hostile or corrupt BLE data fails predictably.
    """
    ks = keystream_block(address, key)
    return bytes(b ^ ks[i % 16] for i, b in enumerate(data))


def auth_response(challenge: bytes, key: bytes) -> bytes:
    """Login response written to the AuthKey characteristic: fold(SHA256(challenge XOR key)).

    Matches the device: the first 16 bytes of an (untrusted) challenge are used; a
    shorter challenge is rejected rather than raising deep in the hash.
    """
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes, got {len(key)}")
    if len(challenge) < KEY_LEN:
        raise ValueError(f"challenge must be at least {KEY_LEN} bytes, got {len(challenge)}")
    xored = bytes(challenge[i] ^ key[i] for i in range(KEY_LEN))
    digest = hashlib.sha256(xored).digest()
    return bytes(digest[i] ^ digest[i + KEY_LEN] for i in range(KEY_LEN))


def _dh_power_mod(base: int, exp: int, mod: int) -> int:
    """64-bit modular exponentiation matching BleCrypto.PowerModulo."""
    result = 1
    base = base % mod
    while exp > 0:
        if exp & 1:
            result = _dh_mul_mod(result, base, mod)
        base = _dh_mul_mod(base, base, mod)
        exp >>= 1
    return result


def _dh_mul_mod(a: int, b: int, mod: int) -> int:
    """64-bit multiply-modulo, matching C# MultiplyModulo's result."""
    a = a % mod
    b = b % mod
    # Python ints never overflow, so no masking is needed: reducing both operands
    # mod `mod` first keeps the product mathematically equivalent to C#'s
    # (overflow-prone) 64-bit multiply-then-modulo, since mod itself fits in 64 bits.
    return (a * b) % mod


def dh_generate_keypair() -> tuple[int, int]:
    """Generate a (private_key, public_key) pair for the commissioning DH exchange.

    Returns two uint64 values matching the app's GenerateDiffieHellmanKeyPair:
    privateKey is a random 64-bit odd integer; publicKey = pow(2, privateKey, DhMod).
    The app picks a random prime; any non-zero value works for DH security here.
    """
    # Use a random 63-bit odd integer (avoids trivial key = 0 or 1).
    raw = int.from_bytes(os.urandom(8), "little") | 1  # ensure odd
    private_key = raw % (_DH_MOD - 2) + 2  # keep in [2, DhMod-1]
    public_key = _dh_power_mod(_DH_BASE, private_key, _DH_MOD)
    return private_key, public_key


def dh_shared_secret(private_key: int, remote_public_key: int) -> int:
    """Compute the DH shared secret = pow(remote_public_key, private_key, DhMod)."""
    return _dh_power_mod(remote_public_key, private_key, _DH_MOD)


def dh_generate_shared_key(
    shared_secret: int, remote_public_key: int, local_public_key: int, device_id: bytes
) -> bytes:
    """BleCrypto.GenerateSharedKey: the key SetCryptoKey actually encrypts with.

    SHA256(secret ++ remotePub ++ localPub ++ reverse(deviceId))[:16], where the
    three uint64s are little-endian 8-byte encodings and device_id is the BLE MAC
    in its normal (forward) byte order - this function reverses it internally.
    """
    data = (
        struct.pack("<Q", shared_secret & 0xFFFFFFFFFFFFFFFF)
        + struct.pack("<Q", remote_public_key & 0xFFFFFFFFFFFFFFFF)
        + struct.pack("<Q", local_public_key & 0xFFFFFFFFFFFFFFFF)
        + bytes(reversed(device_id))
    )
    return hashlib.sha256(data).digest()[:KEY_LEN]


def dh_encrypt_site_key(site_key: bytes, shared_key: bytes) -> bytes:
    """Encrypt the site crypto key for SetCryptoKey: XOR against the derived shared key.

    Both site_key and shared_key (from dh_generate_shared_key) must be KEY_LEN (16) bytes.
    """
    if len(site_key) != KEY_LEN:
        raise ValueError(f"site_key must be {KEY_LEN} bytes, got {len(site_key)}")
    if len(shared_key) != KEY_LEN:
        raise ValueError(f"shared_key must be {KEY_LEN} bytes, got {len(shared_key)}")
    return bytes(a ^ b for a, b in zip(site_key, shared_key))
