# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum


class DocumentSource(str, enum.Enum):
    """Provenance of a Phase-A artifact row.

    GENERATED — produced by the existing generation flow.
    UPLOADED  — supplied by the user as a file, substituting the generated
                document for all downstream contextual use.
    """
    GENERATED = "generated"
    UPLOADED = "uploaded"
