> **Work in progress**

This project is not ready for installation or use. It provides no deployment or compatibility guarantee.

# Playerbot LLM

Playerbot LLM is one public AzerothCore module with two cooperating parts. The C++ bridge connects the
worldserver to a local process. The Python sidecar owns provider access, budgets, durable generation state,
and validation at the external boundary.

The bridge and sidecar communicate through a versioned wire protocol. Provider adapters sit behind the
same sidecar interface. The included adapter uses the Anthropic API, and additional providers can implement
that interface without changing Playerbots, Personality, Economy, or Social.

The language model never controls the game. It proposes bounded text or an opaque candidate token.
Personality, Economy, and Social validate every proposal and remain authoritative. A failed, late, invalid,
or unavailable generation results in deterministic fallback or silence according to the calling module.

## Public dependencies

The C++ module integrates with these public repositories.

1. `Fuitad/mod-playerbots-upstream` supplies the generic Playerbots extension seams.
2. `Fuitad/mod-playerbot-personality` supplies stable personality and fictional identity values.
3. `Fuitad/mod-playerbots-economy` supplies the bounded career provider contract.
4. `Fuitad/mod-playerbots-social` supplies the grounded conversation provider contract.

`PLAYERBOTS_REVISION` records the tested Playerbots fork revision. The continuous integration workflow
checks out only public repositories through anonymous HTTPS.

## Installation

Place the repositories in the following AzerothCore module directories.

```text
modules/mod-playerbots
modules/mod-playerbot-personality
modules/mod-playerbots-economy
modules/mod-playerbots-social
modules/mod-playerbot-llm
```

Configure and build AzerothCore with static modules. Then create the sidecar environment.

```bash
cd modules/mod-playerbot-llm/sidecar
uv sync --locked
```

Copy `conf/mod_playerbot_llm.conf.dist` into the worldserver configuration directory as
`mod_playerbot_llm.conf`. Every generation path ships disabled.

The Playerbots database loader discovers this module's migrations in `data/sql/db_playerbot/updates`.

## Configuration and secrets

`conf/mod_playerbot_llm.conf.dist` documents the bridge, queue, timeout, provider price, budget, and legacy
compatibility settings. These settings belong to this module and do not live in `playerbots.conf`.

Secrets are accepted only through environment variables.

`PLAYERBOT_LLM_BRIDGE_TOKEN` authenticates the loopback connection. Both processes refuse a missing or short
token.

`MOD_PLAYERBOT_LLM_ANTHROPIC_API_KEY` is read only by the Anthropic adapter. A machine wide
`ANTHROPIC_API_KEY` is deliberately ignored.

## Operation

Start the sidecar before worldserver or alongside it.

```bash
cd modules/mod-playerbot-llm/sidecar
export PLAYERBOT_LLM_BRIDGE_TOKEN=replace_with_at_least_32_bytes
export MOD_PLAYERBOT_LLM_ANTHROPIC_API_KEY=replace_with_provider_key
uv run playerbot-llm serve \
  --config /path/to/mod_playerbot_llm.conf \
  --playerbots-config /path/to/playerbots.conf
```

The Playerbots configuration path supplies the shared Playerbots database connection. It does not carry LLM
feature settings or provider secrets.

Check configuration, budget state, and database reachability without printing secrets.

```bash
uv run playerbot-llm doctor \
  --config /path/to/mod_playerbot_llm.conf \
  --playerbots-config /path/to/playerbots.conf
```

Inspect the latest observed bot profile.

```bash
uv run playerbot-llm profile \
  --config /path/to/mod_playerbot_llm.conf \
  --playerbots-config /path/to/playerbots.conf \
  --bot-guid 42
```

## Data boundaries

The sidecar stores its state in the Playerbots database. Conversation turns are bounded and are the only
sidecar table that stores player text. Secrets, account names, addresses, positions, inventories, and combat
state are not stored.

Social assembles privacy scoped context and authoritative evidence before a request leaves worldserver. The
sidecar validates the scope again. Provider output is always a proposal. Social performs the final grounding,
privacy, delivery, and consent checks.

Economy submits opaque legal career candidates. The provider cannot invent a profession, spell, recipe,
item, destination, price, or action. Economy validates the selected token before persistence.

## Development

Run the offline sidecar checks.

```bash
cd sidecar
uv sync --locked --dev
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run basedpyright src tests
```

The optional MySQL integration harness starts and removes its own temporary database container.

```bash
bash scripts/run_ledger_mysql_tests.sh
```

The GitHub workflow also configures AzerothCore, builds the C++ unit target, and runs the Playerbots unit
suite against the tested public module combination.
