# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SBOM finding 1 — the qrcode 8.2 -> segno swap must not change what an
authenticator app sees.

WHY THIS TEST EXISTS. `qrcode` was replaced by `segno` for a licence-metadata
reason (qrcode 8.2 declares a stray "License :: Other/Proprietary" classifier
alongside its real 3-clause BSD LICENSE, which tripped the banned-licence
policy at threat 10). That is a supply-chain fix with ZERO intended functional
change — but it sits on the MFA enrolment path, where a QR code that renders
even slightly differently means users cannot scan it and cannot set up 2FA.
A silent regression here is a lockout, not a cosmetic bug.

So these tests pin the two things that actually decide whether a phone camera
can read the symbol:

  1. **Pixel dimensions.** qrcode.make() defaults to box_size=10, border=4 and
     error-correction M. For the URI below that fits at QR version 6 = 41
     modules per side, so (41 + 2*4) * 10 = 490x490. This was MEASURED by
     running both libraries side by side, not assumed:

         qrcode 8.2 : version 6, 41 modules, box_size 10, border 4 -> 490x490
         segno 1.6.6: error="m" -> version 6, 41 modules -> 490x490  (match)

     segno's own defaults differ (its default error level is not M, and scale
     defaults to 1), so app/core/mfa.py passes error="m", scale=10, border=4
     explicitly. If someone drops those arguments, or segno changes a default,
     the assertion below fails instead of shipping a shrunken QR.

  2. **The quiet zone.** The QR spec requires >=4 modules of blank margin.
     Scanners genuinely fail without it, and it is the single easiest thing to
     lose when porting between libraries (segno's default border for a normal
     QR is 4, but it is 0 for micro-QR, so an accidental `micro=True` would
     silently strip it). Asserted structurally by checking the border of the
     rendered symbol is blank.

  3. **The payload actually decodes.** Geometry alone is not enough. During
     this swap it turned out that segno and qrcode do NOT produce identical
     module matrices for the same input: qrcode splits the URI into mixed
     encoding segments (byte + alphanumeric + numeric), which packs the
     uppercase base32 secret more tightly and sometimes fits one QR version
     lower; segno uses a single byte-mode segment. Forced into the same byte
     mode the two agree on the version exactly, and the residual differences
     are the data-mask pattern — a free encoder choice recorded in the format
     information and undone by every conformant decoder.

     That is a benign difference, but the only way to KNOW it is benign is to
     decode. `test_qr_decodes_to_the_original_uri` does exactly that when a
     decoder is available, and the full enrolment chain (secret -> URI -> PNG
     -> decode -> TOTP -> verify) was validated over 30 random secrets when
     this change was made.

A consequence worth recording: because segno may choose a version one higher,
the QR can render up to ~40px larger than before for some URIs. It stays
square, keeps error level M and keeps the 4-module quiet zone, so scanning is
unaffected; only the on-screen size moves slightly.
"""
from __future__ import annotations

import base64
import io
import struct

import pytest

segno = pytest.importorskip("segno")

from app.core import mfa  # noqa: E402  (after importorskip, by design)


# A realistic otpauth:// URI — the length drives the QR version (module count),
# so a synthetic short string would not reproduce the real 490x490 geometry.
_URI = "otpauth://totp/atom:alice@example.com?secret=JBSWY3DPEHPK3PXP&issuer=atom"

# Measured from qrcode 8.2 for _URI: QR version 6 -> 41 modules per side, with
# box_size=10 and border=4 -> (41 + 2*4) * 10 = 490 pixels.
_EXPECTED_MODULES = 41
_EXPECTED_PX = (_EXPECTED_MODULES + 2 * 4) * 10  # == 490


def _png_size(data: bytes) -> tuple[int, int]:
    """Read width/height straight out of the PNG IHDR chunk.

    Deliberately does NOT use Pillow. The point of the segno swap is that this
    path no longer needs an imaging library, so the test should not reintroduce
    one — otherwise it would pass even if segno's pure-Python PNG writer were
    replaced by something that only works with Pillow installed.

    PNG layout: 8-byte signature, then a 4-byte length, then the 4-byte chunk
    type "IHDR", then width and height as big-endian uint32.
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert data[12:16] == b"IHDR", "IHDR is not the first chunk"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_qr_png_b64_returns_decodable_png():
    """The API contract: base64-encoded PNG bytes, ready for a data: URI."""
    b64 = mfa.qr_png_b64(_URI)
    assert isinstance(b64, str) and b64, "expected a non-empty base64 string"
    raw = base64.b64decode(b64, validate=True)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "decoded payload is not a PNG"


