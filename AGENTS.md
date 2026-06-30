# Repository Guidelines

## Project Structure & Module Organization

GenericAgent is a compact Python agent framework. Core runtime files live at the repository root: `agent_loop.py`, `agentmain.py`, `ga.py`, `llmcore.py`, `simphtml.py`, and `TMWebDriver.py`. The installable CLI package is in `ga_cli/`, with the console script exposed as `ga`. User interfaces are under `frontends/`; the Tauri desktop app is in `frontends/desktop/`, while Python UIs and bot integrations are sibling modules such as `tui_v3.py`, `stapp.py`, and `wechatapp.py`. Reusable skills and SOPs are stored in `memory/`, plugin hooks in `plugins/`, documentation in `docs/`, and images, prompts, installers, and browser-extension assets in `assets/`.

## Build, Test, and Development Commands

- `uv venv && uv pip install -e ".[ui]"`: create a local environment and install the editable package with UI dependencies.
- `uv pip install -e .`: install only the lightweight core dependencies.
- `python frontends/tui_v3.py`: run the recommended terminal UI.
- `python launch.pyw`: launch the Streamlit/web UI wrapper.
- `ga --help`: verify the installed CLI entry point.
- `python -m py_compile agent_loop.py agentmain.py ga.py llmcore.py`: quick syntax check for core changes.
- `cd frontends/desktop && npm install && npm run tauri -- dev`: run the desktop Tauri frontend.

## Coding Style & Naming Conventions

Target Python 3.10-3.13; README recommends Python 3.11 or 3.12 for UI compatibility. Follow the existing compact style: small functions, direct control flow, minimal comments, and limited dependencies. Use `snake_case` for Python modules, functions, and variables; keep frontend assets named by purpose. No repo-wide formatter or linter is configured, so match surrounding code and avoid broad mechanical rewrites.

## Testing Guidelines

There is no formal test suite in this snapshot. For changes, run `py_compile` on affected Python files and perform a focused smoke test through the relevant entry point, such as `ga --help`, `python frontends/tui_v3.py`, or the frontend/plugin touched. Name new tests with `test_*.py` and keep fixtures close to the behavior under test.

## Commit & Pull Request Guidelines

Recent history uses concise conventional-style subjects such as `fix(desktop): ...` and `feat(tui): ...`; keep commits scoped and imperative. PRs should link an issue or give brief context, describe user-visible behavior, list verification commands, and include screenshots or clips for UI changes. Avoid unnecessary dependencies and prefer changes that shrink or localize code.

## Security & Configuration Tips

Do not commit secrets. Use `mykey_template.py` or `mykey_template_en.py` to create local `mykey.py`, and keep API keys, session data, and generated temp files out of commits.
