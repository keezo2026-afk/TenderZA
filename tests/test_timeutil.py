"""Timezone doctrine tests (§10.2.1) — the P0 "closing date is 2 hours late" bug.

The eTenders OCDS API publishes SAST wall-clock times with a literal "Z".
Reading them as UTC pushed every deadline 2 hours into the future, which is
exactly the failure mode that makes a user miss a bid. These tests pin the
correction and its edges.
"""

from datetime import datetime, timedelta, timezone

from tenderza.timeutil import (
    SAST,
    claims_utc,
    ensure_tz,
    is_sentinel,
    looks_date_only,
    parse_iso,
    parse_wall_time_as_sast,
    to_sast,
)

UTC = timezone.utc


class TestSast:
    def test_fixed_two_hour_offset(self):
        assert SAST.utcoffset(None) == timedelta(hours=2)

    def test_no_dst_in_july_or_january(self):
        """SA observes no daylight saving — same offset all year."""
        for month in (1, 7):
            dt = datetime(2026, month, 15, 11, 0, tzinfo=SAST)
            assert dt.utcoffset() == timedelta(hours=2)


class TestParseIso:
    def test_plain_utc(self):
        assert parse_iso("2026-09-16T09:00:00Z") == datetime(2026, 9, 16, 9, tzinfo=UTC)

    def test_offset_preserved(self):
        assert parse_iso("2026-09-16T11:00:00+02:00").utcoffset() == timedelta(hours=2)

    def test_naive_stays_naive(self):
        assert parse_iso("2026-09-16T11:00:00").tzinfo is None

    def test_empty_and_garbage(self):
        assert parse_iso(None) is None
        assert parse_iso("") is None
        assert parse_iso("not a date") is None

    def test_dotnet_min_value_sentinel_is_none(self):
        """0001-01-01T00:00:00Z means "no briefing", not year 1."""
        assert parse_iso("0001-01-01T00:00:00Z") is None


class TestSentinel:
    def test_year_one_is_sentinel(self):
        assert is_sentinel(datetime(1, 1, 1, tzinfo=UTC))

    def test_real_dates_are_not(self):
        assert not is_sentinel(datetime(2026, 9, 16, tzinfo=UTC))
        assert not is_sentinel(None)


class TestClaimsUtc:
    def test_z_suffixes(self):
        assert claims_utc("2026-09-16T11:00:00Z")
        assert claims_utc("2026-09-16T11:00:00z")
        assert claims_utc("2026-09-16T11:00:00+00:00")

    def test_real_offsets_do_not_claim_utc(self):
        assert not claims_utc("2026-09-16T11:00:00+02:00")
        assert not claims_utc("2026-09-16T11:00:00")


class TestParseWallTimeAsSast:
    def test_z_suffix_is_reinterpreted_as_sast(self):
        """THE BUG: 11:00Z from eTenders is 11:00 SAST = 09:00 UTC."""
        dt, corrected = parse_wall_time_as_sast("2026-09-16T11:00:00Z")
        assert corrected is True
        assert dt.utcoffset() == timedelta(hours=2)
        assert (dt.hour, dt.minute) == (11, 0)                 # wall time kept
        assert dt.astimezone(UTC) == datetime(2026, 9, 16, 9, tzinfo=UTC)

    def test_correction_moves_the_instant_two_hours_earlier(self):
        naive_utc_reading = datetime(2026, 9, 16, 11, tzinfo=UTC)
        dt, _ = parse_wall_time_as_sast("2026-09-16T11:00:00Z")
        assert naive_utc_reading - dt == timedelta(hours=2)

    def test_explicit_sast_offset_is_trusted_untouched(self):
        dt, corrected = parse_wall_time_as_sast("2026-09-16T11:00:00+02:00")
        assert corrected is False
        assert dt == datetime(2026, 9, 16, 11, tzinfo=SAST)

    def test_naive_input_assumed_sast_without_flagging_a_correction(self):
        dt, corrected = parse_wall_time_as_sast("2026-09-16T11:00:00")
        assert corrected is False
        assert dt.utcoffset() == timedelta(hours=2)

    def test_sentinel_becomes_none(self):
        assert parse_wall_time_as_sast("0001-01-01T00:00:00Z") == (None, False)

    def test_empty_becomes_none(self):
        assert parse_wall_time_as_sast(None) == (None, False)
        assert parse_wall_time_as_sast("") == (None, False)

    def test_midnight_date_only_keeps_the_calendar_day(self):
        """A date-only value must not slide to the previous day (§10.2.1)."""
        dt, corrected = parse_wall_time_as_sast("2026-08-14T00:00:00Z")
        assert corrected is True
        assert dt.date() == datetime(2026, 8, 14).date()
        assert dt.astimezone(SAST).date() == datetime(2026, 8, 14).date()

    def test_real_world_closing_times_land_on_business_hours(self):
        """Every SA closing time should read as a plausible office hour."""
        for raw, expected_hour in [
            ("2026-09-15T10:00:00Z", 10),   # City of Cape Town
            ("2026-09-16T11:00:00Z", 11),   # KZN Public Works
            ("2026-09-18T12:00:00Z", 12),   # Theewaterskloof
            ("2026-08-28T09:30:00Z", 9),    # briefing
        ]:
            dt, _ = parse_wall_time_as_sast(raw)
            assert dt.astimezone(SAST).hour == expected_hour


class TestEnsureTz:
    def test_naive_gets_sast(self):
        assert ensure_tz(datetime(2026, 9, 16, 11)).utcoffset() == timedelta(hours=2)

    def test_aware_untouched(self):
        dt = datetime(2026, 9, 16, 9, tzinfo=UTC)
        assert ensure_tz(dt) is dt

    def test_assume_override(self):
        assert ensure_tz(datetime(2026, 9, 16, 9), assume=UTC).utcoffset() == timedelta(0)

    def test_none(self):
        assert ensure_tz(None) is None


class TestToSast:
    def test_utc_renders_as_sast(self):
        out = to_sast(datetime(2026, 9, 16, 9, tzinfo=UTC))
        assert (out.hour, out.utcoffset()) == (11, timedelta(hours=2))

    def test_none(self):
        assert to_sast(None) is None


class TestLooksDateOnly:
    def test_midnight_sast(self):
        assert looks_date_only(datetime(2026, 9, 16, 0, 0, tzinfo=SAST))

    def test_midnight_utc(self):
        assert looks_date_only(datetime(2026, 9, 16, 0, 0, tzinfo=UTC))

    def test_naive_midnight(self):
        assert looks_date_only(datetime(2026, 9, 16, 0, 0))

    def test_real_closing_time_is_not_date_only(self):
        assert not looks_date_only(datetime(2026, 9, 16, 11, 0, tzinfo=SAST))

    def test_none(self):
        assert not looks_date_only(None)
