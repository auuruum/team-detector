import tempfile
import unittest
from pathlib import Path

from team_detector import TeamDetector


class AutoDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.detector = TeamDetector(
            search_comments=True,
            search_comments_max_pages=2,
            cache_path=str(Path(self.tempdir.name) / 'cache.sqlite')
        )
        self.detector.get_battlemetrics_players = lambda _server_id: []
        self.detector.roster_status.update({'available': False, 'name_counts': {}})

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def person(steam_id, name, source='friends'):
        return {'steam_id': steam_id, 'custom_id': None, 'name': name, 'type': source}

    def test_direct_seed_friends_are_retained_with_smart_score_two(self):
        seed = '76561198982669820'
        hrusha = '76561199021904253'
        puffch1k = '76561199216048160'

        def collect(profile_id, include_comments=None):
            self.assertEqual(profile_id, seed)
            self.assertTrue(include_comments)
            return 'aurum', None, [
                self.person(hrusha, 'Hrusha'),
                self.person(puffch1k, 'Puffch1k')
            ]

        self.detector._TeamDetector__collect_profile_people = collect
        result = self.detector.start_auto_discovery(
            'rustplus:test', [seed], max_profiles=1, min_score=2,
            output_network=False, human_output=False
        )

        candidates = {candidate['steam_id']: candidate for candidate in result['candidates']}
        self.assertIn(hrusha, candidates)
        self.assertIn(puffch1k, candidates)
        self.assertEqual(candidates[hrusha]['score'], 2)
        self.assertEqual(candidates[puffch1k]['score'], 2)
        self.assertEqual(result['crawl']['comment_profiles'], 1)

    def test_high_confidence_comment_branch_is_inspected_before_plain_friend(self):
        seed = '76561190000000001'
        plain_friend = '76561190000000002'
        commenter = '76561190000000003'
        inspected = []
        comment_modes = []

        def collect(profile_id, include_comments=None):
            inspected.append(profile_id)
            comment_modes.append(include_comments)
            if profile_id == seed:
                return 'Seed', None, [
                    self.person(plain_friend, 'Plain Friend'),
                    self.person(commenter, 'Commenter', 'comments')
                ]
            return 'Commenter', None, []

        self.detector._TeamDetector__collect_profile_people = collect
        result = self.detector.start_auto_discovery(
            'rustplus:test', [seed], max_profiles=2, min_score=2,
            output_network=False, human_output=False
        )

        self.assertEqual(inspected, [seed, commenter])
        self.assertEqual(comment_modes, [True, True])
        self.assertEqual(result['crawl']['comment_profiles'], 2)

    def test_comment_vanity_author_uses_embedded_account_id_without_profile_request(self):
        html = '''
        <a class="hoverunderline commentthread_author_link"
           href="https://steamcommunity.com/id/example_vanity" data-miniprofile="1146245150">
           <bdi>Example</bdi></a>
        '''
        self.detector._TeamDetector__get_steam_profile_comments_page_content_by_steam_id = \
            lambda _steam_id, _page: html

        total, authors = self.detector.get_steam_profile_comments_page_authors('76561198982669820', 1)

        self.assertEqual(total, 1)
        self.assertEqual(authors, [{
            'steam_id': '76561199106510878',
            'custom_id': 'example_vanity',
            'name': 'Example',
            'type': 'comments'
        }])

    def test_runtime_budget_returns_partial_result_with_remaining_frontier(self):
        seed = '76561190000000001'
        friend = '76561190000000002'
        clock_values = iter([0.0, 0.0, 2.0, 2.0])
        self.detector.monotonic_fn = lambda: next(clock_values)
        self.detector._TeamDetector__collect_profile_people = lambda _profile_id, include_comments=None: (
            'Seed', None, [self.person(friend, 'Friend')]
        )

        result = self.detector.start_auto_discovery(
            'rustplus:test', [seed], max_profiles=75, min_score=2,
            output_network=False, human_output=False, max_runtime_seconds=1
        )

        self.assertEqual(result['inspected_profiles'], [seed])
        self.assertTrue(result['partial'])
        self.assertTrue(result['crawl']['truncated'])
        self.assertEqual(result['crawl']['stop_reason'], 'runtime_budget')
        self.assertEqual(result['crawl']['frontier_remaining'], 1)
        self.assertTrue(any('runtime budget' in warning for warning in result['warnings']))


if __name__ == '__main__':
    unittest.main()
