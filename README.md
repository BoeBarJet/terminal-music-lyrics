# lyrics-overlay

Karaoke-style now-playing lyrics for your terminal. Watches whatever's
playing (Spotify, VLC, etc., via `playerctl`/MPRIS) and shows the current
lyric line in big banner text, advancing in sync with playback position.

## Requirements

- `playerctl` (already installed on this machine)
- `python-pyfiglet` (installed via pip for this project)
- Python 3

## Usage

```
python3 lyrics_overlay.py
```

Play some music. Lyrics show a few words at a time, big and centered both
horizontally and vertically in your terminal. The banner font auto-shrinks
to fit small terminal windows (falling back to plain wrapped text if even
the smallest font won't fit), and resizing the terminal updates it live.
Pausing shows a small `[paused]` tag. Press Ctrl+C to quit.

## Lyrics source

Lyrics come from the free [lrclib.net](https://lrclib.net) API, matched by
artist + title:

- If time-synced (LRC) lyrics exist, the display advances line-by-line with
  playback.
- If only plain lyrics exist, they're printed in full with a note that
  timing isn't available.
- If nothing is found, it says so.

Obscure tracks or unusual metadata may not match.