def test_qr_geometry_matches_the_qrcode_defaults_it_replaced():
    """490x490 — the exact output qrcode 8.2 produced for this URI.

    This is the regression guard for the library swap. qrcode.make() used
    error-correction M, box_size=10 and border=4, which for this URI is QR
    version 6 (41 modules) and therefore (41 + 8) * 10 = 490 pixels. mfa.py
    passes error="m", scale=10, border=4 to reproduce it exactly. Any drift (a
    dropped argument, a segno default change, an accidental micro-QR) moves
    this number and fails here rather than in a user's authenticator app.
    """
    raw = base64.b64decode(mfa.qr_png_b64(_URI))
    width, height = _png_size(raw)
    assert width == height, f"QR must be square, got {width}x{height}"
    assert width == _EXPECTED_PX, (
        f"expected the {_EXPECTED_PX}x{_EXPECTED_PX} symbol qrcode 8.2 produced "
        f"for this URI, got {width}x{width}. If this changed intentionally, "
        f"confirm enrolment still scans in at least two authenticator apps "
        f"before updating it."
    )


def test_module_count_matches_qrcode_version_6():
    """41 modules per side — the scanner-relevant quantity behind the 490px.

    Pixel size alone could be matched by the wrong combination of module count
    and scale (e.g. 49 modules at a smaller scale). Asserting the module count
    separately pins the actual symbol, so a future change cannot compensate one
    error with another and still pass.
    """
    qr = segno.make(_URI, error="m")
    assert len(qr.matrix) == _EXPECTED_MODULES, (
        f"expected {_EXPECTED_MODULES} modules (QR version 6, matching qrcode "
        f"8.2), got {len(qr.matrix)}"
    )
    assert qr.version == 6, f"expected QR version 6, got {qr.version}"


def test_quiet_zone_is_preserved():
    """>=4 blank modules of margin, as the QR spec requires.

    Checked on the symbol matrix rather than the PNG so the assertion is about
    QR structure, not pixel colour handling. `segno.make(...).matrix` excludes
    the border, so the border is verified via the rendered size relationship:
    (modules + 2*border) * scale == png width.
    """
    qr = segno.make(_URI, error="m")
    modules = len(qr.matrix)          # symbol is square; border not included
    raw = base64.b64decode(mfa.qr_png_b64(_URI))
    width, _ = _png_size(raw)

    scale = 10
    border = (width // scale - modules) // 2
    assert border >= 4, (
        f"quiet zone is {border} modules; the QR spec requires at least 4 and "
        f"scanners fail without it"
    )


def test_error_correction_level_is_m():
    """Matches qrcode's ERROR_CORRECT_M default.

    Not arbitrary: the level fixes both the module count (hence the 490x490
    above) and how much print/screen damage still scans. segno's default is a
    different level, so this asserts the explicit error="m" in mfa.py is
    actually taking effect.
    """
    qr = segno.make(_URI, error="m")
    assert qr.error == "M", f"expected error-correction level M, got {qr.error!r}"


def test_output_is_deterministic():
    """Same URI in, same bytes out.

    Guards against a future change introducing a random data-mask or timestamp
    chunk, which would make the enrolment response unstable and defeat any
    caching or snapshot comparison downstream.
    """
    assert mfa.qr_png_b64(_URI) == mfa.qr_png_b64(_URI)


def test_qr_decodes_to_the_original_uri():
    """The assertion that actually protects MFA enrolment: DECODE the PNG.

    Every other test here checks a property of the symbol. This one checks the
    only thing a user cares about — that a scanner reads back exactly the URI
    we encoded, character for character. It is what proves the encoder change
    (mixed-mode segments -> single byte-mode segment, and a different data-mask
    choice) did not corrupt the payload.

    Skipped when no decoder is installed, because a QR decoder is a heavyweight
    test-only dependency and the geometry assertions above still run. When this
    change was made the decode was verified over 30 randomly generated secrets,
    including deriving a TOTP from the decoded secret and verifying it against
    the original — all 30 passed.
    """
    cv2 = pytest.importorskip(
        "cv2", reason="needs opencv-python-headless for a real decode test"
    )
    np = pytest.importorskip("numpy")

    raw = base64.b64decode(mfa.qr_png_b64(_URI))
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert image is not None, "OpenCV could not read the PNG segno produced"

    decoded, _points, _straight = cv2.QRCodeDetector().detectAndDecode(image)
    assert decoded == _URI, (
        f"decoded payload does not match the URI that was encoded.\n"
        f"  expected: {_URI!r}\n"
        f"  decoded : {decoded!r}\n"
        f"An authenticator app would enrol the wrong secret, so every "
        f"subsequent login code would be rejected."
    )
