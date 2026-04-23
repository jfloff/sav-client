# SAV Client

Python client and CLI for the **FPB SAV2** basketball management system
([sav2.fpb.pt](https://sav2.fpb.pt)).

Automates player lookups, game scheduling queries, and eligible-players PDF
generation — tasks that otherwise require navigating the SAV2 web interface
manually.

---

## Features

- **Player search** — filter by name, licence, club, association, tier, season, status
- **Player detail** — fetch full profile including photo URL
- **Club & association listing** — browse the federation hierarchy
- **Game listing** — query scheduled and played games with flexible filters
- **Game sheet** — view eligible players/coaches/staff and generate the
  official pre-game PDF directly from SAV2
- **CLI** — `sav` command with table, JSON, and CSV output formats

---

## Installation

Requires Python 3.11+.

```bash
pip install git+https://github.com/jfloff/sav-client.git
```

Or clone and install in editable mode for development:

```bash
git clone https://github.com/jfloff/sav-client.git
cd sav-client
pip install -e .
```

---

## Configuration

Create a `.env` file (or export the variables):

```
SAV_USERNAME=your_username
SAV_PASSWORD=your_password
```

---

## CLI usage

```bash
# Players
sav players --club "Rio Maior" --name "João" --tier "Sénior"
sav players --club "Rio Maior" --status active
sav players --tier "Mini 12" --tier "Mini 10" --tier "Baby Basket" --all-clubs
sav players --club "Rio Maior" --season 0
sav --output json players --license 301772 --all-clubs

# Games
sav games
sav games --date-from 01-04-2026 --date-to 30-04-2026
sav games --tier "Sub 14" --status all

# Game sheets (played games)
sav game-sheets --date 19-04-2026
sav game-sheets --tier "Sub 14" --status Realizado

# View eligible players for a game
sav game-sheet 841                        # both teams
sav game-sheet 841 --home --coaches       # home coaches only

# Generate eligible players PDF
sav game-sheet 841 --home --out
sav game-sheet 841 --home --out sheet.pdf --player 301772 --coach-pri 44321

# Clubs & associations
sav clubs --association 7
sav clubs "Rio Maior" --all-associations
sav associations
```

Run `sav --help` or `sav <command> --help` for full option reference.

---

## Python API

```python
from sav_client import SavClient

client = SavClient.from_env()
client.login()

# Search players
players = client.search_players(name="João", tier="Sénior", status="active", club=270)

# List games
games = client.list_games(date_from="01-04-2026", date_to="30-04-2026")

# Get eligible players for a game
data = client.get_eligible_players(games[0].id, val=1)  # val=1 home, val=2 away

# Generate PDF
pdf = client.get_eligible_players_pdf(
    games[0].id, val=1,
    player_licences=[301772, 285943],
    coaches_pri=[44321],
)
open("sheet.pdf", "wb").write(pdf)
```

See [`sav_client/AGENTS.md`](sav_client/AGENTS.md) for the full API reference.

---

## Agent / LLM reference

Machine-readable documentation for AI agents:

- [`sav_client/AGENTS.md`](sav_client/AGENTS.md) — Python library API
- [`sav_cli/AGENTS.md`](sav_cli/AGENTS.md) — CLI command reference

---

## Disclaimer

This project was built with the assistance of
**[Claude Code](https://claude.ai/code)** (Anthropic). Claude Code was used
throughout — for code generation, refactoring, test writing, and
documentation. All code has been reviewed and is maintained by the author.

---

## License

MIT
