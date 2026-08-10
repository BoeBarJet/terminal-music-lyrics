# terminal-music-lyrics

Karaoke-style now-playing lyrics for your terminal. It watches whatever's
playing on any MPRIS-compatible media player (Spotify, VLC, Rhythmbox,
etc.), fetches time-synced lyrics, and displays them as big, centered
banner text that advances in step with playback — like a lightweight,
terminal-native karaoke screen.

## Features

- **Auto-detects what's playing** via `playerctl`/MPRIS — just start a song, no manual setup per track.
- **Time-synced lyrics** from the free [lrclib.net](https://lrclib.net) API, shown a few words at a time and paced to match the song.
- **Big, centered banner text**, rendered with [pyfiglet](https://github.com/pwaller/pyfiglet), that adapts to your terminal size and falls back to plain wrapped text if the window is too small for banner art.
- **Matches your terminal's theme** — queries the terminal for its actual foreground color (via the OSC 10 escape sequence) instead of using a hardcoded color.
- **Fast**: lyric search runs several query variants in parallel, network requests are forced to IPv4 (works around broken/absent IPv6 routes, which can otherwise silently add seconds of latency to every request), and results are cached locally so replaying a song is instant.
- **Tolerant of messy player metadata** — strips things like `(feat. X)`, `- Remastered 2011`, and multi-artist credits before searching, since real-world track tags rarely match lyric databases exactly.
- Falls back gracefully at every step: unsynced lyrics if no timed version exists, a small note if nothing is found at all.

## Requirements

- Linux (or another OS with MPRIS support)
- [`playerctl`](https://github.com/altdesktop/playerctl)
- Python 3.10+
- [`pyfiglet`](https://pypi.org/project/pyfiglet/)

## Installation

```bash
git clone https://github.com/BoeBarJet/terminal-music-lyrics.git
cd terminal-music-lyrics

# playerctl (if you don't already have it)
sudo pacman -S playerctl        # Arch
sudo apt install playerctl      # Debian/Ubuntu

# pyfiglet
pip install --user pyfiglet     # add --break-system-packages on Arch or other externally-managed setups

chmod +x lyrics_overlay.py
```

To launch it by just typing `terminal-lyrics` from anywhere, symlink it into
a directory on your `$PATH` (`~/.local/bin` is usually already on it):

```bash
ln -s "$(pwd)/lyrics_overlay.py" ~/.local/bin/terminal-lyrics
```

## Usage

Start playing music in any MPRIS-compatible player, then run:

```bash
terminal-lyrics
```

(or `python3 lyrics_overlay.py` if you skipped the symlink step)

Lyrics appear automatically once a track is detected, and update live as
you skip tracks, pause, seek, or resize the terminal. `Ctrl+C` to quit.

## How it works

- **Now-playing detection** polls `playerctl` for the current artist,
  title, status, and position every 0.5s (a couple of lightweight
  subprocess calls). Between polls, the playback position is interpolated
  from the system clock, so line changes and terminal resizes are picked
  up every 50ms without hammering `playerctl`.
- **Lyrics search** queries lrclib.net across a few cleaned-up variants of
  the artist/title in parallel, preferring a time-synced result whose
  artist actually matches.
- **Sync**: LRC only gives one timestamp per full line, not per word. Each
  line is split into short chunks (one word at a time), and each chunk's
  on-screen duration is estimated from its length and capped to the actual
  gap before the next line — so a short line followed by a long
  instrumental break doesn't get its words stretched out to fill the gap.
- **Caching**: results are stored at `~/.cache/lyrics-overlay/cache.json`,
  so replaying a song is instant and needs no network access.

## Configuration

A few constants near the top of `lyrics_overlay.py` are easy to tune:

| Constant | What it controls |
|---|---|
| `MAX_WORDS_PER_CHUNK` | How many words are shown on screen at once |
| `BIG_FONTS` | Which [pyfiglet font](http://www.figlet.org/examples.html) is used for the banner text |
| `RESYNC_INTERVAL` / `TICK_INTERVAL` | How often playback state is polled / the display is checked for updates |
| `DEFAULT_TEXT_COLOR` | Fallback text color if terminal theme detection fails |

## Troubleshooting

- **`playerctl not found on PATH`** — install it (see [Installation](#installation)).
- **`No lyrics found for this track.`** — lrclib.net doesn't have every
  track; obscure releases or unusual metadata may not match.
- **Falls back to small plain text** — the terminal window is too narrow
  for the banner font to fit that song's longest word; try a wider
  terminal.
- **First lookup for a track is slow** — only on a cache miss; every
  subsequent play of that track is instant.
