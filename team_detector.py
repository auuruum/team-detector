#!/usr/bin/env python3

description =\
"""
Team detection program for games on BattleMetrics and Steam. The program goes through the player list on the
BattleMetrics server page and saves all player names in an array. Then it goes through the Steam profile of the player
you want to inspect and compares the friend list names and profile comments with the BattleMetrics player array to
find out which friends are currently on the server. If the program found any matches, it will then continue to go
through the friend list of those friends and so on. What you end up with is a table of all the players that might be
part of the same team as the player you provided the Steam Profile. It will also create a .html file that visualize the
friends network to see who is friends with who etc...
"""

import argparse
import html
import json
import os
import re
import sys
import time

from pathlib import Path
from http_cache import ResilientHttpClient


def roster_name_match(name: str, name_counts: dict[str, int]) -> tuple[bool, str | None]:
    count = name_counts.get(name, 0)
    if count == 1:
        return True, 'exact_unique'
    if count > 1:
        return False, 'exact_ambiguous'
    return False, None

JSON_FILE = 'team_detector.json'
RECURSIVE_DEPTH = 5
COMMENT_PAGES = 1
AUTO_MAX_PROFILES = 75
AUTO_MIN_SCORE = 4
AUTO_MAX_RUNTIME_SECONDS = 150.0

