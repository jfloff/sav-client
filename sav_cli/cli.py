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

from collections.abc import Mapping
import json
import shutil
from dataclasses import asdict
from datetime import date
from typing import Any

import click
from rich import box
from rich.console import Console
from rich.table import Table
from sav_parsers.types import DocType

from sav_client import SavClient, Player
from sav_client.exceptions import (
  SavAuthError,
  SavConfigError,
  SavConnectionError,
  SavResponseError,
)
from sav_shared.clubs import find_club_matches
from sav_shared.enrollment import (
  KWARG_TO_SAV_KEY,
  create_and_fetch_batch,
  derive_enrollment_params,
  find_player_license_by_nif,
  parse_missing_guardian_fields,
  parsed_bool,
  resolve_player_candidates,
)
from sav_shared.fields import ENROLLMENT_FIELD_META
from sav_shared.fpb_mod1 import reconcile_fpb_mod1
from sav_shared.games import filter_games, game_sort_key
from sav_shared.lookups import (
  DOC_TYPE_CHOICES,
  GUARDIAN_RELATIONS,
  ID_TYPES,
  REGISTRATION_TYPE_LABELS,
  distrito_name,
  doc_type_to_tipo_doc,
  find_distrito_id,
  find_id_by_name,
  normalize_doc_type,
)
from sav_shared.medical_exam import extract_medical_exam_info


# Tracks the root --output flag so SavCliError.show() can format errors
# appropriately even after Click has unwound the context stack.
_OUTPUT_MODE: str = "table"
_VERBOSE: bool = False

# Cap on how many clubs a single --club fragment may resolve to before we
# refuse to fan out the search. Short/ambiguous queries can otherwise silently
# trigger parallel searches against dozens of clubs.
_CLUB_MATCH_LIMIT = 5


def _console(*, err: bool = False) -> Console:
  stream_name = "stderr" if err else "stdout"
  return Console(file=click.get_text_stream(stream_name))


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


