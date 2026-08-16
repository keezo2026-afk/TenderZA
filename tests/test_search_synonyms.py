"""Multilingual query expansion (Blueprint §12)."""

from __future__ import annotations

import pytest

from tenderza.search.synonyms import (
    SYNONYM_GROUPS,
    Token,
    expand_to_tsquery,
    parse_query,
    synonyms_for,
)


class TestVocabulary:
    def test_groups_are_lowercase_and_deduped(self):
        for group in SYNONYM_GROUPS:
            assert len(group) == len(set(group)), f"duplicate term in {group}"
            for term in group:
                assert term == term.lower(), f"{term!r} is not lowercase"
                assert term.strip() == term

    def test_groups_have_at_least_two_members(self):
        # A one-member group expands to itself: pure cost, no recall.
        for group in SYNONYM_GROUPS:
            assert len(group) >= 2, f"pointless group {group}"

    def test_lookup_is_bidirectional(self):
        # The whole point: any language finds any other.
        assert "construction" in synonyms_for("bou")
        assert "bou" in synonyms_for("construction")
        assert "ukwakhiwa" in synonyms_for("construction")
        assert "construction" in synonyms_for("ukwakhiwa")

    def test_lookup_is_case_insensitive(self):
        assert synonyms_for("CONSTRUCTION") == synonyms_for("construction")
        assert synonyms_for("  Bou  ") == synonyms_for("bou")

    def test_unknown_term_has_no_expansion(self):
        assert synonyms_for("xyzzy") == ()

    def test_blueprint_example_is_covered(self):
        # §12 names this triple explicitly.
        group = synonyms_for("construction")
        assert {"construction", "bou", "ukwakhiwa"} <= set(group)

    def test_no_term_belongs_to_conflicting_groups(self):
        # A term in two groups silently merges them, which can chain unrelated
        # vocabulary together. Allowed, but it must be deliberate.
        seen: dict[str, int] = {}
        for i, group in enumerate(SYNONYM_GROUPS):
            for term in group:
                assert term not in seen, (
                    f"{term!r} appears in group {seen[term]} and {i}"
                )
                seen[term] = i


class TestParseQuery:
    def test_bare_terms(self):
        assert parse_query("cctv gauteng") == [Token("cctv"), Token("gauteng")]

    def test_negation(self):
        assert parse_query("cctv -maintenance") == [
            Token("cctv"), Token("maintenance", negated=True)
        ]

    def test_quoted_phrase(self):
        assert parse_query('"access control"') == [
            Token("access control", phrase=True)
        ]

    def test_negated_phrase(self):
        assert parse_query('-"access control"') == [
            Token("access control", negated=True, phrase=True)
        ]

    def test_stopwords_dropped(self):
        assert parse_query("supply of the goods") == [
            Token("supply"), Token("goods")
        ]

    def test_boolean_keywords_dropped(self):
        assert parse_query("cctv OR camera") == [Token("cctv"), Token("camera")]

    def test_punctuation_stripped(self):
        assert parse_query("cctv!!!") == [Token("cctv")]

    @pytest.mark.parametrize("q", ["", "   ", "&&&", "!!!", '"', "-", "OR"])
    def test_noise_yields_nothing(self, q):
        assert parse_query(q) == []

    def test_never_raises(self):
        for q in ["'; DROP TABLE tenders;--", "a & b | c", "((()))", "\\", "%s"]:
            parse_query(q)          # must not raise

    def test_hyphenated_word_survives(self):
        # "e-procurement" is one term, not a negation of "procurement".
        assert parse_query("e-procurement") == [Token("e-procurement")]


class TestExpansion:
    def test_known_term_expands_to_group(self):
        tsq, expanded = expand_to_tsquery("construction")
        assert expanded is True
        assert "'bou'" in tsq and "'ukwakhiwa'" in tsq
        assert tsq.startswith("(") and " | " in tsq

    def test_unknown_term_does_not_expand(self):
        tsq, expanded = expand_to_tsquery("xyzzy")
        assert expanded is False
        assert tsq == "'xyzzy'"

    def test_terms_are_and_ed(self):
        tsq, _ = expand_to_tsquery("construction gauteng")
        assert " & " in tsq

    def test_empty_query_returns_none(self):
        assert expand_to_tsquery("") == (None, False)
        assert expand_to_tsquery("   ") == (None, False)
        assert expand_to_tsquery("&&&") == (None, False)

    def test_phrase_is_not_expanded(self):
        # Quoting means "these words, in this order" — expanding defeats it.
        tsq, expanded = expand_to_tsquery('"construction works"')
        assert expanded is False
        assert "<->" in tsq
        assert "bou" not in tsq

    def test_negation_is_expanded(self):
        # Excluding "maintenance" must also exclude "instandhouding",
        # otherwise the Afrikaans notice comes straight back.
        tsq, _ = expand_to_tsquery("cctv -maintenance")
        assert "!(" in tsq
        assert "'instandhouding'" in tsq

    def test_negation_never_cancels_a_positive_term(self):
        # "building" is a synonym of "construction"; expanding the negation
        # would subtract the group from itself and return zero results.
        tsq, _ = expand_to_tsquery("construction -building")
        assert "!'building'" in tsq
        # the positive group survives intact
        assert "'konstruksie'" in tsq.split("&")[0]

    def test_negation_only_query(self):
        tsq, _ = expand_to_tsquery("-construction")
        assert tsq.startswith("!(")

    def test_lexemes_are_quoted(self):
        # Quoting is what makes user input un-interpretable as an operator.
        tsq, _ = expand_to_tsquery("construction")
        for term in ("construction", "bou"):
            assert f"'{term}'" in tsq

    def test_apostrophe_is_escaped(self):
        tsq, _ = expand_to_tsquery("o'brien")
        assert "''" in tsq          # doubled, per SQL string rules

    @pytest.mark.parametrize("q", [
        "'; DROP TABLE tenders;--",
        "a & b | c",
        "!!!&&&",
        "((((",
        "construction & !",
        "\\'",
    ])
    def test_hostile_input_never_raises(self, q):
        expand_to_tsquery(q)

    def test_stopwords_do_not_dominate(self):
        # "of"/"the" must never reach the tsquery: they would OR in the corpus.
        tsq, _ = expand_to_tsquery("supply of the goods")
        assert "'of'" not in tsq and "'the'" not in tsq


class TestSanitize:
    """Postgres parameters cannot contain NUL bytes (found by fuzzing)."""

    def test_nul_byte_removed(self):
        from tenderza.search.synonyms import sanitize
        assert "\x00" not in sanitize("bad\x00query")

    def test_control_chars_removed(self):
        from tenderza.search.synonyms import sanitize
        assert sanitize("a\x01b\x1fc") == "abc"

    def test_tab_and_newline_survive_as_whitespace(self):
        from tenderza.search.synonyms import sanitize
        assert sanitize("a\tb") == "a\tb"

    def test_unicode_survives(self):
        from tenderza.search.synonyms import sanitize
        assert sanitize("café ubuntu") == "café ubuntu"

    def test_none_is_empty(self):
        from tenderza.search.synonyms import sanitize
        assert sanitize(None) == ""

    def test_parse_query_strips_nul(self):
        assert parse_query("road\x00works") == [Token("roadworks")]