class TeamDetector:

    def __init__(self, debug: bool = False, recursive_depth: int = 5, search_comments: bool = False,
                 search_comments_max_pages: int = 1, request_delay: float = 0.0,
                 cache_path: str | None = None, request_retries: int = 3,
                 battlemetrics_players: list[str] | None = None,
                 player_roster: dict | None = None, monotonic_fn=time.monotonic):
        """
        Initializes the TeamDetector instance.

        Args:
            debug (bool): Whether to enable debug mode.
            recursive_depth (int): How deep can the recursive search go?
            search_comments (bool): Whether to search for comments on Steam profiles.
            search_comments_max_pages (int): Maximum number of pages to search for comments.
            request_delay (float): Delay between Steam/BattleMetrics requests.
        """
        self.debug = debug
        self.recursive_depth = recursive_depth
        self.search_comments = search_comments
        self.search_comments_max_pages = search_comments_max_pages
        self.request_delay = max(0.0, request_delay)
        self.request_count = 0
        default_cache = Path(os.getenv('XDG_CACHE_HOME', Path.home() / '.cache')) / 'team-detector' / 'cache.sqlite'
        self.http = ResilientHttpClient(cache_path or str(default_cache), self.request_delay, request_retries)
        self.battlemetrics_token = os.getenv('BATTLEMETRICS_TOKEN', '').strip()
        self.battlemetrics_players = battlemetrics_players if battlemetrics_players else None
        if player_roster is not None:
            self.player_roster = player_roster
        elif battlemetrics_players is not None:
            self.player_roster = {
                'source': 'rustplusplus_battlemetrics_snapshot',
                'available': True,
                'complete': True,
                'players': battlemetrics_players
            }
        else:
            self.player_roster = None
        self.roster_status = {
            'source': 'battlemetrics',
            'capability': 'names_only',
            'available': None,
            'complete': False,
            'observed_at': None,
            'size': 0,
            'population': None,
            'unique_names': 0,
            'name_counts': {},
            'reason': None
        }
        self.fetch_warnings = []
        self.fetch_stats = {'cache': 0, 'network': 0, 'stale': 0, 'failed': 0}
        self.monotonic_fn = monotonic_fn

        self.steam_profiles = dict()                    # steam_id as key and steam profile content as value
        self.steam_profiles_friends = dict()            # steam_id as key and steam friends list as value
        self.custom_id_translation_table = dict()       # custom_id as key and steam_id as value


    ##################################################
    #   Private methods
    ##################################################

    def __get_url_battlemetrics(self, server_id: str) -> str:
        """
        Generate the URL for retrieving information about a server from the BattleMetrics API.

        Args:
            server_id (str): The ID of the server to retrieve information for.

        Returns:
            str: The URL for retrieving server information from the BattleMetrics API.
        """
        return f'https://api.battlemetrics.com/servers/{server_id}?include=player'


    def __get_url_steam_profile_by_steam_id(self, steam_id: str) -> str:
        """
        Generate the URL for a Steam profile based on the provided Steam ID.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            str: The URL for the Steam profile.
        """
        return f'https://steamcommunity.com/profiles/{steam_id}/?l=english'


    def __get_url_steam_profile_by_custom_id(self, custom_id: str) -> str:
        """
        Generate the URL for a Steam profile based on the provided Custom ID.

        Args:
            custom_id (str): The Custom ID of the profile.

        Returns:
            str: The URL for the Steam profile.
        """
        return f'https://steamcommunity.com/id/{custom_id}/?l=english'


    def __get_url_steam_profile_friends_by_steam_id(self, steam_id: str) -> str:
        """
        Generate the URL for the friends list of a Steam profile based on the provided Steam ID.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            str: The URL for the friends list of the Steam profile.
        """
        return f'https://steamcommunity.com/profiles/{steam_id}/friends/?l=english'


    def __get_url_steam_profile_friends_by_custom_id(self, custom_id: str) -> str:
        """
        Generate the URL for the friends list of a Steam profile based on the provided Custom ID.

        Args:
            custom_id (str): The Custom ID of the profile.

        Returns:
            str: The URL for the friends list of the Steam profile.
        """
        return f'https://steamcommunity.com/id/{custom_id}/friends/?l=english'


    def __get_url_steam_profile_comments_page_by_steam_id(self, steam_id: str, page: int = 1) -> str:
        """
        Generate the URL for a specific page of comments on a Steam profile based on the provided Steam ID and page
        number.

        Args:
            steam_id (str): The Steam ID of the profile.
            page (int): The page number of comments. Defaults to 1.

        Returns:
            str: The URL for the specified page of comments on the Steam profile.
        """
        return f'https://steamcommunity.com/profiles/{steam_id}/allcomments/?l=english&ctp={page}'


    def __get_url_steam_profile_comments_page_by_custom_id(self, custom_id: str, page: int = 1) -> str:
        """
        Generate the URL for a specific page of comments on a Steam profile based on the provided Custom ID and page
        number.

        Args:
            custom_id (str): The Custom ID of the profile.
            page (int): The page number of comments. Defaults to 1.

        Returns:
            str: The URL for the specified page of comments on the Steam profile.
        """
        return f'https://steamcommunity.com/id/{custom_id}/allcomments/?l=english&ctp={page}'


    def __print(self, text: str):
        """
        Print the provided text if debug mode is enabled.

        Args:
            text (str): The text to be printed.
        """
        if self.debug: print(text)


    def __request(self, url: str) -> str:
        """
        Make a GET request to the specified URL and return the response text.

        Args:
            url (str): The URL to make the request to.

        Returns:
            str: The text content of the response.

        Raises:
            ValueError: If the URL is empty or None.
        """
        if not url:
            raise ValueError(f'URL cannot be empty or None. URL: {url}')

        self.__print(f'Requesting: {url}')
        self.request_count += 1
        request_headers = None
        if 'api.battlemetrics.com' in url:
            ttl_seconds, stale_seconds = 60, 5 * 60
            if self.battlemetrics_token:
                request_headers = {'Authorization': f'Bearer {self.battlemetrics_token}'}
        elif '/allcomments/' in url:
            ttl_seconds, stale_seconds = 24 * 60 * 60, 14 * 24 * 60 * 60
        elif '/friends/' in url:
            ttl_seconds, stale_seconds = 12 * 60 * 60, 7 * 24 * 60 * 60
        else:
            ttl_seconds, stale_seconds = 6 * 60 * 60, 7 * 24 * 60 * 60

        result = self.http.get(url, ttl_seconds, stale_seconds, headers=request_headers)
        self.fetch_stats[result.source] = self.fetch_stats.get(result.source, 0) + 1
        if result.warning and result.warning not in self.fetch_warnings:
            self.fetch_warnings.append(result.warning)
        if result.source == 'failed':
            print(f'Could not request: {url}. Error: {result.warning}', file=sys.stderr)
        return result.text


    def __clean_name(self, name: str) -> str:
        """
        Clean Steam/BattleMetrics names for stable comparisons and output.

        Args:
            name (str): The raw name.

        Returns:
            str: The cleaned name.
        """
        if name == None:
            return ''
        return html.unescape(re.sub(r'\s+', ' ', name)).strip()


    def __is_steam_profile_cached_by_steam_id(self, steam_id: str) -> bool:
        """
        Check if a Steam profile is cached in the instance by its Steam ID.

        Args:
            steam_id (str): The Steam ID of the profile to check.

        Returns:
            bool: True if the profile is cached, False otherwise.
        """
        value = True if steam_id in self.steam_profiles else False
        self.__print(f'__is_steam_profile_cached_by_steam_id(steam_id:{steam_id}) -> bool:{value}')
        return value


    def __is_steam_profile_cached_by_custom_id(self, custom_id: str) -> bool:
        """
        Check if a Steam profile is cached in the instance by its Custom ID.

        Args:
            custom_id (str): The Custom ID of the profile to check.

        Returns:
            bool: True if the profile is cached, False otherwise.
        """
        if custom_id in self.custom_id_translation_table:
            if self.custom_id_translation_table[custom_id] in self.steam_profiles:
                self.__print(f'__is_steam_profile_cached_by_custom_id(custom_id:{custom_id}) -> bool:{True}')
                return True

        self.__print(f'__is_steam_profile_cached_by_custom_id(custom_id:{custom_id}) -> bool:{False}')
        return False


    def __is_steam_profile_friends_cached_by_steam_id(self, steam_id: str) -> bool:
        """
        Check if a Steam profile friends list is cached in the instance by its Steam ID.

        Args:
            steam_id (str): The Steam ID of the profile friends list to check.

        Returns:
            bool: True if the profile friends list is cached, False otherwise.
        """
        value = True if steam_id in self.steam_profiles_friends else False
        self.__print(f'__is_steam_profile_friends_cached_by_steam_id(steam_id:{steam_id}) -> bool:{value}')
        return value


    def __get_steam_profile_content_by_steam_id(self, steam_id: str) -> str:
        """
        Retrieve the content of a Steam profile page based on the provided Steam ID.

        If the content is already cached in the instance, it will be retrieved from the cache.
        Otherwise, it will be fetched from the Steam API, cached, and returned.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            str: The content of the Steam profile page.
        """
        try:
            self.__print(f'__get_steam_profile_content_by_steam_id(steam_id:{steam_id})')

            content = None
            if self.__is_steam_profile_cached_by_steam_id(steam_id):
                content = self.steam_profiles[steam_id]
            else:
                content = self.__request(self.__get_url_steam_profile_by_steam_id(steam_id))
                if content == '': exit()
                self.steam_profiles[steam_id] = content

            return content
        except Exception as e:
            sys.exit(e)


    def __get_steam_profile_content_by_custom_id(self, custom_id: str) -> str:
        """
        Retrieve the content of a Steam profile page based on the provided Custom ID.

        If the content is already cached in the instance, it will be retrieved from the cache.
        Otherwise, it will be fetched from the Steam API using the provided Custom ID, and the fetched
        content will be cached along with its associated Steam ID.

        Args:
            custom_id (str): The Custom ID of the profile.

        Returns:
            str: The content of the Steam profile page.
        """
        try:
            self.__print(f'__get_steam_profile_content_by_custom_id(custom_id:{custom_id})')

            content = None
            if self.__is_steam_profile_cached_by_custom_id(custom_id):
                content = self.steam_profiles[self.custom_id_translation_table[custom_id]]
            else:
                content = self.__request(self.__get_url_steam_profile_by_custom_id(custom_id))
                if content == '': exit()
                steam_id = self.__get_steam_profile_steam_id_by_content(content)
                if steam_id == '': exit('Steam ID was empty.')
                self.custom_id_translation_table[custom_id] = steam_id
                if not self.__is_steam_profile_cached_by_steam_id(steam_id):
                    self.steam_profiles[steam_id] = content

            return content
        except Exception as e:
            sys.exit(e)


    def __get_steam_profile_friends_content_by_steam_id(self, steam_id: str) -> str:
        """
        Retrieve the content of a Steam profile friends page based on the provided Steam ID.

        If the content is already cached in the instance, it will be retrieved from the cache.
        Otherwise, it will be fetched from the Steam API, cached, and returned.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            str: The content of the Steam profile friends page.
        """
        try:
            self.__print(f'__get_steam_profile_friends_content_by_steam_id(steam_id:{steam_id})')

            content = None
            if self.__is_steam_profile_friends_cached_by_steam_id(steam_id):
                content = self.steam_profiles_friends[steam_id]
            else:
                content = self.__request(self.__get_url_steam_profile_friends_by_steam_id(steam_id))
                if content == '': exit()
                self.steam_profiles_friends[steam_id] = content

            return content
        except Exception as e:
            sys.exit(e)


    def __get_steam_profile_comments_page_content_by_steam_id(self, steam_id: str, page: int = 1) -> str:
        """
        Retrieve the content of a specific page of comments on a Steam profile based on the provided Steam ID and page
        number.

        Args:
            steam_id (str): The Steam ID of the profile.
            page (int): The page number of comments. Defaults to 1.

        Returns:
            str: The content of the specified page of comments on the Steam profile.
        """
        try:
            self.__print(f'__get_steam_profile_comments_page_content_by_steam_id(steam_id:{steam_id}, page:{page})')

            content = self.__request(self.__get_url_steam_profile_comments_page_by_steam_id(steam_id, page))
            if content == '': exit()

            return content
        except Exception as e:
            sys.exit(e)


    def __get_steam_profile_steam_id_by_content(self, steam_profile_content: str) -> str:
        """
        Extract the Steam ID of a Steam profile from the content of the profile page.

        Args:
            steam_profile_content (str): The content of the Steam profile page.

        Returns:
            str: The Steam ID of the Steam profile.
        """
        regex = r',"steamid":"(.*?)",'
        steam_id = re.findall(regex, steam_profile_content, re.MULTILINE|re.S)
        steam_id = '' if len(steam_id) == 0 else steam_id[0]

        self.__print(f'__get_steam_profile_steam_id_by_content(content) -> steam_id:{steam_id}')
        return steam_id


    def __get_steam_profile_custom_id_by_content(self, steam_profile_content: str) -> str:
        """
        Extract the Custom ID of a Steam profile from the content of the profile page.

        Args:
            steam_profile_content (str): The content of the Steam profile page.

        Returns:
            str: The Custom ID of the Steam profile if it exist, else empty str.
        """
        regex = r'g_rgProfileData = {"url":"https:\/\/steamcommunity.com\/id\/(.*)\/'
        custom_id = re.findall(regex, steam_profile_content, re.MULTILINE|re.S)
        custom_id = '' if len(custom_id) == 0 else custom_id[0]

        self.__print(f'__get_steam_profile_custom_id_by_content(content) -> custom_id:{custom_id}')
        return custom_id


    def __remove_duplicates(self, people: list) -> list:
        """
        Removes duplicate entries from a list of people dictionaries based on steam_id or custom_id.

        Args:
            people (list): A list of dictionaries representing people, each with 'steam_id' and/or 'custom_id'.

        Returns:
            list: A list with duplicate entries removed based on 'steam_id' or 'custom_id'.
        """
        temp = []
        for item in people:
            exist = False
            for i in temp:
                if item['steam_id'] != None and i['steam_id'] == item['steam_id']:
                    exist = True
                    break

                if item['custom_id'] != None and i['custom_id'] == item['custom_id']:
                    exist = True
                    break

            if not exist:
                temp.append(item)

        self.__print(f'__remove_duplicates(List[people:{len(people)}]) -> List[temp:{len(temp)}]')
        return temp


    def __remove_self_from_people(self, profile_steam_id: str, profile_custom_id: str, people) -> list:
        """
        Removes the profile identified by profile_steam_id or profile_custom_id from the list of people.

        Args:
            profile_steam_id (str): The Steam ID of the profile to be removed.
            profile_custom_id (str): The custom ID of the profile to be removed.
            people (list): A list of dictionaries representing people, each with 'steam_id' and/or 'custom_id'.

        Returns:
            list: A list with the profile removed.
        """
        temp = []
        for item in people:
            if item['steam_id'] == profile_steam_id or item['custom_id'] == profile_custom_id:
                continue
            temp.append(item)

        self.__print(f'__remove_self_from_people(profile_steam_id:{profile_steam_id}, profile_custom_id:' +
                     f'{profile_custom_id}, List[people:{len(people)}]) -> List[temp:{len(temp)}]')
        return temp


    def __compare_people_to_battlemetrics_players(self, people: list, battlemetrics_players: list) -> list:
        """
        Compares the list of people with the list of BattleMetrics players and returns those that match by name.

        Args:
            people (list): A list of dictionaries representing people.
            battlemetrics_players (list): A list of names representing BattleMetrics players.

        Returns:
            list: A list of dictionaries containing people who match the names in the BattleMetrics players list.
        """
        name_counts = {}
        for name in battlemetrics_players:
            name_counts[name] = name_counts.get(name, 0) + 1
        temp = []
        for item in people:
            if roster_name_match(item['name'], name_counts)[0]:
                temp.append(item)

        self.__print(f'__compare_people_to_battlemetrics_players(List[people:{len(people)}], ' +
                     f'List[battlemetrics_players:{len(battlemetrics_players)}]) -> List[temp:{len(temp)}]')
        return temp


    def __compare_people_to_already_found_players(self, people: list, found_players: list) -> list:
        """
        Compares the list of people with the already found players and returns those that are not already in the found
        players list.

        Args:
            people (list): A list of dictionaries representing people.
            found_players (list): A list of dictionaries representing already found players.

        Returns:
            list: A list of dictionaries containing people who are not already in the found players list.
        """
        temp = []
        for item in people:
            exist = False
            for i in found_players:
                if item['steam_id'] != None and item['steam_id'] == i['steam_id']:
                    exist = True
                    break

                if item['custom_id'] != None and item['custom_id'] == i['custom_id']:
                    exist = True
                    break

            if not exist:
                temp.append(item)

        self.__print(f'__compare_people_to_already_found_players(List[people:{len(people)}], ' +
                     f'List[found_players:{len(found_players)}]) -> List[temp:{len(temp)}]')
        return temp


    def __get_person_key(self, person: dict) -> str:
        """
        Build a stable key for a Steam person dictionary.

        Args:
            person (dict): Steam person data.

        Returns:
            str: A key using Steam ID first, then Custom ID.
        """
        if person['steam_id'] != None:
            return f'steam:{person["steam_id"]}'
        if person['custom_id'] != None:
            return f'custom:{person["custom_id"]}'
        return f'name:{person["name"]}'


    def __collect_profile_people(self, profile_steam_id: str, include_comments: bool | None = None) -> tuple[str, str, list]:
        """
        Collect friends and comment authors from one Steam profile.

        Args:
            profile_steam_id (str): Steam ID to inspect.

        Returns:
            tuple[str, str, list]: Profile name, custom ID, and discovered people.
        """
        people = []

        profile_name = self.get_steam_profile_name(profile_steam_id)
        profile_custom_id = self.get_steam_profile_custom_id_by_steam_id(profile_steam_id)

        if self.is_steam_profile_friends_public(profile_steam_id):
            people += self.get_steam_profile_friends(profile_steam_id)

        should_search_comments = self.search_comments if include_comments is None else \
            self.search_comments and include_comments
        if should_search_comments and self.search_comments_max_pages > 0 and \
            self.is_steam_profile_comments_public(profile_steam_id):
            number_of_comments = self.get_number_of_comments(profile_steam_id)
            for i in range(1, self.search_comments_max_pages + 1):
                if number_of_comments <= 0: break
                number_of_page_comments, authors = self.get_steam_profile_comments_page_authors(profile_steam_id, i)
                number_of_comments -= number_of_page_comments
                people += authors

        people = self.__remove_duplicates(people)
        people = self.__remove_self_from_people(profile_steam_id, profile_custom_id, people)

        return profile_name, profile_custom_id, people


    def __score_candidate(self, candidate: dict) -> int:
        """
        Score a possible teammate by public evidence.

        Args:
            candidate (dict): Candidate metadata.

        Returns:
            int: Higher means more likely teammate.
        """
        score = len(candidate['connection_profile_ids'])
        score += len(candidate['seed_connection_profile_ids'])
        if 'comments' in candidate['sources']:
            score += 2
        if candidate['online']:
            score += 5
        if candidate['seed']:
            score += 3
        return score


    ##################################################
    #   Public methods
    ##################################################

    def __render_network(self, graph, output_path: str):
        """
        Write a pyvis network graph to disk.
        """
        from pyvis.network import Network

        nt = Network('2000px', '2000px')
        nt.from_nx(graph)
        nt.repulsion(damping=1)
        nt.write_html(output_path, open_browser=False, notebook=False)


    def start_search(self, server_id: str, steam_ids: list, output_network: bool = True,
                     network_output_path: str = 'team_network.html', human_output: bool = True) -> dict:
        """
        Starts the search for interconnected Steam profiles based on provided Steam IDs.

        Args:
            server_id (str): The ID of the server.
            steam_ids (list): A list of Steam IDs to start the search from.
        """
        self.__print(f'start_search(server_id:{server_id}, steam_ids:{len(steam_ids)})')

        if output_network:
            import networkx as nx
            G = nx.Graph()
        else:
            G = None

        battlemetrics_players = self.get_battlemetrics_players(server_id)
        found_players = []
        searched_steam_ids = []
        recursives = 0
        peoples_connections = dict()

        def recursive_search(profile_steam_id: str, recursive_depth: int = 0):
            """
            Recursively searches for interconnected Steam profiles.

            Args:
                profile_steam_id (str): The Steam ID of the profile to start the search from.
                recursive_depth (int): The current recursive depth.
            """
            if recursive_depth == self.recursive_depth:
                return

            nonlocal recursives
            self.__print(f'start_search:recursive_search(profile_steam_id:{profile_steam_id}, ' +
                         f'recursive_depth:{recursive_depth})')

            if profile_steam_id in searched_steam_ids:
                self.__print(f'start_search:recursive_search(profile_steam_id:{profile_steam_id}, ' +
                             f'recursive_depth:{recursive_depth}) -> Already searched')
                return

            recursives += 1

            searched_steam_ids.append(profile_steam_id)
            profile_name, profile_custom_id, people = self.__collect_profile_people(profile_steam_id)

            found_players.append({
                'steam_id': profile_steam_id,
                'custom_id': profile_custom_id,
                'name': profile_name
            })

            peoples_connections[profile_steam_id] = (profile_name, profile_custom_id, people)

            people = self.__compare_people_to_battlemetrics_players(people, battlemetrics_players)

            # Create node connections
            if G != None:
                for item in people:
                    G.add_edges_from([(profile_name, item['name'])])

            people = self.__compare_people_to_already_found_players(people, found_players)

            for item in people:
                steam_id = item['steam_id']
                if steam_id == None:
                    steam_id = self.get_steam_profile_steam_id_by_custom_id(item['custom_id'])
                recursive_search(steam_id, recursive_depth + 1)

            if G != None and recursives == 1:
                G.add_node(profile_name)

        for id in steam_ids:
            recursive_search(id)
            recursives = 0

        for steam_id_outer, (name_outer, custom_id_outer, connections_outer) in peoples_connections.items():
            for steam_id_inner, (name_inner, custom_id_inner, connections_inner) in peoples_connections.items():
                if steam_id_outer == steam_id_inner : continue
                if custom_id_outer != '' and custom_id_outer == custom_id_inner: continue

                steam_id_in_connections = any(steam_id_outer == connection['steam_id'] for connection in
                                              connections_inner)
                custom_id_in_connections = any(custom_id_outer != '' and custom_id_outer == connection['custom_id']
                                               for connection in connections_inner)

                if G != None and (steam_id_in_connections or custom_id_in_connections):
                    G.add_edges_from([(name_outer, name_inner)])

        network_file = None
        if output_network:
            if human_output:
                print('\nTeam Detector Network written to:')

            self.__render_network(G, network_output_path)
            network_file = os.path.abspath(network_output_path)

        if human_output:
            print('\nTeam Detector Result:\n')
            print('Name:'.ljust(34) + 'SteamID:'.ljust(19) + 'Link:')

            for player in found_players:
                print(f'{player["name"]}'.ljust(34) + f'{player["steam_id"]}'.ljust(19) +
                      self.__get_url_steam_profile_by_steam_id(player['steam_id']))

        return {
            'mode': 'search',
            'server_id': server_id,
            'seed_steam_ids': steam_ids,
            'network_file': network_file,
            'inspected_profiles': searched_steam_ids,
            'roster': self.roster_status,
            'warnings': self.fetch_warnings,
            'partial': not self.roster_status.get('available', False),
            'players': [{
                'name': player['name'],
                'steam_id': player['steam_id'],
                'custom_id': player['custom_id'],
                'profile_url': self.__get_url_steam_profile_by_steam_id(player['steam_id'])
            } for player in found_players]
        }


    def start_auto_discovery(self, server_id: str, steam_ids: list, max_profiles: int = AUTO_MAX_PROFILES,
                             min_score: int = AUTO_MIN_SCORE, output_network: bool = True,
                             network_output_path: str = 'team_network.html',
                             human_output: bool = True,
                             max_runtime_seconds: float | None = AUTO_MAX_RUNTIME_SECONDS) -> dict:
        """
        Starts a bounded teammate discovery crawl from one or more seed Steam IDs.

        Args:
            server_id (str): The ID of the server.
            steam_ids (list): A list of seed Steam IDs.
            max_profiles (int): Maximum Steam profiles to inspect.
            min_score (int): Minimum score for non-online candidates to be inspected or printed.
        """
        self.__print(f'start_auto_discovery(server_id:{server_id}, steam_ids:{len(steam_ids)}, ' +
                     f'max_profiles:{max_profiles}, min_score:{min_score})')

        self.get_battlemetrics_players(server_id)
        online_name_counts = self.roster_status.get('name_counts', {})
        seed_ids = set(steam_ids)
        searched_steam_ids = []
        skipped_steam_ids = []
        queued_steam_ids = set(steam_ids)
        queue = [(steam_id, 0, 1000) for steam_id in steam_ids]
        candidates = dict()
        comment_profiles = []
        started_at = self.monotonic_fn()
        runtime_budget = None if max_runtime_seconds is None or max_runtime_seconds <= 0 else max_runtime_seconds
        deadline = None if runtime_budget is None else started_at + runtime_budget
        stop_reason = None

        def ensure_candidate(person: dict) -> dict:
            key = self.__get_person_key(person)
            if key not in candidates:
                candidates[key] = {
                    'steam_id': person['steam_id'],
                    'custom_id': person['custom_id'],
                    'name': person['name'],
                    'sources': set(),
                    'connection_profile_ids': set(),
                    'seed_connection_profile_ids': set(),
                    'connection_profile_names': set(),
                    'online': roster_name_match(person['name'], online_name_counts)[0],
                    'online_confidence': roster_name_match(person['name'], online_name_counts)[1],
                    'seed': person['steam_id'] in seed_ids,
                    'inspected': False
                }

            candidate = candidates[key]
            if candidate['steam_id'] == None and person['steam_id'] != None:
                candidate['steam_id'] = person['steam_id']
            if candidate['custom_id'] == None and person['custom_id'] != None:
                candidate['custom_id'] = person['custom_id']
            if candidate['name'] == '' and person['name'] != '':
                candidate['name'] = person['name']
            online, confidence = roster_name_match(person['name'], online_name_counts)
            candidate['online'] = candidate['online'] or online
            if confidence == 'exact_unique' or candidate['online_confidence'] is None:
                candidate['online_confidence'] = confidence
            candidate['seed'] = candidate['seed'] or person['steam_id'] in seed_ids
            return candidate

        while len(queue) > 0 and len(searched_steam_ids) < max_profiles:
            if deadline is not None and self.monotonic_fn() >= deadline:
                stop_reason = 'runtime_budget'
                warning = f'Team discovery reached its {runtime_budget:g}s runtime budget; returning a partial result.'
                if warning not in self.fetch_warnings:
                    self.fetch_warnings.append(warning)
                break
            queue.sort(key=lambda item: (-item[2], item[1], item[0]))
            profile_steam_id, depth, queued_score = queue.pop(0)
            if profile_steam_id in searched_steam_ids:
                continue
            if depth > self.recursive_depth:
                continue

            self.__print(f'start_auto_discovery:inspect(profile_steam_id:{profile_steam_id}, depth:{depth})')
            searched_steam_ids.append(profile_steam_id)

            try:
                include_comments = self.search_comments and (depth == 0 or queued_score >= 4)
                if include_comments:
                    comment_profiles.append(profile_steam_id)
                profile_name, profile_custom_id, people = self.__collect_profile_people(
                    profile_steam_id, include_comments=include_comments)
            except SystemExit as error:
                skipped_steam_ids.append(profile_steam_id)
                warning = f'Skipped Steam profile {profile_steam_id}: {error or "request unavailable"}'
                if warning not in self.fetch_warnings:
                    self.fetch_warnings.append(warning)
                continue

            profile_candidate = ensure_candidate({
                'steam_id': profile_steam_id,
                'custom_id': profile_custom_id,
                'name': profile_name,
                'type': 'seed' if profile_steam_id in seed_ids else 'discovered'
            })
            profile_candidate['inspected'] = True

            for person in people:
                candidate = ensure_candidate(person)
                candidate['sources'].add(person['type'])
                candidate['connection_profile_ids'].add(profile_steam_id)
                candidate['connection_profile_names'].add(profile_name)
                if profile_steam_id in seed_ids:
                    candidate['seed_connection_profile_ids'].add(profile_steam_id)

                score = self.__score_candidate(candidate)
                next_steam_id = candidate['steam_id']
                if next_steam_id == None and (candidate['online'] or score >= min_score) and \
                    candidate['custom_id'] != None:
                    try:
                        next_steam_id = self.get_steam_profile_steam_id_by_custom_id(candidate['custom_id'])
                    except SystemExit as error:
                        warning = f'Skipped Steam vanity {candidate["custom_id"]}: {error or "request unavailable"}'
                        if warning not in self.fetch_warnings:
                            self.fetch_warnings.append(warning)
                        continue
                    candidate['steam_id'] = next_steam_id

                if next_steam_id != None and next_steam_id not in queued_steam_ids and \
                    next_steam_id not in searched_steam_ids and depth < self.recursive_depth and \
                    (candidate['online'] or score >= min_score):
                    queue.append((next_steam_id, depth + 1, score))
                    queued_steam_ids.add(next_steam_id)

        if stop_reason is None and len(queue) > 0 and len(searched_steam_ids) >= max_profiles:
            stop_reason = 'profile_budget'
        elapsed_seconds = max(0.0, self.monotonic_fn() - started_at)
        truncated = len(queue) > 0

        if output_network:
            import networkx as nx
            G = nx.Graph()
        else:
            G = None
        visible_candidates = []
        for candidate in candidates.values():
            candidate['score'] = self.__score_candidate(candidate)
            if candidate['seed'] or candidate['inspected'] or candidate['online'] or candidate['score'] >= min_score:
                visible_candidates.append(candidate)
                if G != None:
                    G.add_node(candidate['name'])

        visible_names = set(candidate['name'] for candidate in visible_candidates)
        if G != None:
            for candidate in visible_candidates:
                for profile_name in candidate['connection_profile_names']:
                    if profile_name in visible_names:
                        G.add_edges_from([(profile_name, candidate['name'])])

        network_file = None
        if output_network:
            if human_output:
                print('\nTeam Detector Auto Network written to:')

            self.__render_network(G, network_output_path)
            network_file = os.path.abspath(network_output_path)

        visible_candidates = sorted(
            visible_candidates,
            key=lambda item: (item['seed'], item['online'], item['score'], item['name']),
            reverse=True
        )

        if human_output:
            print('\nTeam Detector Auto Result:\n')
            print('Name:'.ljust(34) + 'SteamID:'.ljust(19) + 'Score:'.ljust(8) +
                  'Online:'.ljust(9) + 'Sources:')

            for player in visible_candidates:
                steam_id = '' if player['steam_id'] == None else player['steam_id']
                sources = ','.join(sorted(player['sources']))
                print(f'{player["name"]}'.ljust(34) + f'{steam_id}'.ljust(19) +
                      f'{player["score"]}'.ljust(8) + f'{player["online"]}'.ljust(9) + sources)

            print(f'\nInspected {len(searched_steam_ids)} Steam profiles. ' +
                  f'Candidates shown with score >= {min_score}, online on server, inspected, or seed.')

        return {
            'mode': 'auto_discovery',
            'server_id': server_id,
            'seed_steam_ids': steam_ids,
            'network_file': network_file,
            'inspected_profiles': searched_steam_ids,
            'skipped_profiles': skipped_steam_ids,
            'min_score': min_score,
            'max_profiles': max_profiles,
            'crawl': {
                'comments_enabled': self.search_comments,
                'comment_pages': self.search_comments_max_pages,
                'comment_profiles': len(comment_profiles),
                'recursive_depth': self.recursive_depth,
                'min_score': min_score,
                'max_profiles': max_profiles,
                'max_runtime_seconds': runtime_budget,
                'elapsed_seconds': round(elapsed_seconds, 3),
                'truncated': truncated,
                'stop_reason': stop_reason,
                'frontier_remaining': len(queue)
            },
            'fetch_stats': self.fetch_stats,
            'warnings': self.fetch_warnings,
            'roster': self.roster_status,
            'partial': self.fetch_stats.get('failed', 0) > 0 or len(skipped_steam_ids) > 0 or
                       not self.roster_status.get('available', False) or
                       not self.roster_status.get('complete', False) or truncated,
            'candidates': [{
                'name': candidate['name'],
                'steam_id': candidate['steam_id'],
                'custom_id': candidate['custom_id'],
                'score': candidate['score'],
                'online': candidate['online'],
                'online_confidence': candidate['online_confidence'],
                'seed': candidate['seed'],
                'inspected': candidate['inspected'],
                'sources': sorted(candidate['sources']),
                'connection_profile_ids': sorted(candidate['connection_profile_ids']),
                'seed_connection_profile_ids': sorted(candidate['seed_connection_profile_ids']),
                'connection_profile_names': sorted(candidate['connection_profile_names']),
                'profile_url': None if candidate['steam_id'] == None else
                self.__get_url_steam_profile_by_steam_id(candidate['steam_id'])
            } for candidate in visible_candidates]
        }


    def get_battlemetrics_players(self, server_id: str) -> list:
        """
        Retrieve a list of players currently connected to a server from the BattleMetrics API.

        Args:
            server_id (str): The ID of the server to retrieve player information for.

        Returns:
            list: A list of player names currently connected to the server.
        """
        self.__print(f'get_battlemetrics_players(server_id:{server_id})')

        if self.player_roster is not None:
            source = str(self.player_roster.get('source') or 'rustplusplus_snapshot')
            available = bool(self.player_roster.get('available'))
            raw_players = self.player_roster.get('players', [])
            players = [self.__clean_name(player) for player in raw_players if isinstance(player, str) and player != '']
            if not available:
                players = []
            name_counts = {}
            for player in players:
                name_counts[player] = name_counts.get(player, 0) + 1
            self.roster_status = {
                'source': source,
                'capability': str(self.player_roster.get('capability') or 'names_only'),
                'available': available,
                'complete': bool(self.player_roster.get('complete', False)),
                'observed_at': self.player_roster.get('observedAt'),
                'size': len(players),
                'population': self.player_roster.get('population'),
                'unique_names': len(name_counts),
                'name_counts': name_counts,
                'reason': self.player_roster.get('reason')
            }
            if not available:
                warning = f'Player roster unavailable from {source}: {self.roster_status["reason"] or "unknown reason"}'
                if warning not in self.fetch_warnings:
                    self.fetch_warnings.append(warning)
            self.__print(f'get_battlemetrics_players(server_id:{server_id}) -> {source}[{len(players)}]')
            return players

        try:
            content = self.__request(self.__get_url_battlemetrics(server_id))
            if content == '':
                raise RuntimeError('BattleMetrics returned no usable player roster')
            content = json.loads(content)
            players = [self.__clean_name(player['attributes']['name']) for player in content.get('included', [])
                       if player.get('type') == 'player']
            name_counts = {}
            for player in players:
                name_counts[player] = name_counts.get(player, 0) + 1
            self.roster_status = {
                'source': 'battlemetrics',
                'capability': 'names_only',
                'available': True,
                'complete': True,
                'observed_at': None,
                'size': len(players),
                'unique_names': len(name_counts),
                'name_counts': name_counts,
                'reason': None
            }
            self.__print(f'get_battlemetrics_players(server_id:{server_id}) -> List[players:{len(players)}]')
            return players
        except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self.roster_status.update({'available': False, 'reason': str(error)})
            warning = f'BattleMetrics roster unavailable: {error}'
            if warning not in self.fetch_warnings:
                self.fetch_warnings.append(warning)
            return []


    def get_steam_profile_steam_id_by_custom_id(self, custom_id: str) -> str:
        """
        Retrieve the Steam ID associated with a Custom ID.

        Args:
            custom_id (str): The Custom ID of the profile.

        Returns:
            str: The Steam ID associated with the Custom ID.
        """
        self.__print(f'get_steam_profile_steam_id_by_custom_id(custom_id:{custom_id})')

        if custom_id in self.custom_id_translation_table:
            steam_id = self.custom_id_translation_table[custom_id]
            self.__print(f'get_steam_profile_steam_id_by_custom_id(custom_id:{custom_id}) -> steam_id:{steam_id}')
            return steam_id

        content = self.__get_steam_profile_content_by_custom_id(custom_id)
        steam_id = self.__get_steam_profile_steam_id_by_content(content)

        self.__print(f'get_steam_profile_steam_id_by_custom_id(custom_id:{custom_id}) -> steam_id:{steam_id}')
        return steam_id


    def get_steam_profile_custom_id_by_steam_id(self, steam_id: str) -> str:
        """
        Retrieve the Custom ID associated with a Steam ID.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            str: The Custom ID associated with the Steam ID.
        """
        self.__print(f'get_steam_profile_custom_id_by_steam_id(steam_id:{steam_id})')

        for key, value in self.custom_id_translation_table.items():
            if steam_id == value:
                self.__print(f'get_steam_profile_custom_id_by_steam_id(steam_id:{steam_id}) -> custom_id:{key}')
                return key

        content = self.__get_steam_profile_content_by_steam_id(steam_id)
        custom_id = self.__get_steam_profile_custom_id_by_content(content)
        self.__print(f'get_steam_profile_custom_id_by_steam_id(steam_id:{steam_id}) -> custom_id:{custom_id}')
        return custom_id


    def get_steam_profile_name(self, steam_id: str) -> str:
        """
        Retrieve the name of a Steam profile by its Steam ID.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            str: The name of the Steam profile.
        """
        self.__print(f'get_steam_profile_name(steam_id:{steam_id})')

        content = self.__get_steam_profile_content_by_steam_id(steam_id)
        regex = r'<div class="persona_name" style="font-size: 24px;">.*?<span class="actual_persona_name">(.*?)<\/span>'
        name = re.findall(regex, content, re.MULTILINE|re.S)
        name = '' if len(name) == 0 else name[0]
        name = self.__clean_name(name)
        self.__print(f'get_steam_profile_name(steam_id:{steam_id}) -> name:{name}')
        return name


    def is_steam_profile_friends_public(self, steam_id: str) -> bool:
        """
        Check if a Steam profile's friends list is public based on the content of the profile page.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            bool: True if the friends list is public, False otherwise.
        """
        self.__print(f'is_steam_profile_friends_public(steam_id:{steam_id})')

        content = self.__get_steam_profile_content_by_steam_id(steam_id)
        content_no_space = re.sub(r'\s+', '', content)
        value = '/friends/"><spanclass="count_link_label">friends</span>' in content_no_space.lower()
        self.__print(f'is_steam_profile_friends_public(steam_id:{steam_id}) -> bool:{value}')
        return value


    def is_steam_profile_comments_public(self, steam_id: str) -> bool:
        """
        Check if a Steam profile's comments section is public based on the content of the profile page.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            bool: True if the comments section is public, False otherwise.
        """
        self.__print(f'is_steam_profile_comments_public(steam_id:{steam_id})')

        content = self.__get_steam_profile_content_by_steam_id(steam_id)
        content_no_space = re.sub(r'\s+', '', content)
        value = '<spanclass="commentthread_header_label">comments</span>' in content_no_space.lower()
        self.__print(f'is_steam_profile_comments_public(steam_id:{steam_id}) -> bool:{value}')
        return value


    def get_number_of_comments(self, steam_id: str) -> int:
        """
        Get the number of comments on a Steam profile based on the provided Steam ID.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            int: The number of comments on the Steam profile.
        """
        self.__print(f'get_number_of_comments(steam_id:{steam_id})')

        if not self.is_steam_profile_comments_public(steam_id):
            self.__print(f'get_number_of_comments(steam_id:{steam_id}) -> int:0')
            return 0

        content = self.__get_steam_profile_content_by_steam_id(steam_id)
        content_no_space = re.sub(r'\s+', '', content)
        matches = re.findall(r'<spanid="commentthread_profile_\d+_totalcount">(.*?)<\/span>', content_no_space.lower())

        try:
            number = 0 if len(matches) == 0 else int(re.sub(r'[^0-9]', '', matches[0]))
            self.__print(f'get_number_of_comments(steam_id:{steam_id}) -> int:{number}')
            return number
        except Exception as e:
            self.__print(f'get_number_of_comments(steam_id:{steam_id}) -> int:0')
            return 0


    def get_steam_profile_friends(self, steam_id: str) -> list:
        """
        Retrieve the friends list of a Steam profile based on the provided Steam ID.

        Args:
            steam_id (str): The Steam ID of the profile.

        Returns:
            list: A list of dictionaries containing friend information such as steam_id, custom_id, name and type
            ('friends'/'comments').
        """
        self.__print(f'get_steam_profile_friends(steam_id:{steam_id})')

        content = self.__get_steam_profile_friends_content_by_steam_id(steam_id)
        regex = r'data-steamid="(.+?)".*?href="https:\/\/steamcommunity.com\/(.+?)">.*?' + \
                r'<div class="friend_block_content">(.+?)<br>'
        matches = re.findall(regex, content, re.MULTILINE|re.S)

        friends = []
        for friend_steam_id, friend_custom_id, friend_name in matches:
            friend = dict()
            friend['steam_id'] = friend_steam_id

            custom_id = friend_custom_id.replace('id/', '') if friend_custom_id.startswith('id') else None
            if custom_id != None and custom_id not in self.custom_id_translation_table:
                self.custom_id_translation_table[custom_id] = friend_steam_id
            friend['custom_id'] = custom_id

            friend['name'] = self.__clean_name(friend_name)
            friend['type'] = 'friends'
            friends.append(friend)

        self.__print(f'get_steam_profile_friends(steam_id:{steam_id}) -> List[friends:{len(friends)}]')
        return friends


    def get_steam_profile_comments_page_authors(self, steam_id: str, page: int = 1) -> tuple:
        """
        Retrieve the authors of comments on a specific page of a Steam profile based on the provided Steam ID and page number.

        Args:
            steam_id (str): The Steam ID of the profile.
            page (int): The page number of comments. Defaults to 1.

        Returns:
            tuple: A tuple containing the total number of comments read and a list of dictionaries containing comment
            author information such as steam_id, custom_id, name, and type ('comments' or 'friends').
        """
        self.__print(f'get_steam_profile_comments_page_authors(steam_id:{steam_id}, page:{page})')

        content = self.__get_steam_profile_comments_page_content_by_steam_id(steam_id, page)

        comments_page_authors = []
        regex = r'hoverunderline commentthread_author_link"\s+' \
                r'href="https://steamcommunity.com/(profiles|id)/([^"/]+)"\s+' \
                r'data-miniprofile="(\d+)".*?<bdi>(.*?)<\/bdi>'
        author_matches = re.findall(regex, content, re.MULTILINE|re.S)

        for profile_kind, profile_value, account_id, author_name in author_matches:
            author_steam_id = profile_value if profile_kind == 'profiles' else \
                str(76561197960265728 + int(account_id))
            author_custom_id = profile_value if profile_kind == 'id' else None
            if any(author['steam_id'] == author_steam_id for author in comments_page_authors):
                continue

            if author_custom_id != None:
                self.custom_id_translation_table[author_custom_id] = author_steam_id
            comments_page_authors.append({
                'steam_id': author_steam_id,
                'custom_id': author_custom_id,
                'name': self.__clean_name(author_name),
                'type': 'comments'
            })

        if len(author_matches) == 0:
            legacy_regex = r'hoverunderline commentthread_author_link"\s+' \
                           r'href="https://steamcommunity.com/(profiles|id)/([^"/]+)".*?<bdi>(.*?)<\/bdi>'
            for profile_kind, profile_value, author_name in re.findall(
                    legacy_regex, content, re.MULTILINE|re.S):
                comments_page_authors.append({
                    'steam_id': profile_value if profile_kind == 'profiles' else None,
                    'custom_id': profile_value if profile_kind == 'id' else None,
                    'name': self.__clean_name(author_name),
                    'type': 'comments'
                })

        total_read_comments = len(author_matches) if len(author_matches) > 0 else len(comments_page_authors)

        self.__print(f'get_steam_profile_comments_page_authors(steam_id:{steam_id}, page:{page}) -> ' +
                     f'total_read_comments:{total_read_comments}, List[comments_page_authors:' +
                     f'{len(comments_page_authors)}]')
        return total_read_comments, comments_page_authors


