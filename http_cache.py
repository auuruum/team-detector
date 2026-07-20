import email.utils
import hashlib
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


@dataclass
class FetchResult:
    text: str
    source: str
    warning: str | None = None


class PersistentHttpCache:
    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=10000')
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute('''
                CREATE TABLE IF NOT EXISTS http_cache (
                    cache_key TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    body TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    stale_until REAL NOT NULL
                )
            ''')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS host_rate_state (
                    host TEXT PRIMARY KEY,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
            ''')

    @staticmethod
    def key_for(url: str) -> str:
        return hashlib.sha256(url.encode('utf-8')).hexdigest()

    def get(self, url: str, now: float | None = None):
        now = time.time() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                'SELECT body, expires_at, stale_until FROM http_cache WHERE cache_key = ?',
                (self.key_for(url),)
            ).fetchone()
        if row is None or now > row[2]:
            return None
        return {'body': row[0], 'fresh': now <= row[1], 'stale_until': row[2]}

    def put(self, url: str, body: str, ttl_seconds: float, stale_seconds: float,
            now: float | None = None):
        now = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute('''
                INSERT INTO http_cache(cache_key, url, body, fetched_at, expires_at, stale_until)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    url = excluded.url,
                    body = excluded.body,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at,
                    stale_until = excluded.stale_until
            ''', (self.key_for(url), url, body, now, now + ttl_seconds, now + stale_seconds))

    def get_rate_state(self, host: str):
        with self._connect() as connection:
            row = connection.execute(
                'SELECT cooldown_until, failures FROM host_rate_state WHERE host = ?', (host,)
            ).fetchone()
        return {'cooldown_until': row[0], 'failures': row[1]} if row else {
            'cooldown_until': 0.0, 'failures': 0
        }

    def set_rate_state(self, host: str, cooldown_until: float, failures: int):
        with self._connect() as connection:
            connection.execute('''
                INSERT INTO host_rate_state(host, cooldown_until, failures, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    cooldown_until = excluded.cooldown_until,
                    failures = excluded.failures,
                    updated_at = excluded.updated_at
            ''', (host, cooldown_until, failures, time.time()))


class ResilientHttpClient:
    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, cache_path: str, request_delay: float = 0.0, max_retries: int = 3,
                 session=None, sleep_fn=time.sleep, time_fn=time.time):
        self.cache = PersistentHttpCache(cache_path)
        self.request_delay = max(0.0, request_delay)
        self.max_retries = max(0, max_retries)
        self.session = session or requests.Session()
        self.session.headers.update({
            'User-Agent': 'rustplusplus-team-detector/1.0 (+https://github.com/auuruum/team-detector)'
        })
        self.sleep_fn = sleep_fn
        self.time_fn = time_fn
        self._last_request_at = 0.0

    @staticmethod
    def _retry_after_seconds(response, fallback: float) -> float:
        value = response.headers.get('Retry-After')
        if not value:
            return fallback
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value).timestamp()
                return max(0.0, parsed - time.time())
            except (TypeError, ValueError, OverflowError):
                return fallback

    def _respect_delay(self, host: str):
        now = self.time_fn()
        state = self.cache.get_rate_state(host)
        wait = max(0.0, state['cooldown_until'] - now)
        if self._last_request_at:
            wait = max(wait, self.request_delay - (now - self._last_request_at))
        if wait > 0:
            self.sleep_fn(wait)

    def get(self, url: str, ttl_seconds: float, stale_seconds: float, timeout: float = 20) -> FetchResult:
        cached = self.cache.get(url, self.time_fn())
        if cached and cached['fresh']:
            return FetchResult(cached['body'], 'cache')

        host = urlparse(url).netloc.lower()
        last_warning = None
        for attempt in range(self.max_retries + 1):
            self._respect_delay(host)
            self._last_request_at = self.time_fn()
            try:
                response = self.session.get(url, timeout=timeout)
                if response.status_code not in self.RETRYABLE_STATUSES:
                    response.raise_for_status()
                    self.cache.put(url, response.text, ttl_seconds, stale_seconds, self.time_fn())
                    self.cache.set_rate_state(host, 0, 0)
                    return FetchResult(response.text, 'network')

                state = self.cache.get_rate_state(host)
                failures = state['failures'] + 1
                fallback = min(60.0, [2.0, 5.0, 15.0, 30.0][min(attempt, 3)])
                wait = self._retry_after_seconds(response, fallback + random.uniform(0, 1))
                self.cache.set_rate_state(host, self.time_fn() + wait, failures)
                last_warning = f'{host} returned HTTP {response.status_code}; retrying after {wait:.1f}s'
                if wait > 30:
                    last_warning = f'{host} is in cooldown for {wait:.1f}s; using cached data if available'
                    break
            except requests.HTTPError as error:
                last_warning = f'{host} request failed: {error}'
                break
            except requests.exceptions.RequestException as error:
                wait = min(30.0, (2 ** attempt) + random.uniform(0, 1))
                state = self.cache.get_rate_state(host)
                self.cache.set_rate_state(host, self.time_fn() + wait, state['failures'] + 1)
                last_warning = f'{host} request failed: {error}'

            if attempt < self.max_retries:
                continue

        if cached:
            return FetchResult(cached['body'], 'stale', last_warning)
        return FetchResult('', 'failed', last_warning)
