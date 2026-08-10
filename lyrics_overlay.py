#!/usr/bin/env python3
"""Karaoke-style now-playing lyrics overlay for the terminal.

Watches MPRIS-compatible players (Spotify, VLC, etc.) via playerctl,
fetches time-synced lyrics from lrclib.net, and shows the current line
in big banner text that advances in step with playback position.
"""
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import termios
import textwrap
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import pyfiglet

# Some networks/hosts have a broken or absent IPv6 route while DNS still
# returns AAAA records. Python's socket connection tries addresses in the
# order getaddrinfo returns them (IPv6 first) and, unlike curl's "happy
# eyeballs", tries them one at a time — so a dead IPv6 route can add several
# seconds of connect-timeout delay to *every* request before it falls back
# to IPv4. Forcing IPv4-only here made lrclib.net requests go from ~8s to
# ~0.15s on this machine.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(*args, **kwargs):
    return [ai for ai in _orig_getaddrinfo(*args, **kwargs) if ai[0] == socket.AF_INET]


socket.getaddrinfo = _ipv4_only_getaddrinfo

LRCLIB_SEARCH = "https://lrclib.net/api/search"
USER_AGENT = "lyrics-overlay/1.0 (personal terminal app)"
SEARCH_TIMEOUT = 4
CACHE_PATH = os.path.expanduser("~/.cache/lyrics-overlay/cache.json")
CACHE_VERSION = 4
# playerctl calls (~5-10ms each) are only made every RESYNC_INTERVAL; in between,
# position is interpolated from the system clock so line changes and terminal
# resizes are checked (and redrawn) every TICK_INTERVAL instead of lagging
# behind the next subprocess poll.
RESYNC_INTERVAL = 0.5
TICK_INTERVAL = 0.05
# Single fixed font, always — no switching between styles as the terminal
# is resized. If it doesn't fit at the current terminal size (pick_font()
# checks this against the actual lyrics), render_big() falls back to plain
# wrapped bold text rather than a different banner font.
BIG_FONTS = ["ansi_shadow"]
DEFAULT_TEXT_COLOR = (255, 255, 255)  # white, used if theme detection fails
GRADIENT_TOP = DEFAULT_TEXT_COLOR
GRADIENT_BOTTOM = DEFAULT_TEXT_COLOR
LRC_LINE_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")
# LRC only gives one timestamp per full line, which is often a whole sentence
# and looks like a wall of text blown up to banner size. Each line gets split
# into MAX_WORDS-word chunks whose timestamps are spread evenly across the
# original line's time window. Kept to a single word so the one fixed banner
# font (BIG_FONTS) reliably fits at normal terminal widths even for fairly
# long individual words — bigger chunks make it fall back to plain text.
MAX_WORDS_PER_CHUNK = 1

# Used to build looser search queries when the exact player metadata doesn't
# match anything (extra artists, "(feat. X)", "- Remastered 2011", etc.).
ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|/| feat\.?| ft\.?| featuring| with| x )\s*", re.I)
PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
SUFFIX_RE = re.compile(
    r"\s*-\s*(remaster(ed)?(\s*\d{4})?|live|mono|stereo|single version|"
    r"radio edit|deluxe(\s*edition)?|explicit|clean|bonus track|from .*)\s*$",
    re.I,
)


def require_playerctl():
    if not shutil.which("playerctl"):
        sys.exit(
            "playerctl not found on PATH.\n"
            "Install it first, e.g. `sudo pacman -S playerctl` (Arch) "
            "or `sudo apt install playerctl` (Debian/Ubuntu)."
        )


