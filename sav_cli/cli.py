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
import os
from pathlib import Path
import shutil
from dataclasses import asdict
from datetime import date
from typing import Any

import click
from dotenv import load_dotenv
from rich.console import Console
from sav_parsers.types import DocType

from sav_client import SavClient, Player
from sav_client.exceptions import (
  LicenseNotEnrolledError,
  SavAuthError,
  SavConfigError,
  SavConnectionError,
  SavResponseError,
)
from sav_shared.clubs import find_club_matches
from sav_shared.files import staged_pdf
from sav_shared.enrollment import (
  KWARG_TO_SAV_KEY,
  create_and_fetch_batch,
  derive_enrollment_params,
  find_player_license_by_nif,
  parse_missing_guardian_fields,
  parsed_bool,
  resolve_player_candidates,
  try_replace_document,
)
from sav_shared.fields import ENROLLMENT_FIELD_META
from sav_shared.fpb_mod1 import (
  OverlayResult,
  carimbo_overlay,
  inscricao_overlay,
  overlaid_pdf,
  read_carimbo,
  read_tipo_inscricao,
  reconcile_fpb_mod1,
)
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
  is_uploadable_doc_type,
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


def _require_env(name: str) -> str:
  """Fail fast at command entry if a required env var is not set.

  Called at the top of each command path that needs the var, so downstream
  code (fpb_mod1, stamped_pdf, etc.) can trust the var is present and read
  it directly without defensive fallback branches.
  """
  value = os.environ.get(name)
  if not value:
    raise SavCliError(f"Environment variable {name} is required but not set.", code="config_error")
  return value


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


def _resolve_batch_id_or_raise(client: SavClient, batch_number: str) -> int:
  """Translate a human-visible batch number to the internal batch_id, or raise SavCliError."""
  try:
    return client.resolve_batch_id(batch_number)
  except ValueError as e:
    raise SavCliError(str(e), code="batch_not_found")
  except (SavConnectionError, SavResponseError) as e:
    raise SavCliError(str(e), code=_exc_code(e))


def _resolve_batch_id_by_license_or_raise(client: SavClient, license: int) -> int:
  """Resolve the batch a license is enrolled in, or raise SavCliError with a hint."""
  try:
    return client.resolve_batch_id_by_license(license)
  except LicenseNotEnrolledError as e:
    raise SavCliError(str(e), code="license_not_enrolled")
  except ValueError as e:
    raise SavCliError(str(e), code="license_not_enrolled")
  except (SavConnectionError, SavResponseError) as e:
    raise SavCliError(str(e), code=_exc_code(e))


