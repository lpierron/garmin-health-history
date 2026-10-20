# Qualité du code — un point d'entrée pour tout :
#
#   make check   → lint + formatage + types + tests (ce que fait la CI)
#   make fix     → corrections automatiques + formatage
#   make lint / format / format-check / types / test → une étape à la fois
#
# Tout passe par `uv` : les outils (ruff, mypy) sont déclarés dans le
# groupe « dev » du pyproject.toml, `uv sync` les installe.

.PHONY: check lint format format-check types test fix lock

check: lint format-check types test

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

# mypy (statique, configuration dans pyproject.toml) puis ty
types:
	uv run mypy
	uv run ty check .

test:
	uv run python -m unittest discover -s tests

fix:
	uv run ruff check --fix .
	uv run ruff format .

lock:
	uv lock
