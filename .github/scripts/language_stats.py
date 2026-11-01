#!/usr/bin/env python3
"""Render a language breakdown of a GitHub account as a markdown table.

Computes total bytes per language across the account's repositories using
GitHub's REST API, then either prints the table or splices it into a README
between two marker comments.

Usage
-----
    python3 .github/scripts/language_stats.py                    # print table to stdout
    python3 .github/scripts/language_stats.py --write            # rewrite README.md section
    python3 .github/scripts/language_stats.py --check            # exit 1 if README.md is stale

Scope
-----
`--scope auto` (default) picks a mode that cannot silently under-count:

  * LANGUAGE_STATS_TOKEN set (a PAT)  -> every repo you own, private included
  * running inside GitHub Actions     -> public repos only (GITHUB_TOKEN is
                                         scoped to a single repo, so /user/repos
                                         there would return almost nothing)
  * local run with GH_TOKEN/GITHUB_TOKEN -> every repo you own

Requires nothing outside the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

API = "https://api.github.com"
START_MARKER = "<!-- LANG_STATS:START -->"
END_MARKER = "<!-- LANG_STATS:END -->"
OTHER_LABEL = "Other"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _token() -> str | None:
    for name in ("LANGUAGE_STATS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def api(path: str, token: str | None, params: dict | None = None):
    """GET an API path, returning (payload, headers). Retries transient errors."""
    url = f"{API}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    for attempt in range(1, 5):
        request = urllib.request.Request(url)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "profile-language-stats")
        if token:
            request.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response), dict(response.headers)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            remaining = error.headers.get("X-RateLimit-Remaining")
            secondary = "secondary rate limit" in body
            primary = remaining == "0"

            if error.code in (403, 429) and (primary or secondary):
                wait = int(error.headers.get("Retry-After") or 60)
                if primary:
                    reset = int(error.headers.get("X-RateLimit-Reset") or 0)
                    wait = max(1, min(reset - int(time.time()), 900))
                print(f"rate limited; waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue

            if error.code >= 500 and attempt < 4:
                time.sleep(2**attempt)
                continue

            hint = ""
            if error.code in (401, 403):
                hint = " (token missing/expired or lacking scope?)"
            raise SystemExit(f"GitHub API {error.code} on {path}{hint}: {body[:200]}")
        except urllib.error.URLError as error:
            if attempt == 4:
                raise SystemExit(f"network error on {path}: {error.reason}")
            time.sleep(2**attempt)

    raise SystemExit(f"gave up on {path} after repeated retries")


def resolve_login(token: str | None, explicit: str | None) -> str:
    if explicit:
        return explicit
    env_owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if env_owner:
        return env_owner
    if token:
        return api("/user", token)[0]["login"]
    raise SystemExit("no account found: pass --user or set GITHUB_REPOSITORY_OWNER")


def resolve_scope(requested: str) -> str:
    """Turn `auto` into a concrete scope without ever guessing wrong."""
    if requested != "auto":
        return requested
    if os.environ.get("LANGUAGE_STATS_TOKEN", "").strip():
        return "all"
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "public"
    if _token():
        return "all"
    return "public"


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #
def iter_repos(login: str, token: str | None, scope: str, affiliation: str):
    page = 1
    while True:
        if scope == "all":
            path = "/user/repos"
            params = {
                "per_page": 100,
                "page": page,
                "affiliation": affiliation,
                "sort": "pushed",
                "direction": "desc",
            }
        else:
            path = f"/users/{login}/repos"
            params = {"per_page": 100, "page": page, "type": "owner", "sort": "pushed"}

        batch, _ = api(path, token, params)
        if not batch:
            return
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def collect(login: str, token: str | None, scope: str, affiliation: str, include_forks: bool):
    totals: Counter[str] = Counter()
    repos = 0
    skipped = 0

    for repo in iter_repos(login, token, scope, affiliation):
        if repo.get("fork") and not include_forks:
            skipped += 1
            continue
        if repo.get("size", 0) == 0:
            skipped += 1
            continue

        languages, _ = api(f"/repos/{repo['full_name']}/languages", token)
        totals.update({lang: size for lang, size in languages.items() if size})
        repos += 1
        print(f"  {repo['full_name']}", file=sys.stderr)

    print(
        f"counted {repos} repos ({skipped} skipped: forks/empty), "
        f"{sum(totals.values()):,} bytes, {len(totals)} languages",
        file=sys.stderr,
    )
    return totals


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def shares(totals: dict[str, int], decimals: int = 1) -> dict[str, float]:
    """Percentages that sum to exactly 100.0 (largest-remainder rounding)."""
    total = sum(totals.values())
    scale = 10**decimals
    raw = {lang: size / total * 100 * scale for lang, size in totals.items()}
    floored = {lang: math.floor(value) for lang, value in raw.items()}

    missing = int(round(100 * scale)) - sum(floored.values())
    ranked = sorted(raw, key=lambda lang: raw[lang] - floored[lang], reverse=True)
    for lang in ranked[: max(0, missing)]:
        floored[lang] += 1

    return {lang: value / scale for lang, value in floored.items()}


def fold(totals: dict[str, int], min_share: float) -> dict[str, int]:
    """Group languages below `min_share` percent into a single Other row."""
    if min_share <= 0:
        return totals

    percent = shares(totals)
    kept: Counter[str] = Counter()
    other = 0
    for lang, size in totals.items():
        if percent[lang] < min_share:
            other += size
        else:
            kept[lang] += size

    if other and kept:
        kept[OTHER_LABEL] += other
    elif other:
        kept["All languages"] += other
    return kept


def order(data: dict[str, int]) -> list[str]:
    """Descending by size, with the Other bucket pinned to the end."""
    ordered = sorted(data, key=data.get, reverse=True)
    if OTHER_LABEL in ordered:
        ordered.remove(OTHER_LABEL)
        ordered.append(OTHER_LABEL)
    return ordered


# Mermaid's pie parser treats `"` as a terminator and `#` as an entity escape,
# so label text is normalised rather than interpolated raw.
LABEL_ALIASES = {"C#": "C Sharp", "F#": "F Sharp"}


def mermaid_label(lang: str) -> str:
    text = LABEL_ALIASES.get(lang, lang)
    return text.replace('"', "'").replace("\n", " ").strip()


def pie_block(
    totals: dict[str, int],
    min_share: float,
    colors: list[str] | None = None,
) -> str:
    """A mermaid pie chart; GitHub renders these natively, no service involved."""
    data = fold(totals, min_share)
    percent = shares(data)

    lines = ["```mermaid"]
    if colors:
        palette = {
            f"pie{index}": color for index, color in enumerate(colors[:12], start=1)
        }
        variables = ", ".join(f"'{key}': '{value}'" for key, value in palette.items())
        lines.append(
            "%%{init: {'theme': 'dark', 'themeVariables': {" + variables + "}}}%%"
        )
    else:
        lines.append("%%{init: {'theme': 'dark'}}%%")

    lines.append("pie showData title Language breakdown")
    lines.extend(
        f'    "{mermaid_label(lang)}" : {percent[lang]:.1f}' for lang in order(data)
    )
    lines.append("```")
    return "\n".join(lines)


def table_block(
    data: dict[str, int], percent: dict[str, float], show_bytes: bool
) -> str:
    header = "| Language | Share |" if not show_bytes else "| Language | Share | Bytes |"
    divider = "|:--|--:|" if not show_bytes else "|:--|--:|--:|"
    rows = [header, divider]

    for lang in order(data):
        cells = [lang, f"{percent[lang]:.1f}%"]
        if show_bytes:
            cells.append(f"{data[lang]:,}")
        rows.append("| " + " | ".join(cells) + " |")

    return "\n".join(rows)


def render(
    totals: dict[str, int],
    min_share: float,
    show_bytes: bool,
    pie: bool = True,
    pie_min_share: float = 2.0,
    pie_colors: list[str] | None = None,
) -> str:
    if not totals:
        raise SystemExit("no language data found for this account")

    data = fold(totals, min_share)
    blocks: list[str] = []

    if pie:
        blocks.append(pie_block(totals, pie_min_share, pie_colors))
    blocks.append(table_block(data, shares(data), show_bytes))

    return "\n\n".join(blocks)


def splice(readme: str, block: str, path: str) -> str:
    if START_MARKER not in readme or END_MARKER not in readme:
        raise SystemExit(
            f"{path} is missing the markers {START_MARKER} ... {END_MARKER}. "
            "Add them where the table should live, then re-run."
        )
    before, rest = readme.split(START_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    return f"{before}{START_MARKER}\n{block}\n{END_MARKER}{after}"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown language breakdown from GitHub.")
    parser.add_argument("--user", help="account login (default: from API/env)")
    parser.add_argument("--scope", choices=("auto", "all", "public"), default="auto")
    parser.add_argument(
        "--affiliation",
        default="owner",
        help="comma-separated affiliations for --scope all (default: owner)",
    )
    parser.add_argument("--include-forks", action="store_true")
    parser.add_argument(
        "--min-share",
        type=float,
        default=0.0,
        metavar="PCT",
        help="fold languages under this percent into 'Other' (default: show all)",
    )
    parser.add_argument("--bytes", action="store_true", help="add a Bytes column")
    parser.add_argument("--no-pie", action="store_true", help="table only, no mermaid chart")
    parser.add_argument(
        "--pie-min-share",
        type=float,
        default=2.0,
        metavar="PCT",
        help="fold chart slices under this percent into 'Other' (default: 2.0)",
    )
    parser.add_argument(
        "--pie-colors",
        metavar="HEX,...",
        help="comma-separated hex colours for pie1..pie12, e.g. '#7C3AED,#22D3EE'",
    )
    parser.add_argument("--readme", default="README.md")
    parser.add_argument("--write", action="store_true", help="splice into --readme")
    parser.add_argument("--check", action="store_true", help="exit 1 if --readme is stale")
    args = parser.parse_args()

    token = _token()
    scope = resolve_scope(args.scope)
    login = resolve_login(token, args.user)
    print(f"collecting language bytes for {login} (scope: {scope})", file=sys.stderr)

    totals = collect(login, token, scope, args.affiliation, args.include_forks)
    colors = (
        [color.strip() for color in args.pie_colors.split(",") if color.strip()]
        if args.pie_colors
        else None
    )
    table = render(
        totals,
        args.min_share,
        args.bytes,
        pie=not args.no_pie,
        pie_min_share=args.pie_min_share,
        pie_colors=colors,
    )

    if not (args.write or args.check):
        print(table)
        return 0

    with open(args.readme, encoding="utf-8") as handle:
        original = handle.read()

    updated = splice(original, table, args.readme)

    if args.check:
        if updated != original:
            print(f"{args.readme} is out of date; run with --write", file=sys.stderr)
            return 1
        print(f"{args.readme} is up to date", file=sys.stderr)
        return 0

    if updated == original:
        print(f"{args.readme} unchanged", file=sys.stderr)
        return 0

    with open(args.readme, "w", encoding="utf-8") as handle:
        handle.write(updated)
    print(f"updated {args.readme}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