def _batch_number_for_log(client: SavClient, batch_id: int, fallback: str | None = None) -> str:
  """Best-effort cached batch number for log/print purposes; falls back to `#<id>`."""
  cache = getattr(client, "_cache", None)
  number = cache.get_batch_number(batch_id) if cache is not None else None
  if number:
    return number
  if fallback:
    return fallback
  return f"#{batch_id}"


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
  load_dotenv(".env", override=False)
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
  *,
  indent: str = "",
) -> tuple[int, Any]:
  """Interactively find an open batch or create one. Returns (batch_id, batch).
  `indent` prefixes the messages so they nest under a document scope."""
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
      f"{indent}[cyan]:open_file_folder: Open batch found:[/] #{existing.number} "
      f"({type_label} · {existing.tier} · {gender_label} · "
      f"{existing.item_count} player(s) already added)"
    )
    choice = click.prompt(
      click.style(f"{indent}  Use existing or create new?", fg="cyan"),
      type=click.Choice(["existing", "new"]),
      default="existing",
    )
    if choice == "new":
      if not click.confirm(
        click.style(f"{indent}  Create new {type_label} batch ({tier_name} · {gender_label})?", fg="cyan")
      ):
        raise click.Abort()
      return _create()
    return existing.id, existing

  console.print(
    f"{indent}[yellow]:warning: No open batch for {type_label} · {tier_name} · {gender_label}.[/]"
  )
  if not click.confirm(click.style(f"{indent}  Create new batch?", fg="cyan")):
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
  client: Any, batch: Any, parsed: dict, reg_type: int | None = None,
  *,
  indent: str = "",
) -> tuple[int, Any] | None:
  """Return ``(license, target_batch)`` for the parsed form, or None to skip.

  ``target_batch`` is normally the input ``batch``, but switches when the
  player is already enrolled in a different open batch with matching
  params — in that case the caller should add/update against the returned
  batch instead, since SAV2 won't allow re-adding to a second one.

  For revalidação (reg_type=2), uses NIF-based license lookup directly,
  skipping manual entry prompts. `indent` prefixes the messages so they nest
  under a document scope.
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
    name_suffix = f" — {ocr_name}" if ocr_name else ""
    console.print(f"{indent}[green]:dart: Matched player licence {license}{name_suffix}.[/]")
    return license, batch

  # Already-enrolled fallback: licence-first, then NIF, against open batches
  # matching the same (type, tier, gender). Returning the matching batch lets
  # the caller route the wizard's edit path (op=30 + op=33/op=31).
  enrolled_license: int | None = None
  if ocr_license is not None:
    enrolled_license = ocr_license

  nif_license = find_player_license_by_nif(parsed, client, club_id=batch.club_id)
  if enrolled_license is None and nif_license is not None:
    enrolled_license = nif_license

  if enrolled_license is not None:
    with console.status("[bold cyan]:link: Checking other open batches for an existing enrolment...[/]"):
      target = _find_enrolled_in_matching_batches(client, batch, enrolled_license)
    if target is not None:
      if target.id != batch.id:
        console.print(
          f"{indent}[cyan]:repeat: Already enrolled in batch #{target.number}; updating there."
        )
      else:
        console.print(f"{indent}[cyan]:repeat: Already enrolled in this batch — updating.[/]")
      return enrolled_license, target

  # For revalidação, surface the NIF-based license before prompting for manual entry.
  # Only offer if from NIF lookup (ocr_license may be misread).
  # Note: this license may not be eligible for this specific batch; if so, submit
  # will fail and the user will need to manually add them to the eligible list or
  # use a different batch.
  if reg_type == 2 and nif_license is not None and ocr_license is None:
    console.print(
      f"{indent}[cyan]:information_source: Found in club roster via NIF:[/] licence {nif_license}"
    )
    if click.confirm(f"{indent}  Use this licence?", default=True):
      return nif_license, batch

  if len(candidates) > 1:
    console.print(f"{indent}[yellow]:warning: Multiple players match {ocr_name!r}:[/]")
    for i, p in enumerate(candidates, 1):
      console.print(f"{indent}  {i}.  {p.name}  (licence {p.license})")
    idx = click.prompt(f"{indent}  Pick", type=click.IntRange(1, len(candidates)))
    return int(candidates[idx - 1].license), batch

  if ocr_license is not None:
    console.print(
      f"{indent}[yellow]:warning: OCR licence {ocr_license} is not in the eligible list for this batch.[/]"
    )
    if click.confirm(f"{indent}  Use it anyway?"):
      return ocr_license, batch
  elif ocr_name:
    console.print(f"{indent}[yellow]:warning: Player not found for {ocr_name!r} in eligible list.[/]")
  else:
    console.print(f"{indent}[yellow]:warning: Could not determine player from OCR.[/]")

  while True:
    raw = click.prompt(f"{indent}  Licence number (blank to skip)", default="")
    if not raw:
      return None
    try:
      lic = int(raw)
    except ValueError:
      console.print(f"{indent}[yellow]:warning: Not a valid number.[/]")
      continue
    if lic not in eligible:
      with console.status("[bold cyan]:link: Checking other open batches for an existing enrolment...[/]"):
        target = _find_enrolled_in_matching_batches(client, batch, lic)
      if target is not None:
        if target.id != batch.id:
          console.print(
            f"{indent}[cyan]:repeat: Licence {lic} is enrolled in batch #{target.number}; updating there."
          )
        else:
          console.print(f"{indent}[cyan]:repeat: Licence {lic} is already in this batch — updating.[/]")
        return lic, target
      console.print(
        f"{indent}[yellow]:warning: Licence {lic} is not in the eligible list for this batch.[/]"
      )
      if not click.confirm(f"{indent}  Use it anyway?"):
        continue
    return lic, batch


def _prompt_field(
  kwarg: str,
  hint: str = "",
  *,
  field_type: str | Mapping[Any, str] = "text",
  prompt_text: str | None = None,
  indent: str = "",
) -> Any:
  """Prompt the user for one field value, returning the entered value or None."""
  console = _console()
  label = f"{indent}    {kwarg}" + (f"  ({hint})" if hint else "")
  if isinstance(field_type, Mapping):
    if not field_type:
      raise ValueError("Choice field_type must not be empty.")
    console.print(label)
    options_text = "  ".join(f"{key}={value}" for key, value in field_type.items())
    console.print(f"[dim]{indent}      {options_text}[/]")
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


def _player_label(sav_profile: dict, license: int) -> str:
  """Human-readable player label for headers and prompts."""
  name_str = sav_profile.get("nome", "") or ""
  return f"{name_str} (licence {license})" if name_str else f"licence {license}"


def _review_and_fill(result: Any, sav_profile: dict, *, indent: str = "") -> dict:
  """Prompt for needs_review fields and return the final kwargs dict.

  Fields flagged for low OCR confidence are prompted one by one; distrito and
  concelho answers are resolved to ids. `indent` nests the output under a
  document scope. The license key is popped out of the returned dict.
  """
  console = _console()
  kwargs = dict(result.kwargs)
  if result.needs_review:
    console.print(f"{indent}[bold]:memo: Review & fill:[/]")
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
      val = _prompt_field(kwarg, hint, indent=indent)
      if val is None:
        continue
      if kwarg == "distrito_id":
        resolved = find_distrito_id(val) if isinstance(val, str) else None
        if resolved is None:
          console.print(f"{indent}[yellow]:warning: {val!r} is not a known distrito; keeping SAV value.[/]")
          continue
        kwargs[kwarg] = resolved
      elif kwarg == "concelho_id":
        resolved = find_id_by_name(val, result.concelhos) if isinstance(val, str) else None
        if resolved is None:
          known = ", ".join(sorted(result.concelhos.values())) or "(none — distrito unknown)"
          console.print(f"{indent}[yellow]:warning: {val!r} is not a known concelho for this distrito.[/]")
          console.print(f"[dim]{indent}    Known: {known}[/]")
          continue
        kwargs[kwarg] = resolved
      else:
        kwargs[kwarg] = val
  elif not (result.updated or result.kept):
    console.print(f"{indent}[cyan]:information_source: No changes (OCR matches SAV).[/]")

  kwargs.pop("license", None)
  return kwargs


def _confirm_documents_and_submit(
  player_label: str, documents: list[tuple[str, str]] | None,
) -> bool:
  """List the documents to upload and prompt for the submit confirmation."""
  console = _console()
  if documents:
    console.print("[bold]:open_file_folder: Documents to upload:[/]")
    for doc_type_label, filename in documents:
      console.print(f"    • {doc_type_label}  [dim]{filename}[/]")
  return click.confirm(f"  Submit {player_label}?", default=True)


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


def _print_submission_summary(
  kwargs: dict, result: Any, sav_profile: dict,
  *,
  ocr_source: str = "OCR",
  extras: list[tuple[str, str, str, str, str]] | None = None,
  indent: str = "",
) -> None:
  """Render the final pre-submit field list.

  Each field shows its final value and source. Verbose mode appends the SAV/OCR
  origin in dim text. Empty values render as em-dash. `indent` prefixes every
  line so the list can nest under a document scope.

  `ocr_source` overrides the Source label for OCR-derived FPB rows so callers
  can include the originating filename, e.g. "OCR (form.pdf)".
  `extras` appends pre-rendered (label, sav, ocr, final, source) rows for
  fields outside the OCR/SAV reconcile model.
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
        source = ocr_source
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

  if extras:
    rows.extend(extras)

  if not rows:
    return

  console = _console()
  console.print(f"{indent}[bold]:clipboard: Reconciling form fields:[/]")
  for label, sav, ocr, final, source in rows:
    line = (
      f"{indent}    [green]:white_check_mark:[/] {label}  {final}  [dim]({source})[/]"
    )
    if _VERBOSE:
      line += f" [dim](SAV: {sav} → OCR: {ocr})[/]"
    console.print(line, soft_wrap=True)


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


def _carimbo_extras_row(
  carimbo: bool | None, r: OverlayResult,
) -> tuple[str, str, str, str, str]:
  """Build the (label, sav, ocr, final, source) summary row for carimbo_clube_presente.

  Uses the OCR value for the OCR column and the OverlayResult for what we did.
  Because the overlay runs before the summary, Final reports the real outcome.
  """
  ocr_str = {True: "yes", False: "no", None: "—"}[carimbo]
  if carimbo is True:
    final, source = "on form", "OCR"
  elif r.applied is True:
    final, source = "missing", "overlay"
  elif r.applied is False:
    final, source = "missing", "overlay failed"
  elif carimbo is False:
    final, source = "missing", "skip"
  else:
    final, source = "skip (no OCR result)", "auto"
  return ("Carimbo do Clube", "—", ocr_str, final, source)


