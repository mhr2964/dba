"""Scrape NBA 2K player ratings from 2kratings.com and save to data/2k_ratings/<edition>.json.

2kratings.com blocks plain HTTP requests (Cloudflare) but works with a real browser.
This script uses Playwright to fetch each of the 30 NBA team pages and extract player
name + OVR from the roster table.

Usage:
    python scripts/fetch_2k_ratings.py --edition 2k26
    python scripts/fetch_2k_ratings.py --edition 2k26 --dry-run

Run this once per year when a new 2K title drops. The output file is committed to the
repo so re-scraping is not needed for every import.

Requirements:
    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

# Team code → 2kratings.com slug
TEAM_SLUGS: dict[str, str] = {
    "ATL": "atlanta-hawks",
    "BOS": "boston-celtics",
    "BKN": "brooklyn-nets",
    "CHA": "charlotte-hornets",
    "CHI": "chicago-bulls",
    "CLE": "cleveland-cavaliers",
    "DAL": "dallas-mavericks",
    "DEN": "denver-nuggets",
    "DET": "detroit-pistons",
    "GSW": "golden-state-warriors",
    "HOU": "houston-rockets",
    "IND": "indiana-pacers",
    "LAC": "los-angeles-clippers",
    "LAL": "los-angeles-lakers",
    "MEM": "memphis-grizzlies",
    "MIA": "miami-heat",
    "MIL": "milwaukee-bucks",
    "MIN": "minnesota-timberwolves",
    "NOP": "new-orleans-pelicans",
    "NYK": "new-york-knicks",
    "OKC": "oklahoma-city-thunder",
    "ORL": "orlando-magic",
    "PHI": "philadelphia-76ers",
    "PHX": "phoenix-suns",
    "POR": "portland-trail-blazers",
    "SAC": "sacramento-kings",
    "SAS": "san-antonio-spurs",
    "TOR": "toronto-raptors",
    "UTA": "utah-jazz",
    "WAS": "washington-wizards",
}

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "2k_ratings"


def _normalize_name(name: str) -> str:
    """Lowercase, strip extra whitespace, collapse interior whitespace."""
    return re.sub(r"\s+", " ", name.strip().lower())


async def _scrape_team(page, team_code: str, slug: str) -> dict[str, int]:
    """Fetch one team page and return {normalized_player_name: overall}."""
    url = f"https://www.2kratings.com/teams/{slug}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Extract player names and OVR from the first roster table only.
        # The first table is the current Play Now roster (columns: Rank, Player, OVR, 3PT, DNK).
        # Later tables on the page contain All-Time/Legend players — we skip those.
        # Player names are stored in img[alt] inside the player cell, not as visible text.
        rows = await page.evaluate(
            """() => {
                const result = [];
                // Use only the first table on the page (current roster).
                const table = document.querySelector('table');
                if (!table) return result;
                for (const row of table.querySelectorAll('tr')) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 3) continue;
                    // Name is the alt attribute of the player photo img.
                    const img = cells[1].querySelector('img[alt]');
                    const name = img ? img.alt.trim() : '';
                    if (!name || name === 'NBA All-Star') continue;
                    // OVR cell may have badge count on a second line; take only the first line.
                    const ovrText = cells[2].innerText.split('\\n')[0].trim();
                    const ovr = parseInt(ovrText, 10);
                    if (!isNaN(ovr) && ovr > 40) {
                        result.push([name, ovr]);
                    }
                }
                return result;
            }"""
        )
        team_ratings = {}
        for name, ovr in rows:
            normalized = _normalize_name(name)
            if normalized:
                team_ratings[normalized] = ovr
        print(f"  {team_code}: {len(team_ratings)} players scraped")
        return team_ratings
    except Exception as exc:
        print(f"  [ERROR] {team_code} ({slug}): {exc}")
        return {}


async def scrape_all(edition: str, dry_run: bool) -> None:
    from playwright.async_api import async_playwright

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{edition}.json"

    all_ratings: dict[str, int] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        for team_code, slug in TEAM_SLUGS.items():
            team_ratings = await _scrape_team(page, team_code, slug)
            # Prefer higher OVR if player appears on multiple teams (traded player).
            for name, ovr in team_ratings.items():
                if name not in all_ratings or ovr > all_ratings[name]:
                    all_ratings[name] = ovr
            time.sleep(0.3)  # polite delay between requests

        await browser.close()

    print(f"\nTotal unique players: {len(all_ratings)}")

    if dry_run:
        top10 = sorted(all_ratings.items(), key=lambda x: -x[1])[:10]
        print("Top 10 (dry run):")
        for name, ovr in top10:
            print(f"  {name}: {ovr}")
        print("[DRY RUN] Not writing file.")
        return

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_ratings, f, indent=2, ensure_ascii=False, sort_keys=True)
    print(f"Saved to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape 2K ratings from 2kratings.com")
    parser.add_argument("--edition", default="2k26", help="2K edition (e.g. 2k26, 2k25)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    asyncio.run(scrape_all(args.edition.lower(), args.dry_run))


if __name__ == "__main__":
    main()
