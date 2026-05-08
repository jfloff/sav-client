"""
Command-line interface for sav-client.

Usage
-----
    sav players --club 270
    sav players --name "João" --tier "Sénior" --club 270
    sav players --license 301772 --all-clubs
    sav --output json players --all-clubs
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
from sav_shared import (
  create_and_fetch_batch,
  derive_enrollment_params,
  distrito_name,
  ENROLLMENT_FIELD_META,
  filter_games,
  find_club_matches,
  find_distrito_id,
  find_id_by_name,
  game_sort_key,
  GUARDIAN_RELATIONS,
  ID_TYPES,
  KWARG_TO_SAV_KEY,
  parse_missing_guardian_fields,
  parsed_bool,
  REGISTRATION_TYPE_LABELS,
  resolve_player_candidates,
)


# Tracks the root --output flag so SavCliError.show() can format errors
# appropriately even after Click has unwound the context stack.
_OUTPUT_MODE: str = "table"

# Cap on how many clubs a single --club fragment may resolve to before we
# refuse to fan out the search. Short/ambiguous queries can otherwise silently
# trigger parallel searches against dozens of clubs.
_CLUB_MATCH_LIMIT = 5


class SavCliError(click.ClickException):
  """CLI error with a machine-readable `code`. Emits JSON to stderr when --output json."""

  exit_code = 1

  def __init__(self, message: str, code: str = "error") -> None:
    super().__init__(message)
    self.code = code

  def show(self, file=None) -> None:  # noqa: D401
    if _OUTPUT_MODE == "json":
      click.echo(
        json.dumps({"error": self.message, "code": self.code}, ensure_ascii=False),
        err=True,
      )
    else:
      super().show(file)


def _exc_code(exc: Exception) -> str:
  if isinstance(exc, SavAuthError):       return "auth_failed"
  if isinstance(exc, SavConnectionError): return "connection_error"
  if isinstance(exc, SavResponseError):   return "response_error"
  if isinstance(exc, SavConfigError):     return "config_error"
  return "error"

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
    association_id = int(association)
    if association_id == 0:
      raise click.UsageError(
        "--association no longer accepts 0. Omit --association to avoid filtering, "
        "or use --all-clubs for federation-wide search."
      )
    return association_id

  try:
    associations = client.list_associations()
  except Exception as e:
    raise SavCliError(f"Could not fetch associations list: {e}", code="fetch_failed")

  q = association.lower()
  matches = [a for a in associations if q in a.name.lower()]

  if not matches:
    raise SavCliError(
      f"No association found matching {association!r}. "
      "Use 'sav associations' to list available associations.",
      code="not_found",
    )
  if len(matches) > 1:
    names = "\n  ".join(f"{a.id}: {a.name}" for a in matches)
    raise SavCliError(
      f"Multiple associations match {association!r}:\n  {names}\n"
      "Be more specific or use the numeric ID.",
      code="ambiguous_match",
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
    clubs = client.list_clubs(all_associations=True)
  except Exception as e:
    raise SavCliError(f"Could not fetch clubs list: {e}", code="fetch_failed")

  matches = find_club_matches(clubs, club)

  if not matches:
    raise SavCliError(
      f"No club found matching {club!r}. "
      "Use 'sav clubs' to list available clubs.",
      code="not_found",
    )

  if len(matches) > _CLUB_MATCH_LIMIT:
    names = "\n  ".join(f"{c.id}: {c.name}" for c in matches[:_CLUB_MATCH_LIMIT])
    raise SavCliError(
      f"{len(matches)} clubs match {club!r}; be more specific or use the numeric ID. "
      f"First {_CLUB_MATCH_LIMIT}:\n  {names}",
      code="ambiguous_match",
    )

  if len(matches) > 1:
    names = ", ".join(c.name for c in matches)
    click.echo(f"Matched {len(matches)} clubs for {club!r}: {names}", err=True)

  return [c.id for c in matches]


def _make_client() -> SavClient:
  try:
    client = SavClient.from_env()
  except SavConfigError as e:
    raise SavCliError(str(e), code="config_error")
  try:
    client.login()
  except SavAuthError as e:
    raise SavCliError(f"Authentication failed: {e}", code="auth_failed")
  except SavConnectionError as e:
    raise SavCliError(f"Connection error: {e}", code="connection_error")
  except SavResponseError as e:
    raise SavCliError(f"Login failed: {e}", code="response_error")
  return client


@click.group()
@click.option(
  "--output", "-o",
  type=click.Choice(["table", "json", "csv"]),
  default="table",
  show_default=True,
  help="Output format.",
)
@click.option(
  "--fields",
  default=None,
  help="Comma-separated field projection for JSON/CSV output (e.g. 'name,license,active'). Ignored for table output.",
)
@click.pass_context
def cli(ctx, output, fields):
  """SAV2 API client."""
  global _OUTPUT_MODE
  _OUTPUT_MODE = output
  ctx.ensure_object(dict)
  ctx.obj["output"] = output
  ctx.obj["fields"] = [f.strip() for f in fields.split(",") if f.strip()] if fields else None


def _project(rows: list[dict], fields: list[str] | None) -> list[dict]:
  """Project each row dict down to the requested fields."""
  if not fields:
    return rows
  if rows:
    unknown = [f for f in fields if f not in rows[0]]
    if unknown:
      raise click.UsageError(
        f"Unknown field(s): {', '.join(unknown)}. Available: {', '.join(rows[0].keys())}"
      )
  return [{f: r[f] for f in fields} for r in rows]


@cli.command("players")
@click.option("--name", default="", help="Filter by player name (partial).")
@click.option("--license", "license_", default="", help="Filter by licence number.")
@click.option("--number", default="", help="Filter by shirt number.")
@click.option(
  "--status",
  default="all",
  show_default=True,
  type=click.Choice(["active", "inactive", "all"], case_sensitive=False),
  help="Filter by player eligibility status.",
)
@click.option("--tier", "tiers", default=None, multiple=True, help="Filter by tier/escalão; repeatable (e.g. --tier 'Mini 12' --tier 'Mini 10').")
@click.option("--gender", default=0, type=int, help="Filter by gender code (0 = any).")
@click.option("--season", default=None, type=int, help="Season epoch ID (defaults to current). Use 0 for all seasons.")
@click.option("--club", "clubs", default=None, multiple=True, help="Club ID or name fragment; repeatable (e.g. --club SBC --club 'Rio Maior').")
@click.option(
  "--association", default=None,
  help="Association ID or name fragment (e.g. 'Santarém' or 7). When omitted, association is not filtered.",
)
@click.option("--all-clubs", "all_clubs", is_flag=True, default=False, help="Search every club across all associations (federation-wide).")
@click.option("--birth-year", "birth_years", default=None, multiple=True, type=int, help="Filter by birth year; repeatable (e.g. --birth-year 2008 --birth-year 2009).")
@click.option("--limit", default=None, type=int, help="Maximum number of results to return.")
@click.option("--count", is_flag=True, default=False, help="Return only the number of matching players instead of the list.")
@click.pass_context
def players_cmd(ctx, name, license_, number, status, tiers, gender, season, clubs, association, all_clubs, birth_years, limit, count):
  """Search and list players. Requires exactly one of --club, --association, or --all-clubs."""
  output = ctx.obj["output"]

  if clubs and (association is not None or all_clubs):
    raise click.UsageError("--club cannot be combined with --association or --all-clubs.")
  if association is not None and all_clubs:
    raise click.UsageError("--association and --all-clubs are mutually exclusive.")
  if not clubs and association is None and not all_clubs:
    raise click.UsageError("One of --club, --association, or --all-clubs is required.")
  if count and limit is not None:
    raise click.UsageError("--count and --limit are mutually exclusive.")

  client = _make_client()

  if all_clubs:
    club_arg: int | list[int] | None = 0
    association_id = None
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
    association_id = None
  else:
    club_arg = None
    association_id = None

  tier_arg: str | list[str] = list(tiers) if len(tiers) > 1 else (tiers[0] if tiers else "")

  try:
    results = client.search_players(
      name=name,
      license=license_,
      number=number,
      status=status,
      tier=tier_arg,
      gender=gender,
      season=season,
      club=club_arg,
      association=association_id,
      birth_year=list(birth_years) if birth_years else None,
      limit=limit,
    )
  except (SavConnectionError, SavResponseError) as e:
    raise SavCliError(str(e), code=_exc_code(e))

  if count:
    n = len(results)
    if output == "json":
      click.echo(json.dumps({"count": n}))
    elif output == "csv":
      click.echo("count")
      click.echo(n)
    else:
      click.echo(f"{n} player(s) match.")
    return

  if not results:
    if output == "json":
      click.echo("[]")
    elif output == "csv":
      pass
    else:
      click.echo("No players found.")
    return

  fields = ctx.obj.get("fields")

  if output == "json":
    rows = _project([asdict(a) for a in results], fields)
    click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
    return

  if output == "csv":
    default_fields = ["id", "license", "name", "association", "club", "tier", "gender",
                      "season", "status", "birth_date", "nationality", "active"]
    rows = _project([asdict(a) for a in results], fields)
    csv_fields = fields or default_fields
    click.echo(",".join(csv_fields))
    for r in rows:
      click.echo(",".join(str(r[f]) for f in csv_fields))
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
@click.argument("license_nums", nargs=-1, required=True)
@click.option("--photo", is_flag=True, default=False, help="Fetch and include each player's photo URL.")
@click.option("--club", "clubs", default=None, multiple=True, help="Club ID or name fragment; repeatable.")
@click.option("--association", default=None, help="Association ID or name fragment.")
@click.option("--all-clubs", "all_clubs", is_flag=True, default=False, help="Search every club across all associations.")
@click.pass_context
def player_cmd(ctx, license_nums, photo, clubs, association, all_clubs):
  """Show detail for one or more players by licence number.

  Pass multiple licence numbers to fetch them in parallel. JSON/CSV always
  returns a list; a single licence returns a 1-element list.
  """
  from concurrent.futures import ThreadPoolExecutor
  from dataclasses import replace

  output = ctx.obj["output"]
  if clubs and (association is not None or all_clubs):
    raise click.UsageError("--club cannot be combined with --association or --all-clubs.")
  if association is not None and all_clubs:
    raise click.UsageError("--association and --all-clubs are mutually exclusive.")
  if not clubs and association is None and not all_clubs:
    raise click.UsageError("One of --club, --association, or --all-clubs is required.")

  client = _make_client()

  if all_clubs:
    club_arg: int | list[int] = 0
    association_id = None
  elif association is not None:
    association_id = _resolve_association(client, association)
    club_arg = 0
  else:
    club_ids: list[int] = []
    for c in clubs:
      club_ids.extend(_resolve_clubs(client, c))
    club_arg = club_ids[0] if len(club_ids) == 1 else club_ids
    association_id = None

  def _fetch(lic: str) -> Player | None:
    try:
      results = client.search_players(license=lic, club=club_arg, association=association_id)
    except (SavConnectionError, SavResponseError):
      return None
    if not results:
      return None
    p = results[0]
    if photo:
      try:
        detail = client.get_player_detail(p.id, photo=True)
        p = replace(p, photo_url=detail.photo_url)
      except (SavConnectionError, SavResponseError):
        pass
    return p

  if len(license_nums) == 1:
    fetched = [_fetch(license_nums[0])]
  else:
    with ThreadPoolExecutor(max_workers=min(8, len(license_nums))) as pool:
      fetched = list(pool.map(_fetch, license_nums))

  missing = [lic for lic, p in zip(license_nums, fetched) if p is None]
  players = [p for p in fetched if p is not None]

  if not players:
    raise SavCliError(
      f"No player found with licence(s): {', '.join(repr(l) for l in missing)}.",
      code="not_found",
    )
  if missing:
    click.echo(f"Warning: no player found for licence(s): {', '.join(missing)}", err=True)

  projection = ctx.obj.get("fields")

  if output == "json":
    rows = _project([asdict(p) for p in players], projection)
    click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
    return

  if output == "csv":
    default_fields = ["id", "license", "name", "birth_date", "gender", "nationality",
                      "club", "association", "tier", "status", "season", "active", "photo_url"]
    rows = _project([asdict(p) for p in players], projection)
    csv_fields = projection or default_fields
    click.echo(",".join(csv_fields))
    for r in rows:
      click.echo(",".join(str(r[f]) for f in csv_fields))
    return

  for i, player in enumerate(players):
    if i > 0:
      click.echo()
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


@cli.command("profile")
@click.argument("license_num", type=int)
@click.pass_context
def profile_cmd(ctx, license_num):
  """Show the SAV2 player profile for one license number.

  Read-only fetch from jogadoresdb.php?op=2 — the athlete-form view the
  enrollment wizard prefills from. Returns the full canonical profile
  (personal data + address). License alone is enough; internal id is
  resolved transparently via the cached search.
  """
  output = ctx.obj["output"]
  client = _make_client()

  try:
    profile = client.load_player_profile(license_num)
  except (SavConnectionError, SavResponseError) as e:
    raise SavCliError(str(e), code=_exc_code(e))

  fields_proj = ctx.obj.get("fields")

  if output == "json":
    rows = _project([profile], fields_proj)
    click.echo(json.dumps(rows[0] if rows else profile, ensure_ascii=False, indent=2))
    return

  if output == "csv":
    rows = _project([profile], fields_proj)
    keys = fields_proj or list(profile.keys())
    click.echo(",".join(keys))
    if rows:
      click.echo(",".join(str(rows[0].get(k, "")) for k in keys))
    return

  # Table view: key/value with distrito and concelho IDs resolved to names
  # for readability. The per-distrito concelho list is cached client-side.
  display = dict(profile)
  raw_distrito = profile.get("distrito")
  if raw_distrito:
    name = distrito_name(raw_distrito)
    if name:
      display["distrito"] = f"{raw_distrito} ({name})"

  raw_concelho = profile.get("concelho")
  if str(raw_concelho or "").isdigit() and str(raw_distrito or "").isdigit():
    try:
      concelhos = client.list_concelhos(int(raw_distrito))
      name = concelhos.get(int(raw_concelho))
      if name:
        display["concelho"] = f"{raw_concelho} ({name})"
    except (SavConnectionError, SavResponseError):
      pass

  width = max(len(k) for k in display)
  for key, val in display.items():
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
    raise SavCliError(str(e), code=_exc_code(e))

  if not results:
    click.echo("No associations found.")
    return

  fields = ctx.obj.get("fields")
  data = [{"id": a.id, "name": a.name} for a in results]

  if output == "json":
    click.echo(json.dumps(_project(data, fields), ensure_ascii=False, indent=2))
    return

  if output == "csv":
    projected = _project(data, fields)
    csv_fields = fields or ["id", "name"]
    click.echo(",".join(csv_fields))
    for r in projected:
      click.echo(",".join(str(r[f]) for f in csv_fields))
    return

  headers = ["ID", "Name"]
  rows = [[str(a.id), a.name] for a in results]
  _render_table(headers, rows)
  click.echo(f"\n{len(results)} association(s) found.")


@cli.command("clubs")
@click.argument("query", default="", required=False)
@click.option("--association", default=None, type=int, help="Association ID (from 'sav associations').")
@click.option("--all-associations", is_flag=True, default=False, help="Search clubs across every association.")
@click.pass_context
def clubs_cmd(ctx, query, association, all_associations):
  """List clubs, optionally filtered by a name/code query.

  QUERY is an optional case-insensitive substring matched against the short
  name, full name, and code of each club (e.g. "Rio Maior" or "SBC").
  """
  output = ctx.obj["output"]
  if association is None and not all_associations:
    raise click.UsageError("One of --association or --all-associations is required.")
  if association is not None and all_associations:
    raise click.UsageError("--association and --all-associations are mutually exclusive.")

  client = _make_client()

  try:
    results = client.list_clubs(association=association, all_associations=all_associations)
  except (SavConnectionError, SavResponseError) as e:
    raise SavCliError(str(e), code=_exc_code(e))

  if query:
    results = find_club_matches(results, query)

  if not results:
    click.echo("No clubs found.")
    return

  fields = ctx.obj.get("fields")
  data = [{"id": c.id, "name": c.name, "full_name": c.full_name, "code": c.code} for c in results]

  if output == "json":
    click.echo(json.dumps(_project(data, fields), ensure_ascii=False, indent=2))
    return

  if output == "csv":
    projected = _project(data, fields)
    csv_fields = fields or ["id", "name", "full_name", "code"]
    click.echo(",".join(csv_fields))
    for r in projected:
      click.echo(",".join(str(r[f]) for f in csv_fields))
    return

  headers = ["ID", "Name", "Full Name", "Code"]
  rows = [[str(c.id), c.name, c.full_name, c.code] for c in results]
  _render_table(headers, rows, max_widths=[None, 24, 40, 8])
  click.echo(f"\n{len(results)} club(s) found.")




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
    raise SavCliError(str(e), code=_exc_code(e))

  if status.lower() != "all":
    results = [g for g in results if g.game_status == status]

  results = sorted(results, key=game_sort_key)

  if not results:
    click.echo("No games found.")
    return

  fields = ctx.obj.get("fields")

  if output == "json":
    rows = _project([asdict(g) for g in results], fields)
    click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
    return

  if output == "csv":
    default_fields = ["number", "date", "time", "home", "away", "home_score",
                      "away_score", "competition", "tier", "venue", "game_status"]
    rows = _project([asdict(g) for g in results], fields)
    csv_fields = fields or default_fields
    click.echo(",".join(csv_fields))
    for r in rows:
      click.echo(",".join(str(r[f]) for f in csv_fields))
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
    raise SavCliError(str(e), code=_exc_code(e))

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
    raise SavCliError(str(e), code=_exc_code(e))

  games = [g for g in games if g.number == game_number]
  if not games:
    raise SavCliError(f"No game found with number {game_number!r}.", code="not_found")
  if len(games) > 1:
    raise SavCliError(
      f"Multiple games match number {game_number!r}; use a more specific filter.",
      code="ambiguous_match",
    )

  game = games[0]
  if game.id == 0:
    raise SavCliError(
      f"Game {game_number!r} found but has no internal ID — cannot fetch eligible players.",
      code="no_internal_id",
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
      raise SavCliError(str(e), code=_exc_code(e))
    if pdf is None:
      raise SavCliError(f"No eligible players PDF available for the {team} team.", code="no_pdf")
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
      raise SavCliError(str(e), code=_exc_code(e))
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
    raise SavCliError(str(e), code=_exc_code(e))

  results = filter_games(
    results, competition=competition, status=status,
    date_from=date_from, date_to=date_to,
  )
  results = sorted(results, key=game_sort_key)

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



# ── enroll helpers ────────────────────────────────────────────────────────────

def _resolve_enroll_batch(
  client: Any,
  reg_type: int,
  tier_id: int,
  gender_id: int,
) -> tuple[int, Any]:
  """Interactively find an open batch or create one. Returns (batch_id, batch)."""
  type_label = REGISTRATION_TYPE_LABELS.get(reg_type, str(reg_type))
  gender_label = "Feminino" if gender_id == 2 else "Masculino"
  tiers = client.list_player_registration_tiers(gender_id=gender_id)
  tier_name = tiers.get(tier_id, str(tier_id))

  def _create() -> tuple[int, Any]:
    try:
      return create_and_fetch_batch(
        client, type=reg_type, tier_id=tier_id, gender_id=gender_id,
      )
    except RuntimeError as exc:
      raise SavCliError(str(exc), code="batch_error")

  existing = client.find_open_player_registration_batch(
    type=reg_type, tier_id=tier_id, gender_id=gender_id,
  )

  if existing:
    click.echo(
      f"\nOpen batch found: #{existing.number} "
      f"({type_label} · {existing.tier} · {gender_label} · "
      f"{existing.item_count} player(s) already added)"
    )
    choice = click.prompt(
      "Append to existing or create new?",
      type=click.Choice(["append", "new"]),
      default="append",
    )
    if choice == "new":
      if not click.confirm(
        f"Create new {type_label} batch ({tier_name} · {gender_label})?"
      ):
        raise click.Abort()
      return _create()
    return existing.id, existing

  click.echo(f"\nNo open batch for {type_label} · {tier_name} · {gender_label}.")
  if not click.confirm("Create new batch?"):
    raise click.Abort()
  return _create()


def _resolve_enroll_player(client: Any, batch: Any, parsed: dict) -> int | None:
  """Return the player licence from OCR / name search / manual input, or None to skip."""
  try:
    eligible = client._list_revalidable_licenses(batch)
  except Exception as exc:
    raise SavCliError(f"Could not fetch eligible players: {exc}", code="fetch_failed")

  license, candidates, ocr_name, ocr_license = resolve_player_candidates(
    parsed, eligible, client, batch.club_id,
  )
  if license is not None:
    return license

  if len(candidates) > 1:
    click.echo(f"  Multiple players match {ocr_name!r}:")
    for i, p in enumerate(candidates, 1):
      click.echo(f"    {i}.  {p.name}  (licence {p.license})")
    idx = click.prompt("  Pick", type=click.IntRange(1, len(candidates)))
    return int(candidates[idx - 1].license)

  if ocr_license is not None:
    click.echo(f"  OCR licence {ocr_license} is not in the eligible list for this batch.")
    if click.confirm("  Use it anyway?"):
      return ocr_license
  elif ocr_name:
    click.echo(f"  Player not found for {ocr_name!r} in eligible list.")
  else:
    click.echo("  Could not determine player from OCR.")

  while True:
    raw = click.prompt("  Licence number (blank to skip)", default="")
    if not raw:
      return None
    try:
      lic = int(raw)
    except ValueError:
      click.echo("  Not a valid number.")
      continue
    if lic not in eligible:
      click.echo(f"  Licence {lic} is not in the eligible list for this batch.")
      if not click.confirm("  Use it anyway?"):
        continue
    return lic


def _prompt_field(kwarg: str, hint: str = "") -> Any:
  """Prompt the user for one field value, returning the entered value or None."""
  label = f"    {kwarg}" + (f"  ({hint})" if hint else "")
  if kwarg == "guardian_relation":
    click.echo(f"{label}")
    click.echo("      1=Pai  2=Mãe  3=Tutor")
    return int(click.prompt("      Relation", type=click.Choice(["1", "2", "3"])))
  entered = click.prompt(label, default="")
  return entered if entered else None


def _confirm_enroll(result: Any, sav_profile: dict, license: int) -> dict | None:
  """
  Show reconciliation summary and prompt for needs_review fields.
  Returns final kwargs dict (license already popped out) or None to skip.
  """
  kwargs = dict(result.kwargs)
  any_changes = bool(result.updated or result.kept or result.needs_review)

  if result.updated:
    click.echo("  Updated (OCR overrides SAV):")
    for kwarg, (sav_val, ocr_val) in result.updated.items():
      click.echo(f"    {kwarg}:  {sav_val!r}  →  {ocr_val!r}")

  if result.kept:
    click.echo("  Kept from SAV (close enough):")
    for kwarg, (sav_val, ocr_val, sim) in result.kept.items():
      click.echo(f"    {kwarg}:  {sav_val!r}  (OCR: {ocr_val!r}, {sim:.0%} match)")

  if result.needs_review:
    click.echo("  Needs review (low OCR confidence):")
    for kwarg in result.needs_review:
      sav_key = KWARG_TO_SAV_KEY.get(kwarg, "")
      sav_val = str(sav_profile.get(sav_key) or "") if sav_key else ""
      ocr_val = kwargs.get(kwarg)
      if ocr_val is not None:
        hint = f"OCR: {ocr_val!r}" + (f", SAV: {sav_val!r}" if sav_val else "")
      elif sav_val:
        hint = f"SAV: {sav_val!r} (kept if blank)"
      else:
        hint = "no value found"
      val = _prompt_field(kwarg, hint)
      if val is None:
        continue
      if kwarg == "distrito_id":
        resolved = find_distrito_id(val) if isinstance(val, str) else None
        if resolved is None:
          click.echo(f"    {val!r} is not a known distrito — keeping SAV value.")
          continue
        kwargs[kwarg] = resolved
      elif kwarg == "concelho_id":
        resolved = find_id_by_name(val, result.concelhos) if isinstance(val, str) else None
        if resolved is None:
          known = ", ".join(sorted(result.concelhos.values())) or "(none — distrito unknown)"
          click.echo(f"    {val!r} is not a known concelho for this distrito.")
          click.echo(f"    Known: {known}")
          continue
        kwargs[kwarg] = resolved
      else:
        kwargs[kwarg] = val

  name_str = sav_profile.get("nome", "") or ""
  player_label = f"{name_str} (licence {license})" if name_str else f"licence {license}"

  if not any_changes:
    click.echo(f"  No changes — {player_label}.")

  _print_submission_summary(kwargs, result, sav_profile)

  if not click.confirm(f"  Submit {player_label}?", default=True):
    return None

  kwargs.pop("license", None)
  return kwargs


def _format_submit_value(
  val: Any,
  kwarg: str | None = None,
  *,
  concelhos: dict[int, str] | None = None,
) -> str:
  if isinstance(val, bool):
    return "yes" if val else "no"
  if val is None or val == "":
    return "—"
  if kwarg == "distrito_id":
    # When val is the resolved int we want the name; when val is the raw
    # OCR text (held in result.ocr) we want it verbatim. distrito_name()
    # returns "" for non-int input, so falling back to str(val) covers both.
    return distrito_name(val) or str(val)
  if kwarg == "concelho_id" and concelhos and not isinstance(val, str):
    try:
      return concelhos.get(int(val), str(val))
    except (ValueError, TypeError):
      return str(val)
  if kwarg in ("id_type", "guardian_relation") and not isinstance(val, str):
    table = ID_TYPES if kwarg == "id_type" else GUARDIAN_RELATIONS
    try:
      return table.get(int(val), str(val))
    except (ValueError, TypeError):
      return str(val)
  return str(val)


def _print_submission_summary(kwargs: dict, result: Any, sav_profile: dict) -> None:
  """Render the final pre-submit table: SAV vs OCR side-by-side, plus the chosen value.

  Final column shows what SAV will see (kept SAV value, OCR override, or user-typed)
  with a source tag in parens. Empty SAV/OCR cells render as em-dash.
  """
  rows: list[tuple[str, str, str, str]] = []
  for kwarg, (label, sav_key) in ENROLLMENT_FIELD_META.items():
    sav_raw = sav_profile.get(sav_key) if sav_key else None
    ocr_raw = result.ocr.get(kwarg)
    has_sav = sav_raw not in (None, "")
    has_ocr = ocr_raw not in (None, "")

    if kwarg in kwargs:
      final_raw = kwargs[kwarg]
      if final_raw in (None, ""):
        continue
      if kwarg in result.updated:
        source = "OCR over SAV"
      elif kwarg in result.needs_review:
        source = "user"
      else:
        source = "OCR"
    elif has_sav:
      final_raw = sav_raw
      if kwarg in result.retrain_corrections:
        source = "SAV (OCR mismatch — will retrain)"
      elif kwarg in result.kept:
        source = "SAV kept (close to OCR)"
      elif kwarg in result.needs_review:
        source = "SAV kept (review skipped)"
      else:
        source = "SAV"
    else:
      continue

    sav_str   = _format_submit_value(sav_raw, kwarg, concelhos=result.concelhos) if has_sav else "—"
    ocr_str   = _format_submit_value(ocr_raw, kwarg, concelhos=result.concelhos) if has_ocr else "—"
    final_str = f"{_format_submit_value(final_raw, kwarg, concelhos=result.concelhos)}  [{source}]"
    rows.append((label, sav_str, ocr_str, final_str))

  if not rows:
    return

  click.echo("\n  Submission summary:")
  label_w = max(len("Field"), max(len(r[0]) for r in rows))
  sav_w   = max(len("SAV"),   max(len(r[1]) for r in rows))
  ocr_w   = max(len("OCR"),   max(len(r[2]) for r in rows))

  click.echo(f"    {'Field'.ljust(label_w)}  {'SAV'.ljust(sav_w)}  {'OCR'.ljust(ocr_w)}  Final")
  click.echo(f"    {'-'*label_w}  {'-'*sav_w}  {'-'*ocr_w}  -----")
  for label, sav, ocr, final in rows:
    click.echo(f"    {label.ljust(label_w)}  {sav.ljust(sav_w)}  {ocr.ljust(ocr_w)}  {final}")
  click.echo("")


@cli.command("enroll")
@click.argument("pdfs", nargs=-1, required=True, type=click.Path(exists=True))
@click.pass_context
def enroll_cmd(ctx, pdfs):
  """Enroll players from FPB registration PDFs into SAV."""
  try:
    from sav_parsers import classify, close_processing, parse_fpb_mod1
    from sav_shared import KWARG_TO_ENTITY, reconcile_fpb_mod1
  except ImportError as exc:
    raise SavCliError(f"sav-parsers not installed: {exc}", code="import_error")

  client = _make_client()

  batch_id: int | None = None
  batch: Any = None

  for pdf_path in pdfs:
    click.echo(f"\n── {pdf_path} ──")

    # Step 1 — classify
    try:
      doc_type = classify(pdf_path)
    except Exception as exc:
      click.echo(f"  classify error: {exc}", err=True)
      continue
    if doc_type != "fpb-mod1":
      click.echo(f"  Unsupported document type {doc_type!r} — skipped.")
      continue

    # Step 2 — parse
    click.echo("  Processing OCR ...", nl=False)
    try:
      parse_result = parse_fpb_mod1(pdf_path)
      parsed = parse_result["fields"]
      processing_id = parse_result["processing_id"]
    except Exception as exc:
      click.echo(f"\n  parse error: {exc}", err=True)
      continue
    click.echo(f" Finished ({processing_id})!")

    # Once parse_fpb_mod1 has created a processing session, we must close it
    # on every exit path or the dir leaks under files/processing/<id>/ until
    # gc sweeps it. close_called flips to True at step 10 (success path);
    # the finally falls back to a no-corrections close for any earlier exit.
    close_called = False
    try:
      # Step 4 — resolve batch (once per invocation, derived from first PDF)
      if batch is None:
        # When the form has neither tipo_inscricao box checked, derive_enrollment_params
        # falls back to a NIF-based club-roster scan. Surface that since reg_type
        # otherwise looks like it came from OCR, and the scan is N profile fetches.
        type_inferred = not (
          parsed_bool(parsed, "tipo_inscricao_revalidacao")
          or parsed_bool(parsed, "tipo_inscricao_primeira")
        )
        try:
          if type_inferred:
            click.echo("  No tipo_inscricao on form; checking club roster by NIF ...", nl=False)
          reg_type, tier_id, gender_id = derive_enrollment_params(parsed, client)
          if type_inferred:
            label = REGISTRATION_TYPE_LABELS.get(reg_type, str(reg_type))
            click.echo(f" {label}.")
          batch_id, batch = _resolve_enroll_batch(
            client, reg_type, tier_id, gender_id,
          )
        except ValueError as exc:
          raise SavCliError(str(exc), code="parse_error")
        except click.Abort:
          return
        except (SavConnectionError, SavResponseError) as exc:
          raise SavCliError(str(exc), code=_exc_code(exc))

      # Step 5 — resolve player licence
      try:
        license = _resolve_enroll_player(client, batch, parsed)
      except (SavConnectionError, SavResponseError) as exc:
        raise SavCliError(str(exc), code=_exc_code(exc))
      if license is None:
        click.echo("  Skipped.")
        continue

      # Step 6 — fetch SAV profile (op=2 athlete form). Read-only; any
      # server-side validation surfaces at real submit.
      try:
        sav_profile = client.load_player_profile(license, club_id=batch.club_id)
      except (SavConnectionError, SavResponseError) as exc:
        click.echo(f"  Could not load player profile: {exc}", err=True)
        continue

      # Step 7 — reconcile OCR vs SAV. reconcile_fpb_mod1 fetches the
      # distrito-scoped concelho list itself (cached client-side); without it
      # concelho silently falls to needs_review.
      try:
        result = reconcile_fpb_mod1(parsed, sav_profile, client=client)
      except (SavConnectionError, SavResponseError) as exc:
        click.echo(f"  Could not load concelhos: {exc}", err=True)
        continue
      except Exception as exc:
        click.echo(f"  reconcile error: {exc}", err=True)
        continue

      # Step 8 — confirm with user
      kwargs = _confirm_enroll(result, sav_profile, license)
      if kwargs is None:
        click.echo("  Skipped.")
        continue

      # Step 9 — submit (retry loop for missing guardian fields)
      submitted = False
      internal_id: int | None = None
      while not submitted:
        try:
          internal_id = client.add_player_to_registration_batch(
            batch_id, license, **kwargs,
          )
          click.echo(f"  Added licence {license} to batch #{batch_id}.")
          submitted = True
        except SavConfigError as exc:
          click.echo(f"  Guardian info required for minor:")
          for field_name in parse_missing_guardian_fields(exc):
            val = _prompt_field(field_name)
            if val is not None:
              kwargs[field_name] = val
        except (SavConnectionError, SavResponseError) as exc:
          raise SavCliError(str(exc), code=_exc_code(exc))

      # Step 9b — upload the source PDF as Modelo 1. Non-fatal: if it fails
      # the player is still registered, so we just warn and continue.
      try:
        client.upload_player_registration_document(
          batch_id, license, pdf_path, internal_id=internal_id,
        )
        click.echo(f"  Uploaded {pdf_path} (Modelo 1).")
      except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
        click.echo(
          f"  Warning: enrollment succeeded but document upload failed: {exc}",
          err=True,
        )

      # Step 10 — close processing session; only send corrections the user
      # explicitly answered (needs_review). Updated and kept were silent paths
      # from the user's perspective, so we don't stage them as labeled training
      # data — that would risk noise in the dataset.
      corrections: dict[str, str] = {}
      for kwarg in result.needs_review:
        entity = KWARG_TO_ENTITY.get(kwarg)
        val = kwargs.get(kwarg)
        if entity and val is not None:
          corrections[entity] = str(val)
      corrections.update(result.retrain_corrections)
      close_called = True
      try:
        close_processing(processing_id, corrections=corrections or None)
      except Exception as exc:
        click.echo(f"  Warning: could not close processing {processing_id}: {exc}", err=True)
    finally:
      if not close_called:
        try:
          close_processing(processing_id)
        except Exception:
          pass


def main():
  cli()