def _doc_type_text(doc_type: DocType | str) -> str:
  return doc_type.value if isinstance(doc_type, DocType) else str(doc_type)

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
  "--verbose",
  is_flag=True,
  help="Show verbose human output.",
)
@click.option(
  "--fields",
  default=None,
  help="Comma-separated field projection for JSON/CSV output (e.g. 'name,license,active'). Ignored for table output.",
)
@click.pass_context
def cli(ctx, output, verbose, fields):
  """SAV2 API client."""
  global _OUTPUT_MODE, _VERBOSE
  _OUTPUT_MODE = output
  _VERBOSE = verbose
  ctx.ensure_object(dict)
  ctx.obj["output"] = output
  ctx.obj["verbose"] = verbose
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
  console = _console()
  type_label = REGISTRATION_TYPE_LABELS.get(reg_type, str(reg_type))
  gender_label = "Feminino" if gender_id == 2 else "Masculino"

  def _create() -> tuple[int, Any]:
    try:
      with console.status("[bold cyan]:hammer_and_wrench: Creating registration batch...[/]"):
        return create_and_fetch_batch(
          client, batch_type=reg_type, tier_id=tier_id, gender_id=gender_id,
        )
    except RuntimeError as exc:
      raise SavCliError(str(exc), code="batch_error")

  with console.status("[bold cyan]:open_file_folder: Looking up open registration batches...[/]"):
    tiers = client.list_player_registration_tiers(gender_id=gender_id)
    tier_name = tiers.get(tier_id, str(tier_id))
    existing = client.find_open_player_registration_batch(
      type=reg_type, tier_id=tier_id, gender_id=gender_id,
    )

  if existing:
    console.print(
      f"[cyan]:open_file_folder: Open batch found:[/] #{existing.number} "
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

  console.print(
    f"[yellow]:warning: No open batch for {type_label} · {tier_name} · {gender_label}.[/]"
  )
  if not click.confirm("Create new batch?"):
    raise click.Abort()
  return _create()


def _find_enrolled_in_matching_batches(
  client: Any, batch: Any, license: int,
) -> Any | None:
  """Locate the open batch (same type/tier/gender) where ``license`` is
  already enrolled, or None.

  SAV2 only allows a player in one open batch at a time, so a parsed form
  whose licence is missing from the current batch's eligible list usually
  means they're sitting in a different open batch with the same params —
  which is the batch the update path needs to point at.
  """
  try:
    all_batches = client.list_player_registration_batches()
  except (SavConnectionError, SavResponseError):
    return None
  candidates = [
    b for b in all_batches
    if b.is_open
    and b.type_id == batch.type_id
    and b.tier_id == batch.tier_id
    and b.gender_id == batch.gender_id
  ]
  for b in candidates:
    try:
      items = client.list_player_registration_batch_items(b.id)
    except (SavConnectionError, SavResponseError):
      continue
    if any(item["license"] == license for item in items):
      return b
  return None


def _resolve_enroll_player(
  client: Any, batch: Any, parsed: dict,
) -> tuple[int, Any] | None:
  """Return ``(license, target_batch)`` for the parsed form, or None to skip.

  ``target_batch`` is normally the input ``batch``, but switches when the
  player is already enrolled in a different open batch with matching
  params — in that case the caller should add/update against the returned
  batch instead, since SAV2 won't allow re-adding to a second one.
  """
  console = _console()
  try:
    with console.status("[bold cyan]:busts_in_silhouette: Matching the form to eligible players...[/]"):
      eligible = client._list_revalidable_licenses(batch)
      license, candidates, ocr_name, ocr_license = resolve_player_candidates(
        parsed, eligible, client, batch.club_id,
      )
  except Exception as exc:
    raise SavCliError(f"Could not fetch eligible players: {exc}", code="fetch_failed")

  if license is not None:
    return license, batch

  # Already-enrolled fallback: licence-first, then NIF, against open batches
  # matching the same (type, tier, gender). Returning the matching batch lets
  # the caller route the wizard's edit path (op=30 + op=33/op=31).
  enrolled_license: int | None = None
  if ocr_license is not None:
    enrolled_license = ocr_license
  else:
    nif_license = find_player_license_by_nif(parsed, client, club_id=batch.club_id)
    if nif_license is not None:
      enrolled_license = nif_license

  if enrolled_license is not None:
    with console.status("[bold cyan]:link: Checking other open batches for an existing enrolment...[/]"):
      target = _find_enrolled_in_matching_batches(client, batch, enrolled_license)
    if target is not None:
      if target.id != batch.id:
        console.print(
          f"[cyan]:repeat: Already enrolled in batch #{target.number} (id={target.id}); "
          f"updating there."
        )
      else:
        console.print("[cyan]:repeat: Already enrolled in this batch — updating.[/]")
      return enrolled_license, target

  if len(candidates) > 1:
    console.print(f"[yellow]:warning: Multiple players match {ocr_name!r}:[/]")
    for i, p in enumerate(candidates, 1):
      console.print(f"  {i}.  {p.name}  (licence {p.license})")
    idx = click.prompt("  Pick", type=click.IntRange(1, len(candidates)))
    return int(candidates[idx - 1].license), batch

  if ocr_license is not None:
    console.print(
      f"[yellow]:warning: OCR licence {ocr_license} is not in the eligible list for this batch.[/]"
    )
    if click.confirm("  Use it anyway?"):
      return ocr_license, batch
  elif ocr_name:
    console.print(f"[yellow]:warning: Player not found for {ocr_name!r} in eligible list.[/]")
  else:
    console.print("[yellow]:warning: Could not determine player from OCR.[/]")

  while True:
    raw = click.prompt("  Licence number (blank to skip)", default="")
    if not raw:
      return None
    try:
      lic = int(raw)
    except ValueError:
      console.print("[yellow]:warning: Not a valid number.[/]")
      continue
    if lic not in eligible:
      with console.status("[bold cyan]:link: Checking other open batches for an existing enrolment...[/]"):
        target = _find_enrolled_in_matching_batches(client, batch, lic)
      if target is not None:
        if target.id != batch.id:
          console.print(
            f"[cyan]:repeat: Licence {lic} is enrolled in batch #{target.number} "
            f"(id={target.id}); updating there."
          )
        else:
          console.print(f"[cyan]:repeat: Licence {lic} is already in this batch — updating.[/]")
        return lic, target
      console.print(
        f"[yellow]:warning: Licence {lic} is not in the eligible list for this batch.[/]"
      )
      if not click.confirm("  Use it anyway?"):
        continue
    return lic, batch


def _prompt_field(
  kwarg: str,
  hint: str = "",
  *,
  field_type: str | Mapping[Any, str] = "text",
  prompt_text: str | None = None,
) -> Any:
  """Prompt the user for one field value, returning the entered value or None."""
  console = _console()
  label = f"    {kwarg}" + (f"  ({hint})" if hint else "")
  if isinstance(field_type, Mapping):
    if not field_type:
      raise ValueError("Choice field_type must not be empty.")
    console.print(label)
    options_text = "  ".join(f"{key}={value}" for key, value in field_type.items())
    console.print(f"[dim]      {options_text}[/]")
    choices = {str(key): key for key in field_type}
    entered = click.prompt(prompt_text or label, type=click.Choice(tuple(choices)))
    return choices[entered]
  if field_type == "date":
    while True:
      entered = click.prompt(prompt_text or label, default="")
      if not entered:
        return None
      try:
        return date.fromisoformat(entered).isoformat()
      except ValueError:
        console.print("[yellow]:warning: Enter the date as YYYY-MM-DD.[/]")
  if field_type != "text":
    raise ValueError(f"Unknown prompt field_type {field_type!r}.")
  entered = click.prompt(label, default="")
  return entered if entered else None


def _confirm_enroll(result: Any, sav_profile: dict, license: int) -> dict | None:
  """
  Show reconciliation summary and prompt for needs_review fields.
  Returns final kwargs dict (license already popped out) or None to skip.
  """
  console = _console()
  kwargs = dict(result.kwargs)
  any_changes = bool(result.updated or result.kept or result.needs_review)

  if result.updated:
    console.print("[green]:white_check_mark: Updated (OCR overrides SAV):[/]")
    for kwarg, (sav_val, ocr_val) in result.updated.items():
      console.print(f"    {kwarg}:  {sav_val!r}  ->  {ocr_val!r}")

  if result.needs_review:
    console.print("[yellow]:warning: Needs review (low OCR confidence):[/]")
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
          console.print(f"[yellow]:warning: {val!r} is not a known distrito; keeping SAV value.[/]")
          continue
        kwargs[kwarg] = resolved
      elif kwarg == "concelho_id":
        resolved = find_id_by_name(val, result.concelhos) if isinstance(val, str) else None
        if resolved is None:
          known = ", ".join(sorted(result.concelhos.values())) or "(none — distrito unknown)"
          console.print(f"[yellow]:warning: {val!r} is not a known concelho for this distrito.[/]")
          console.print(f"[dim]    Known: {known}[/]")
          continue
        kwargs[kwarg] = resolved
      else:
        kwargs[kwarg] = val

  name_str = sav_profile.get("nome", "") or ""
  player_label = f"{name_str} (licence {license})" if name_str else f"licence {license}"

  if not any_changes:
    console.print(f"[cyan]:information_source: No changes for {player_label}.[/]")

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
  if kwarg in ("id_type", "guardian_relation"):
    # SAV stores `tipo` as a numeric string ("1"), OCR may carry raw text
    # ("Cartão de Cidadão"); int() succeeds on the former, raises on the
    # latter — fall through to str(val) when the lookup doesn't apply.
    table = ID_TYPES if kwarg == "id_type" else GUARDIAN_RELATIONS
    try:
      return table.get(int(val), str(val))
    except (ValueError, TypeError):
      return str(val)
  return str(val)


def _print_submission_summary(kwargs: dict, result: Any, sav_profile: dict) -> None:
  """Render the final pre-submit summary.

  Default mode shows a compact field/value/source view. Verbose mode expands
  that into the full SAV vs OCR side-by-side table with the chosen final value.
  Empty SAV/OCR cells render as em-dash.
  """
  rows: list[tuple[str, str, str, str, str]] = []
  for kwarg, (label, sav_key) in ENROLLMENT_FIELD_META.items():
    sav_raw = sav_profile.get(sav_key) if sav_key else None
    ocr_raw = result.ocr.get(kwarg)
    has_sav = sav_raw not in (None, "")
    has_ocr = ocr_raw not in (None, "")

    if kwarg in kwargs:
      final_raw = kwargs[kwarg]
      if final_raw in (None, ""):
        continue
      if kwarg in result.needs_review:
        source = "user"
      else:
        source = "OCR"
    elif has_sav:
      final_raw = sav_raw
      source = "SAV"
    else:
      continue

    sav_str   = _format_submit_value(sav_raw, kwarg, concelhos=result.concelhos) if has_sav else "—"
    ocr_str   = _format_submit_value(ocr_raw, kwarg, concelhos=result.concelhos) if has_ocr else "—"
    ocr_confidence = getattr(result, "ocr_confidence", {}).get(kwarg)
    if ocr_confidence is not None and has_ocr:
      ocr_str = f"{ocr_str} ({ocr_confidence:.0%})"
    final_str = _format_submit_value(final_raw, kwarg, concelhos=result.concelhos)
    rows.append((label, sav_str, ocr_str, final_str, source))

  if not rows:
    return

  console = _console()
  if _VERBOSE:
    table = Table(
      box=box.SIMPLE_HEAVY,
      header_style="bold cyan",
      title="Submission Summary",
      title_style="bold cyan",
    )
    table.add_column("Field", style="bold")
    table.add_column("SAV")
    table.add_column("OCR")
    table.add_column("Final", style="green")
    for label, sav, ocr, final, source in rows:
      table.add_row(label, sav, ocr, f"{final}  [{source}]")
  else:
    table = Table(
      box=box.SIMPLE_HEAVY,
      header_style="bold cyan",
      title="Submission Summary",
      title_style="bold cyan",
    )
    table.add_column("Field", style="bold")
    table.add_column("Value", style="green")
    table.add_column("Source", style="cyan")
    for label, _, _, final, source in rows:
      table.add_row(label, final, source)
  console.print()
  console.print(table)
  console.print()


# Field types accepted by enrollment create/update --field. Defined here (before the
# enrollment group) so the --field decorator can reference _UPDATE_FIELDS at import time.
_UPDATE_FIELDS: dict[str, tuple[str, type]] = {
  "id_type":        ("step1", int),
  "id_number":      ("step1", str),
  "id_expiry":      ("step1", str),
  "telemovel":      ("step1", str),
  "telefone":       ("step1", str),
  "email":          ("step1", str),
  "nome_pai":       ("step1", str),
  "nome_mae":       ("step1", str),
  "morada":         ("step2", str),
  "cod_postal":     ("step2", str),
  "localidade_txt": ("step2", str),
  "distrito_id":    ("resolve_distrito", int),
  "concelho_id":    ("resolve_concelho", int),
}
# Aliases so users don't have to type the `_id` suffix.
_UPDATE_FIELD_ALIASES = {"distrito": "distrito_id", "concelho": "concelho_id"}


@cli.group("enrollment")
def enrollment_grp():
  """Create, read, update, and delete player enrolments in Revalidação batches."""


@enrollment_grp.command("create")
@click.argument("pdfs", nargs=-1, required=False, type=click.Path(exists=True))
@click.option(
  "--mod1", "mod1_path", type=click.Path(exists=True), default=None,
  help="Explicit fpb_modelo_1 form (skips auto-classify; trains the classifier).",
)
@click.option(
  "--medical-exam", type=click.Path(exists=True),
  help="Parse and upload the matching exame_medico PDF.",
)
@click.option(
  "--batch", "batch_id_opt", type=int, default=None,
  help="Batch ID for manual enrolment (no PDF).",
)
@click.option(
  "--license", "license_opt", type=int, default=None,
  help="Player licence for manual enrolment (no PDF).",
)
@click.option(
  "--field", "fields", multiple=True, metavar="KEY=VAL",
  help=(
    "Override a field value (repeatable). Applied on top of OCR in PDF mode or "
    "as the full field set in manual mode. "
    "Supported: " + ", ".join(sorted(_UPDATE_FIELDS)) + "."
  ),
)
@click.pass_context
def enrollment_create_cmd(ctx, pdfs, mod1_path, medical_exam, batch_id_opt, license_opt, fields):
  """Enroll players from FPB registration PDFs into SAV.

  \b
    sav enrollment create player.pdf
        Auto-classify, OCR-reconcile, and enroll.
    sav enrollment create --mod1 player.pdf --medical-exam exam.pdf
        Explicit form types (skip classify); trains the classifier.
    sav enrollment create --batch ID --license ID [--field KEY=VAL ...]
        Manual enrolment from SAV profile, no PDF.
  """
  from sav_parsers import classify, close_processing, parse_em, parse_fpb_mod1, train_classifier
  from sav_shared.fields import KWARG_TO_ENTITY

  pdf_mode = bool(pdfs or mod1_path)
  manual_mode = bool(batch_id_opt is not None and license_opt is not None)

  if not pdf_mode and not manual_mode:
    raise click.UsageError(
      "Pass one or more PDFs (or --mod1), or --batch + --license for manual enrolment."
    )
  if medical_exam and not pdf_mode:
    raise click.UsageError("--medical-exam requires a PDF or --mod1.")

  # Manual mode: no PDF, enroll directly from SAV profile + optional field overrides.
  if manual_mode and not pdf_mode:
    client = _make_client()
    field_overrides = _parse_update_fields(fields)
    console = _console()
    try:
      with console.status("[bold cyan]:open_book: Loading SAV player profile...[/]"):
        sav_profile = client.load_player_profile(license_opt)
    except (SavConnectionError, SavResponseError) as exc:
      raise SavCliError(f"Could not load player profile: {exc}", code=_exc_code(exc))
    console.print(
      f"[bold]Enrolling:[/] {sav_profile.get('nome', '?')} "
      f"(licence {license_opt}) in batch #{batch_id_opt}"
    )
    if field_overrides:
      console.print(
        "[cyan]:information_source: Field overrides:[/] "
        + ", ".join(f"{k}={v}" for k, v in sorted(field_overrides.items()))
      )
    if not click.confirm("Proceed?"):
      console.print("[yellow]:fast_forward: Aborted.[/]")
      return
    try:
      with console.status("[bold cyan]:inbox_tray: Submitting enrollment...[/]"):
        client.add_player_to_registration_batch(
          batch_id_opt, license_opt, **field_overrides,
        )
    except (SavConnectionError, SavResponseError, ValueError) as exc:
      raise SavCliError(str(exc), code=_exc_code(exc))
    console.print(
      f"[green]:white_check_mark: Enrolled licence {license_opt} in batch #{batch_id_opt}.[/]"
    )
    return

  client = _make_client()

  batch_id: int | None = None
  batch: Any = None
  console = _console()
  err_console = _console(err=True)

  # Build list of (pdf_path, explicit_doc_type) pairs.
  # Positional PDFs are auto-classified; --mod1 path skips classify.
  paths_to_process: list[tuple[str, DocType | None]] = [(p, None) for p in pdfs]
  if mod1_path:
    paths_to_process.append((mod1_path, DocType.FPB_MOD1))

  for pdf_path, explicit_doc_type in paths_to_process:
    medical_exam_path = medical_exam
    if explicit_doc_type is None:
      # Step 1 — classify
      try:
        with console.status("[bold cyan]:mag: Classifying document...[/]"):
          doc_type = classify(pdf_path)
      except Exception as exc:
        err_console.print(f"[red]:x: Classify error:[/] {exc} [dim]({pdf_path})[/]")
        continue
      if doc_type != DocType.FPB_MOD1:
        console.print(
          f"[yellow]:warning: Unsupported document type {_doc_type_text(doc_type)!r}; skipped.[/] "
          f"[dim]({pdf_path})[/]"
        )
        continue
    else:
      # Explicit type: skip classify, train classifier with known label.
      doc_type = explicit_doc_type
      try:
        with console.status("[bold cyan]:bookmark_tabs: Labeling mod1 form for classifier training...[/]"):
          train_classifier(pdf_path, doc_type)
      except Exception as exc:
        err_console.print(
          f"[yellow]:warning: Could not submit classifier training for mod1:[/] "
          f"{exc} [dim]({pdf_path})[/]"
        )

    # Step 2 — parse
    try:
      with console.status("[bold cyan]:robot: Running OCR and parsing form...[/]"):
        parse_result = parse_fpb_mod1(pdf_path)
        parsed = parse_result["fields"]
        processing_id = parse_result["processing_id"]
    except Exception as exc:
      err_console.print(f"[red]:x: Parse error:[/] {exc} [dim]({pdf_path})[/]")
      continue
    console.print(
      f"[green]:white_check_mark: OCR ready[/] [dim]{pdf_path} ({processing_id})[/]"
    )

    # Once parse_fpb_mod1 has created a processing session, we must close it
    # on every exit path or the dir leaks under files/processing/<id>/ until
    # gc sweeps it. close_called flips to True at step 10 (success path);
    # the finally falls back to a no-corrections close for any earlier exit.
    close_called = False
    medical_processing_id: str | None = None
    medical_close_pending = False
    manual_exam_date = False
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
          status_message = (
            "[bold cyan]:mag_right: No tipo_inscricao on form; checking club roster by NIF...[/]"
            if type_inferred
            else "[bold cyan]:card_index_dividers: Resolving enrollment parameters...[/]"
          )
          with console.status(status_message):
            reg_type, tier_id, gender_id = derive_enrollment_params(parsed, client)
          if type_inferred:
            label = REGISTRATION_TYPE_LABELS.get(reg_type, str(reg_type))
            console.print(f"[cyan]:information_source: Inferred registration type:[/] {label}")
          batch_id, batch = _resolve_enroll_batch(
            client, reg_type, tier_id, gender_id,
          )
        except ValueError as exc:
          raise SavCliError(str(exc), code="parse_error")
        except click.Abort:
          return
        except (SavConnectionError, SavResponseError) as exc:
          raise SavCliError(str(exc), code=_exc_code(exc))

      # Step 5 — resolve player licence (may redirect to a different open
      # batch when the player is already enrolled there — SAV2 only permits
      # one open enrolment per player).
      try:
        resolved = _resolve_enroll_player(client, batch, parsed)
      except (SavConnectionError, SavResponseError) as exc:
        raise SavCliError(str(exc), code=_exc_code(exc))
      if resolved is None:
        console.print("[yellow]:fast_forward: Skipped.[/]")
        continue
      license, batch = resolved
      batch_id = batch.id

      # Step 6 — fetch SAV profile (op=2 athlete form). Read-only; any
      # server-side validation surfaces at real submit.
      try:
        with console.status("[bold cyan]:open_book: Loading SAV player profile...[/]"):
          sav_profile = client.load_player_profile(license, club_id=batch.club_id)
      except (SavConnectionError, SavResponseError) as exc:
        err_console.print(f"[red]:x: Could not load player profile:[/] {exc}")
        continue

      # Step 7 — reconcile OCR vs SAV. reconcile_fpb_mod1 fetches the
      # distrito-scoped concelho list itself (cached client-side); without it
      # concelho silently falls to needs_review.
      try:
        with console.status("[bold cyan]:balance_scale: Reconciling OCR with SAV...[/]"):
          result = reconcile_fpb_mod1(parsed, sav_profile, client=client)
      except (SavConnectionError, SavResponseError) as exc:
        err_console.print(f"[red]:x: Could not load concelhos:[/] {exc}")
        continue
      except Exception as exc:
        err_console.print(f"[red]:x: Reconcile error:[/] {exc}")
        continue

      medical_exam_info = None
      if medical_exam_path:
        try:
          with console.status("[bold cyan]:stethoscope: Processing medical exam...[/]"):
            medical_parse_result = parse_em(medical_exam_path)
          medical_processing_id = medical_parse_result["processing_id"]
          medical_close_pending = True
          medical_exam_info = extract_medical_exam_info(medical_parse_result["fields"])
        except SavCliError:
          raise
        except Exception as exc:
          err_console.print(
            f"[red]:x: Medical exam parse error:[/] {exc} [dim]({medical_exam_path})[/]"
          )
          continue
        try:
          with console.status("[bold cyan]:bookmark_tabs: Labeling medical exam for classifier training...[/]"):
            train_classifier(medical_exam_path, DocType.EM)
        except Exception as exc:
          err_console.print(
            f"[yellow]:warning: Could not submit classifier training for medical exam:[/] "
            f"{exc} [dim]({medical_exam_path})[/]"
          )

        if medical_exam_info.exam_date:
          console.print(
            f"[cyan]:information_source: Medical exam date:[/] "
            f"{medical_exam_info.exam_date} [dim]({medical_exam_path})[/]"
          )
        else:
          raw_text = medical_exam_info.raw_exam_date or "not found"
          console.print(
            f"[yellow]:warning: Medical exam date needs review:[/] "
            f"{raw_text!r} [dim]({medical_exam_path})[/]"
          )

      # Step 8 — confirm with user
      kwargs = _confirm_enroll(result, sav_profile, license)
      if kwargs is None:
        console.print("[yellow]:fast_forward: Skipped.[/]")
        continue
      if medical_exam_info is not None and medical_exam_info.exam_date:
        kwargs["exam_date"] = medical_exam_info.exam_date

      # Apply --field overrides on top of reconciled values (user wins over OCR).
      if fields:
        field_overrides = _parse_update_fields(fields)
        if field_overrides:
          applied = ", ".join(f"{k}={v}" for k, v in sorted(field_overrides.items()))
          console.print(f"[cyan]:information_source: Applying manual field overrides:[/] {applied}")
        kwargs.update(field_overrides)
      if not kwargs.get("exam_date"):
        entered = _prompt_field(
          "exam_date",
          (
            f"OCR: {medical_exam_info.raw_exam_date!r}"
            if medical_exam_info is not None and medical_exam_info.raw_exam_date
            else "required"
          ),
          field_type="date",
        )
        if entered is None:
          console.print("[yellow]:warning: Medical exam date required; skipped.[/]")
          continue
        kwargs["exam_date"] = entered
        manual_exam_date = medical_exam_info is not None
      if kwargs.get("exam_date"):
        console.print(
          f"[cyan]:information_source: Step 3 exam_date:[/] {kwargs['exam_date']}"
        )

      # Step 9 — submit (retry loop for missing guardian fields)
      submitted = False
      while not submitted:
        try:
          with console.status("[bold cyan]:inbox_tray: Submitting enrollment...[/]"):
            client.add_player_to_registration_batch(
              batch_id, license, **kwargs,
            )
          console.print(f"[green]:white_check_mark: Added licence {license} to batch #{batch_id}.[/]")
          submitted = True
        except SavConfigError as exc:
          console.print("[yellow]:warning: Guardian info required for minor.[/]")
          for field_name in parse_missing_guardian_fields(exc):
            if field_name == "guardian_relation":
              val = _prompt_field(
                field_name,
                field_type=GUARDIAN_RELATIONS,
                prompt_text="      Relation",
              )
            else:
              val = _prompt_field(field_name)
            if val is not None:
              kwargs[field_name] = val
        except (SavConnectionError, SavResponseError) as exc:
          raise SavCliError(str(exc), code=_exc_code(exc))

      # Step 9b — upload the source PDF as fpb_modelo_1. Replace semantics:
      # any prior fpb_modelo_1 for this player+batch is deleted first so a
      # re-submit leaves exactly the new file in place. Non-fatal: if it
      # fails the player is still registered, so we just warn and continue.
      try:
        with console.status(
          "[bold cyan]:page_facing_up: Uploading source document (fpb_modelo_1)...[/]"
        ):
          client.replace_player_registration_document(
            batch_id, license, pdf_path, tipo_doc=doc_type_to_tipo_doc(doc_type),
          )
        console.print(
          f"[green]:white_check_mark: Uploaded {pdf_path} (fpb_modelo_1).[/]"
        )
      except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
        err_console.print(
          f"[yellow]:warning: Enrollment succeeded but document upload failed:[/] {exc}"
        )
      if medical_exam_path:
        try:
          with console.status(
            "[bold cyan]:page_facing_up: Uploading medical exam (exame_medico)...[/]"
          ):
            client.replace_player_registration_document(
              batch_id, license, medical_exam_path, tipo_doc=doc_type_to_tipo_doc(DocType.EM),
            )
          console.print(
            f"[green]:white_check_mark: Uploaded {medical_exam_path} (exame_medico).[/]"
          )
        except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
          err_console.print(
            f"[yellow]:warning: Enrollment succeeded but medical exam upload failed:[/] {exc}"
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
        with console.status("[bold cyan]:sparkles: Finalizing OCR processing...[/]"):
          close_processing(processing_id, corrections=corrections or None)
      except Exception as exc:
        err_console.print(
          f"[yellow]:warning: Could not close processing {processing_id}:[/] {exc}"
        )
      if medical_processing_id is not None:
        medical_close_pending = False
        medical_corrections = {}
        if manual_exam_date and kwargs.get("exam_date") is not None:
          medical_corrections["exam_date"] = str(kwargs["exam_date"])
        try:
          with console.status("[bold cyan]:sparkles: Finalizing medical exam OCR...[/]"):
            close_processing(
              medical_processing_id,
              corrections=medical_corrections or None,
            )
        except Exception as exc:
          err_console.print(
            f"[yellow]:warning: Could not close processing {medical_processing_id}:[/] {exc}"
          )
    finally:
      if not close_called:
        try:
          close_processing(processing_id)
        except Exception:
          pass
      if medical_processing_id is not None and medical_close_pending:
        try:
          close_processing(medical_processing_id)
        except Exception:
          pass



def _resolve_doc_type(value: str) -> DocType:
  """Coerce a --tipo arg to a canonical sav-parsers DocType."""
  try:
    return normalize_doc_type(value)
  except ValueError as exc:
    raise click.UsageError(str(exc))


def _classify_pdf_doc_type(pdf_path: str) -> DocType:
  """Classify a PDF and return the canonical sav-parsers DocType."""
  from sav_parsers import classify
  try:
    return classify(pdf_path)
  except Exception as exc:
    raise SavCliError(f"classify error: {exc}", code="parse_error")


def _tipo_doc_for_upload(doc_type: DocType | str) -> int:
  """Translate a public doc type to the SAV2 upload tipo_doc integer."""
  try:
    return doc_type_to_tipo_doc(doc_type)
  except ValueError as exc:
    raise SavCliError(str(exc), code="parse_error")


def _parse_update_fields(field_args: tuple[str, ...]) -> dict[str, Any]:
  """Parse --field K=V flags into the kwargs dict for update_player_in_registration_batch."""
  out: dict[str, Any] = {}
  for arg in field_args:
    if "=" not in arg:
      raise click.UsageError(f"Bad --field {arg!r}: expected KEY=VALUE.")
    key, _, val = arg.partition("=")
    key = _UPDATE_FIELD_ALIASES.get(key.strip(), key.strip())
    val = val.strip()
    if key not in _UPDATE_FIELDS:
      supported = sorted(set(_UPDATE_FIELDS) | set(_UPDATE_FIELD_ALIASES))
      raise click.UsageError(
        f"Unknown field {key!r}. Supported: {', '.join(supported)}."
      )
    kind, _ = _UPDATE_FIELDS[key]
    if kind == "step1" or kind == "step2":
      coerced: Any = val
      if _UPDATE_FIELDS[key][1] is int:
        try:
          coerced = int(val)
        except ValueError:
          raise click.UsageError(f"--field {key} expects an integer; got {val!r}.")
      out[key] = coerced
    elif kind == "resolve_distrito":
      resolved = find_distrito_id(val)
      if resolved is None:
        try:
          resolved = int(val)
        except ValueError:
          raise click.UsageError(
            f"Unknown distrito {val!r}; pass a numeric distrito ID or the name."
          )
      out["distrito_id"] = resolved
    elif kind == "resolve_concelho":
      try:
        out["concelho_id"] = int(val)
      except ValueError:
        raise click.UsageError(
          f"--field concelho_id expects a numeric ID (name resolution needs the "
          f"distrito context which isn't available from a single --field)."
        )
  return out


@enrollment_grp.command("read")
@click.argument("batch_id", type=int)
@click.argument("license", type=int, required=False)
@click.pass_context
def enrollment_read_cmd(ctx, batch_id, license):
  """Show player enrolments in a batch. (Not yet implemented.)

  \b
    sav enrollment read BATCH_ID              List all players in the batch.
    sav enrollment read BATCH_ID LICENSE      Show one player's enrolment detail.
  """
  # TODO: list_player_registration_batch_items(batch_id) for list view
  #       client._load_existing_registration_record(batch_id, license) for detail
  raise click.UsageError("enrollment read is not yet implemented.")


@enrollment_grp.command("update")
@click.argument("batch_id", type=int)
@click.argument("license", type=int)
@click.argument("pdf", type=click.Path(exists=True), required=False)
@click.option(
  "--mod1", "mod1_path", type=click.Path(exists=True), default=None,
  help="Explicit fpb_modelo_1 form (alternative to positional pdf; skips classify).",
)
@click.option(
  "--medical-exam", "medical_exam_path", type=click.Path(exists=True), default=None,
  help="Upload a medical exam PDF for this player.",
)
@click.option(
  "--field", "fields", multiple=True, metavar="KEY=VAL",
  help=(
    "Override a field value (repeatable). May be combined with a PDF. "
    "Supported: " + ", ".join(sorted(_UPDATE_FIELDS)) + "."
  ),
)
@click.option(
  "--file-only", is_flag=True, default=False,
  help="With PDF: only replace the document; do not touch fields.",
)
@click.option(
  "--tipo", default=None,
  help=(
    "Document type "
    f"({ '|'.join(DOC_TYPE_CHOICES) }). "
    "When omitted with a PDF, sav-parsers classifies it automatically."
  ),
)
@click.pass_context
def enrollment_update_cmd(
  ctx, batch_id, license, pdf, mod1_path, medical_exam_path, fields, file_only, tipo,
):
  """Update an existing player enrolment.

  \b
    sav enrollment update BATCH LICENSE FILE
        OCR-reconcile FILE against SAV, patch fields, replace doc.
    sav enrollment update BATCH LICENSE --mod1 FILE
        Same with explicit doc type (skips classify; trains classifier).
    sav enrollment update BATCH LICENSE FILE --field KEY=VAL [...]
        OCR-reconcile FILE, apply manual field overrides, patch, replace doc.
    sav enrollment update BATCH LICENSE FILE --file-only
        Replace document only; leave fields untouched.
    sav enrollment update BATCH LICENSE --field KEY=VAL [--field ...]
        Patch fields; no document.
    sav enrollment update BATCH LICENSE --medical-exam FILE
        Upload medical exam document only.
  """
  if pdf and (mod1_path or medical_exam_path):
    raise click.UsageError("Positional pdf and --mod1/--medical-exam are mutually exclusive.")
  has_pdf = bool(pdf or mod1_path)
  if not has_pdf and not fields and not medical_exam_path:
    raise click.UsageError(
      "Pass a PDF (positional or --mod1), --medical-exam, or --field K=V. "
      "Run `sav enrollment update --help` for examples."
    )
  if file_only and not has_pdf:
    raise click.UsageError("--file-only requires a PDF argument (positional or --mod1).")

  doc_type: DocType | None = _resolve_doc_type(tipo) if tipo is not None else None

  # Mode B — doc-only replace (pdf/--mod1 + --file-only).
  if has_pdf and file_only:
    active_pdf = mod1_path or pdf
    if mod1_path:
      active_doc_type: DocType = DocType.FPB_MOD1
      from sav_parsers import train_classifier
      try:
        train_classifier(mod1_path, active_doc_type)
      except Exception as exc:
        click.echo(f"  Warning: could not submit classifier training: {exc}", err=True)
    else:
      if doc_type is None:
        doc_type = _classify_pdf_doc_type(active_pdf)
      active_doc_type = doc_type
    tipo_doc = _tipo_doc_for_upload(active_doc_type)
    client = _make_client()
    try:
      client.replace_player_registration_document(
        batch_id, license, active_pdf, tipo_doc=tipo_doc,
      )
    except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
      raise SavCliError(str(exc), code=_exc_code(exc))
    click.echo(
      f"Replaced {_doc_type_text(active_doc_type)} for licence {license} in batch #{batch_id}."
    )
    if medical_exam_path:
      _upload_medical_exam_update(batch_id, license, medical_exam_path)
    return

  # Mode A / D — PDF-driven (parse → reconcile → patch fields → replace doc).
  # Mode A: PDF only. Mode D: PDF + --field overrides.
  if has_pdf and not file_only:
    active_pdf = mod1_path or pdf
    from sav_parsers import close_processing, parse_fpb_mod1, train_classifier

    if mod1_path:
      # Explicit type: skip classify, train classifier.
      active_doc_type: DocType = DocType.FPB_MOD1
      try:
        train_classifier(mod1_path, active_doc_type)
      except Exception as exc:
        click.echo(
          f"  Warning: could not submit classifier training for mod1: {exc}", err=True,
        )
    else:
      if doc_type is None:
        doc_type = _classify_pdf_doc_type(pdf)
      if doc_type != DocType.FPB_MOD1:
        raise SavCliError(
          f"Unsupported document type {_doc_type_text(doc_type)!r}; "
          "only fpb_modelo_1 forms are reconciled. Use --file-only to upload as-is.",
          code="parse_error",
        )
      active_doc_type = doc_type
    tipo_doc = _tipo_doc_for_upload(active_doc_type)

    client = _make_client()

    click.echo("Processing OCR ...", nl=False)
    try:
      parse_result = parse_fpb_mod1(active_pdf)
    except Exception as exc:
      click.echo()
      raise SavCliError(f"parse error: {exc}", code="parse_error")
    parsed = parse_result["fields"]
    processing_id = parse_result["processing_id"]
    click.echo(" done.")

    close_called = False
    try:
      try:
        sav_profile = client.load_player_profile(license)
      except (SavConnectionError, SavResponseError) as exc:
        raise SavCliError(f"Could not load player profile: {exc}", code=_exc_code(exc))

      try:
        result = reconcile_fpb_mod1(parsed, sav_profile, client=client)
      except (SavConnectionError, SavResponseError) as exc:
        raise SavCliError(f"Reconcile failed: {exc}", code=_exc_code(exc))

      kwargs = _confirm_enroll(result, sav_profile, license)
      if kwargs is None:
        click.echo("Skipped.")
        return

      # Apply --field overrides on top of reconciled values (user wins over OCR).
      if fields:
        field_overrides = _parse_update_fields(fields)
        if field_overrides:
          click.echo(
            "  Field overrides: "
            + ", ".join(f"{k}={v}" for k, v in sorted(field_overrides.items()))
          )
        kwargs.update(field_overrides)

      # Drop fields that update_player_in_registration_batch doesn't accept.
      ignored = {k: v for k, v in kwargs.items() if k not in _UPDATE_FIELDS}
      patch_kwargs = {k: v for k, v in kwargs.items() if k in _UPDATE_FIELDS}
      if ignored:
        click.echo(
          f"  Ignored (not patchable on existing enrolments): "
          f"{', '.join(sorted(ignored))}."
        )

      try:
        client.update_player_in_registration_batch(
          batch_id, license, **patch_kwargs,
        )
      except (SavConnectionError, SavResponseError, ValueError) as exc:
        raise SavCliError(str(exc), code=_exc_code(exc))
      click.echo(f"Updated licence {license} in batch #{batch_id}.")

      try:
        client.replace_player_registration_document(
          batch_id, license, active_pdf, tipo_doc=tipo_doc,
        )
        click.echo(f"  Uploaded {active_pdf} ({_doc_type_text(active_doc_type)}).")
      except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
        click.echo(
          f"  Warning: field update succeeded but document upload failed: {exc}",
          err=True,
        )

      if medical_exam_path:
        _upload_medical_exam_update(batch_id, license, medical_exam_path, client=client)

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
    return

  # Mode C — fields-only (and optional medical exam upload).
  client = _make_client()
  if fields:
    patch_kwargs = _parse_update_fields(fields)
    try:
      client.update_player_in_registration_batch(
        batch_id, license, **patch_kwargs,
      )
    except (SavConnectionError, SavResponseError, ValueError) as exc:
      raise SavCliError(str(exc), code=_exc_code(exc))
    applied = ", ".join(sorted(patch_kwargs))
    click.echo(f"Updated licence {license} in batch #{batch_id}: {applied}.")
  if medical_exam_path:
    _upload_medical_exam_update(batch_id, license, medical_exam_path, client=client)


def _upload_medical_exam_update(
  batch_id: int, license: int, path: str, *, client: Any = None,
) -> None:
  """Upload or replace a medical exam document for an existing enrolment."""
  from sav_parsers import train_classifier
  if client is None:
    client = _make_client()
  try:
    train_classifier(path, DocType.EM)
  except Exception as exc:
    click.echo(f"  Warning: could not submit classifier training for exam: {exc}", err=True)
  tipo_doc = _tipo_doc_for_upload(DocType.EM)
  try:
    client.replace_player_registration_document(batch_id, license, path, tipo_doc=tipo_doc)
    click.echo(f"  Uploaded {path} (exame_medico).")
  except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
    click.echo(f"  Warning: medical exam upload failed: {exc}", err=True)


@enrollment_grp.command("delete")
@click.argument("batch_id", type=int)
@click.argument("license", type=int)
@click.pass_context
def enrollment_delete_cmd(ctx, batch_id, license):
  """Remove a player from a registration batch. (Not yet implemented.)"""
  # TODO: click.confirm prompt + client.remove_player_from_registration_batch(batch_id, license)
  raise click.UsageError("enrollment delete is not yet implemented.")


def main():
  cli()
