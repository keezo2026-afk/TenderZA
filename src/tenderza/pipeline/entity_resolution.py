"""Organisation entity resolution (Blueprint §7).

"eThekwini Municipality" / "eThekwini Metropolitan Municipality" /
"City of eThekwini" must collapse into one organisation. This module
provides the alias normalizer and an in-memory resolver backed by the
organisation_aliases table's contents (loaded by the caller).
"""

from __future__ import annotations

import re

# Generic org-name noise words removed BEFORE matching, as WHOLE WORDS only
# (a bare .replace() would eat the "the" inside "eThekwini"). Order matters:
# multi-word phrases first.
_NOISE_PHRASES = [
    "metropolitan municipality",
    "metro municipality",
    "local municipality",
    "district municipality",
    "municipality",
    "city of",
    "province of",
    "provincial government",
    "government of",
    "the",
]

_NOISE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _NOISE_PHRASES) + r")\b"
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_org_name(name: str | None) -> str:
    """Canonical alias key for an organisation name.

    >>> normalize_org_name("City of eThekwini")
    'ethekwini'
    >>> normalize_org_name("eThekwini Metropolitan Municipality")
    'ethekwini'
    """
    if not name:
        return ""
    value = _NOISE_RE.sub(" ", name.casefold())
    return _NON_ALNUM.sub("", value)


class OrgResolver:
    """Resolves free-text buyer names to organisation ids via aliases.

    Aliases are exact-match on the normalized key; unresolved names are
    collected so the admin UI can turn them into new aliases or new orgs
    (the reconciliation step of §7).
    """

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        # normalized alias -> org_id
        self._aliases: dict[str, str] = dict(aliases or {})
        self.unresolved: list[str] = []

    def add_alias(self, alias: str, org_id: str) -> None:
        key = normalize_org_name(alias)
        if key:
            self._aliases[key] = org_id

    def resolve(self, buyer_name: str | None) -> str | None:
        key = normalize_org_name(buyer_name)
        if not key:
            return None
        org_id = self._aliases.get(key)
        if org_id is None and buyer_name:
            self.unresolved.append(buyer_name)
        return org_id