def read_config() -> tuple[str, list[str]]:
    """
    Read configuration from a JSON file and return the BattleMetrics ID and Steam ID(s).

    Returns:
        Tuple[str, List[str]]: A tuple containing BattleMetrics ID (str) and Steam ID (List[str]).
    """
    battlemetrics_id = None
    steam_id = None

    if os.path.isfile(JSON_FILE) and os.access(JSON_FILE, os.R_OK):
        with open(JSON_FILE, 'r') as f:
            jsonFile = json.load(f)

            if 'battlemetrics_id' in jsonFile:
                battlemetrics_id = jsonFile['battlemetrics_id']

            if 'steam_id' in jsonFile:
                steam_id = jsonFile['steam_id']

    return battlemetrics_id, steam_id


def write_config(battlemetrics_id: str, steam_id: list) -> None:
    """
    Write BattleMetrics ID and Steam ID(s) to a JSON file.

    Args:
        battleMetrics_id (str): The BattleMetrics ID to be written to the config.
        steam_id (list): The Steam ID(s) to be written to the config.
    """
    with open(JSON_FILE, 'w') as f:
        jsonFile = dict()
        jsonFile['battlemetrics_id'] = battlemetrics_id
        jsonFile['steam_id'] = steam_id
        json.dump(jsonFile, f)


