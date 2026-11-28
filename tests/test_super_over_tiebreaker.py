"""Super Over, second Super Over, boundary count, bowl-out and tie shapes.

Fixtures mirror the live ESPN payloads audited 27 Nov 2026 (1529281,
1512731, 1144530, 287862, 1442990, 1322277, 1417822): Super Over innings
carry scorecard lines only and no commentary deliveries.
"""
import unittest

from src import constructor, validator
from helpers import ball, player

from scripts.scrape_schedule import normalize_schedule_match


def _xi(start, end):
    return [{'player': player(i)} for i in range(start, end)]


def _regular_innings(number, team_id, team_name):
    return {
        'inningNumber': number,
        'team': {'id': team_id, 'name': team_name},
        'runs': None,
        'wickets': None,
        'balls': None,
        'inningBatsmen': [{'player': player(1 if number == 1 else 12), 'runs': 0, 'balls': 0}],
        'inningBowlers': [{'player': player(12 if number == 1 else 1), 'balls': 0}],
        'inningDidNotBats': [],
    }


def _super_innings(number, team_id, team_name, *, runs, wickets, balls, extra_batter=None):
    batters = [
        {
            'player': player(1 if team_id == 10 else 12),
            'runs': 0, 'balls': 1, 'fours': 0, 'sixes': 0, 'strikerate': 0.0,
            'isOut': True, 'dismissalType': 2,
            'dismissalText': {'short': 'bowled', 'long': ' b Bowler'},
        },
        {
            'player': player(2 if team_id == 10 else 13),
            'runs': 0, 'balls': 1, 'fours': 0, 'sixes': 0, 'strikerate': 0.0,
            'isOut': True, 'dismissalType': 1,
            'dismissalText': {'short': 'caught', 'long': 'c Fielder b Bowler'},
            'dismissalFielders': [{'player': player(12 if team_id == 10 else 1)}],
        },
        {
            'player': player(3 if team_id == 10 else 14),
            'runs': runs, 'balls': max(balls - 2, 0), 'fours': 0, 'sixes': 0,
            'strikerate': None, 'isOut': False,
            'dismissalText': {'short': 'not out'},
        },
    ]
    if extra_batter is not None:
        batters.append({
            'player': player(extra_batter), 'runs': 0, 'balls': 0, 'fours': 0,
            'sixes': 0, 'strikerate': 0.0, 'isOut': False,
            'dismissalText': {'short': 'not out'},
        })
    return {
        'inningNumber': number,
        'team': {'id': team_id, 'name': team_name},
        'runs': runs, 'wickets': wickets, 'balls': balls, 'penalties': 0,
        'totalBalls': None,
        'inningBatsmen': batters,
        'inningBowlers': [{
            'player': player(12 if team_id == 10 else 1),
            'overs': float(f'{balls // 6}.{balls % 6}'), 'balls': balls,
            'maidens': 0, 'conceded': runs, 'wickets': wickets,
            'economy': round(runs * 6 / balls, 2) if balls else None,
            'wides': 0, 'noballs': 0,
        }],
        'inningFallOfWickets': [
            {'dismissalBatsman': player(1 if team_id == 10 else 12),
             'fowWicketNum': 1, 'fowRuns': 0, 'fowOvers': 0.1},
        ] if wickets else [],
    }


def details(*, fmt='T20', series='Indian Premier League', balls_per_over=None,
            status_text, lang_key, tiebreaker_id=None, winner_id=None,
            is_super_over=False, super_overs=()):
    match = {
        'generalClassId': 6,
        'internationalClassId': None,
        'format': fmt,
        'series': {'name': series},
        'startDate': '2023-04-01',
        'teams': [
            {'team': {'id': 10, 'name': 'A'}},
            {'team': {'id': 20, 'name': 'B'}},
        ],
        'ground': {'name': 'Ground'},
        'season': '2023',
        'statusText': status_text,
        'statusData': {'statusTextLangKey': lang_key},
        'winnerTeamId': winner_id,
        'tiebreakerTeamId': tiebreaker_id,
        'isSuperOver': is_super_over,
    }
    if balls_per_over is not None:
        match['ballsPerOver'] = balls_per_over
    innings = [_regular_innings(1, 10, 'A'), _regular_innings(2, 20, 'B')]
    if fmt == 'HUNDRED_BALL':
        for inn in innings:
            inn.update(totalBalls=100, ballsPerOver=5)
    raw = {
        'match': match,
        'content': {
            'matchPlayers': {'teamPlayers': [
                {'type': 'PLAYING', 'team': {'id': 10, 'name': 'A'}, 'players': _xi(1, 12)},
                {'type': 'PLAYING', 'team': {'id': 20, 'name': 'B'}, 'players': _xi(12, 23)},
            ]},
            'innings': innings,
        },
    }
    if super_overs:
        raw['content']['superOverInnings'] = list(super_overs)
    return raw


