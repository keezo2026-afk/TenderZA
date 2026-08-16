"""Multilingual synonym expansion for tender search (Blueprint §12).

South African tender notices are published in English, Afrikaans and the
official African languages — often mixed inside one notice ("Konstruksie /
Construction of a clinic"). A supplier searching "construction" must find
the notice titled "bou van 'n kliniek".

Why query-side expansion instead of a PostgreSQL synonym dictionary
--------------------------------------------------------------------
A `synonym` or `thesaurus` dictionary is the textbook answer, but it needs a
file installed in the server's ``$SHAREDIR/tsearch_data`` and a custom text
search configuration. That is impossible on managed Postgres (RDS/Cloud SQL
do not expose the share dir), impossible under the embedded ``pgserver`` used
for dev/test, and it bakes the vocabulary into the *index* — so every
dictionary edit needs a full REINDEX of the tenders table.

Expanding at query time keeps the vocabulary in version control, lets us ship
a synonym fix without touching the database, and costs one extra OR-group per
matched term. If the term count ever becomes a ranking problem, the
documented scale-out path (§12) is OpenSearch with a managed synonym graph.

The expansion is deliberately *recall-oriented*: it only ever widens a query.
Precision is preserved by ranking — see `tenderza.search.ranking`.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
# Each group is a set of interchangeable terms across languages. Groups are
# expanded bidirectionally: matching ANY member expands to ALL members.
#
# Curation rules, learned the hard way:
#   * Only terms that are genuinely interchangeable *in a procurement notice*.
#     "bou" (build) and "construction" are; "diens" (service/duty) and
#     "service" drift far enough apart in general Afrikaans that we only keep
#     the procurement sense.
#   * No abbreviations that collide with tender numbers (e.g. "GT" is a
#     Gauteng plate prefix AND appears in bid numbers) — those cost precision
#     for almost no recall.
#   * isiZulu/isiXhosa share much vocabulary; where a term is common to both
#     it is listed once.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    # -- works / construction ------------------------------------------------
    ("construction", "konstruksie", "bou", "ukwakhiwa", "building", "gebou"),
    ("renovation", "opknapping", "restourasie", "refurbishment", "upgrade",
     "opgradering", "ukuvuselelwa"),
    ("maintenance", "instandhouding", "onderhoud", "ukulungiswa", "repair",
     "herstel"),
    ("road", "pad", "umgwaqo", "roads", "paaie"),
    ("bridge", "brug", "ibhuloho"),
    ("housing", "behuising", "izindlu", "house", "huis"),
    ("plumbing", "loodgieterswerk", "sanitation", "sanitasie"),
    ("electrical", "elektries", "ugesi", "electricity", "elektrisiteit"),
    # -- water -------------------------------------------------------------
    ("water", "amanzi"),
    ("borehole", "boorgat", "umthombo"),
    ("sewer", "riool", "sewerage", "indle"),
    # -- security ----------------------------------------------------------
    ("security", "sekuriteit", "veiligheid", "ezokuphepha", "guarding",
     "bewaking"),
    ("surveillance", "toesig", "cctv", "camera", "kamera"),
    # -- cleaning / facilities ---------------------------------------------
    ("cleaning", "skoonmaak", "ukuhlanza", "hygiene", "higiene"),
    ("catering", "spysenierings", "ukudla", "food", "kos"),
    ("garden", "tuin", "landscaping", "landskap", "ingadi"),
    # -- supply / goods -----------------------------------------------------
    ("supply", "verskaffing", "ukuhlinzeka", "delivery", "aflewering",
     "supplies", "voorrade"),
    ("equipment", "toerusting", "izinsiza", "apparatus", "apparaat"),
    ("furniture", "meubels", "ifenisha"),
    ("stationery", "skryfbehoeftes"),
    ("vehicle", "voertuig", "imoto", "fleet", "vloot"),
    ("fuel", "brandstof", "uphethiloli", "diesel", "petrol"),
    # -- professional services ---------------------------------------------
    ("consultant", "konsultant", "consulting", "konsultasie", "advisory",
     "adviesdiens"),
    ("training", "opleiding", "ukuqeqeshwa", "skills", "vaardighede"),
    ("audit", "oudit", "ukucwaninga", "auditing"),
    ("legal", "regs", "ezomthetho", "attorney", "prokureur"),
    ("medical", "medies", "ezokwelapha", "health", "gesondheid", "ezempilo"),
    ("transport", "vervoer", "ezokuthutha", "logistics", "logistiek"),
    ("insurance", "versekering", "umshwalense"),
    # -- ICT ----------------------------------------------------------------
    ("software", "sagteware"),
    ("hardware", "hardeware"),
    ("network", "netwerk", "connectivity", "konnektiwiteit"),
    # -- procurement process vocabulary ------------------------------------
    ("tender", "tenders", "bid", "bod", "itenda", "rfq", "rfp", "rft"),
    ("quotation", "kwotasie", "quote", "isilinganiso"),
    ("contract", "kontrak", "inkontileka"),
    ("briefing", "inligtingsessie", "clarification", "opklaring"),
    ("appointment", "aanstelling", "appoint", "aanstel"),
    ("panel", "paneel", "framework", "raamwerk"),
    ("municipality", "munisipaliteit", "umasipala", "municipal", "munisipale"),
    ("department", "departement", "umnyango"),
    ("province", "provinsie", "isifundazwe", "provincial", "provinsiale"),
)


def _build_index() -> dict[str, tuple[str, ...]]:
    index: dict[str, tuple[str, ...]] = {}
    for group in SYNONYM_GROUPS:
        for term in group:
            key = term.lower()
            # A term appearing in two groups gets the union, deduped, order
            # preserved — "water" could plausibly join a sanitation group later.
            if key in index:
                merged = list(index[key])
                merged.extend(t for t in group if t not in merged)
                index[key] = tuple(merged)
            else:
                index[key] = group
    return index


_INDEX = _build_index()

# Terms that add nothing to a procurement search but appear in most notices;
# expanding them would OR half the corpus into every query.
STOPWORDS = frozenset({
    "the", "of", "for", "and", "a", "an", "to", "in", "on", "at", "by",
    "van", "die", "en", "vir", "'n",
})


def synonyms_for(term: str) -> tuple[str, ...]:
    """All interchangeable forms of `term`, including `term` itself.

    Returns an empty tuple when the term is unknown, so callers can cheaply
    distinguish "no expansion needed" from "expanded to itself".
    """
    return _INDEX.get(term.strip().lower(), ())


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------
# We mirror the subset of `websearch_to_tsquery` syntax our users actually
# type: quoted "phrases", -negation, and bare terms (implicitly AND-ed).
_TOKEN_RE = re.compile(r'(-?)"([^"]*)"|(-?)(\S+)')
# Strip anything that is a tsquery operator or would break the lexeme quoting.
_CLEAN_RE = re.compile(r"[^\w\s'/-]", re.UNICODE)


class Token:
    """One parsed unit of a user query."""

    __slots__ = ("text", "negated", "phrase")

    def __init__(self, text: str, *, negated: bool = False, phrase: bool = False) -> None:
        self.text = text
        self.negated = negated
        self.phrase = phrase

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        flags = "".join(["-" if self.negated else "", '"' if self.phrase else ""])
        return f"Token({flags}{self.text!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Token)
            and (self.text, self.negated, self.phrase)
            == (other.text, other.negated, other.phrase)
        )


def sanitize(q: str | None) -> str:
    """Strip characters that cannot be sent to Postgres as a parameter.

    Postgres text fields cannot hold NUL bytes: leaving one in a parameter
    raises DataError deep in the driver, so a crafted query string becomes a
    500. Other C0 control characters carry no search meaning either.
    """
    return "".join(ch for ch in (q or "") if ch == "\t" or ch >= " ")


def parse_query(q: str) -> list[Token]:
    """Split a user query into tokens, discarding noise.

    Never raises: any input that survives to here must produce *some* query
    rather than a 500.
    """
    q = sanitize(q)

    tokens: list[Token] = []
    for m in _TOKEN_RE.finditer(q):
        quoted_neg, quoted, bare_neg, bare = m.groups()
        if quoted is not None:
            text = _CLEAN_RE.sub(" ", quoted).strip()
            if text:
                tokens.append(Token(text, negated=bool(quoted_neg), phrase=True))
            continue
        raw = bare or ""
        # "or" is websearch's OR keyword, not a search term.
        if raw.lower() in {"or", "and"}:
            continue
        text = _CLEAN_RE.sub(" ", raw).strip()
        if not text or text.lower() in STOPWORDS:
            continue
        # A cleaned bare token can split into several words ("e.g.foo bar").
        for word in text.split():
            # Punctuation-only leftovers ("-", "/", "--") are not search
            # terms; keeping them would emit a lexeme that matches nothing and
            # make `-` behave differently from every other noise character.
            if not any(c.isalnum() for c in word):
                continue
            tokens.append(Token(word, negated=bool(bare_neg)))
    return tokens


def _lexeme(word: str) -> str:
    """Render one word as a quoted tsquery lexeme.

    Quoting means a term can never be reinterpreted as a tsquery operator,
    which is what keeps `to_tsquery` from raising on user input. `to_tsquery`
    still normalises (stems) quoted lexemes, so expansion terms are stemmed by
    exactly the same configuration as the indexed text.
    """
    return "'" + word.replace("'", "''") + "'"


def _render(token: Token) -> str:
    words = token.text.split()
    if not words:
        return ""
    if token.phrase or len(words) > 1:
        # <-> is the phrase (FOLLOWED BY) operator: adjacency, in order.
        return "(" + " <-> ".join(_lexeme(w) for w in words) + ")"
    return _lexeme(words[0])


def expand_to_tsquery(q: str) -> tuple[str | None, bool]:
    """Build a tsquery string for `q` with synonyms OR-ed into each term.

    Returns ``(tsquery, expanded)``. ``tsquery`` is None when the query has no
    usable terms. ``expanded`` is False when no token had a synonym — the
    caller then keeps `websearch_to_tsquery`, which is more forgiving of odd
    input than anything we can reconstruct.

    Negated terms are expanded too: excluding "maintenance" must also exclude
    "instandhouding", otherwise the Afrikaans notice the user was trying to
    filter out comes straight back.
    """
    tokens = parse_query(q)
    if not tokens:
        return None, False

    expanded = False
    positives: list[str] = []
    negatives: list[str] = []

    # Terms the user positively asked for, as lexemes. A negation must never
    # expand into one of these: "construction -building" would otherwise
    # subtract the whole construction synonym group from itself and return
    # nothing, which reads as a broken search engine.
    positive_lexemes: set[str] = set()
    for token in tokens:
        if token.negated or token.phrase:
            continue
        for word in token.text.split():
            group = synonyms_for(word)
            positive_lexemes.update(t.lower() for t in (group or (word,)))

    for token in tokens:
        rendered = _render(token)
        if not rendered:
            continue
        # Phrases are matched literally: expanding inside a quoted phrase
        # would defeat the reason the user quoted it.
        if not token.phrase and " " not in token.text:
            group = synonyms_for(token.text)
            if group and token.negated:
                # Keep only the parts of the exclusion that do not contradict
                # what was positively requested; always keep the literal term.
                literal = token.text.lower()
                kept = [
                    t for t in group
                    if t.lower() == literal or t.lower() not in positive_lexemes
                ]
                if len(kept) > 1:
                    expanded = True
                    rendered = "(" + " | ".join(_lexeme(t) for t in kept) + ")"
                else:
                    rendered = _lexeme(literal)
            elif group:
                expanded = True
                rendered = "(" + " | ".join(_lexeme(t) for t in group) + ")"
        (negatives if token.negated else positives).append(rendered)

    if not positives and not negatives:
        return None, False

    clauses = list(positives)
    clauses.extend(f"!{n}" for n in negatives)
    return " & ".join(clauses), expanded
