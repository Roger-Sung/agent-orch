"""Typed rejections shared by the pack-v1 pure-function modules.

IDENTITIES I-7 distinguishes three failure shapes.  Only the third one - the
refusal to mint an identity at all - is raised as an exception here; the other
two are values carried by the callers' own records.
"""
from __future__ import annotations


class PackError(Exception):
    """Base for every pack-v1 rejection."""


class IdentityRefused(PackError):
    """An identity cannot be produced (IDENTITIES I-7 ``⊘``).

    ``code`` is the reject code named by the spec (for example
    ``unsupported_entry``); ``path`` is the repo-relative path that triggered
    it, when the code is about one path.  The sampling stage keeps only the
    first code of a category, so callers compare ``code`` and never the
    message text.
    """

    def __init__(self, code: str, path: bytes | None = None, detail: str | None = None) -> None:
        self.code = code
        self.path = path
        self.detail = detail
        shown = path.decode("utf-8", "backslashreplace") if path is not None else None
        parts = [code]
        if shown is not None:
            parts.append(shown)
        if detail:
            parts.append(detail)
        super().__init__(": ".join(parts))


class ContractRejected(PackError):
    """A contract cannot be assembled (for example ``invalid_exclude_pattern``)."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(": ".join(p for p in (code, detail) if p))


class EnvelopeInvalid(PackError):
    """An envelope fails a validation rule; ``code`` is the EV-*/EM-* id."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(": ".join(p for p in (code, detail) if p))