def scorecard_innings(raw):
    return raw['content']['innings'] + (raw['content'].get('superOverInnings') or [])


def build(raw):
    match, error = constructor.construct_match_v3(
        'so', raw, {}, {}, {}, {}
    )
    if error:
        raise AssertionError(error)
    return match


def assert_valid(case, match, raw):
    case.assertEqual(
        validator.validate_match_data(match, scorecard_innings(raw)), [],
        validator.validate_match_data(match, scorecard_innings(raw)),
    )
    from jsonschema import Draft202012Validator
    import json
    from pathlib import Path
    schema = json.loads((Path(__file__).parents[1] / 'schema/schema.json').read_text())
    errors = list(Draft202012Validator(schema).iter_errors(match))
    case.assertEqual(errors, [], [e.message for e in errors])


class ValidationTierGuardTests(unittest.TestCase):
    """A scorecard-only Super Over must not excuse a broken regulation innings."""

    def test_a_scorecard_only_super_over_does_not_excuse_a_broken_innings(self):
        raw = details(
            status_text='Match tied (A won the Super Over)',
            lang_key='match_status_text_tied_tiebreaker_on_super_over',
            tiebreaker_id=10, is_super_over=True,
            super_overs=[_super_innings(3, 10, 'A', runs=1, wickets=2, balls=3)],
        )
        raw['content']['innings'][0].update(
            runs=0, wickets=0, balls=1, penalties=0,
            wides=0, noballs=0, byes=0, legbyes=0,
        )
        match, error = constructor.construct_match_v3(
            'so', raw, {1: [ball()]}, {}, {}, {}
        )
        self.assertIsNone(error)
        # The Super Over has no deliveries, but the regulation innings is
        # fully covered, so the coverage-gap tier must not be available.
        self.assertFalse(validator._scorecard_only_match(match))

        # A structural contradiction in the match still fails it.
        match['meta']['format'] = 'Test'
        assessment = validator.assess_match(match, scorecard_innings(raw))
        self.assertEqual(assessment.tier, "failed")
        self.assertFalse(assessment.is_publishable)


