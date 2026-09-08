"""RFC-6238 TOTP in the standard library — no pyotp on the host required.

Used by `demo_login.sh` to complete the demo operator's MFA enrolment against
a locally running stack. Deliberately dependency-free so the helper works on a
bare machine.
"""
import base64
import hashlib
import hmac
import struct
import sys
import time


def totp(secret: str, *, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret.strip().replace(" ", "").upper() + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(time.time()) // step)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


if __name__ == "__main__":
    print(totp(sys.argv[1]))
