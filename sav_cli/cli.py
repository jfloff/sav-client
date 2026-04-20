"""
Command-line interface for sav-client.

Usage
-----
    sav players
    sav players --name "João" --tier "Sénior"
    sav players --license 301772
    sav --output json players
    sav --output csv games
    sav game-sheets
    sav game-sheets --tier "Sénior" --date-from 01-01-2026
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from typing import Any

import click

from sav_client import SavClient, Player
from sav_client.exceptions import (
  SavAuthError,
  SavConfigError,
  SavConnectionError,
  SavResponseError,
)

_COL_SEP = "  "
_ELLIPSIS = "…"


def _truncate(value: str, width: int) -> str:
  """Truncate a string to `width` characters, dropping whole words when possible.

  For multi-word strings, removes words from the middle until the result fits,
  keeping the first and last word intact.  Falls back to a character-level
  mid-ellipsis only when no word boundary works.

  E.g. "Francisco Pereira Almeida Caipira" at width=28 →
       "Francisco Pereira … Caipira"  (drops "Almeida" whole)
  """
  if len(value) <= width:
    return value

  words = value.split()
  if len(words) >= 3:
    last = words[-1]
    for prefix_count in range(len(words) - 1, 0, -1):
      candidate = " ".join(words[:prefix_count]) + " " + _ELLIPSIS + " " + last
      if len(candidate) <= width:
        return candidate

  # Fallback: character-level mid-ellipsis
  left = (width - 1) // 2
  right = width - left - 1
  return value[:left] + _ELLIPSIS + value[-right:]


def _render_table(
  headers: list[str],
  rows: list[list[str]],
  max_widths: list[int | None] | None = None,
) -> None:
  """
  Print a table that fits within the current terminal width.

  Column widths are computed from the widest value in each column (header or
  data).  `max_widths` caps individual columns; None means uncapped.  Any
  value wider than its final column width is truncated with an ellipsis.
  """
  term_width = shutil.get_terminal_size((120, 24)).columns

  n = len(headers)
  if max_widths is None:
    max_widths = [None] * n

  # Natural width: max of header and every data cell
  natural = [
    max(len(headers[i]), *(len(row[i]) for row in rows) if rows else [0])
    for i in range(n)
  ]

  # Apply per-column caps
  widths = [
    min(natural[i], max_widths[i]) if max_widths[i] is not None else natural[i]
    for i in range(n)
  ]

  # If total still exceeds terminal, shrink the widest column(s) proportionally
  sep_total = len(_COL_SEP) * (n - 1)
  while sum(widths) + sep_total > term_width and max(widths) > 4:
    widths[widths.index(max(widths))] -= 1

  # Render
  header_row = _COL_SEP.join(h.ljust(widths[i]) for i, h in enumerate(headers))
  separator = _COL_SEP.join("-" * widths[i] for i in range(n))
  click.echo(header_row)
  click.echo(separator)
  for row in rows:
    click.echo(_COL_SEP.join(_truncate(row[i], widths[i]).ljust(widths[i]) for i in range(n)))


def _resolve_association(client: SavClient, association: str) -> int:
  """Resolve an association argument to a numeric ID (name fragment or number)."""
  if association.lstrip("-").isdigit():
    return int(association)

  try:
    associations = client.list_associations()
  except Exception as e:
    raise click.ClickException(f"Could not fetch associations list: {e}")

  q = association.lower()
  matches = [a for a in associations if q in a.name.lower()]

  if not matches:
    raise click.ClickException(
      f"No association found matching {association!r}. "
      "Use 'sav associations' to list available associations."
    )
  if len(matches) > 1:
    names = "\n  ".join(f"{a.id}: {a.name}" for a in matches)
    raise click.ClickException(
      f"Multiple associations match {association!r}:\n  {names}\n"
      "Be more specific or use the numeric ID."
    )
  return matches[0].id


def _resolve_clubs(client: SavClient, club: str) -> list[int]:
  """
  Resolve a club argument to one or more numeric IDs.

  Accepts either a numeric ID (returned as a single-element list) or a name
  fragment (case-insensitive substring match against the association's club
  list).  All matching clubs are returned — so "Rio Maior" can resolve to
  multiple clubs.  Raises ClickException only when nothing matches.
  """
  if club.lstrip("-").isdigit():
    return [int(club)]

  try:
    clubs = client.list_clubs()
  except Exception as e:
    raise click.ClickException(f"Could not fetch clubs list: {e}")

  q = club.lower()
  matches = [
    c for c in clubs
    if q in c.name.lower() or q in c.full_name.lower() or q in c.code.lower()
  ]

  if not matches:
    raise click.ClickException(
      f"No club found matching {club!r}. "
      "Use 'sav clubs' to list available clubs."
    )

  if len(matches) > 1:
    names = ", ".join(c.name for c in matches)
    click.echo(f"Matched {len(matches)} clubs for {club!r}: {names}", err=True)

  return [c.id for c in matches]


def _make_client() -> SavClient:
  try:
    client = SavClient.from_env()
  except SavConfigError as e:
    raise click.ClickException(str(e))
  try:
    client.login()
  except SavAuthError as e:
    raise click.ClickException(f"Authentication failed: {e}")
  except SavConnectionError as e:
    raise click.ClickException(f"Connection error: {e}")
  return client


@click.group()
@click.option(
  "--output", "-o",
  type=click.Choice(["table", "json", "csv"]),
  default="table",
  show_default=True,
  help="Output format.",
)
@click.pass_context
def cli(ctx, output):
  """SAV2 API client."""
  ctx.ensure_object(dict)
  ctx.obj["output"] = output


@cli.command("players")
@click.option("--name", default="", help="Filter by player name (partial).")
@click.option("--license", "license_", default="", help="Filter by licence number.")
@click.option("--number", default="", help="Filter by shirt number.")
@click.option("--tier", "tiers", default=None, multiple=True, help="Filter by tier/escalão; repeatable (e.g. --tier 'Mini 12' --tier 'Mini 10').")
@click.option("--gender", default=0, type=int, help="Filter by gender code (0 = any).")
@click.option("--season", default=None, type=int, help="Season epoch ID (defaults to current). Use 0 for all seasons.")
@click.option("--club", "clubs", default=None, multiple=True, help="Club ID or name fragment; repeatable (e.g. --club SBC --club 'Rio Maior').")
@click.option("--association", default=None, help="Association ID or name fragment (e.g. 'Santarém' or 7). Searches all clubs within it.")
@click.option("--all-clubs", "all_clubs", is_flag=True, default=False, help="Search every club across all associations (federation-wide).")
@click.option("--birth-date", default="", help="Filter by birth date (YYYY-MM-DD).")
@click.option("--page", default=1, type=int, show_default=True, help="Result page.")
@click.pass_context
def players_cmd(ctx, name, license_, number, tiers, gender, season, clubs, association, all_clubs, birth_date, page):
  """Search and list players."""
  output = ctx.obj["output"]
  client = _make_client()

  if clubs and (association is not None or all_clubs):
    raise click.UsageError("--club cannot be combined with --association or --all-clubs.")
  if association is not None and all_clubs:
    raise click.UsageError("--association and --all-clubs are mutually exclusive.")

  if all_clubs:
    club_arg: int | list[int] | None = 0
    association_id = 0
    click.echo("Searching all clubs across all associations (this may take a while)…", err=True)
  elif association is not None:
    association_id = _resolve_association(client, association)
    club_arg = 0
    click.echo(f"Searching all clubs in {association!r} (this may take a while)…", err=True)
  elif clubs:
    club_ids: list[int] = []
    for c in clubs:
      club_ids.extend(_resolve_clubs(client, c))
    club_arg = club_ids[0] if len(club_ids) == 1 else club_ids
    association_id = 0
  else:
    club_arg = None
    association_id = 0

  tier_arg: str | list[str] = list(tiers) if len(tiers) > 1 else (tiers[0] if tiers else "")

  try:
    results = client.search_players(
      name=name,
      license=license_,
      number=number,
      tier=tier_arg,
      gender=gender,
      season=season,
      club=club_arg,
      association=association_id,
      birth_date=birth_date,
      page=page,
    )
  except (SavConnectionError, SavResponseError) as e:
    raise click.ClickException(str(e))

  if not results:
    click.echo("No players found.")
    return

  if output == "json":
    click.echo(json.dumps([asdict(a) for a in results], ensure_ascii=False, indent=2))
    return

  if output == "csv":
    fields = ["id", "license", "name", "association", "club", "tier", "gender",
              "season", "status", "birth_date", "nationality", "active"]
    click.echo(",".join(fields))
    for a in results:
      row = asdict(a)
      click.echo(",".join(str(row[f]) for f in fields))
    return

  headers = ["License", "Name", "Club", "Tier", "Gender", "Season", "Birth Date", "Active"]
  rows = [
    [
      a.license,
      a.name,
      a.club,
      a.tier,
      a.gender,
      a.season,
      a.birth_date,
      "yes" if a.active else "no",
    ]
    for a in results
  ]
  _render_table(headers, rows, max_widths=[None, 28, 20, None, None, None, None, None])
  suffix = " (all seasons)" if season == 0 else ""
  click.echo(f"\n{len(results)} player(s) found{suffix}.")


@cli.command("player")
@click.argument("license_num")
@click.option("--photo", is_flag=True, default=False, help="Fetch and include the player's photo URL.")
@click.pass_context
def player_cmd(ctx, license_num, photo):
  """Show detail for a single player by their licence number."""
  output = ctx.obj["output"]
  client = _make_client()

  try:
    results = client.search_players(license=license_num)
  except (SavConnectionError, SavResponseError) as e:
    raise click.ClickException(str(e))

  if not results:
    raise click.ClickException(f"No player found with licence {license_num!r}.")

  player = results[0]

  if photo:
    try:
      from dataclasses import replace
      detail = client.get_player_detail(player.id, photo=True)
      player = replace(player, photo_url=detail.photo_url)
    except (SavConnectionError, SavResponseError) as e:
      raise click.ClickException(str(e))

  if output == "json":
    click.echo(json.dumps(asdict(player), ensure_ascii=False, indent=2))
    return

  if output == "csv":
    csv_fields = ["id", "license", "name", "birth_date", "gender", "nationality",
                  "club", "association", "tier", "status", "season", "active", "photo_url"]
    click.echo(",".join(csv_fields))
    row = asdict(player)
    click.echo(",".join(str(row[f]) for f in csv_fields))
    return

  fields = [
    ("License",     player.license),
    ("Name",        player.name),
    ("Birth Date",  player.birth_date),
    ("Gender",      player.gender),
    ("Nationality", player.nationality),
    ("Club",        player.club),
    ("Association", player.association),
    ("Tier",        player.tier),
    ("Status",      player.status),
    ("Season",      player.season),
    ("Active",      "yes" if player.active else ""),
    ("Photo URL",   player.photo_url),
  ]
  width = max(len(k) for k, _ in fields)
  for key, val in fields:
    if val:
      click.echo(f"{key:<{width}}  {val}")


@cli.command("associations")
@click.pass_context
def associations_cmd(ctx):
  """List all associations."""
  output = ctx.obj["output"]
  client = _make_client()

  try:
    results = client.list_associations()
  except (SavConnectionError, SavResponseError) as e:
    raise click.ClickException(str(e))

  if not results:
    click.echo("No associations found.")
    return

  if output == "json":
    click.echo(json.dumps([{"id": a.id, "name": a.name} for a in results], ensure_ascii=False, indent=2))
    return

  if output == "csv":
    click.echo("id,name")
    for a in results:
      click.echo(f"{a.id},{a.name}")
    return

  headers = ["ID", "Name"]
  rows = [[str(a.id), a.name] for a in results]
  _render_table(headers, rows)
  click.echo(f"\n{len(results)} association(s) found.")


@cli.command("clubs")
@click.argument("query", default="", required=False)
@click.option("--association", default=None, type=int, help="Association ID (from 'sav associations'). Defaults to your own.")
@click.pass_context
def clubs_cmd(ctx, query, association):
  """List clubs, optionally filtered by a name/code query.

  QUERY is an optional case-insensitive substring matched against the short
  name, full name, and code of each club (e.g. "Rio Maior" or "SBC").
  """
  output = ctx.obj["output"]
  client = _make_client()

  try:
    results = client.list_clubs(association=association)
  except (SavConnectionError, SavResponseError) as e:
    raise click.ClickException(str(e))

  if query:
    q = query.lower()
    results = [
      c for c in results
      if q in c.name.lower() or q in c.full_name.lower() or q in c.code.lower()
    ]

  if not results:
    click.echo("No clubs found.")
    return

  if output == "json":
    click.echo(json.dumps(
      [{"id": c.id, "name": c.name, "full_name": c.full_name, "code": c.code} for c in results],
      ensure_ascii=False, indent=2,
    ))
    return

  if output == "csv":
    click.echo("id,name,full_name,code")
    for c in results:
      click.echo(f"{c.id},{c.name},{c.full_name},{c.code}")
    return

  headers = ["ID", "Name", "Full Name", "Code"]
  rows = [[str(c.id), c.name, c.full_name, c.code] for c in results]
  _render_table(headers, rows, max_widths=[None, 24, 40, 8])
  click.echo(f"\n{len(results)} club(s) found.")


def _normalise_date(date_ddmmyyyy: str) -> str:
  """Convert DD-MM-YYYY to YYYY-MM-DD for lexicographic comparison."""
  try:
    d, m, y = date_ddmmyyyy.split("-")
    return f"{y}-{m}-{d}"
  except Exception:
    return date_ddmmyyyy


def _game_sort_key(g) -> tuple:
  """Return a (date, time) tuple for sorting games chronologically."""
  try:
    d, m, y = g.date.split("-")
    date_key = (int(y), int(m), int(d))
  except Exception:
    date_key = (9999, 99, 99)
  try:
    h, mi = g.time.split(":")
    time_key = (int(h), int(mi))
  except Exception:
    time_key = (99, 99)
  return date_key + time_key


@cli.command("games")
@click.option("--season", default=None, type=int, help="Season epoch ID (defaults to current).")
@click.option("--date-from", default="", help="Start date filter (DD-MM-YYYY).")
@click.option("--date-to", default="", help="End date filter (DD-MM-YYYY).")
@click.option("--tier", default="", help="Filter by tier (e.g. 'Sub 14').")
@click.option("--gender", default=0, type=int, help="Filter by gender code (0 = any).")
@click.option(
  "--status", default="Marcado", show_default=True,
  help="Filter by game status (e.g. 'Marcado', 'Não Marcado'). Use 'all' to show every status.",
)
@click.pass_context
def games_cmd(ctx, season, date_from, date_to, tier, gender, status):
  """List scheduled and played games for the profile's club."""
  output = ctx.obj["output"]
  client = _make_client()

  try:
    results = client.list_games(
      season=season,
      date_from=date_from,
      date_to=date_to,
      tier=tier,
      gender=gender,
    )
  except (SavConnectionError, SavResponseError) as e:
    raise click.ClickException(str(e))

  if status.lower() != "all":
    results = [g for g in results if g.game_status == status]

  results = sorted(results, key=_game_sort_key)

  if not results:
    click.echo("No games found.")
    return

  if output == "json":
    click.echo(json.dumps([asdict(g) for g in results], ensure_ascii=False, indent=2))
    return

  if output == "csv":
    fields = ["number", "date", "time", "home", "away", "home_score",
              "away_score", "competition", "tier", "venue", "game_status"]
    click.echo(",".join(fields))
    for g in results:
      row = asdict(g)
      click.echo(",".join(str(row[f]) for f in fields))
    return

  headers = ["#", "Date", "Time", "Home", "Away", "Score", "Tier", "Status"]
  rows = [
    [
      g.number,
      g.date,
      g.time,
      g.home,
      g.away,
      f"{g.home_score}-{g.away_score}" if g.home_score else "-",
      g.tier,
      g.game_status,
    ]
    for g in results
  ]
  _render_table(headers, rows, max_widths=[6, 12, 6, 28, 28, 6, 10, 16])
  click.echo(f"\n{len(results)} game(s) found.")