class SuperOverShapeTests(unittest.TestCase):
    def test_t20_super_over_is_scorecard_only_and_decided(self):
        raw = details(
            status_text='Match tied (A won the Super Over)',
            lang_key='match_status_text_tied_tiebreaker_on_super_over',
            tiebreaker_id=10, is_super_over=True,
            super_overs=[
                _super_innings(3, 10, 'A', runs=1, wickets=2, balls=3),
                _super_innings(4, 20, 'B', runs=4, wickets=0, balls=1, extra_batter=23),
            ],
        )
        match = build(raw)
        assert_valid(self, match, raw)
        outcome = match['outcome']
        self.assertEqual(outcome['result'], 'tie')
        self.assertIsNone(outcome['winner'])
        self.assertEqual(outcome['eliminator'], 'A')
        self.assertEqual(outcome['decider_method'], 'super_over')
        self.assertTrue(match['meta']['is_super_over'])
        self.assertEqual(match['meta']['status_text'], 'Match tied (A won the Super Over)')
        so = [i for i in match['innings'] if i['super_over']]
        self.assertEqual([i['innings_number'] for i in so], [3, 4])
        self.assertEqual({i['innings_kind'] for i in so}, {'super_over'})
        self.assertTrue(all(i['include_in_standard_stats'] is False for i in so))
        self.assertTrue(all(i['overs'] == [] for i in so))
        self.assertTrue(all(i['source_legal_balls'] == i['source_scorecard']['balls'] for i in so))
        details_ = so[0]['scorecard_only_details']
        self.assertEqual([b['runs'] for b in details_['batters']], [0, 0, 1])
        self.assertEqual(details_['bowlers'][0]['runs_conceded'], 1)
        self.assertEqual(details_['fow'][0]['wicket_number'], 1)
        self.assertEqual(details_['batters'][0]['player_id'], 'espn_1001')
        # The actor listed only on the Super Over line still joins the registry.
        self.assertIn('espn_1023', match['players'])

    def test_innings_end_fall_of_wickets_marker_is_not_stored_as_a_wicket(self):
        raw = details(
            status_text='Match tied (A won the Super Over)',
            lang_key='match_status_text_tied_tiebreaker_on_super_over',
            tiebreaker_id=10, is_super_over=True,
            super_overs=[
                _super_innings(3, 10, 'A', runs=17, wickets=0, balls=6),
                _super_innings(4, 20, 'B', runs=4, wickets=1, balls=3),
            ],
        )
        end_marker = {
            'dismissalBatsman': None, 'fowType': 2, 'fowOrder': 0,
            'fowWicketNum': 0, 'fowRuns': 17, 'fowOvers': 1, 'fowBalls': 6,
        }
        raw['content']['superOverInnings'][0]['inningFallOfWickets'] = [end_marker]
        raw['content']['superOverInnings'][1]['inningFallOfWickets'][0]['fowType'] = 1
        match = build(raw)
        assert_valid(self, match, raw)
        so = [i for i in match['innings'] if i['super_over']]
        self.assertEqual(so[0]['scorecard_only_details']['fow'], [])
        self.assertEqual(so[1]['scorecard_only_details']['fow'][0]['wicket_number'], 1)

    def test_double_super_over_keeps_four_decider_innings(self):
        raw = details(
            status_text='Match tied (B won the 2nd Super Over)',
            lang_key='match_status_text_tied_tiebreaker_on_second_super_over',
            tiebreaker_id=20, is_super_over=True,
            super_overs=[
                _super_innings(3, 10, 'A', runs=17, wickets=0, balls=6),
                _super_innings(4, 20, 'B', runs=17, wickets=1, balls=6),
                _super_innings(5, 10, 'A', runs=23, wickets=0, balls=6),
                _super_innings(6, 20, 'B', runs=19, wickets=2, balls=7),
            ],
        )
        match = build(raw)
        assert_valid(self, match, raw)
        self.assertEqual(match['outcome']['decider_method'], 'second_super_over')
        self.assertEqual(len([i for i in match['innings'] if i['super_over']]), 4)

    def test_boundary_count_win_without_decider_innings(self):
        raw = details(
            status_text='Match tied (B won on boundary count)',
            lang_key='match_status_text_tied_tiebreaker_on_boundary_count',
            tiebreaker_id=20, is_super_over=True,
        )
        match = build(raw)
        assert_valid(self, match, raw)
        self.assertEqual(match['outcome']['result'], 'tie')
        self.assertIsNone(match['outcome']['winner'])
        self.assertEqual(match['outcome']['eliminator'], 'B')
        self.assertEqual(match['outcome']['decider_method'], 'boundary_count')
        # Match-level isSuperOver is recorded as served; no decider innings exist.
        self.assertTrue(match['meta']['is_super_over'])
        self.assertEqual([i for i in match['innings'] if i['super_over']], [])

    def test_bowl_out_is_a_decider_not_a_super_over(self):
        raw = details(
            status_text='Match tied',
            lang_key='match_status_text_tied',
            tiebreaker_id=20,
        )
        match = build(raw)
        assert_valid(self, match, raw)
        self.assertEqual(match['outcome']['eliminator'], 'B')
        self.assertEqual(match['outcome']['decider_method'], 'bowl_out')
        self.assertFalse(match['meta']['is_super_over'])

    def test_genuine_tie_has_no_eliminator(self):
        raw = details(status_text='Match tied', lang_key='match_status_text_tied')
        match = build(raw)
        assert_valid(self, match, raw)
        self.assertEqual(match['outcome']['result'], 'tie')
        self.assertIsNone(match['outcome']['winner'])
        self.assertIsNone(match['outcome']['eliminator'])
        self.assertIsNone(match['outcome']['decider_method'])

    def test_dls_tie_keeps_rain_method_and_no_decider(self):
        raw = details(
            status_text='Match tied (DLS method)',
            lang_key='match_status_text_tied_rain_rule',
        )
        match = build(raw)
        assert_valid(self, match, raw)
        self.assertEqual(match['outcome']['result'], 'tie')
        self.assertEqual(match['outcome']['method'], 'DLS')
        self.assertIsNone(match['outcome']['decider_method'])
        self.assertIsNone(match['outcome']['eliminator'])

    def test_hundred_super_five_is_scorecard_only(self):
        raw = details(
            fmt='HUNDRED_BALL', series="The Hundred Men's Competition", balls_per_over=5,
            status_text='Match tied (B won the Super Over)',
            lang_key='match_status_text_tied_tiebreaker_on_super_over',
            tiebreaker_id=20, is_super_over=True,
            super_overs=[
                _super_innings(3, 10, 'A', runs=7, wickets=2, balls=5),
                _super_innings(4, 20, 'B', runs=11, wickets=0, balls=4),
            ],
        )
        match = build(raw)
        assert_valid(self, match, raw)
        self.assertTrue(match['outcome']['decider_method'] == 'super_five')
        so = [i for i in match['innings'] if i['super_over']]
        self.assertEqual({i['innings_kind'] for i in so}, {'super_five'})
        self.assertTrue(all(i['overs'] == [] for i in so))
        self.assertTrue(all(i['include_in_standard_stats'] is False for i in so))