def _inscricao_extras_row(
  reg_type: int | None,
  tipo_checked: bool | None,
  r: OverlayResult,
) -> tuple[str, str, str, str, str]:
  """Build the (label, sav, ocr, final, source) summary row for tipo_inscricao."""
  label = REGISTRATION_TYPE_LABELS.get(reg_type or 0, "Tipo Inscrição")
  ocr_str = {True: "yes", False: "no", None: "—"}[tipo_checked]
  if tipo_checked is True:
    final, source = "on form", "OCR"
  elif r.applied is True:
    final, source = "missing", "overlay"
  elif r.applied is False:
    final, source = "missing", "overlay failed"
  elif tipo_checked is False:
    final, source = "missing", "skip"
  else:
    final, source = "skip (no OCR result)", "auto"
  return (label, "—", ocr_str, final, source)


def _prepare_club_stamp(
  ctx: click.Context, console: Console, err_console: Console,
  parsed: dict, pdf_path: str, processing_id: str,
  *, reg_type: int | None = None, indent: str = "",
) -> tuple[str, bool | None, OverlayResult, bool | None, OverlayResult]:
  """Overlay the inscription checkbox and/or club stamp (when OCR says they
  are missing) *before* the summary, so the summary reports the real outcome.

  Returns (upload_path, carimbo, carimbo_r, tipo_checked, inscricao_r).

  The modified copy is written into the OCR processing dir for `processing_id`,
  sharing that session's lifecycle. Registered on `ctx` so it survives the
  confirm→submit→upload span. Neither overlay raises out here — overlaid_pdf
  catches failures inside each factory and falls back to the original PDF.
  """
  from sav_parsers import processing_dir

  carimbo, bbox = read_carimbo(parsed)
  tipo_checked, tipo_bbox = (
    read_tipo_inscricao(parsed, reg_type) if reg_type is not None else (None, None)
  )
  upload_path, (inscricao_r, carimbo_r) = ctx.with_resource(
    overlaid_pdf(
      pdf_path,
      inscricao_overlay(reg_type=reg_type, already_checked=tipo_checked, bbox=tipo_bbox),
      carimbo_overlay(carimbo_present=carimbo, bbox=bbox),
      dest_dir=processing_dir(processing_id),
    )
  )
  if inscricao_r.applied is True:
    tipo_label = "Revalidação" if reg_type == 2 else "1ª Inscrição"
    console.print(
      f"{indent}[green]:ballot_box_with_check:  Marked {tipo_label} checkbox on [bold]{_display_name(pdf_path)}[/].[/]"
    )
  if inscricao_r.error:
    err_console.print(
      f"[yellow]:warning: {inscricao_r.error}[/] — please mark the checkbox manually."
    )
  if carimbo_r.applied is True:
    console.print(
      f"{indent}[green]:label:  Applied club stamp to [bold]{_display_name(pdf_path)}[/] at OCR-detected location.[/]"
    )
  if carimbo_r.error:
    err_console.print(
      f"[yellow]:warning: {carimbo_r.error}[/] — will upload WITHOUT the club stamp; "
      "please stamp the document manually."
    )
  return upload_path, carimbo, carimbo_r, tipo_checked, inscricao_r


# Maps a staged temp-PDF path back to the user's original filename. Image
# inputs are converted to temp PDFs with random names (tmpXXXX.pdf); without
# this, every downstream log would show the meaningless temp name.
_STAGED_DISPLAY_NAMES: dict[str, str] = {}


def _display_name(path: str) -> str:
  """User-facing filename for `path`: the original name when `path` is a staged
  temp PDF, otherwise its basename."""
  return _STAGED_DISPLAY_NAMES.get(path, os.path.basename(path))


def _stage_pdf(
  ctx: click.Context, console: Console, path: str,
  *, converted: list[str] | None = None,
) -> str:
  """Stage `path` (PDF or supported image) into a PDF, register cleanup on
  `ctx`, and return the staged PDF path. If `converted` is given, append the
  source basename to it on conversion (the caller prints a grouped block);
  otherwise announce the conversion inline."""
  staged, was_converted = ctx.with_resource(staged_pdf(path))
  if was_converted:
    _STAGED_DISPLAY_NAMES[staged] = os.path.basename(path)
    if converted is not None:
      converted.append(os.path.basename(path))
    else:
      console.print(f"[cyan]:arrows_counterclockwise: Converted [bold]{path}[/] to PDF.[/]")
  return staged


