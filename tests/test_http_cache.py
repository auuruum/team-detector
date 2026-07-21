import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from http_cache import PersistentHttpCache, ResilientHttpClient
from team_detector import TeamDetector, roster_name_match


class FakeResponse:
    def __init__(self, status_code=200, text='ok', headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}')


class HttpCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache_path = str(Path(self.tempdir.name) / 'cache.sqlite')

    def tearDown(self):
        self.tempdir.cleanup()

    def test_fresh_and_stale_cache_windows(self):
        cache = PersistentHttpCache(self.cache_path)
        cache.put('https://example.test/a', 'body', 10, 100, now=1000)

        self.assertEqual(cache.get('https://example.test/a', now=1005)['fresh'], True)
        self.assertEqual(cache.get('https://example.test/a', now=1020)['fresh'], False)
        self.assertIsNone(cache.get('https://example.test/a', now=1200))

    def test_fresh_cache_avoids_network(self):
        session = Mock()
        client = ResilientHttpClient(self.cache_path, session=session, time_fn=lambda: 1000)
        client.cache.put('https://example.test/a', 'cached', 60, 600, now=1000)

        result = client.get('https://example.test/a', 60, 600)

        self.assertEqual(result.text, 'cached')
        self.assertEqual(result.source, 'cache')
        session.get.assert_not_called()

    def test_429_retries_and_respects_retry_after(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [
            FakeResponse(429, headers={'Retry-After': '3'}),
            FakeResponse(200, text='recovered')
        ]
        sleeps = []
        current_time = [1000.0]

        def sleep(seconds):
            sleeps.append(seconds)
            current_time[0] += seconds

        client = ResilientHttpClient(
            self.cache_path,
            max_retries=1,
            session=session,
            sleep_fn=sleep,
            time_fn=lambda: current_time[0]
        )
        result = client.get('https://example.test/a', 60, 600)

        self.assertEqual(result.text, 'recovered')
        self.assertEqual(result.source, 'network')
        self.assertTrue(any(seconds >= 3 for seconds in sleeps))

    def test_rustplusplus_player_snapshot_skips_battlemetrics_http(self):
        detector = TeamDetector(battlemetrics_players=['Alice', 'Bob'])
        detector.http.get = Mock(side_effect=AssertionError('BattleMetrics must not be requested'))

        players = detector.get_battlemetrics_players('20151421')

        self.assertEqual(players, ['Alice', 'Bob'])

    def test_source_aware_a2s_snapshot_preserves_duplicate_names(self):
        snapshot = {
            'source': 'a2s',
            'available': True,
            'complete': True,
            'observedAt': 123456,
            'players': ['Alice', 'Alice', 'Bob']
        }
        detector = TeamDetector(player_roster=snapshot)
        detector.http.get = Mock(side_effect=AssertionError('BattleMetrics must not be requested'))

        players = detector.get_battlemetrics_players('20151421')

        self.assertEqual(players, ['Alice', 'Alice', 'Bob'])
        self.assertEqual(detector.roster_status['source'], 'a2s')
        self.assertEqual(detector.roster_status['name_counts'], {'Alice': 2, 'Bob': 1})

    def test_unavailable_snapshot_returns_partial_roster_without_battlemetrics_request(self):
        snapshot = {
            'source': 'a2s',
            'available': False,
            'players': [],
            'reason': 'A2S_PLAYER timed out'
        }
        detector = TeamDetector(player_roster=snapshot)
        detector.http.get = Mock(side_effect=AssertionError('BattleMetrics must not be requested'))

        players = detector.get_battlemetrics_players('20151421')

        self.assertEqual(players, [])
        self.assertEqual(detector.roster_status['available'], False)
        self.assertTrue(detector.fetch_warnings)

    def test_duplicate_roster_name_is_ambiguous_not_online(self):
        self.assertEqual(roster_name_match('Alice', {'Alice': 1}), (True, 'exact_unique'))
        self.assertEqual(roster_name_match('Alice', {'Alice': 2}), (False, 'exact_ambiguous'))
        self.assertEqual(roster_name_match('Bob', {'Alice': 1}), (False, None))

    def test_direct_battlemetrics_failure_returns_partial_roster_instead_of_exiting(self):
        detector = TeamDetector()
        with patch.object(detector, '_TeamDetector__request', return_value=''):
            players = detector.get_battlemetrics_players('20151421')

        self.assertEqual(players, [])
        self.assertEqual(detector.roster_status['available'], False)
        self.assertTrue(detector.fetch_warnings)

    def test_request_headers_are_sent_only_to_the_requested_call(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = FakeResponse(200, text='ok')
        client = ResilientHttpClient(self.cache_path, session=session)

        client.get('https://api.example.test/a', 60, 600, headers={'Authorization': 'Bearer token'})

        self.assertEqual(session.get.call_args.kwargs['headers'], {'Authorization': 'Bearer token'})

    def test_non_retryable_http_error_does_not_retry(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = FakeResponse(404)
        client = ResilientHttpClient(self.cache_path, max_retries=3, session=session)

        result = client.get('https://example.test/missing', 60, 600)

        self.assertEqual(result.source, 'failed')
        self.assertEqual(session.get.call_count, 1)

    def test_long_retry_after_uses_stale_cache_without_blocking(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = FakeResponse(429, headers={'Retry-After': '3600'})
        sleeps = []
        client = ResilientHttpClient(
            self.cache_path, max_retries=3, session=session, sleep_fn=sleeps.append, time_fn=lambda: 1000
        )
        client.cache.put('https://example.test/a', 'stale', 10, 600, now=900)

        result = client.get('https://example.test/a', 60, 600)

        self.assertEqual(result.source, 'stale')
        self.assertEqual(sleeps, [])

    def test_stale_cache_is_returned_after_retry_failure(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = FakeResponse(503)
        current_time = [1020.0]

        def sleep(seconds):
            current_time[0] += seconds

        client = ResilientHttpClient(
            self.cache_path,
            max_retries=0,
            session=session,
            sleep_fn=sleep,
            time_fn=lambda: current_time[0]
        )
        client.cache.put('https://example.test/a', 'stale', 10, 600, now=1000)

        result = client.get('https://example.test/a', 60, 600)

        self.assertEqual(result.text, 'stale')
        self.assertEqual(result.source, 'stale')
        self.assertIn('HTTP 503', result.warning)


if __name__ == '__main__':
    unittest.main()