class ScheduleTiebreakerTests(unittest.TestCase):
    def _fixture(self, **overrides):
        raw = {
            'objectId': 1529281,
            'format': 'T20',
            'internationalClassId': 3,
            'generalClassId': 6,
            'startDate': '2026-11-27T00:00:00.000Z',
            'endDate': '2026-11-27T00:00:00.000Z',
            'state': 'POST',
            'stage': 'FINISHED',
            'statusText': 'Match tied (Australia won the Super Over)',
            'statusData': {'statusTextLangKey': 'match_status_text_tied_tiebreaker_on_super_over'},
            'isSuperOver': True,
            'winnerTeamId': None,
            'tiebreakerTeamId': 2,
            'ground': {'name': 'Melbourne Cricket Ground'},
            'teams': [
                {'team': {'id': 2, 'name': 'Australia'}},
                {'team': {'id': 1, 'name': 'England'}},
            ],
            'series': {
                'id': 18004, 'objectId': 1528677,
                'slug': 'england-in-australia-2026-27',
                'name': 'England in Australia',
                'season': '2026/27',
            },
        }
        raw.update(overrides)
        return raw

    def test_tiebreaker_team_is_the_schedule_winner(self):
        record = normalize_schedule_match(self._fixture())
        self.assertEqual(record['winner_team'], 'Australia')
        self.assertEqual(record['tiebreaker_team'], 'Australia')
        self.assertEqual(record['decider_method'], 'super_over')

    def test_real_winner_is_not_overridden_by_tiebreaker(self):
        record = normalize_schedule_match(self._fixture(
            statusText='Australia won by 3 wickets', winnerTeamId=2, tiebreakerTeamId=2,
        ))
        self.assertEqual(record['winner_team'], 'Australia')

    def test_bowl_out_and_boundary_count_labels(self):
        bowl_out = normalize_schedule_match(self._fixture(
            statusText='Match tied', statusData={'statusTextLangKey': 'match_status_text_tied'},
            isSuperOver=False, tiebreakerTeamId=2,
        ))
        self.assertEqual(bowl_out['decider_method'], 'bowl_out')
        boundary = normalize_schedule_match(self._fixture(
            statusText='Match tied (Australia won on boundary count)',
            statusData={'statusTextLangKey': 'match_status_text_tied_tiebreaker_on_boundary_count'},
            isSuperOver=True, tiebreakerTeamId=2,
        ))
        self.assertEqual(boundary['decider_method'], 'boundary_count')


if __name__ == '__main__':
    unittest.main()