def _print_eligible(client, game, val: int, *, show_players: bool, show_coaches: bool, show_staff: bool) -> None:
  """Print the eligible players, coaches, and/or staff for one team."""
  team_name = game.home if val == 1 else game.away
  try:
    data = client.get_eligible_players(game.id, val=val)
  except (SavConnectionError, SavResponseError) as e:
    raise click.ClickException(str(e))

  players_data = data.get("players", [])

  if show_players:
    click.echo(f"\nEligible players — {team_name}")
    if players_data:
      _render_table(
        ["Licence", "Name", "Birth Date", "Status"],
        [[p.get("licence",""), p.get("name",""), p.get("birth_date",""), p.get("status","")] for p in players_data],
        max_widths=[None, 32, None, None],
      )
    else:
      click.echo("  (none)")

  if show_coaches:
    coaches = data.get("coaches_pri", []) + data.get("coaches_adj", [])
    if coaches:
      click.echo(f"\nCoaches — {team_name}")
      _render_table(
        ["Wallet", "Name", "Grade", "Function"],
        [[c.get("wallet",""), c.get("name",""), c.get("grade",""), c.get("function","")] for c in coaches],
        max_widths=[None, 32, None, 30],
      )
    else:
      click.echo(f"\nCoaches — {team_name}\n  (none)")

  if show_staff:
    staff = data.get("staff", [])
    if staff:
      click.echo(f"\nStaff — {team_name}")
      _render_table(
        ["Licence", "Name", "Function"],
        [[s.get("licence",""), s.get("name",""), s.get("function","")] for s in staff],
        max_widths=[None, 32, None],
      )
    else:
      click.echo(f"\nStaff — {team_name}\n  (none)")

  if show_players:
    click.echo(f"\n{len(players_data)} eligible player(s).")


