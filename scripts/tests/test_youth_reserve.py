"""Tests for the youth/reserve title filter in highlights_common.

Youth / reserve / academy clips name both senior clubs and even the senior
competition ("Premier League 2"), so they slip past the both-teams and
competition-keyword gates and get mis-linked to the senior fixture.
is_youth_reserve_title() drops them; it must NOT drop legitimate senior clips
(notably "Premier League 2025/26", where "2" follows "league").
"""

from highlights_common import is_youth_reserve_title


# Real titles that were wrongly linked to senior fixtures (must be excluded).
WRONG = [
    "Leeds United U21 2-2 Brighton and Hove Albion U21 | Premier League 2 highlights",
    "Back to Winning Ways 3⃣✅ | U21 Match Highlights: Aston Villa 0-2 "
    "Crystal Palace | Premier League 2",
    "LATE KALUM THOMPSON WINNER! \U0001F3AF | Fulham 1-2 Nottingham Forest B-Team "
    "| Premier League 2 Highlights \U0001F3A5",
    "PREMIER LEAGUE CUP WINNERS! | Crystal Palace 1-1 Manchester United | U18 Highlights",
    "Résumé AS Monaco 3-5 Manchester City - Youth League",
]

# Legitimate senior clips that must be KEPT.
SENIOR = [
    "Nottingham Forest 0-0 Fulham | Premier League Highlights",
    "Chelsea 0-0 Crystal Palace | HIGHLIGHTS - Extended | Premier League 2025/26",
    "Highlights AS Monaco 2 - 2 Manchester City",
    "Points shared \U0001F91D | Premier League Highlights: Crystal Palace 0-0 West Ham United",
    "Man City 2-1 Bournemouth | Premier League Highlights",
    "Newcastle United 2 Liverpool 2 | EXTENDED Premier League Highlights",
]


def test_youth_and_reserve_titles_are_excluded():
    for t in WRONG:
        assert is_youth_reserve_title(t) is True, t


def test_senior_titles_are_kept():
    for t in SENIOR:
        assert is_youth_reserve_title(t) is False, t


def test_premier_league_2025_is_not_treated_as_premier_league_2():
    # Guard the specific false-positive: "Premier League 2025/26" must not match.
    assert is_youth_reserve_title("Arsenal 3-1 Spurs | Premier League 2025/26") is False


def test_b_team_variants_excluded():
    assert is_youth_reserve_title("Fulham B-Team 1-0 Chelsea B Team")


def test_case_and_accents_insensitive():
    assert is_youth_reserve_title("MONACO 3-5 MAN CITY - YOUTH LEAGUE")
    assert is_youth_reserve_title("Villa Reserves 2-0 City")  # "reserves"