def playerctl(*args) -> str | None:
    try:
        result = subprocess.run(
            ["playerctl", *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_now_playing():
    """Returns (artist, title, status, position_seconds) or None if nothing is playing."""
    meta = playerctl("metadata", "--format", "{{artist}}\t{{title}}\t{{status}}")
    if not meta:
        return None
    parts = meta.split("\t")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    artist, title, status = parts
    pos_raw = playerctl("position")
    try:
        position = float(pos_raw) if pos_raw else 0.0
    except ValueError:
        position = 0.0
    return artist, title, status, position


def strip_parens_chars(text: str) -> str:
    """Drop literal '(' and ')' characters from displayed lyric text (e.g.
    backing-vocal annotations like "(oh)"), keeping the words inside."""
    return re.sub(r"\s+", " ", text.replace("(", "").replace(")", "")).strip()


def parse_lrc(text: str):
    lines = []
    for raw_line in text.splitlines():
        match = LRC_LINE_RE.match(raw_line)
        if not match:
            continue
        minutes, seconds, content = match.groups()
        timestamp = int(minutes) * 60 + float(seconds)
        lines.append((timestamp, strip_parens_chars(content)))
    lines.sort(key=lambda pair: pair[0])
    return lines


def primary_artist(artist: str) -> str:
    """First artist out of 'A, B & C' / 'A feat. B' / 'A x B' style credits."""
    parts = ARTIST_SPLIT_RE.split(artist.strip())
    return parts[0].strip() if parts and parts[0].strip() else artist.strip()


def clean_title(title: str) -> str:
    """Strip '(feat. X)', '[Live]', '- Remastered 2011' style noise from a title."""
    t = SUFFIX_RE.sub("", title.strip())
    t = PAREN_RE.sub("", t).strip()
    t = SUFFIX_RE.sub("", t).strip()
    return t or title.strip()


def build_query_candidates(artist: str, title: str):
    artist, title = artist.strip(), title.strip()
    a_primary, t_clean = primary_artist(artist), clean_title(title)
    candidates = [
        (artist, title),
        (a_primary, t_clean),
        (a_primary, PAREN_RE.sub("", title).strip() or title),
        (artist, t_clean),
    ]
    seen, deduped = set(), []
    for pair in candidates:
        if pair not in seen and all(pair):
            seen.add(pair)
            deduped.append(pair)
    return deduped


def artist_matches(query_artist: str, result_artist: str) -> bool:
    q, r = query_artist.lower(), (result_artist or "").lower()
    return bool(q) and bool(r) and (q in r or r in q)


def search_lrclib(artist: str, title: str):
    params = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    url = f"{LRCLIB_SEARCH}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []


def load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass


def cache_key(artist: str, title: str) -> str:
    # Cached entries store already-chunked lines. Bumping CACHE_VERSION
    # whenever chunking (or anything else baked into the cached value)
    # changes naturally orphans old entries instead of serving stale data
    # that no longer matches the current MAX_WORDS_PER_CHUNK etc.
    return f"v{CACHE_VERSION}\x1f{artist.strip().lower()}\x1f{title.strip().lower()}"


def fetch_lyrics(artist: str, title: str):
    """Returns (lines, synced) where lines is [(timestamp, text), ...].

    Falls back to plain lyrics (all timestamps 0) when no synced version exists.
    Returns (None, False) if nothing is found at all.

    Checks a local disk cache first. On a miss, searches several cleaned-up
    variants of the artist/title *in parallel* (player metadata is often
    noisy: multiple artists, "(feat. X)", "- Remastered 2011", etc., so a
    single exact query misses a lot of real-world tracks) and takes whichever
    good match comes back first instead of waiting on each one sequentially.
    """
    cache = load_cache()
    key = cache_key(artist, title)
    if key in cache:
        cached_lines, cached_synced = cache[key]
        return (cached_lines if cached_lines is not None else None), cached_synced

    candidates = build_query_candidates(artist, title)
    best_plain = None
    result_lines, result_synced = None, False

    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        futures = {
            pool.submit(search_lrclib, cand_artist, cand_title): cand_artist
            for cand_artist, cand_title in candidates
        }
        for future in as_completed(futures):
            cand_artist = futures[future]
            results = future.result()
            if not results:
                continue

            # A synced result whose artist actually matches our query wins
            # outright — stop waiting on the remaining candidates.
            matched = next(
                (
                    c
                    for c in results
                    if c.get("syncedLyrics") and artist_matches(cand_artist, c.get("artistName", ""))
                ),
                None,
            )
            if matched:
                parsed = parse_lrc(matched["syncedLyrics"])
                if parsed:
                    result_lines, result_synced = expand_to_short_lines(parsed), True
                    break

            if result_lines is None:
                any_synced = next((c for c in results if c.get("syncedLyrics")), None)
                if any_synced:
                    parsed = parse_lrc(any_synced["syncedLyrics"])
                    if parsed:
                        result_lines, result_synced = expand_to_short_lines(parsed), True

            if best_plain is None:
                for candidate in results:
                    if candidate.get("plainLyrics"):
                        plain_lines = [
                            (0.0, strip_parens_chars(line))
                            for line in candidate["plainLyrics"].splitlines()
                            if line.strip()
                        ]
                        if plain_lines:
                            best_plain = plain_lines
                            break

    if result_lines is None and best_plain:
        result_lines, result_synced = best_plain, False

    cache[key] = (result_lines, result_synced)
    save_cache(cache)
    return result_lines, result_synced


def chunk_words(text: str, max_words: int = MAX_WORDS_PER_CHUNK):
    words = text.split()
    if not words:
        return [text]
    return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]


# Rough estimate of how long a chunk takes to sing, used to pace chunks
# within a line — see expand_to_short_lines() for why.
SECONDS_PER_CHAR = 0.06
MIN_CHUNK_SECONDS = 0.35


def expand_to_short_lines(lines, tail_span: float = 4.0):
    """Split each (timestamp, line) into short word-chunks, paced within
    that line's real LRC time window.

    LRC only gives one timestamp per full line, so where each word actually
    falls has to be estimated. Spreading words *evenly* across the full gap
    to the next line's timestamp looked fine for short gaps but badly wrong
    for long ones — a 2-word line followed by a 7s instrumental break would
    have those 2 words stretched across the entire 7s, looking out of sync
    even though the line's own timestamp was accurate. Instead, estimate
    each chunk's duration from its length (longer chunks get proportionally
    more time) and cap the total to whichever is shorter: that estimate, or
    the actual gap to the next line. The last chunk then just lingers on
    screen for any leftover gap instead of being artificially stretched.
    """
    expanded = []
    for i, (t, text) in enumerate(lines):
        t_next = lines[i + 1][0] if i + 1 < len(lines) else t + tail_span
        available_span = max(t_next - t, 0.1)
        chunks = chunk_words(text)
        if not chunks:
            continue
        est_durations = [max(MIN_CHUNK_SECONDS, len(c) * SECONDS_PER_CHAR) for c in chunks]
        natural_total = sum(est_durations)
        span = min(available_span, natural_total)
        scale = span / natural_total if natural_total > 0 else 0
        cursor = t
        for chunk, duration in zip(chunks, est_durations):
            expanded.append((cursor, chunk))
            cursor += duration * scale
    return expanded


def active_index(lines, position: float) -> int:
    idx = -1
    for i, (timestamp, _) in enumerate(lines):
        if timestamp <= position:
            idx = i
        else:
            break
    return idx


FONT_FIT_PERCENTILE = 0.95  # fraction of chunks that must fit, see pick_font()


def pick_font(lines, width: int, height: int) -> str | None:
    """Return the biggest BIG_FONTS entry that fits at least
    FONT_FIT_PERCENTILE of this track's chunks as a single row at this
    terminal size, or None if nothing does (caller should fall back to
    plain text for every line).

    Checked against the actual upcoming lyric chunks rather than a generic
    placeholder — word length varies a lot in real lyrics, so a synthetic
    probe reliably underestimates width. Sized against the real content so
    the whole track renders in one consistent font — but tolerating a small
    fraction of unusually long outlier words rather than requiring every
    single one to fit: render_big() already falls back to plain text for
    just that one line when a chunk doesn't fit, so letting one rare long
    word veto the banner font for the *entire* track (previously: any
    single chunk too wide meant plain text for the whole song) was overkill.
    """
    texts = [text for _, text in (lines or []) if text.strip()]
    if not texts:
        return BIG_FONTS[0] if BIG_FONTS else None
    for font in BIG_FONTS:
        fig = pyfiglet.Figlet(font=font, width=10_000)  # single row, no auto-wrap
        widths = []
        too_tall = False
        for text in texts:
            rendered_lines = fig.renderText(text).rstrip("\n").split("\n")
            widths.append(max((len(line) for line in rendered_lines), default=0))
            if len(rendered_lines) > height:
                too_tall = True
                break
        if too_tall:
            continue
        widths.sort()
        cutoff = widths[int(len(widths) * FONT_FIT_PERCENTILE)]
        if cutoff <= width:
            return font
    return None


def render_big(text: str, font: str | None, width: int) -> str:
    width = max(width, 1)
    if font is not None:
        fig = pyfiglet.Figlet(font=font, width=10_000)
        rendered = fig.renderText(text).rstrip("\n")
        rendered_lines = rendered.split("\n")
        if rendered_lines and max(len(line) for line in rendered_lines) <= width:
            return rendered
        # An unusually long chunk that doesn't fit the chosen font: fall
        # through to plain text for just this line, rather than swapping to
        # a different banner font (which is what caused the flicker).

    return "\n".join(textwrap.wrap(text, width=width) or [text[:width]])


def gradient_text(block: str, top: tuple, bottom: tuple) -> str:
    """Color a multi-line block with a top-to-bottom RGB gradient."""
    lines = block.split("\n")
    n = len(lines)
    colored = []
    for i, line in enumerate(lines):
        t = i / (n - 1) if n > 1 else 0
        r = round(top[0] + (bottom[0] - top[0]) * t)
        g = round(top[1] + (bottom[1] - top[1]) * t)
        b = round(top[2] + (bottom[2] - top[2]) * t)
        colored.append(f"\033[1m\033[38;2;{r};{g};{b}m{line}\033[0m")
    return "\n".join(colored)


def clear_screen():
    sys.stdout.write("\033[H\033[2J")


def get_terminal_dims():
    size = shutil.get_terminal_size((80, 20))
    return size.columns, size.lines


def detect_terminal_fg_color(default=DEFAULT_TEXT_COLOR):
    """Ask the terminal for its actual foreground color via the OSC 10
    escape sequence, so lyrics use the user's real theme color instead of a
    hardcoded one. Most modern terminals (kitty, alacritty, foot, iTerm2,
    xterm, ...) answer this; falls back to `default` immediately if not a
    real TTY, and within ~0.3s if the terminal doesn't respond at all."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return default
    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdout.write("\033]10;?\033\\")
            sys.stdout.flush()

            response = ""
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                ready, _, _ = select.select([fd], [], [], max(remaining, 0))
                if not ready:
                    break
                chunk = os.read(fd, 1)
                if not chunk:
                    break
                response += chunk.decode("latin-1")
                if response.endswith("\033\\") or response.endswith("\x07"):
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception:
        return default

    match = re.search(r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", response)
    if not match:
        return default

    def to_byte(hexval):
        value = int(hexval, 16)
        max_value = 16 ** len(hexval) - 1
        return round(value * 255 / max_value)

    try:
        return tuple(to_byte(h) for h in match.groups())
    except ValueError:
        return default


def center_block(block: str, width: int, height: int) -> str:
    """Center a (possibly multi-line) block of plain text both horizontally
    (per line) and vertically (top-padded) within width x height."""
    content_lines = [line.center(width) for line in block.split("\n")]
    pad_top = max((height - len(content_lines)) // 2, 0)
    return "\n".join([""] * pad_top + content_lines)


def render_message(msg: str, width: int, height: int):
    clear_screen()
    print(f"\033[2;3m{center_block(msg, width, height)}\033[0m")


def render(status, lines, synced, idx, width, height, font):
    clear_screen()
    content_height = height - (1 if status == "Paused" else 0)

    if lines is None:
        print(center_block("No lyrics found for this track.", width, height))
        return

    if not synced:
        body = "\n".join(text for _, text in lines)
        print(center_block(body, width, content_height))
        return

    if status == "Paused":
        print("\033[2m" + "[paused]".center(width) + "\033[0m")

    current_text = lines[idx][1] if idx >= 0 else ""
    if not current_text.strip():
        # Instrumental/blank gap: a small dim note, not banner art blown up
        # from a "..." placeholder (which looked like garbled text).
        print("\033[2m" + center_block("♪", width, content_height) + "\033[0m")
        return

    big = render_big(current_text, font, width)
    print(gradient_text(center_block(big, width, content_height), GRADIENT_TOP, GRADIENT_BOTTOM))


def watch():
    require_playerctl()

    global GRADIENT_TOP, GRADIENT_BOTTOM
    theme_color = detect_terminal_fg_color()
    GRADIENT_TOP = GRADIENT_BOTTOM = theme_color

    current_track = None
    lines = None
    synced = False
    status = None
    real_position = 0.0
    synced_at = time.monotonic()

    last_idx = None
    last_status = None
    last_width = None
    last_height = None
    last_waiting_size = None
    current_font = None

    next_resync = 0.0

    try:
        while True:
            now_mono = time.monotonic()
            width, height = get_terminal_dims()

            if now_mono >= next_resync:
                next_resync = now_mono + RESYNC_INTERVAL
                now = get_now_playing()
                if now is None:
                    current_track = None
                    lines = None
                    status = None
                    last_idx = None
                    last_status = None
                    if (width, height) != last_waiting_size:
                        last_waiting_size = (width, height)
                        render_message("waiting for something to play...", width, height)
                    time.sleep(TICK_INTERVAL)
                    continue
                last_waiting_size = None

                artist, title, status, real_position = now
                synced_at = now_mono
                track = (artist, title)

                if track != current_track:
                    current_track = track
                    render_message("finding lyrics...", width, height)
                    lines, synced = fetch_lyrics(artist, title)
                    last_idx = None
                    last_status = None
                    current_font = pick_font(lines, width, height) if (lines and synced) else None

            if status is None:
                time.sleep(TICK_INTERVAL)
                continue

            position = real_position + (now_mono - synced_at) if status == "Playing" else real_position

            idx = active_index(lines, position) if (lines and synced) else -1

            if (width != last_width or height != last_height) and lines and synced:
                current_font = pick_font(lines, width, height)

            if idx != last_idx or status != last_status or width != last_width or height != last_height:
                last_idx, last_status, last_width, last_height = idx, status, width, height
                render(status, lines, synced, idx, width, height, current_font)

            time.sleep(TICK_INTERVAL)
    except KeyboardInterrupt:
        clear_screen()
        print("Bye.")


if __name__ == "__main__":
    watch()