def _print_converted(console: Console, converted: list[str]) -> None:
  """Print the grouped block of image→PDF conversions, if any happened."""
  if not converted:
    return
  console.print("[bold]:arrows_counterclockwise: Converted to PDF:[/]")
  for name in converted:
    console.print(f"  [green]:white_check_mark:[/] {name}", soft_wrap=True)


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
  help="Explicit exame_medico PDF (skips auto-classify for this file).",
)
@click.option(
  "--mod4", "mod4_path", type=click.Path(exists=True), default=None,
  help="Explicit fpb_modelo_4 form; marks this enrollment as a subida de escalão.",
)
@click.option(
  "--batch", "batch_number_opt", type=str, default=None,
  help="Batch number (as shown in the SAV2 UI) for manual enrolment (no PDF).",
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
def enrollment_create_cmd(ctx, pdfs, mod1_path, medical_exam, mod4_path, batch_number_opt, license_opt, fields):
  """Enroll one player into SAV from one or more supporting PDFs.

  A single invocation creates a single enrollment. Pass one fpb_modelo_1 form
  (required in PDF mode) plus any number of supporting documents — currently
  zero or one exame_medico. Positional PDFs are auto-classified; --mod1 and
  --medical-exam pin the type explicitly (and skip classify for that file).

  \b
    sav enrollment create form.pdf
        Auto-classify the form; enroll without a medical exam.
    sav enrollment create form.pdf exam.pdf
        Auto-classify both PDFs into mod1 + exame_medico; enroll one player.
    sav enrollment create --mod1 form.pdf --medical-exam exam.pdf
        Skip classify for both; trains the classifier.
    sav enrollment create form.pdf subida.pdf
        A classified (or --mod4) fpb_modelo_4 marks a subida de escalão.
    sav enrollment create --batch ID --license ID [--field KEY=VAL ...]
        Manual enrolment from SAV profile, no PDF.
  """
  from sav_parsers import close_processing, parse_em, parse_fpb_mod1, train_classifier
  from sav_shared.fields import KWARG_TO_ENTITY

  pdf_mode = bool(pdfs or mod1_path)
  manual_mode = bool(batch_number_opt is not None and license_opt is not None)

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
    with console.status("[bold cyan]:open_file_folder: Resolving batch...[/]"):
      batch_id = _resolve_batch_id_or_raise(client, batch_number_opt)
    try:
      with console.status("[bold cyan]:open_book: Loading SAV player profile...[/]"):
        sav_profile = client.load_player_profile(license_opt)
    except (SavConnectionError, SavResponseError) as exc:
      raise SavCliError(f"Could not load player profile: {exc}", code=_exc_code(exc))
    console.print(
      f"[bold]Enrolling:[/] {sav_profile.get('nome', '?')} "
      f"(licence {license_opt}) in batch #{batch_number_opt}"
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
          batch_id, license_opt, **field_overrides,
        )
    except (SavConnectionError, SavResponseError, ValueError) as exc:
      raise SavCliError(str(exc), code=_exc_code(exc))
    console.print(
      f"[green]:white_check_mark: Enrolled licence {license_opt} in batch #{batch_number_opt}.[/]"
    )
    return

  _require_env("CLUB_STAMP_PATH")
  client = _make_client()

  console = _console()
  err_console = _console(err=True)

  # Convert any non-PDF inputs (images) to PDFs up front so OCR, classify,
  # and upload all see PDF paths. PDFs pass through unchanged. Cleanup is
  # registered on the Click context.
  converted: list[str] = []
  pdfs = tuple(_stage_pdf(ctx, console, p, converted=converted) for p in pdfs)
  if mod1_path:
    mod1_path = _stage_pdf(ctx, console, mod1_path, converted=converted)
  if medical_exam:
    medical_exam = _stage_pdf(ctx, console, medical_exam, converted=converted)
  if mod4_path:
    mod4_path = _stage_pdf(ctx, console, mod4_path, converted=converted)
  _print_converted(console, converted)

  # Pre-classify each positional PDF and bucket by doc type. Explicit
  # --mod1 / --medical-exam paths are appended directly (no classify call).
  positional_mod1: list[str] = []
  positional_em: list[str] = []
  positional_mod4: list[str] = []
  # Supplementary docs (atestado_residencia, documento_identificacao, …): no
  # parser extracts fields, but each has a tipo_doc mapping, so we attach them
  # to the player with the classified type instead of dropping them.
  extra_docs: list[tuple[str, DocType]] = []
  if pdfs:
    console.print("[bold]:mag: Classified:[/]")
  for path in pdfs:
    filename = _display_name(path)
    try:
      with console.status(f"[bold cyan]:mag: Classifying {filename}...[/]"):
        dt = _classify_pdf_doc_type(path)
    except Exception as exc:
      raise SavCliError(f"Classify error for {path}: {exc}", code="parse_error")
    if dt == DocType.FPB_MODELO_1:
      positional_mod1.append(path)
    elif dt == DocType.EXAME_MEDICO:
      positional_em.append(path)
    elif dt == DocType.FPB_MODELO_4:
      positional_mod4.append(path)
    elif is_uploadable_doc_type(dt):
      extra_docs.append((path, dt))
    else:
      console.print(
        f"  [yellow]:warning:[/] {filename} "
        f"[dim](unsupported type {_doc_type_text(dt)!r}; ignored)[/]",
        soft_wrap=True,
      )
      continue
    console.print(
      f"  [green]:white_check_mark:[/] {filename} [dim]({_doc_type_text(dt)})[/]",
      soft_wrap=True,
    )

  mod1_candidates = positional_mod1 + ([mod1_path] if mod1_path else [])
  em_candidates = positional_em + ([medical_exam] if medical_exam else [])
  mod4_candidates = positional_mod4 + ([mod4_path] if mod4_path else [])

  if not mod1_candidates:
    raise click.UsageError(
      "No fpb_modelo_1 form provided. Pass one as a positional PDF or via --mod1."
    )
  if len(mod1_candidates) > 1:
    raise click.UsageError(
      f"enrollment create now produces one enrollment per invocation, but got "
      f"{len(mod1_candidates)} fpb_modelo_1 forms: {', '.join(mod1_candidates)}. "
      f"To enroll multiple players, run this command once per form "
      f"(e.g. `for f in *.pdf; do sav enrollment create \"$f\"; done`)."
    )
  if len(em_candidates) > 1:
    raise click.UsageError(
      f"enrollment create accepts at most one exame_medico per invocation, but "
      f"got {len(em_candidates)}: {', '.join(em_candidates)}. A medical exam is "
      f"per-player, so attach exactly one alongside the fpb_modelo_1 form."
    )
  if len(mod4_candidates) > 1:
    raise click.UsageError(
      f"enrollment create accepts at most one fpb_modelo_4 per invocation, but "
      f"got {len(mod4_candidates)}: {', '.join(mod4_candidates)}. A subida is "
      f"per-player, so attach exactly one alongside the fpb_modelo_1 form."
    )

  pdf_path = mod1_candidates[0]
  doc_type = DocType.FPB_MODELO_1
  medical_exam_path: str | None = em_candidates[0] if em_candidates else None
  # A mod4 (anywhere in the input) marks this enrollment as a subida de escalão;
  # the target tier is fetched from SAV at submit time.
  mod4_doc_path: str | None = mod4_candidates[0] if mod4_candidates else None
  is_subida = mod4_doc_path is not None
  # Upload the mod4 alongside the other supplementary docs (tipo_doc=6).
  if mod4_doc_path is not None:
    extra_docs.append((mod4_doc_path, DocType.FPB_MODELO_4))
  # Train the classifier on the mod1 only when the user pinned it with --mod1;
  # an auto-classified positional already represents a confident classify call.
  if mod1_path is not None:
    try:
      with console.status("[bold cyan]:bookmark_tabs: Labeling mod1 form for classifier training...[/]"):
        train_classifier(pdf_path, doc_type)
    except Exception as exc:
      err_console.print(
        f"[yellow]:warning: Could not submit classifier training for mod1:[/] "
        f"{exc} [dim]({pdf_path})[/]"
      )
  # Same for a pinned --mod4 (positional mod4s were already auto-classified).
  if mod4_path is not None:
    try:
      with console.status("[bold cyan]:bookmark_tabs: Labeling mod4 form for classifier training...[/]"):
        train_classifier(mod4_path, DocType.FPB_MODELO_4)
    except Exception as exc:
      err_console.print(
        f"[yellow]:warning: Could not submit classifier training for mod4:[/] "
        f"{exc} [dim]({mod4_path})[/]"
      )

  # Step 2 — parse
  try:
    with console.status("[bold cyan]:robot: Running OCR and extracting mod1 form fields...[/]"):
      parse_result = parse_fpb_mod1(pdf_path)
      parsed = parse_result["fields"]
      processing_id = parse_result["processing_id"]
  except Exception as exc:
    raise SavCliError(f"Parse error for {pdf_path}: {exc}", code="parse_error")

  # Once parse_fpb_mod1 has created a processing session, we must close it
  # on every exit path or the dir leaks under files/processing/<id>/ until
  # gc sweeps it. close_called flips to True at step 10 (success path);
  # the finally falls back to a no-corrections close for any earlier exit.
  close_called = False
  medical_processing_id: str | None = None
  medical_close_pending = False
  manual_exam_date = False
  try:
    # ── mod1 scope: OCR result, batch/player resolution, and the field list ──
    # Everything for the mod1 form nests under one header, in execution order:
    # OCR → registration type → batch → matched player → club stamp → fields.
    console.print("[bold]:clipboard: Processing mod1:[/]")
    console.print(
      f"  [green]:white_check_mark:[/] OCR ready [dim]({processing_id})[/]",
      soft_wrap=True,
    )

    # Step 4 — resolve batch
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
        console.print(f"  [cyan]:information_source:[/] Inferred registration type: {label}")
      batch_id, batch = _resolve_enroll_batch(
        client, reg_type, tier_id, gender_id, indent="  ",
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
      resolved = _resolve_enroll_player(client, batch, parsed, reg_type, indent="  ")
    except (SavConnectionError, SavResponseError) as exc:
      raise SavCliError(str(exc), code=_exc_code(exc))
    if resolved is None:
      console.print("[yellow]:fast_forward: Skipped.[/]")
      return
    license, batch = resolved
    batch_id = batch.id

    # Step 6 — fetch SAV profile (op=2 athlete form). Read-only; any
    # server-side validation surfaces at real submit.
    try:
      with console.status("[bold cyan]:open_book: Loading SAV player profile...[/]"):
        sav_profile = client.load_player_profile(license, club_id=batch.club_id)
    except (SavConnectionError, SavResponseError) as exc:
      raise SavCliError(f"Could not load player profile: {exc}", code=_exc_code(exc))

    # Step 7 — reconcile OCR vs SAV. reconcile_fpb_mod1 fetches the
    # distrito-scoped concelho list itself (cached client-side); without it
    # concelho silently falls to needs_review.
    try:
      with console.status("[bold cyan]:balance_scale: Reconciling OCR with SAV...[/]"):
        result = reconcile_fpb_mod1(parsed, sav_profile, client=client)
    except (SavConnectionError, SavResponseError) as exc:
      raise SavCliError(f"Could not load concelhos: {exc}", code=_exc_code(exc))
    except Exception as exc:
      raise SavCliError(f"Reconcile error: {exc}", code="reconcile_error")

    player_label = _player_label(sav_profile, license)

    # Stamp the club mark and/or mark the inscription checkbox (when OCR says
    # they are missing) before the field list so the summary rows report what
    # we actually did, not a prediction.
    upload_path, carimbo, carimbo_r, tipo_checked, inscricao_r = _prepare_club_stamp(
      ctx, console, err_console, parsed, pdf_path, processing_id,
      reg_type=reg_type, indent="  ",
    )
    kwargs = _review_and_fill(result, sav_profile, indent="  ")
    _print_submission_summary(
      kwargs, result, sav_profile,
      ocr_source=f"OCR ({_display_name(pdf_path)})",
      extras=[
        _inscricao_extras_row(reg_type, tipo_checked, inscricao_r),
        ("Subida de Escalão", "—", "—",
         "yes" if is_subida else "no",
         "fpb_modelo_4" if is_subida else "default"),
        _carimbo_extras_row(carimbo, carimbo_r),
      ],
      indent="  ",
    )

    # ── medical exam scope: its own OCR result and the (required) exam date ──
    # exam_date is mandatory for every enrollment; the exam PDF is optional and
    # only supplies an OCR candidate. Resolve it before the submit confirm so
    # the value confirmed is the value submitted.
    medical_exam_info = None
    exam_date_final: str | None = None
    if medical_exam_path:
      console.print(
        f"[bold]:stethoscope: Medical exam — {_display_name(medical_exam_path)}:[/]"
      )
      try:
        with console.status("[bold cyan]:stethoscope: Running OCR and extracting medical exam fields...[/]"):
          medical_parse_result = parse_em(medical_exam_path)
        medical_processing_id = medical_parse_result["processing_id"]
        medical_close_pending = True
        medical_exam_info = extract_medical_exam_info(medical_parse_result["fields"])
      except SavCliError:
        raise
      except Exception as exc:
        raise SavCliError(
          f"Medical exam parse error: {exc} ({medical_exam_path})",
          code="parse_error",
        )
      console.print("  [green]:white_check_mark:[/] OCR ready", soft_wrap=True)
      # Train the classifier on the medical exam only when the user pinned it
      # with --medical-exam; a positional was already auto-classified.
      if medical_exam is not None:
        try:
          with console.status("[bold cyan]:bookmark_tabs: Labeling medical exam for classifier training...[/]"):
            train_classifier(medical_exam_path, DocType.EXAME_MEDICO)
        except Exception as exc:
          err_console.print(f"  [yellow]:warning:[/] Could not submit classifier training: {exc}")

      conf_suffix = (
        f" ({medical_exam_info.exam_date_confidence:.0%})"
        if medical_exam_info.exam_date_confidence is not None
        else ""
      )
      if medical_exam_info.exam_date:
        exam_date_final = medical_exam_info.exam_date
        console.print(
          f"  [cyan]:information_source:[/] Exam date: {exam_date_final}{conf_suffix}",
          soft_wrap=True,
        )
      else:
        raw_text = medical_exam_info.raw_exam_date or "not found"
        console.print(
          f"  [yellow]:warning:[/] Exam date needs review: {raw_text!r}{conf_suffix}",
          soft_wrap=True,
        )
    else:
      console.print("[bold]:stethoscope: Medical exam:[/]")

    if exam_date_final is None:
      entered = _prompt_field(
        "exam_date",
        (
          f"OCR: {medical_exam_info.raw_exam_date!r}"
          if medical_exam_info is not None and medical_exam_info.raw_exam_date
          else "required"
        ),
        field_type="date",
        indent="  ",
      )
      if entered is None:
        raise SavCliError(
          "Medical exam date required. Supply a parseable exame_medico PDF "
          "(positional or --medical-exam), or run interactively and answer "
          "the prompt.",
          code="missing_input",
        )
      exam_date_final = entered
      manual_exam_date = medical_exam_info is not None

    # ── documents + submit confirmation ─────────────────────────────────
    # Mirror the upload steps below (mod1, then optional exam, then extras) so
    # the user confirms exactly the set of files that gets uploaded.
    documents = [(_doc_type_text(doc_type), _display_name(pdf_path))]
    if medical_exam_path:
      documents.append((_doc_type_text(DocType.EXAME_MEDICO), _display_name(medical_exam_path)))
    documents.extend(
      (_doc_type_text(extra_dt), _display_name(extra_path))
      for extra_path, extra_dt in extra_docs
    )

    if not _confirm_documents_and_submit(player_label, documents):
      console.print("[yellow]:fast_forward: Skipped.[/]")
      return
    kwargs["exam_date"] = exam_date_final

    # Apply --field overrides on top of reconciled values (user wins over OCR).
    if fields:
      field_overrides = _parse_update_fields(fields)
      if field_overrides:
        applied = ", ".join(f"{k}={v}" for k, v in sorted(field_overrides.items()))
        console.print(f"[cyan]:information_source: Applying manual field overrides:[/] {applied}")
      kwargs.update(field_overrides)

    # Step 9 — submit (retry loop for missing guardian fields)
    submitted = False
    while not submitted:
      try:
        with console.status("[bold cyan]:inbox_tray: Submitting enrollment...[/]"):
          client.add_player_to_registration_batch(
            batch_id, license, is_subida=is_subida, **kwargs,
          )
        console.print(f"[green]:white_check_mark: Added licence {license} to batch #{batch.number}.[/]")
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
    console.print("[bold]:outbox_tray: Uploaded:[/]")
    with console.status(
      "[bold cyan]:page_facing_up: Uploading source document (fpb_modelo_1)...[/]"
    ):
      # upload_path is the already-stamped copy from _prepare_club_stamp.
      ok, err = try_replace_document(
        client, batch_id, license, upload_path,
        tipo_doc=doc_type_to_tipo_doc(doc_type),
      )
    if ok:
      console.print(
        f"  [green]:white_check_mark:[/] {_display_name(pdf_path)} [dim](fpb_modelo_1)[/]",
        soft_wrap=True,
      )
    else:
      err_console.print(
        f"  [yellow]:warning:[/] {_display_name(pdf_path)} (fpb_modelo_1) upload failed: {err}"
      )
    if medical_exam_path:
      with console.status(
        "[bold cyan]:page_facing_up: Uploading medical exam (exame_medico)...[/]"
      ):
        # Medical exams don't have a carimbo concept → skip stamping.
        ok, err = try_replace_document(
          client, batch_id, license, medical_exam_path,
          tipo_doc=doc_type_to_tipo_doc(DocType.EXAME_MEDICO),
        )
      if ok:
        console.print(
          f"  [green]:white_check_mark:[/] {_display_name(medical_exam_path)} [dim](exame_medico)[/]",
          soft_wrap=True,
        )
      else:
        err_console.print(
          f"  [yellow]:warning:[/] {_display_name(medical_exam_path)} (exame_medico) upload failed: {err}"
        )

    # Step 9c — attach supplementary docs with their classified type. Replace
    # semantics (per tipo_doc) keep re-submits idempotent, matching mod1/em; two
    # files of the same type would collapse to the last one. Non-fatal.
    for extra_path, extra_dt in extra_docs:
      with console.status(
        f"[bold cyan]:page_facing_up: Uploading {_doc_type_text(extra_dt)}...[/]"
      ):
        ok, err = try_replace_document(
          client, batch_id, license, extra_path,
          tipo_doc=doc_type_to_tipo_doc(extra_dt),
        )
      if ok:
        console.print(
          f"  [green]:white_check_mark:[/] {_display_name(extra_path)} [dim]({_doc_type_text(extra_dt)})[/]",
          soft_wrap=True,
        )
      else:
        err_console.print(
          f"  [yellow]:warning:[/] {_display_name(extra_path)} ({_doc_type_text(extra_dt)}) "
          f"upload failed: {err}"
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
@click.option("--batch", "batch_number", type=str, default=None,
              help="List every player in this batch.")
@click.option("--license", "license_", type=int, default=None,
              help="Show this player's enrolment detail (batch resolved automatically).")
@click.pass_context
def enrollment_read_cmd(ctx, batch_number, license_):
  """Show player enrolments.

  \b
    sav enrollment read --batch BATCH_NUMBER    List every player in the batch.
    sav enrollment read --license LICENSE       Show one player's enrolment detail.
  """
  if (batch_number is None) == (license_ is None):
    raise click.UsageError("Pass exactly one of --batch BATCH_NUMBER or --license LICENSE.")

  output = ctx.obj["output"]
  client = _make_client()

  if batch_number is not None:
    batch_id = _resolve_batch_id_or_raise(client, batch_number)
    try:
      items = client.list_player_registration_batch_items(batch_id)
    except (SavConnectionError, SavResponseError, ValueError) as e:
      raise SavCliError(str(e), code=_exc_code(e))

    if output == "json":
      click.echo(json.dumps(items, ensure_ascii=False, indent=2))
      return

    if output == "csv":
      click.echo("license,name")
      for item in items:
        click.echo(f"{item['license']},{item['name']}")
      return

    if not items:
      _console().print("[cyan]:information_source: No players enrolled in this batch.[/]")
      return

    _render_table(["License", "Name"], [[str(i["license"]), i["name"]] for i in items])
    _console().print(f"\n[cyan]:information_source: {len(items)} player(s) enrolled.[/]")
    return

  batch_id = _resolve_batch_id_by_license_or_raise(client, license_)
  try:
    record = client.load_existing_registration_record(batch_id, license_)
  except (SavConnectionError, SavResponseError, ValueError) as e:
    raise SavCliError(str(e), code=_exc_code(e))

  if output == "json":
    click.echo(json.dumps(record, ensure_ascii=False, indent=2))
    return

  rows = [[k, str(v)] for k, v in record.items() if v not in (None, "")]

  if output == "csv":
    click.echo("field,value")
    for k, v in rows:
      click.echo(f"{k},{v}")
    return

  _render_table(["Field", "Value"], rows, max_widths=[24, None])


@enrollment_grp.command("update")
@click.option("--license", "license_", type=int, required=True,
              help="Licence number of the player to update (batch resolved automatically).")
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
  "--tipo", default=None,
  help=(
    "Document type "
    f"({ '|'.join(DOC_TYPE_CHOICES) }). "
    "When omitted with a PDF, sav-parsers classifies it automatically."
  ),
)
@click.pass_context
def enrollment_update_cmd(
  ctx, license_, pdf, mod1_path, medical_exam_path, fields, tipo,
):
  """Update an existing player enrolment.

  \b
    sav enrollment update --license LICENSE FILE
        OCR-reconcile FILE against SAV, patch fields, replace doc.
    sav enrollment update --license LICENSE --mod1 FILE
        Same with explicit doc type (skips classify; trains classifier).
    sav enrollment update --license LICENSE FILE --field KEY=VAL [...]
        OCR-reconcile FILE, apply manual field overrides, patch, replace doc.
    sav enrollment update --license LICENSE --field KEY=VAL [--field ...]
        Patch fields; no document.
    sav enrollment update --license LICENSE --medical-exam FILE
        Upload medical exam document only.
    sav enrollment update --license LICENSE FILE --tipo atestado_residencia
        Upload FILE as a supplementary document (classified, or pinned with
        --tipo; no OCR, replaces any existing doc of that type).
  """
  if pdf and (mod1_path or medical_exam_path):
    raise click.UsageError("Positional pdf and --mod1/--medical-exam are mutually exclusive.")
  has_pdf = bool(pdf or mod1_path)
  if not has_pdf and not fields and not medical_exam_path:
    raise click.UsageError(
      "Pass a PDF (positional or --mod1), --medical-exam, or --field K=V. "
      "Run `sav enrollment update --help` for examples."
    )

  doc_type: DocType | None = _resolve_doc_type(tipo) if tipo is not None else None

  console = _console()
  err_console = _console(err=True)

  # Convert any non-PDF inputs (images) to PDFs up front; cleanup is registered
  # on the Click context.
  converted: list[str] = []
  if pdf:
    pdf = _stage_pdf(ctx, console, pdf, converted=converted)
  if mod1_path:
    mod1_path = _stage_pdf(ctx, console, mod1_path, converted=converted)
  if medical_exam_path:
    medical_exam_path = _stage_pdf(ctx, console, medical_exam_path, converted=converted)
  _print_converted(console, converted)

  # Mode A / D — PDF-driven. fpb_modelo_1 runs the full OCR-reconcile-patch
  # flow; every other type has no field parser, so it's a plain attachment.
  if has_pdf:
    active_pdf = mod1_path or pdf
    active_filename = _display_name(active_pdf)
    from sav_parsers import close_processing, parse_fpb_mod1, train_classifier

    if mod1_path:
      # Explicit mod1: skip classify, train classifier.
      active_doc_type: DocType = DocType.FPB_MODELO_1
      try:
        train_classifier(mod1_path, active_doc_type)
      except Exception as exc:
        err_console.print(f"[yellow]:warning: Could not submit classifier training:[/] {exc}")
    elif doc_type is not None:
      active_doc_type = doc_type
    else:
      active_doc_type = _classify_pdf_doc_type(pdf)
      console.print(
        f"[green]:white_check_mark: Classified {active_filename} as {_doc_type_text(active_doc_type)}[/]",
        soft_wrap=True,
      )

    client = _make_client()
    batch_id = _resolve_batch_id_by_license_or_raise(client, license_)
    batch_number = _batch_number_for_log(client, batch_id)

    # Non-mod1 docs have no field parser: upload as-is (replace per tipo_doc),
    # optionally applying --field patches, and skip OCR/reconcile/stamping.
    if active_doc_type != DocType.FPB_MODELO_1:
      if fields:
        _patch_enrollment_fields(client, batch_id, license_, fields, console, batch_number)
      if active_doc_type == DocType.EXAME_MEDICO:
        _upload_medical_exam_update(
          batch_id, license_, active_pdf,
          client=client, console=console, err_console=err_console,
        )
      elif is_uploadable_doc_type(active_doc_type):
        tipo_doc = _tipo_doc_for_upload(active_doc_type)
        try:
          client.replace_player_registration_document(
            batch_id, license_, active_pdf, tipo_doc=tipo_doc,
          )
        except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
          raise SavCliError(str(exc), code=_exc_code(exc))
        console.print(
          f"[green]:white_check_mark: Uploaded {_doc_type_text(active_doc_type)}[/] "
          f"[dim]({_display_name(active_pdf)}) to batch #{batch_number}[/]",
          soft_wrap=True,
        )
      else:
        raise SavCliError(
          f"Document type {_doc_type_text(active_doc_type)!r} has no SAV2 "
          "tipo_doc mapping; cannot upload.",
          code="parse_error",
        )
      return

    # fpb_modelo_1 — full OCR-reconcile-patch-replace flow.
    _require_env("CLUB_STAMP_PATH")
    tipo_doc = _tipo_doc_for_upload(active_doc_type)

    try:
      with console.status("[bold cyan]:mag: Processing OCR...[/]"):
        parse_result = parse_fpb_mod1(active_pdf)
    except Exception as exc:
      raise SavCliError(f"parse error: {exc}", code="parse_error")
    parsed = parse_result["fields"]
    processing_id = parse_result["processing_id"]
    console.print(
      f"[green]:white_check_mark: OCR ready[/] [dim]{active_pdf} ({processing_id})[/]",
      soft_wrap=True,
    )

    close_called = False
    try:
      try:
        sav_profile = client.load_player_profile(license_)
      except (SavConnectionError, SavResponseError) as exc:
        raise SavCliError(f"Could not load player profile: {exc}", code=_exc_code(exc))

      try:
        result = reconcile_fpb_mod1(parsed, sav_profile, client=client)
      except (SavConnectionError, SavResponseError) as exc:
        raise SavCliError(f"Reconcile failed: {exc}", code=_exc_code(exc))

      # Stamp the club mark and/or mark the inscription checkbox (when OCR says
      # they are missing) up front so the summary reports what we actually did.
      # In the update flow reg_type is not derived from SAV, so fall back to
      # whatever OCR can see; if neither box is checked we skip the overlay.
      _ocr_reg_type = (
        2 if parsed_bool(parsed, "tipo_inscricao_revalidacao") else
        1 if parsed_bool(parsed, "tipo_inscricao_primeira") else
        None
      )
      upload_path, carimbo, carimbo_r, tipo_checked, inscricao_r = _prepare_club_stamp(
        ctx, console, err_console, parsed, active_pdf, processing_id,
        reg_type=_ocr_reg_type,
      )
      player_label = _player_label(sav_profile, license_)
      kwargs = _review_and_fill(result, sav_profile)
      _print_submission_summary(
        kwargs, result, sav_profile,
        ocr_source=f"OCR ({_display_name(active_pdf)})",
        extras=[
          _inscricao_extras_row(_ocr_reg_type, tipo_checked, inscricao_r),
          _carimbo_extras_row(carimbo, carimbo_r),
        ],
      )
      if not _confirm_documents_and_submit(
        player_label,
        [(_doc_type_text(active_doc_type), _display_name(active_pdf))],
      ):
        console.print("[yellow]:fast_forward: Skipped.[/]")
        return

      # Apply --field overrides on top of reconciled values (user wins over OCR).
      if fields:
        field_overrides = _parse_update_fields(fields)
        if field_overrides:
          applied = ", ".join(f"{k}={v}" for k, v in sorted(field_overrides.items()))
          console.print(f"[cyan]:information_source: Applying manual field overrides:[/] {applied}")
        kwargs.update(field_overrides)

      # Drop fields that update_player_in_registration_batch doesn't accept.
      ignored = {k: v for k, v in kwargs.items() if k not in _UPDATE_FIELDS}
      patch_kwargs = {k: v for k, v in kwargs.items() if k in _UPDATE_FIELDS}
      if ignored:
        console.print(
          f"[yellow]:warning: Ignored (not patchable on existing enrolments):[/] "
          f"{', '.join(sorted(ignored))}."
        )

      try:
        client.update_player_in_registration_batch(
          batch_id, license_, **patch_kwargs,
        )
      except (SavConnectionError, SavResponseError, ValueError) as exc:
        raise SavCliError(str(exc), code=_exc_code(exc))
      console.print(f"[green]:white_check_mark: Updated licence {license_} in batch #{batch_number}.[/]")

      try:
        # upload_path is the already-stamped copy from _prepare_club_stamp.
        client.replace_player_registration_document(
          batch_id, license_, upload_path, tipo_doc=tipo_doc,
        )
        console.print(
          f"[green]:white_check_mark: Uploaded {_doc_type_text(active_doc_type)}[/]"
          f" [dim]({_display_name(active_pdf)})[/]",
          soft_wrap=True,
        )
      except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
        err_console.print(
          f"[yellow]:warning: Field update succeeded but document upload failed:[/] {exc}"
        )

      if medical_exam_path:
        _upload_medical_exam_update(batch_id, license_, medical_exam_path, client=client, console=console, err_console=err_console)

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
        err_console.print(f"[yellow]:warning: Could not close processing {processing_id}:[/] {exc}")
    finally:
      if not close_called:
        try:
          close_processing(processing_id)
        except Exception:
          pass
    return

  # Mode C — fields-only (and optional medical exam upload).
  client = _make_client()
  batch_id = _resolve_batch_id_by_license_or_raise(client, license_)
  batch_number = _batch_number_for_log(client, batch_id)
  if fields:
    _patch_enrollment_fields(client, batch_id, license_, fields, console, batch_number)
  if medical_exam_path:
    _upload_medical_exam_update(batch_id, license_, medical_exam_path, client=client, console=console, err_console=err_console)


def _patch_enrollment_fields(
  client: Any, batch_id: int, license_: int, fields: tuple[str, ...],
  console: Any, batch_number: Any,
) -> None:
  """Apply --field K=V overrides to an existing enrolment record."""
  patch_kwargs = _parse_update_fields(fields)
  try:
    client.update_player_in_registration_batch(batch_id, license_, **patch_kwargs)
  except (SavConnectionError, SavResponseError, ValueError) as exc:
    raise SavCliError(str(exc), code=_exc_code(exc))
  applied = ", ".join(sorted(patch_kwargs))
  console.print(
    f"[green]:white_check_mark: Updated licence {license_} in batch #{batch_number}:[/] {applied}."
  )


def _upload_medical_exam_update(
  batch_id: int, license: int, path: str,
  *, client: Any = None, console: Any = None, err_console: Any = None,
) -> None:
  """Upload or replace a medical exam document for an existing enrolment."""
  from sav_parsers import train_classifier
  if client is None:
    client = _make_client()
  if console is None:
    console = _console()
  if err_console is None:
    err_console = _console(err=True)
  with staged_pdf(path) as (pdf_path, was_converted):
    if was_converted:
      console.print(f"[cyan]:arrows_counterclockwise: Converted [bold]{path}[/] to PDF.[/]")
    try:
      train_classifier(pdf_path, DocType.EXAME_MEDICO)
    except Exception as exc:
      err_console.print(f"[yellow]:warning: Could not submit classifier training for exam:[/] {exc}")
    tipo_doc = _tipo_doc_for_upload(DocType.EXAME_MEDICO)
    try:
      # Medical exams don't have a carimbo concept → skip stamping.
      client.replace_player_registration_document(batch_id, license, pdf_path, tipo_doc=tipo_doc)
      console.print(
        f"[green]:white_check_mark: Uploaded exame_medico[/] [dim]({Path(path).name})[/]",
        soft_wrap=True,
      )
    except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
      err_console.print(f"[yellow]:warning: Medical exam upload failed:[/] {exc}")


@enrollment_grp.command("delete")
@click.option("--license", "license_", type=int, default=None,
              help="Remove this player's enrolment (batch resolved automatically).")
@click.option("--batch", "batch_number", type=str, default=None,
              help="Delete the entire batch (only open batches can be deleted).")
@click.pass_context
def enrollment_delete_cmd(ctx, license_, batch_number):
  """Remove a player from a batch, or delete a whole batch.

  \b
    sav enrollment delete --license LICENSE       Remove one player's enrolment.
    sav enrollment delete --batch BATCH_NUMBER    Delete the entire batch.
  """
  if (license_ is None) == (batch_number is None):
    raise click.UsageError("Pass exactly one of --license LICENSE or --batch BATCH_NUMBER.")

  client = _make_client()
  console = _console()

  if license_ is not None:
    batch_id = _resolve_batch_id_by_license_or_raise(client, license_)
    batch_number_log = _batch_number_for_log(client, batch_id)
    if not click.confirm(
      f"Remove licence {license_} from batch {batch_number_log}?", default=False
    ):
      raise click.Abort()
    try:
      client.remove_player_from_registration_batch(batch_id, license_)
    except (SavConnectionError, SavResponseError, ValueError) as e:
      raise SavCliError(str(e), code=_exc_code(e))
    console.print(
      f"[green]:white_check_mark: Licence {license_} removed from batch #{batch_number_log}.[/]"
    )
    return

  batch_id = _resolve_batch_id_or_raise(client, batch_number)
  if not click.confirm(
    f"Delete entire batch {batch_number} (and every enrolment in it)?", default=False
  ):
    raise click.Abort()
  try:
    client.delete_player_registration_batch(batch_id)
  except (SavConnectionError, SavResponseError, ValueError) as e:
    raise SavCliError(str(e), code=_exc_code(e))
  console.print(f"[green]:white_check_mark: Batch #{batch_number} deleted.[/]")


def main():
  cli()
