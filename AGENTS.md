# Repository Guidelines

## Project Structure & Module Organization

Personal-review-agent is a focused fork of GenericAgent for personal Obsidian review workflows. Core runtime files live at the repository root: `agent_loop.py`, `agentmain.py`, `ga.py`, `llmcore.py`, `simphtml.py`, and `TMWebDriver.py`. The installable CLI package is in `ga_cli/`, with the console script exposed as `ga`. The retained user interfaces are `frontends/tui_v3.py` for terminal use and the Tauri desktop app under `frontends/desktop/` with `frontends/desktop_bridge.py`. The WeChat adapter remains in `frontends/wechatapp.py`. Obsidian review logic lives in `frontends/obsidian_review_cmd.py`, `memory/obsidian_review/obsidian_review.py`, `memory/obsidian_profile_init_sop.md`, and `memory/obsidian_periodic_review_sop.md`.

## Build, Test, and Development Commands

- `uv venv && uv pip install -e ".[ui]"`: create a local environment and install the editable package with terminal UI dependencies.
- `uv pip install -e .`: install only the lightweight core dependencies.
- `python frontends/tui_v3.py`: run the recommended terminal UI.
- `python launch.pyw`: start the retained desktop bridge UI in a browser.
- `ga --help`: verify the installed CLI entry point.
- `python -m py_compile agent_loop.py agentmain.py ga.py llmcore.py frontends/tui_v3.py frontends/desktop_bridge.py frontends/obsidian_review_cmd.py memory/obsidian_review/obsidian_review.py`: quick syntax check for core changes.
- `cd frontends/desktop && npm install && npm run tauri -- dev`: run the desktop Tauri frontend.

## Coding Style & Naming Conventions

Target Python 3.10-3.13; use Python 3.11 or 3.12 for best UI compatibility. Follow the existing compact style: small functions, direct control flow, minimal comments, and limited dependencies. Use `snake_case` for Python modules, functions, and variables; keep frontend assets named by purpose. No repo-wide formatter or linter is configured, so match surrounding code and avoid broad mechanical rewrites.

## Testing Guidelines

There is no formal test suite in this snapshot. For changes, run `py_compile` on affected Python files and perform a focused smoke test through the relevant entry point, such as `ga --help`, `python frontends/tui_v3.py`, or the Obsidian review command touched. Name new tests with `test_*.py` and keep fixtures close to the behavior under test.

## Commit & Pull Request Guidelines

Recent history uses concise conventional-style subjects such as `fix(desktop): ...` and `feat(tui): ...`; keep commits scoped and imperative. PRs should give brief context, describe user-visible behavior, list verification commands, and include screenshots or clips for UI changes. Avoid unnecessary dependencies and prefer changes that shrink or localize code.

## Security & Configuration Tips

Do not commit secrets. Use `mykey_template.py` or `mykey_template_en.py` to create local `mykey.py`, and keep API keys, session data, vault-local `.obsidian-review-agent/` state, and generated temp files out of commits.