def main():
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-b', '--battlemetrics-id', type=str, required=False, help='BattleMetrics Server ID.')
    parser.add_argument('-s', '--steam-id', type=str, nargs='+', required=False,
                        help='SteamID(s) of the person(s) you want to inspect (Separated by space).')
    parser.add_argument('-r', '--recursive-depth', type=int, required=False,
                        help=f'How deep can the recursive search go? (Default {RECURSIVE_DEPTH}).')
    parser.add_argument('-c', '--comments', action='store_true', required=False,
                        help='Search through profile comments.')
    parser.add_argument('-p', '--comment-pages', type=int, required=False,
                        help='The number of comment pages to go through per profile (Default 1 page).')
    parser.add_argument('-a', '--auto-discover', action='store_true', required=False,
                        help='Auto-discover likely teammates from seed SteamID(s), friends, comments, and server names.')
    parser.add_argument('--auto-max-profiles', type=int, required=False,
                        help=f'Maximum Steam profiles to inspect in auto-discover mode (Default {AUTO_MAX_PROFILES}).')
    parser.add_argument('--auto-min-score', type=int, required=False,
                        help=f'Minimum score for non-online candidates in auto-discover mode (Default {AUTO_MIN_SCORE}).')
    parser.add_argument('--auto-max-runtime-seconds', type=float, required=False,
                        help=f'Maximum runtime for auto-discover before returning partial results '
                             f'(Default {AUTO_MAX_RUNTIME_SECONDS:g}s).')
    parser.add_argument('--request-delay', type=float, required=False,
                        help='Delay in seconds between web requests (Default 0).')
    parser.add_argument('--cache-path', type=str, required=False,
                        help='Persistent SQLite HTTP cache path.')
    parser.add_argument('--request-retries', type=int, required=False, default=3,
                        help='Retry count for rate limits and transient HTTP errors (Default 3).')
    parser.add_argument('--battlemetrics-players-file', type=str, required=False,
                        help='JSON list of current player names supplied by Rust++.')
    parser.add_argument('--player-roster-file', type=str, required=False,
                        help='Source-aware JSON player roster supplied by Rust++.')
    parser.add_argument('--json', action='store_true', required=False,
                        help='Print machine-readable JSON result. Suppresses human output.')
    parser.add_argument('--no-network', action='store_true', required=False,
                        help='Do not write the pyvis network HTML file.')
    parser.add_argument('--network-output', type=str, required=False, default='team_network.html',
                        help='Path for the generated network HTML file (Default team_network.html).')
    parser.add_argument('--no-config', action='store_true', required=False,
                        help='Do not read or write team_detector.json.')
    parser.add_argument('-d', '--debug', action='store_true', required=False, help='Enables debug print.')
    args = parser.parse_args()

    battlemetrics_id = args.battlemetrics_id
    steam_id = args.steam_id
    recursive_depth = RECURSIVE_DEPTH if args.recursive_depth == None else args.recursive_depth
    comments = args.comments
    comment_pages = COMMENT_PAGES if args.comment_pages == None else args.comment_pages
    auto_discover = args.auto_discover
    auto_max_profiles = AUTO_MAX_PROFILES if args.auto_max_profiles == None else args.auto_max_profiles
    auto_min_score = AUTO_MIN_SCORE if args.auto_min_score == None else args.auto_min_score
    auto_max_runtime_seconds = AUTO_MAX_RUNTIME_SECONDS if args.auto_max_runtime_seconds == None else \
        args.auto_max_runtime_seconds
    request_delay = 0.0 if args.request_delay == None else args.request_delay
    cache_path = args.cache_path
    request_retries = args.request_retries
    battlemetrics_players = None
    player_roster = None
    if args.player_roster_file:
        try:
            with open(args.player_roster_file, encoding='utf-8') as snapshot_file:
                player_roster = json.load(snapshot_file)
            if not isinstance(player_roster, dict):
                raise ValueError('snapshot must be a JSON object')
            players = player_roster.get('players', [])
            if not isinstance(players, list) or not all(isinstance(player, str) for player in players):
                raise ValueError('snapshot players must be a JSON array of names')
            if not isinstance(player_roster.get('available'), bool):
                raise ValueError('snapshot available must be boolean')
        except (OSError, ValueError, json.JSONDecodeError) as error:
            sys.exit(f'Could not read player roster snapshot: {error}')
    if args.battlemetrics_players_file:
        try:
            with open(args.battlemetrics_players_file, encoding='utf-8') as snapshot_file:
                snapshot = json.load(snapshot_file)
            if not isinstance(snapshot, list) or not all(isinstance(player, str) for player in snapshot):
                raise ValueError('snapshot must be a JSON array of player names')
            battlemetrics_players = snapshot
        except (OSError, ValueError, json.JSONDecodeError) as error:
            sys.exit(f'Could not read BattleMetrics player snapshot: {error}')
    json_output = args.json
    output_network = not args.no_network
    network_output_path = args.network_output
    use_config = not args.no_config
    debug = args.debug

    config_battlemetrics_id, config_steam_id = (None, None)
    if use_config:
        config_battlemetrics_id, config_steam_id = read_config()

    if battlemetrics_id == None:
        battlemetrics_id = config_battlemetrics_id
    if steam_id == None:
        steam_id = config_steam_id

    if battlemetrics_id == None or steam_id == None:
        sys.exit('BattleMetrics Server ID or Steam ID is not provided.')

    if debug and not json_output:
        print('Running with the following arguments:')
        print(f' - Battlemetrics Server ID:     {battlemetrics_id}')
        print(f' - Steam ID(s):                 {steam_id}')
        print(f' - Recursive Depth:             {recursive_depth}')
        print(f' - Comments:                    {comments}')
        print(f' - Comment Pages:               {comment_pages}')
        print(f' - Auto Discover:               {auto_discover}')
        print(f' - Auto Max Profiles:           {auto_max_profiles}')
        print(f' - Auto Min Score:              {auto_min_score}')
        print(f' - Auto Max Runtime Seconds:    {auto_max_runtime_seconds}')
        print(f' - Request Delay:               {request_delay}')
        print(f' - JSON Output:                 {json_output}')
        print(f' - Network Output:              {output_network}')
        print(f' - Network Output Path:         {network_output_path}')
        print(f' - Config:                      {use_config}')
        print(f' - Debug:                       {debug}')
        print()

    td = TeamDetector(debug and not json_output, recursive_depth, comments, comment_pages, request_delay,
                      cache_path, request_retries, battlemetrics_players, player_roster)
    if auto_discover:
        result = td.start_auto_discovery(battlemetrics_id, steam_id, auto_max_profiles, auto_min_score,
                                         output_network, network_output_path, not json_output,
                                         auto_max_runtime_seconds)
    else:
        result = td.start_search(battlemetrics_id, steam_id, output_network, network_output_path, not json_output)

    if json_output:
        print(json.dumps(result, ensure_ascii=False))

    if use_config:
        write_config(battlemetrics_id, steam_id)


if __name__ == '__main__':
    main()
