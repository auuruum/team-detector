# Team-Detector
Team detection program for Rust player rosters and Steam. The program accepts a source-aware current-player snapshot
from Rust++ or uses BattleMetrics when it is available. Then it goes through the Steam profile of the player
you want to inspect and compares the friend list names and profile comments with the current-player roster to
find out which friends are currently on the server. If the program found any matches, it will then continue to go
through the friend list of those friends and so on. What you end up with is a table of all the players that might be
part of the same team as the player you provided the Steam Profile. It will also create a .html file that visualize the
friends network to see who is friends with who etc...

# Clone and Setup
**Tested with Python version: 3.12.1**
<br>
To clone and setup the repository:
```bash
$ git clone https://github.com/alexemanuelol/team-detector.git
$ cd team-detector
$ uv sync
```

# Usage

| Argument                      | Description                                                               |
|-------------------------------|---------------------------------------------------------------------------|
| -h, --help                    | Display help message.                                                     |
| -b, --battlemetrics-id ID     | BattleMetrics Server ID.                                                  |
| -s, --steam-id ID             | SteamID(s) of the person(s) you want to inspect (Separated by space).     |
| -r, --recursive-depth NUMBER  | How deep can the recursive search go? (Default 5)                         |
| -c, --comments                | Search through profile comments (Default False).                          |
| -p, --comment-pages PAGES     | The number of comment pages to go through per profile (Default 1 page).   |
| -a, --auto-discover           | Auto-discover likely teammates from seed SteamID(s), friends, comments, and roster names. |
| --auto-max-profiles NUMBER    | Maximum Steam profiles to inspect in auto-discover mode (Default 75).      |
| --auto-min-score NUMBER       | Minimum score for non-online candidates in auto-discover mode (Default 4). |
| --player-roster-file FILE     | Source-aware current-player JSON snapshot supplied by Rust++.             |
| --request-delay SECONDS       | Delay between web requests if you want to be gentler with rate limits.     |
| --json                        | Print machine-readable JSON and suppress human table output.               |
| --no-network                  | Do not write the pyvis network HTML file.                                  |
| --network-output PATH         | Write the pyvis network HTML file to this path.                            |
| --no-config                   | Do not read or write team_detector.json. Useful for integrations.          |
| -d, --debug                   | Enables debug print (Default False).                                      |

<br>
When you run the program once, the Battlemetrics Server ID and SteamID will be saved in team_detector.json. That means that next time you want to run the program, if you don't provide the -s or -b flags, the values in the json file will be used.

![Image of the command output for a Rust Server](images/command_image.png)

![Image of the network](images/network_image.png)

You can download the windows executable from [releases](https://github.com/alexemanuelol/team-detector/releases) page and run the .exe file like so:

```bash
$ team_detector.exe -b 11378166 -s 76561198114074446
```

Auto-discover mode lets you provide one or a few seed Steam IDs instead of a large manually collected list. It inspects
their public friends and, when `--comments` is enabled, profile comment authors. Candidates are scored higher when they
are currently visible in the selected server roster, are connected to multiple inspected profiles, or appear in
comments.

```bash
$ uv run python team_detector.py -a -b 11378166 -s 76561198114074446 -c --comment-pages 2 --auto-max-profiles 100 --request-delay 0.5
```

For integrations such as rustplusplus, use JSON mode:

```bash
$ uv run python team_detector.py -a -b 11378166 -s 76561198114074446 --json --no-network --no-config
```

# Notes
The program can only mark players online when the selected source provides a current roster. A2S snapshots contain
display names but no Steam IDs, so duplicate names are reported as ambiguous rather than treated as permanent identity
bindings. If the server hides `A2S_PLAYER`, or a Steam profile has private friends and comments, results may be partial.