@cli.command("game-sheet")
@click.argument("game_number")
@click.option("--home", "team", flag_value="home", help="Show/export home team eligible players.")
@click.option("--away", "team", flag_value="away", help="Show/export away team eligible players.")
@click.option("--players", "show_players", is_flag=True, default=False, help="Show eligible players (list mode). If no section flag is set, all sections are shown.")
@click.option("--coaches", "show_coaches", is_flag=True, default=False, help="Show eligible coaches (list mode).")
@click.option("--staff", "show_staff", is_flag=True, default=False, help="Show eligible staff (list mode).")
@click.option("--player", "players", default=None, multiple=True, help="Player licence to include in PDF; repeatable. Defaults to all eligible.")
@click.option("--coach-pri", "coaches_pri", default=None, multiple=True, help="Head coach wallet to include in PDF; repeatable. Defaults to all eligible.")
@click.option("--coach-adj", "coaches_adj", default=None, multiple=True, help="Adjunct coach wallet to include in PDF; repeatable. Defaults to all eligible.")
@click.option("--out", "out_path", default=None, help="Save eligible players as PDF (default: game_<number>_<team>.pdf).")
@click.pass_context
def game_sheet_cmd(ctx, game_number, team, show_players, show_coaches, show_staff, players, coaches_pri, coaches_adj, out_path):
  """Show or export the eligible players list for a game.

  GAME_NUMBER is the human-readable game number shown in 'sav games'.

  Without --home or --away, prints eligible players for both teams.
  Use --players, --coaches, --staff to show only specific sections (default: all).
  With --out, --player, --coach-pri, or --coach-adj, --home or --away is required.
  """
  output = ctx.obj["output"]
  is_pdf_mode = out_path is not None or players or coaches_pri or coaches_adj
  if is_pdf_mode and not team:
    raise click.UsageError("Specify either --home or --away when generating a PDF.")

  # If no section flag is set, show everything
  if not (show_players or show_coaches or show_staff):
    show_players = show_coaches = show_staff = True

  client = _make_client()

  try:
    games = client.list_games(game_number=game_number)
  except (SavConnectionError, SavResponseError) as e:
    raise click.ClickException(str(e))

  games = [g for g in games if g.number == game_number]
  if not games:
    raise click.ClickException(f"No game found with number {game_number!r}.")
  if len(games) > 1:
    raise click.ClickException(
      f"Multiple games match number {game_number!r}; use a more specific filter."
    )

  game = games[0]
  if game.id == 0:
    raise click.ClickException(
      f"Game {game_number!r} found but has no internal ID — cannot fetch eligible players."
    )

  # Only print game header in human-readable mode
  if output == "table":
    click.echo(
      f"Game {game.number}: {game.home} vs {game.away}  |  {game.date} {game.time}  |  {game.venue}"
    )

  if is_pdf_mode:
    val = 1 if team == "home" else 2
    dest = out_path or f"game_{game.number}_{team}.pdf"
    licence_list = [int(p) for p in players]     if players     else None
    pri_list     = [int(p) for p in coaches_pri] if coaches_pri else None
    adj_list     = [int(p) for p in coaches_adj] if coaches_adj else None
    try:
      pdf = client.get_eligible_players_pdf(
        game.id, val=val,
        player_licences=licence_list,
        coaches_pri=pri_list,
        coaches_adj=adj_list,
      )
    except (SavConnectionError, SavResponseError) as e:
      raise click.ClickException(str(e))
    if pdf is None:
      raise click.ClickException(f"No eligible players PDF available for the {team} team.")
    with open(dest, "wb") as f:
      f.write(pdf)
    click.echo(f"Saved {team} team PDF → {dest}")
    return

  # List mode
  teams = [(1, "home")] if team == "home" else [(2, "away")] if team == "away" else [(1, "home"), (2, "away")]

  if output == "json":
    try:
      result = {}
      for val, label in teams:
        result[label] = client.get_eligible_players(game.id, val=val)
      # Single team: unwrap from the dict
      if len(teams) == 1:
        click.echo(json.dumps(list(result.values())[0], ensure_ascii=False, indent=2))
      else:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except (SavConnectionError, SavResponseError) as e:
      raise click.ClickException(str(e))
    return

  for val, _ in teams:
    _print_eligible(client, game, val, show_players=show_players, show_coaches=show_coaches, show_staff=show_staff)


