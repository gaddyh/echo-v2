#!/usr/bin/env python3
"""Prove that media delivered in the batch envelope can be downloaded from
the WhatsApp CDN and decrypted with mediaKey (client-side E2E decryption).

Reads /tmp/audio_meta.json (audioMessage fields extracted from a delivered
batch). No WAHA involvement - exactly what Echo could do on its own.
"""
import base64
import hashlib
import hmac
import json
import subprocess
import urllib.request

meta = json.load(open("/tmp/audio_meta.json"))

# 1. download the ciphertext from the WhatsApp CDN (plain HTTPS GET)
req = urllib.request.Request(meta["URL"], headers={"User-Agent": "WhatsApp/2.0"})
with urllib.request.urlopen(req, timeout=30) as r:
    enc = r.read()
print("CDN download OK:", len(enc), "bytes (ciphertext + 10-byte MAC)")

# 2. derive keys: HKDF-SHA256(mediaKey, info="WhatsApp Audio Keys", 112)
media_key = base64.b64decode(meta["mediaKey"])
info = b"WhatsApp Audio Keys"


def hkdf_sha256(ikm, info, length, salt=b""):
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


keys = hkdf_sha256(media_key, info, 112)
iv, cipher_key, mac_key = keys[:16], keys[16:48], keys[48:80]

# 3. integrity: HMAC-SHA256 over ciphertext == trailing 10-byte truncated MAC
calc = hmac.new(mac_key, enc[:-10], hashlib.sha256).digest()[:10]
print("HMAC MAC check:", "PASS" if calc == enc[-10:] else "FAIL")

# 4. AES-256-CBC decrypt via openssl, truncate to fileLength (no PKCS7 trim)
open("/tmp/enc.bin", "wb").write(enc[:-10])
subprocess.run(
    ["openssl", "enc", "-d", "-aes-256-cbc", "-nopad",
     "-K", cipher_key.hex(), "-iv", iv.hex(),
     "-in", "/tmp/enc.bin", "-out", "/tmp/dec.bin"], check=True)
pt = open("/tmp/dec.bin", "rb").read()[:int(meta["fileLength"])]

# 5. verify the plaintext hash against fileSHA256 from the batch envelope
expected = base64.b64decode(meta["fileSHA256"])
ok = hashlib.sha256(pt).digest() == expected
print("plaintext SHA256 check:", "PASS" if ok else "FAIL")
open("/tmp/audio.oga", "wb").write(pt)
print("wrote /tmp/audio.oga:", len(pt), "bytes, header:", pt[:8])
