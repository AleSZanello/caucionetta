# CLAUDE.md

Read [AGENTS.md](AGENTS.md): setup, tests, layout and the safety rules for agents all live there. In short:
never enable live mode or place/apply orders without the human's explicit yes, never touch the real IOL API or
credentials while developing (use `CAUCIONETTA_DEMO=1`), and run `pytest -q && ruff check .` before handing back.