@cli.command("game-sheets")
@click.option("--season", default=None, type=int, help="Season epoch ID (defaults to current).")
@click.option("--date", "single_date", default="", help="Filter by a single date (DD-MM-YYYY). Shorthand for --date-from and --date-to.")
@click.option("--date-from", default="", help="Start date filter (DD-MM-YYYY).")
@click.option("--date-to", default="", help="End date filter (DD-MM-YYYY).")
@click.option("--tier", default="", help="Filter by tier (e.g. 'Sub 14').")
@click.option("--competition", default="", help="Filter by competition name fragment (case-insensitive, e.g. 'Ribas').")
@click.option("--status", default="", help="Filter by game status (e.g. 'Realizado'). Shows all statuses by default.")
@click.pass_context
def game_sheets_cmd(ctx, season, single_date, date_from, date_to, tier, competition, status):
  """List games where a game-sheet may be available."""
  if single_date:
    date_from = date_from or single_date
    date_to = date_to or single_date
  output = ctx.obj["output"]
  client = _make_client()

  try:
    # Do not pass date filters to the API — the SAV2 server only returns
    # scheduled (Marcado) games when date-filtering, dropping completed ones.
    # Fetch all games for the season and filter by date client-side instead.
    results = client.list_games(season=season, tier=tier)
  except (SavConnectionError, SavResponseError) as e:
    raise click.ClickException(str(e))

  if competition:
    results = [g for g in results if competition.lower() in g.competition.lower()]
  if status:
    results = [g for g in results if g.game_status == status]

  if date_from:
    results = [g for g in results if _normalise_date(g.date) >= _normalise_date(date_from)]
  if date_to:
    results = [g for g in results if _normalise_date(g.date) <= _normalise_date(date_to)]
  results = sorted(results, key=_game_sort_key)

  if not results:
    click.echo("No games found.")
    return

  if output == "json":
    click.echo(json.dumps([asdict(g) for g in results], ensure_ascii=False, indent=2))
    return

  if output == "csv":
    fields = ["number", "date", "time", "home", "away", "competition", "tier", "game_status"]
    click.echo(",".join(fields))
    for g in results:
      row = asdict(g)
      click.echo(",".join(str(row[f]) for f in fields))
    return

  headers = ["#", "Date", "Time", "Home", "Away", "Competition", "Tier", "Status"]
  rows = [
    [g.number, g.date, g.time, g.home, g.away, g.competition, g.tier, g.game_status]
    for g in results
  ]
  _render_table(headers, rows, max_widths=[6, 12, 6, 24, 24, 30, 10, 16])
  click.echo(f"\n{len(results)} game(s) found.")



def main():
  cli()
