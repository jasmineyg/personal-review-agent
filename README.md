# Personal-review-agent

Personal-review-agent is a focused fork of GenericAgent for personal Obsidian review workflows.

The retained product path is:

- First-run vault profiling with `/obsidian-review init-profile --vault <path>`.
- User-confirmed vault model in `Reviews/_AgentProfile/vault_profile.draft.md`.
- Periodic reviews through `/obsidian-review this-week --vault <path>`.
- Deterministic scan, diff, and state handling in `memory/obsidian_review/obsidian_review.py`.

## Main Entry Points

- `python frontends/tui_v3.py`: terminal UI.
- `cd frontends/desktop && npm install && npm run tauri -- dev`: Tauri desktop UI.
- `python launch.pyw`: start the retained desktop bridge UI in a browser.
- `python agentmain.py`: lightweight CLI chat.

## Verification

```powershell
python -m py_compile agent_loop.py agentmain.py ga.py llmcore.py frontends\tui_v3.py frontends\desktop_bridge.py frontends\obsidian_review_cmd.py memory\obsidian_review\obsidian_review.py
ga --help
```

## Local Configuration

Do not commit secrets. Copy `mykey_template.py` or `mykey_template_en.py` to `mykey.py` for local model credentials.
