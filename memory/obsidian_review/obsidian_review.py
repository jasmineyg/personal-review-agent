#!/usr/bin/env python
"""Deterministic Obsidian review helper for GenericAgent.

This script prepares privacy-filtered, block-level Obsidian changes for the
current GenericAgent LLM session, then finalizes snapshots only after the report
has been written back to the vault.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9 fallback, kept harmless.
    ZoneInfo = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
DEFAULT_CONFIG: dict[str, Any] = {
    "vault_path": "",
    "state_dir": ".obsidian-review-agent",
    "output_dir": "Reviews",
    "profile_draft_dir": "Reviews/_AgentProfile",
    "ignore_dirs": [".obsidian", ".trash", ".obsidian-review-agent", "Reviews"],
    "ignore_tags": ["#private", "#secret", "#ignore-review", "#no-review"],
    "default_period": "this-week",
    "timezone": "Asia/Shanghai",
    "week_start": "monday",
    "report_language": "zh-CN",
    "topic_mode": "auto_with_preferences",
    "preferred_topics": [],
}

PERIOD_CHOICES = ("today", "this-week", "last-week", "this-month")
BLOCK_ID_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9_-]+)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
TODO_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\]\s+(.+?)\s*$")
TAG_BOUNDARY_TEMPLATE = r"(?<![\w/-]){}(?![\w/-])"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and finalize Obsidian review data.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create Obsidian review config/state directories.")
    init_parser.add_argument("--vault", required=True, help="Obsidian Vault path.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing config.json.")

    profile_init_parser = subparsers.add_parser("profile-init", help="Scan vault and write editable profile draft.")
    add_config_args(profile_init_parser)

    profile_confirm_parser = subparsers.add_parser("profile-confirm", help="Confirm edited profile draft and establish initial snapshot.")
    add_config_args(profile_confirm_parser)

    prepare_parser = subparsers.add_parser("prepare", help="Scan vault and write changed_blocks.latest.json.")
    add_config_args(prepare_parser)
    prepare_parser.add_argument("--period", choices=PERIOD_CHOICES, help="Built-in review period.")
    prepare_parser.add_argument("--from", dest="from_date", help="Start date, inclusive, in YYYY-MM-DD format.")

    finalize_parser = subparsers.add_parser("finalize", help="Commit pending snapshot after report writeback.")
    add_config_args(finalize_parser)
    finalize_parser.add_argument("--report", required=True, help="Report Markdown path written into the vault.")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = cmd_init(Path(args.vault), force=args.force)
        elif args.command == "profile-init":
            result = cmd_profile_init(args)
        elif args.command == "profile-confirm":
            result = cmd_profile_confirm(args)
        elif args.command == "prepare":
            result = cmd_prepare(args)
        elif args.command == "finalize":
            result = cmd_finalize(args)
        else:  # pragma: no cover - argparse prevents this.
            parser.error("unknown command")
            return 2
    except UserFacingError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault", help="Obsidian Vault path.")
    parser.add_argument("--config", help="Path to .obsidian-review-agent/config.json.")


class UserFacingError(RuntimeError):
    pass


def cmd_init(vault_arg: Path, force: bool = False) -> dict[str, Any]:
    vault = vault_arg.expanduser().resolve()
    if not vault.exists() or not vault.is_dir():
        raise UserFacingError(f"Vault path does not exist or is not a directory: {vault}")

    config = dict(DEFAULT_CONFIG)
    config["vault_path"] = str(vault)
    state_dir = vault / config["state_dir"]
    output_dir = vault / config["output_dir"]
    profile_draft_dir = vault / config["profile_draft_dir"]
    state_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_draft_dir.mkdir(parents=True, exist_ok=True)

    config_path = state_dir / "config.json"
    if config_path.exists() and not force:
        existing = load_json(config_path)
        return {
            "ok": True,
            "action": "init",
            "created": False,
            "config": str(config_path),
            "state_dir": str(state_dir),
            "output_dir": str(output_dir),
            "profile_draft_dir": str(profile_draft_dir),
            "message": "config.json already exists; use --force to overwrite",
            "config_preview": existing,
        }

    atomic_write_json(config_path, config)
    ensure_state_files(state_dir)
    return {
        "ok": True,
        "action": "init",
        "created": True,
        "config": str(config_path),
        "state_dir": str(state_dir),
        "output_dir": str(output_dir),
        "profile_draft_dir": str(profile_draft_dir),
    }


def cmd_profile_init(args: argparse.Namespace) -> dict[str, Any]:
    config, config_path, vault = load_config_from_args(args)
    state_dir = vault / config["state_dir"]
    profile_dir = vault / config["profile_draft_dir"]
    state_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    ensure_inside_vault(profile_dir, vault, "profile draft directory")
    ensure_config_file(config_path, config)
    ensure_state_files(state_dir)

    tz = get_timezone(config.get("timezone", "Asia/Shanghai"))
    run_at = datetime.now(tz)
    snapshot, skipped = build_snapshot(vault, config, run_at)
    summary = build_vault_profile_summary(snapshot, skipped, config, run_at)
    draft_path = profile_dir / "vault_profile.draft.md"
    atomic_write_text(draft_path, render_vault_profile_draft(summary))

    return {
        "ok": True,
        "action": "profile-init",
        "vault_path": str(vault),
        "config_path": str(config_path),
        "profile_draft": str(draft_path),
        "profile_draft_rel": rel_to_vault(draft_path, vault),
        "markdown_files": summary["overview"]["markdown_files"],
        "folders": len(summary["folder_summaries"]),
        "skipped": skipped,
        "next": "Edit Reviews/_AgentProfile/vault_profile.draft.md in Obsidian, then run /obsidian-review confirm-profile --vault <path>.",
    }


def cmd_profile_confirm(args: argparse.Namespace) -> dict[str, Any]:
    config, config_path, vault = load_config_from_args(args)
    state_dir = vault / config["state_dir"]
    profile_dir = vault / config["profile_draft_dir"]
    draft_path = profile_dir / "vault_profile.draft.md"
    confirmed_path = state_dir / "vault_profile.confirmed.json"
    snapshot_path = state_dir / "review_snapshot.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    ensure_config_file(config_path, config)
    ensure_state_files(state_dir)

    if not draft_path.exists() or not draft_path.is_file():
        raise UserFacingError(
            f"Profile draft does not exist: {draft_path}. Run profile-init first."
        )

    tz = get_timezone(config.get("timezone", "Asia/Shanghai"))
    run_at = datetime.now(tz)
    draft_text = draft_path.read_text(encoding="utf-8-sig", errors="replace")
    snapshot, skipped = build_snapshot(vault, config, run_at)
    summary = build_vault_profile_summary(snapshot, skipped, config, run_at)
    confirmed_profile = build_confirmed_profile(draft_text, summary, config, vault, draft_path, run_at)

    atomic_write_json(confirmed_path, confirmed_profile)
    atomic_write_json(snapshot_path, snapshot)

    return {
        "ok": True,
        "action": "profile-confirm",
        "vault_path": str(vault),
        "config_path": str(config_path),
        "profile_draft": str(draft_path),
        "confirmed_profile": str(confirmed_path),
        "snapshot": str(snapshot_path),
        "markdown_files": summary["overview"]["markdown_files"],
        "skipped": skipped,
        "next": "Confirmed profile saved. Periodic /obsidian-review runs can now use this calibrated vault model.",
    }


def cmd_prepare(args: argparse.Namespace) -> dict[str, Any]:
    config, config_path, vault = load_config_from_args(args)
    state_dir = vault / config["state_dir"]
    output_dir = vault / config["output_dir"]
    state_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    ensure_config_file(config_path, config)
    profile_update_path = state_dir / "vault_profile_update.latest.json"

    tz = get_timezone(config.get("timezone", "Asia/Shanghai"))
    run_at = datetime.now(tz)
    period_info = resolve_period(args.period, args.from_date, config, run_at)

    confirmed_profile_path = state_dir / "vault_profile.confirmed.json"
    confirmed_profile = load_json(confirmed_profile_path, default=None)
    if not confirmed_profile:
        raise UserFacingError(
            "Missing confirmed vault profile. Run /obsidian-review init-profile --vault <path>, "
            "edit Reviews/_AgentProfile/vault_profile.draft.md, then run "
            "/obsidian-review confirm-profile --vault <path> before periodic review."
        )

    previous_snapshot_path = state_dir / "review_snapshot.json"
    previous_snapshot = load_json(previous_snapshot_path, default=None)
    if not previous_snapshot:
        raise UserFacingError(
            "Missing initial review snapshot. Run /obsidian-review confirm-profile --vault <path> "
            "to establish the confirmed baseline from the current Vault."
        )
    previous_state = load_json(state_dir / "review_state.json", default={})
    current_snapshot, skipped = build_snapshot(vault, config, run_at)
    if isinstance(previous_state, dict) and not previous_state.get("last_run"):
        changed_files = period_file_summaries(current_snapshot, period_info, confirmed_profile)
        changed_blocks = first_baseline_blocks(current_snapshot, period_info)
        run_mode = "first_baseline"
    else:
        changed_files = diff_file_summaries(previous_snapshot, current_snapshot, period_info, confirmed_profile)
        changed_blocks = diff_snapshots(previous_snapshot, current_snapshot, period_info)
        run_mode = "block_diff"

    suggested_report = allocate_report_path(vault, output_dir, period_info["period"], run_at)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "source": "GenericAgent",
        "vault_path": str(vault),
        "config_path": str(config_path),
        "state_dir": rel_to_vault(state_dir, vault),
        "period": period_info["period"],
        "date_start": period_info["date_start"],
        "date_end": period_info["date_end"],
        "run_at": run_at.isoformat(),
        "run_mode": run_mode,
        "changed_files_count": len(changed_files),
        "changed_file_status_counts": count_by(changed_files, "file_status"),
        "changed_blocks_count": len(changed_blocks),
        "changed_status_counts": count_by(changed_blocks, "status"),
        "candidate_activity_counts": count_by(changed_blocks, "candidate_activity"),
        "skipped": skipped,
        "preferred_topics": config.get("preferred_topics", []),
        "topic_mode": config.get("topic_mode", "auto_with_preferences"),
        "vault_profile_file": rel_to_vault(confirmed_profile_path, vault),
        "vault_profile_confirmed_at": confirmed_profile.get("confirmed_at"),
        "vault_profile_update_file": rel_to_vault(profile_update_path, vault),
        "suggested_report": rel_to_vault(suggested_report, vault),
        "suggested_report_path": str(suggested_report),
        "pending_snapshot": rel_to_vault(state_dir / "pending_snapshot.latest.json", vault),
    }

    payload = {
        "meta": meta,
        "changed_files": changed_files,
        "blocks": changed_blocks,
        "previous_state": previous_state,
        "vault_profile": confirmed_profile,
        "report_outline": report_outline(),
        "writing_guidelines": writing_guidelines(run_mode),
    }
    digest_payload = build_review_digest(payload)
    profile_update_payload = build_profile_update_suggestions(digest_payload, confirmed_profile, run_at)
    changed_path = state_dir / "changed_blocks.latest.json"
    digest_path = state_dir / "review_digest.latest.json"
    pending_path = state_dir / "pending_snapshot.latest.json"
    atomic_write_json(changed_path, payload)
    atomic_write_json(digest_path, digest_payload)
    atomic_write_json(pending_path, current_snapshot)
    atomic_write_json(profile_update_path, profile_update_payload)

    return {
        "ok": True,
        "action": "prepare",
        "vault_path": str(vault),
        "run_mode": run_mode,
        "period": period_info["period"],
        "date_start": period_info["date_start"],
        "date_end": period_info["date_end"],
        "changed_files": len(changed_files),
        "changed_blocks": len(changed_blocks),
        "changed_blocks_file": str(changed_path),
        "review_digest_file": str(digest_path),
        "pending_snapshot": str(pending_path),
        "confirmed_profile": str(confirmed_profile_path),
        "vault_profile_update_file": str(profile_update_path),
        "suggested_report": str(suggested_report),
        "next": "Generate the Markdown report from review_digest.latest.json using changed/new files as the primary evidence and changed_blocks.latest.json only for detail lookup, write review_state_update.latest.json and optional vault_profile_update.latest.json suggestions, then run finalize.",
    }


def cmd_finalize(args: argparse.Namespace) -> dict[str, Any]:
    config, _config_path, vault = load_config_from_args(args)
    state_dir = vault / config["state_dir"]
    pending_path = state_dir / "pending_snapshot.latest.json"
    snapshot_path = state_dir / "review_snapshot.json"
    changed_path = state_dir / "changed_blocks.latest.json"
    state_path = state_dir / "review_state.json"
    update_path = state_dir / "review_state_update.latest.json"

    report_path = Path(args.report).expanduser()
    if not report_path.is_absolute():
        report_path = (Path.cwd() / report_path).resolve()
    else:
        report_path = report_path.resolve()
    if not report_path.exists() or not report_path.is_file():
        raise UserFacingError(f"Report file does not exist: {report_path}")
    ensure_inside_vault(report_path, vault, "report")
    if not pending_path.exists():
        raise UserFacingError(f"Missing pending snapshot. Run prepare first: {pending_path}")

    pending_snapshot = load_json(pending_path)
    changed_payload = load_json(changed_path, default={})
    changed_meta = changed_payload.get("meta", {}) if isinstance(changed_payload, dict) else {}
    atomic_write_json(snapshot_path, pending_snapshot)

    existing_state = load_json(state_path, default={})
    if not isinstance(existing_state, dict):
        existing_state = {}
    state_update = load_json(update_path, default={})
    if not isinstance(state_update, dict):
        state_update = {}
    next_state = merge_review_state(existing_state, state_update)
    next_state["latest_report"] = rel_to_vault(report_path, vault)
    next_state["last_run"] = {
        "period": changed_meta.get("period"),
        "run_mode": changed_meta.get("run_mode"),
        "date_start": changed_meta.get("date_start"),
        "date_end": changed_meta.get("date_end"),
        "changed_blocks": changed_meta.get("changed_blocks_count", 0),
    }
    atomic_write_json(state_path, next_state)

    return {
        "ok": True,
        "action": "finalize",
        "report": str(report_path),
        "snapshot": str(snapshot_path),
        "state": str(state_path),
        "latest_report": next_state["latest_report"],
    }


def ensure_state_files(state_dir: Path) -> None:
    state_path = state_dir / "review_state.json"
    if not state_path.exists():
        atomic_write_json(
            state_path,
            {
                "latest_report": None,
                "open_items": [],
                "blockers": [],
                "active_topics": [],
                "last_run": None,
            },
        )


def ensure_config_file(config_path: Path, config: dict[str, Any]) -> None:
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(config_path, config)


def load_config_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], Path, Path]:
    config_path: Path | None = None
    if getattr(args, "config", None):
        config_path = Path(args.config).expanduser().resolve()
        if not config_path.exists():
            raise UserFacingError(f"Config file does not exist: {config_path}")
        config = normalize_config(load_json(config_path))
        vault = Path(config.get("vault_path") or "").expanduser().resolve()
    else:
        vault = resolve_vault_arg(getattr(args, "vault", None))
        config_path = vault / DEFAULT_CONFIG["state_dir"] / "config.json"
        if config_path.exists():
            config = normalize_config(load_json(config_path))
        else:
            config = dict(DEFAULT_CONFIG)
            config["vault_path"] = str(vault)

    if not vault.exists() or not vault.is_dir():
        raise UserFacingError(f"Vault path does not exist or is not a directory: {vault}")
    config["vault_path"] = str(vault)
    return config, config_path, vault


def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UserFacingError("Config JSON must be an object.")
    config = dict(DEFAULT_CONFIG)
    config.update(raw)
    for key in ("ignore_dirs", "ignore_tags", "preferred_topics"):
        if not isinstance(config.get(key), list):
            raise UserFacingError(f"config.{key} must be a list.")
    mandatory_ignores = [DEFAULT_CONFIG["state_dir"], config.get("state_dir", ".obsidian-review-agent")]
    normalized_existing = {normalize_path_part(item) for item in config.get("ignore_dirs", [])}
    for item in mandatory_ignores:
        if normalize_path_part(item) not in normalized_existing:
            config["ignore_dirs"].append(item)
            normalized_existing.add(normalize_path_part(item))
    return config


def resolve_vault_arg(vault_arg: str | None) -> Path:
    if vault_arg:
        return Path(vault_arg).expanduser().resolve()
    discovered = discover_obsidian_vaults()
    if len(discovered) == 1:
        return discovered[0]
    if len(discovered) > 1:
        choices = "\n".join(f"- {path}" for path in discovered)
        raise UserFacingError("Multiple Obsidian vaults discovered; pass --vault explicitly:\n" + choices)
    raise UserFacingError("No vault specified and no Obsidian vault could be discovered. Pass --vault.")


def discover_obsidian_vaults() -> list[Path]:
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        obsidian_json = Path(appdata) / "Obsidian" / "obsidian.json"
        if obsidian_json.exists():
            try:
                data = load_json(obsidian_json)
                for item in data.get("vaults", {}).values():
                    path_text = item.get("path") if isinstance(item, dict) else None
                    if path_text:
                        path = Path(path_text).expanduser()
                        if path.exists() and path.is_dir():
                            candidates.append(path.resolve())
            except Exception:
                pass
    return sorted(dict.fromkeys(candidates))


def get_timezone(name: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise UserFacingError(f"Invalid timezone in config: {name}") from exc


def resolve_period(period_arg: str | None, from_date: str | None, config: dict[str, Any], run_at: datetime) -> dict[str, str]:
    if period_arg and from_date:
        raise UserFacingError("Use either --period or --from, not both.")
    if from_date:
        start_date = parse_ymd(from_date)
        start_dt = datetime.combine(start_date, time.min, tzinfo=run_at.tzinfo)
        return {"period": "since", "date_start": start_dt.isoformat(), "date_end": run_at.isoformat()}

    period = period_arg or config.get("default_period") or "this-week"
    if period not in PERIOD_CHOICES:
        raise UserFacingError(f"Unsupported period: {period}")

    today = run_at.date()
    if period == "today":
        start = datetime.combine(today, time.min, tzinfo=run_at.tzinfo)
        end = run_at
    elif period == "this-week":
        week_start = week_start_offset(config.get("week_start", "monday"))
        delta_days = (today.weekday() - week_start) % 7
        start = datetime.combine(today - timedelta(days=delta_days), time.min, tzinfo=run_at.tzinfo)
        end = run_at
    elif period == "last-week":
        week_start = week_start_offset(config.get("week_start", "monday"))
        delta_days = (today.weekday() - week_start) % 7
        this_week_start = today - timedelta(days=delta_days)
        start_date = this_week_start - timedelta(days=7)
        end_date = this_week_start - timedelta(days=1)
        start = datetime.combine(start_date, time.min, tzinfo=run_at.tzinfo)
        end = datetime.combine(end_date, time.max.replace(microsecond=0), tzinfo=run_at.tzinfo)
    else:  # this-month
        start = datetime.combine(today.replace(day=1), time.min, tzinfo=run_at.tzinfo)
        end = run_at
    return {"period": period, "date_start": start.isoformat(), "date_end": end.isoformat()}


def week_start_offset(value: str) -> int:
    value = str(value).strip().lower()
    if value in ("monday", "mon", "1"):
        return 0
    if value in ("sunday", "sun", "0", "7"):
        return 6
    raise UserFacingError(f"Unsupported week_start: {value}")


def parse_ymd(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise UserFacingError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc


def build_snapshot(vault: Path, config: dict[str, Any], run_at: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    files: dict[str, Any] = {}
    skipped = {"ignored_dirs": 0, "ignored_tags": 0, "non_markdown": 0, "read_errors": []}
    for path in sorted(vault.rglob("*")):
        if not path.is_file():
            continue
        rel = rel_to_vault(path, vault)
        if is_ignored_path(rel, config):
            skipped["ignored_dirs"] += 1
            continue
        if path.suffix.lower() != ".md":
            skipped["non_markdown"] += 1
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            skipped["read_errors"].append({"file": rel, "error": str(exc)})
            continue
        if has_ignored_tag(text, config):
            skipped["ignored_tags"] += 1
            continue
        stat = path.stat()
        blocks = parse_markdown_blocks(rel, text)
        for block in blocks:
            block["candidate_activity"] = classify_activity(block)
        files[rel] = {
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=run_at.tzinfo).isoformat(),
            "size": stat.st_size,
            "content_hash": sha256_text(text),
            "blocks": blocks,
        }
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": run_at.isoformat(),
            "vault_path": str(vault),
            "files": files,
        },
        skipped,
    )


def build_vault_profile_summary(
    snapshot: dict[str, Any],
    skipped: dict[str, Any],
    config: dict[str, Any],
    run_at: datetime,
) -> dict[str, Any]:
    folder_summaries: dict[str, dict[str, Any]] = {}
    activity_counts: dict[str, int] = {}
    total_blocks = 0
    for rel, file_info in snapshot.get("files", {}).items():
        folder_paths = folder_paths_for_file(rel)
        mtime = file_info.get("mtime", "")
        for folder_path in folder_paths:
            item = folder_summaries.setdefault(
                folder_path,
                {
                    "path": folder_path,
                    "depth": 0 if folder_path == "(vault root)" else folder_path.count("/") + 1,
                    "file_count": 0,
                    "block_count": 0,
                    "activity_counts": {},
                    "sample_files": [],
                    "sample_headings": [],
                    "sample_points": [],
                    "latest_mtime": "",
                },
            )
            item["file_count"] += 1
            append_limited(item["sample_files"], rel, 8)
            if mtime and mtime > item["latest_mtime"]:
                item["latest_mtime"] = mtime
        for block in file_info.get("blocks", []):
            total_blocks += 1
            activity = block.get("candidate_activity", "unknown")
            increment(activity_counts, activity)
            for folder_path in folder_paths:
                item = folder_summaries[folder_path]
                item["block_count"] += 1
                increment(item["activity_counts"], activity)
                if block.get("type") == "heading":
                    append_limited(
                        item["sample_headings"],
                        {
                            "heading_path": block.get("heading_path", []),
                            "source_link": block.get("source_link"),
                        },
                        8,
                    )
                elif not is_noise_block(block):
                    append_ranked_point(
                        item["sample_points"],
                        {
                            "type": block.get("type"),
                            "activity": activity,
                            "source_link": block.get("source_link"),
                            "text": truncate_text(clean_digest_text(block.get("text", "")), 180),
                        },
                        block_signal_score(block),
                        5,
                    )

    folders = []
    for item in folder_summaries.values():
        item["activity_counts"] = dict(sorted(item["activity_counts"].items()))
        item["sample_points"] = [point for _score, point in item["sample_points"]]
        item["role_candidate"] = infer_folder_role_candidate(item)
        item["confidence"] = "low_candidate"
        folders.append(item)
    folders.sort(key=lambda x: (x.get("path", "").count("/"), x.get("path", "")))

    content_type_candidates = [
        {"type": key, "count": value, "confidence": "low_candidate"}
        for key, value in sorted(activity_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if key != "unknown"
    ]
    active_topic_candidates = [
        {
            "topic": item["role_candidate"],
            "folder": item["path"],
            "evidence_sources": [p.get("source_link") for p in item.get("sample_points", []) if p.get("source_link")][:5],
            "confidence": "low_candidate",
        }
        for item in folders[:12]
        if item.get("sample_points")
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": run_at.isoformat(),
        "vault_path": snapshot.get("vault_path"),
        "overview": {
            "markdown_files": len(snapshot.get("files", {})),
            "blocks": total_blocks,
            "folders": len(folders),
            "skipped": skipped,
            "profile_draft_dir": config.get("profile_draft_dir"),
        },
        "folder_summaries": folders,
        "content_type_candidates": content_type_candidates,
        "active_topic_candidates": active_topic_candidates,
    }


def folder_paths_for_file(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    if not parts:
        return ["(vault root)"]
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def infer_folder_role_candidate(folder_summary: dict[str, Any]) -> str:
    path = str(folder_summary.get("path", ""))
    activities = folder_summary.get("activity_counts", {})
    lowered = path.lower()
    normalized = path.replace("\\", "/")
    path_roles = {
        "AI-Agent": "Agent/RAG 相关学习、论文笔记和面试准备的综合区",
        "AI-Agent/Paper": "Agent 方向论文阅读笔记",
        "AI-Agent/Paper/Agent": "Agent 机制、工具调用、记忆等方向的论文笔记",
        "AI-Agent/Paper/Multi-Agent": "Multi-Agent 方向论文阅读笔记",
        "AI-Agent/interview": "Agent/RAG 面试准备、面试记录和复盘",
        "AI-Agent/knowledge": "Agent 工程知识、框架资料和学习整理",
        "AI-Agent/knowledge/Codex": "Codex/Agent 工具使用、源码理解和工作流整理",
        "Clippings": "网页剪藏和待读资料，通常不等同于已经完成的学习成果",
        "LLM": "LLM 基础知识和论文阅读笔记",
        "LLM/Paper": "LLM 论文阅读笔记",
        "MISGL": "MISGL/图神经网络相关实验、结果和方法记录",
        "RAG": "RAG 项目、论文和面试表达整理",
        "RAG/Paper": "RAG 论文阅读笔记",
    }
    if normalized in path_roles:
        return path_roles[normalized]
    if "interview" in lowered or "面试" in path:
        return "面试准备、面试记录和复盘"
    if "clippings" in lowered or "剪藏" in path:
        return "网页剪藏和待读资料"
    if "paper" in lowered or "论文" in path:
        return "论文阅读笔记"
    if "project" in lowered or "项目" in path:
        return "项目方案、设计思路和推进记录"
    if activities.get("paper_reading", 0) >= 3:
        return "论文或资料阅读笔记"
    if activities.get("project_planning", 0) >= 3:
        return "项目方案、设计思路和推进记录"
    if activities.get("experiment_log", 0) >= 3:
        return "实验过程、结果和分析记录"
    if activities.get("interview_review", 0) >= 3:
        return "面试准备和复盘记录"
    if activities.get("daily_log", 0) >= 3:
        return "日记或周期性记录"
    return "用途不明确，需要用户补充说明"


def render_vault_profile_draft(summary: dict[str, Any]) -> str:
    folders = summary.get("folder_summaries", [])
    mainlines = infer_active_mainlines_for_draft(folders)
    lines = [
        "# Obsidian 知识库初始化理解草案",
        "",
        "请只校准下表中的“作用”。如果模型判断不对，直接改成你的真实理解即可。",
        "",
        "| 文件夹 | 作用（模型初步判断，可直接修改） |",
        "|---|---|",
    ]
    for item in folders:
        lines.append(f"| `{item.get('path')}` | {item.get('role_candidate')} |")
    lines += [
        "",
        "## 当前进行中的主线（模型初步判断，可直接修改）",
        "",
    ]
    for item in mainlines:
        lines.append(f"- **{item['name']}**：{item['description']}")
    return "\n".join(lines)


def infer_active_mainlines_for_draft(folders: list[dict[str, Any]]) -> list[dict[str, str]]:
    paths = {str(item.get("path", "")) for item in folders}
    mainlines: list[dict[str, str]] = []
    if any(path.startswith("AI-Agent") for path in paths):
        mainlines.append(
            {
                "name": "Agent/RAG 学习与面试准备",
                "description": "知识库中有较多 Agent 论文、框架资料、面试记录和复盘内容，说明这一方向可能是近期重点。",
            }
        )
    if any(path.startswith("RAG") for path in paths):
        mainlines.append(
            {
                "name": "RAG 项目与论文问答系统整理",
                "description": "RAG 目录同时包含论文笔记和项目表达材料，可能服务于项目复盘、面试表达和后续改进。",
            }
        )
    if any(path.startswith("MISGL") for path in paths):
        mainlines.append(
            {
                "name": "MISGL/图神经网络实验整理",
                "description": "MISGL 目录主要是实验结果、方法分析和图学习相关记录，可能是科研实验线索。",
            }
        )
    if any(path.startswith("LLM") for path in paths):
        mainlines.append(
            {
                "name": "LLM 基础与论文阅读",
                "description": "LLM 目录更像基础知识和论文阅读积累，可作为 Agent/RAG 学习的底层支撑。",
            }
        )
    return mainlines[:4] or [
        {
            "name": "主线待确认",
            "description": "当前只能看到文件夹结构，无法可靠判断正在推进的主线，请在这里改成你的真实主线。",
        }
    ]


def build_confirmed_profile(
    draft_text: str,
    summary: dict[str, Any],
    config: dict[str, Any],
    vault: Path,
    draft_path: Path,
    run_at: datetime,
) -> dict[str, Any]:
    calibration = extract_user_calibration(draft_text)
    return {
        "schema_version": SCHEMA_VERSION,
        "confirmed_at": run_at.isoformat(),
        "vault_path": str(vault),
        "confirmation_source": rel_to_vault(draft_path, vault),
        "profile_draft_dir": config.get("profile_draft_dir"),
        "user_calibration_priority": "highest",
        "folder_roles": calibration_entries(
            calibration.get("folder_roles", ""),
            summary.get("folder_summaries", []),
            "role_candidate",
        ),
        "content_types": calibration_entries(
            calibration.get("content_types", ""),
            summary.get("content_type_candidates", []),
            "type",
        ),
        "active_mainlines": calibration_entries(
            calibration.get("active_mainlines", ""),
            summary.get("active_topic_candidates", []),
            "topic",
        ),
        "archive_or_ignore": calibration_entries(calibration.get("archive_or_ignore", ""), [], "text"),
        "review_preferences": calibration_entries(calibration.get("review_preferences", ""), [], "text"),
        "user_calibration": calibration,
        "draft_scan_summary": {
            "generated_at": summary.get("generated_at"),
            "confirmed_scan_at": run_at.isoformat(),
            "overview": summary.get("overview", {}),
        },
        "agent_candidate_context": {
            "folder_summaries": summary.get("folder_summaries", [])[:40],
            "content_type_candidates": summary.get("content_type_candidates", []),
            "active_topic_candidates": summary.get("active_topic_candidates", []),
        },
    }


def extract_user_calibration(draft_text: str) -> dict[str, str]:
    calibration = {
        key: extract_markdown_subsection(draft_text, key)
        for key in ("folder_roles", "content_types", "active_mainlines", "archive_or_ignore", "review_preferences")
    }
    if not calibration.get("folder_roles"):
        calibration["folder_roles"] = extract_folder_role_table(draft_text)
    if not calibration.get("active_mainlines"):
        calibration["active_mainlines"] = extract_heading_section(
            draft_text,
            "当前进行中的主线",
        )
    return calibration


def extract_heading_section(text: str, heading_prefix: str) -> str:
    pattern = re.compile(rf"(?im)^##\s+{re.escape(heading_prefix)}.*$")
    match = pattern.search(text or "")
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"(?m)^##\s+", text[start:])
    end = start + next_match.start() if next_match else len(text)
    return strip_html_comments(text[start:end]).strip()


def extract_folder_role_table(text: str) -> str:
    rows = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "文件夹" in stripped or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        folder = cells[0].strip("` ")
        role = cells[1].strip()
        if folder and role:
            rows.append(f"{folder} = {role}")
    return "\n".join(rows)


def extract_markdown_subsection(text: str, heading: str) -> str:
    pattern = re.compile(rf"(?im)^###\s+{re.escape(heading)}\s*$")
    match = pattern.search(text or "")
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"(?m)^###\s+|\n##\s+", text[start:])
    end = start + next_match.start() if next_match else len(text)
    return strip_html_comments(text[start:end]).strip()


def strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text or "", flags=re.DOTALL)


def calibration_entries(user_text: str, fallback: list[dict[str, Any]], fallback_label: str) -> list[dict[str, Any]]:
    lines = [
        line.strip().lstrip("-*").strip()
        for line in (user_text or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if lines:
        return [{"source": "user_calibration", "text": line} for line in lines]
    entries = []
    for item in fallback:
        entry = dict(item)
        entry["source"] = "agent_draft_unedited"
        if fallback_label in item:
            entry["text"] = item.get(fallback_label)
        entries.append(entry)
    return entries


def is_ignored_path(rel: str, config: dict[str, Any]) -> bool:
    parts = Path(rel).parts
    ignore_dirs = {normalize_path_part(item) for item in config.get("ignore_dirs", [])}
    return any(normalize_path_part(part) in ignore_dirs for part in parts)


def normalize_path_part(value: str) -> str:
    return str(value).replace("\\", "/").strip("/").lower()


def has_ignored_tag(text: str, config: dict[str, Any]) -> bool:
    for tag in config.get("ignore_tags", []):
        pattern = TAG_BOUNDARY_TEMPLATE.format(re.escape(str(tag)))
        if re.search(pattern, text):
            return True
    return False


def parse_markdown_blocks(rel: str, text: str) -> list[dict[str, Any]]:
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    headings: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        heading_match = HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            title = clean_heading(heading_match.group(2))
            headings = headings[: level - 1] + [title]
            blocks.append(make_block("heading", rel, headings, i + 1, i + 1, line))
            i += 1
            continue

        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            fence = line.lstrip()[:3]
            start = i
            i += 1
            while i < len(lines):
                if lines[i].lstrip().startswith(fence):
                    i += 1
                    break
                i += 1
            block_text = "\n".join(lines[start:i])
            blocks.append(make_block("code", rel, headings, start + 1, i, block_text))
            continue

        todo_match = TODO_RE.match(line)
        if todo_match:
            blocks.append(make_block("todo", rel, headings, i + 1, i + 1, line))
            i += 1
            continue

        list_match = LIST_RE.match(line)
        if list_match:
            blocks.append(make_block("list", rel, headings, i + 1, i + 1, line))
            i += 1
            continue

        start = i
        paragraph_lines = [line]
        i += 1
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip():
                break
            if HEADING_RE.match(next_line) or TODO_RE.match(next_line) or LIST_RE.match(next_line):
                break
            if next_line.lstrip().startswith("```") or next_line.lstrip().startswith("~~~"):
                break
            paragraph_lines.append(next_line)
            i += 1
        block_text = "\n".join(paragraph_lines)
        blocks.append(make_block("paragraph", rel, headings, start + 1, start + len(paragraph_lines), block_text))
    return blocks


def make_block(block_type: str, rel: str, headings: list[str], start_line: int, end_line: int, text: str) -> dict[str, Any]:
    normalized = normalize_text(text)
    text_hash = sha256_text(normalized)
    obsidian_block_id = extract_obsidian_block_id(text)
    heading_path = list(headings)
    block_id = (
        f"obsidian:{rel}:^{obsidian_block_id}"
        if obsidian_block_id
        else f"{rel}|{' > '.join(heading_path)}|{block_type}|{text_hash}"
    )
    parent_dirs = rel.split("/")[:-1]
    return {
        "block_id": block_id,
        "type": block_type,
        "file": rel,
        "top_dir": parent_dirs[0] if parent_dirs else "",
        "parent_dirs": parent_dirs,
        "heading_path": heading_path,
        "start_line": start_line,
        "end_line": end_line,
        "text": text.strip(),
        "text_hash": text_hash,
        "source_link": make_source_link(rel, heading_path),
        "structural_key": structural_key(rel, heading_path, block_type),
    }


def top_dir_of(rel: str) -> str:
    parts = rel.split("/")
    return parts[0] if len(parts) > 1 else ""


def clean_heading(title: str) -> str:
    return title.strip().rstrip("#").strip()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def extract_obsidian_block_id(text: str) -> str | None:
    match = BLOCK_ID_RE.search(text.strip())
    return match.group(1) if match else None


def structural_key(rel: str, headings: list[str], block_type: str) -> str:
    return f"{rel}|{' > '.join(headings)}|{block_type}"


def make_source_link(rel: str, headings: list[str]) -> str:
    note = rel[:-3] if rel.lower().endswith(".md") else rel
    if headings:
        return f"[[{note}#{headings[-1]}]]"
    return f"[[{note}]]"


def classify_activity(block: dict[str, Any]) -> str:
    haystack = " ".join(
        [
            block.get("file", ""),
            " ".join(block.get("heading_path", [])),
            block.get("text", ""),
        ]
    ).lower()
    if block.get("type") == "todo" or any(word in haystack for word in ("todo", "待办", "未完成", "任务")):
        return "todo"
    if any(word in haystack for word in ("meeting", "会议", "纪要", "周会", "讨论")):
        return "meeting_note"
    if any(word in haystack for word in ("experiment", "实验", "ablation", "result", "结果", "指标")):
        return "experiment_log"
    if any(word in haystack for word in ("interview", "面试", "八股", "复盘题")):
        return "interview_review"
    if any(word in haystack for word in ("paper", "论文", "arxiv", "agent paper")):
        return "paper_reading"
    if any(word in haystack for word in ("book", "读书", "阅读", "摘录")):
        return "reading_note"
    if any(word in haystack for word in ("daily", "diary", "journal", "日记", "日报")):
        return "daily_log"
    if any(word in haystack for word in ("project", "项目", "plan", "规划", "方案", "roadmap", "设计")):
        return "project_planning"
    return "unknown"


def first_baseline_blocks(snapshot: dict[str, Any], period_info: dict[str, str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for rel, file_info in snapshot.get("files", {}).items():
        if not file_in_period(file_info, period_info):
            continue
        for block in file_info.get("blocks", []):
            changed = dict(block)
            changed["status"] = "added"
            changed["run_note"] = "first_baseline: file modified in period; not proof that this block was newly created"
            blocks.append(strip_internal_fields(changed))
    return blocks


def diff_snapshots(previous: dict[str, Any], current: dict[str, Any], period_info: dict[str, str]) -> list[dict[str, Any]]:
    prev_files = previous.get("files", {})
    current_files = current.get("files", {})
    changed: list[dict[str, Any]] = []
    matched_prev_ids: set[str] = set()

    changed_files = {
        rel
        for rel, file_info in current_files.items()
        if file_in_period(file_info, period_info)
        and file_info.get("content_hash") != prev_files.get(rel, {}).get("content_hash")
    }

    prev_blocks_by_id, prev_blocks_by_struct = index_blocks(prev_files)
    current_ids = {
        block.get("block_id")
        for file_info in current_files.values()
        for block in file_info.get("blocks", [])
        if block.get("block_id")
    }

    for rel, file_info in current_files.items():
        for block in file_info.get("blocks", []):
            block_id = block.get("block_id")
            prev_block = prev_blocks_by_id.get(block_id)
            file_changed = rel in changed_files

            if prev_block:
                matched_prev_ids.add(block_id)
                if file_changed and is_open_todo(block) and is_open_todo(prev_block):
                    item = dict(block)
                    item["status"] = "continued"
                    item["previous_text"] = prev_block.get("text", "")
                    changed.append(strip_internal_fields(item))
                elif file_changed and block.get("text_hash") != prev_block.get("text_hash"):
                    item = dict(block)
                    item["status"] = "modified"
                    item["previous_text"] = prev_block.get("text", "")
                    changed.append(strip_internal_fields(item))
                continue

            if not file_changed:
                continue

            similar_prev = find_similar_previous(block, prev_blocks_by_struct, matched_prev_ids)
            item = dict(block)
            if similar_prev:
                item["status"] = "modified"
                item["previous_text"] = similar_prev.get("text", "")
                matched_prev_ids.add(similar_prev.get("block_id", ""))
            else:
                item["status"] = "added"
            changed.append(strip_internal_fields(item))

    for rel, file_info in prev_files.items():
        rel_missing = rel not in current_files
        rel_changed = rel in changed_files
        if not rel_missing and not rel_changed:
            continue
        for block in file_info.get("blocks", []):
            block_id = block.get("block_id")
            if block_id in current_ids or block_id in matched_prev_ids:
                continue
            item = dict(block)
            item["status"] = "deleted"
            item["source_link"] = make_source_link(item.get("file", rel), item.get("heading_path", []))
            changed.append(strip_internal_fields(item))

    return changed


def diff_file_summaries(
    previous: dict[str, Any],
    current: dict[str, Any],
    period_info: dict[str, str],
    vault_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    prev_files = previous.get("files", {}) if isinstance(previous, dict) else {}
    current_files = current.get("files", {}) if isinstance(current, dict) else {}
    summaries: list[dict[str, Any]] = []
    for rel, file_info in current_files.items():
        if not file_in_period(file_info, period_info):
            continue
        prev_info = prev_files.get(rel)
        if not prev_info:
            file_status = "new"
        elif file_info.get("content_hash") != prev_info.get("content_hash"):
            file_status = "modified"
        else:
            continue
        summaries.append(build_changed_file_summary(rel, file_info, file_status, vault_profile))
    summaries.sort(key=lambda item: (-item.get("signal_score", 0), item.get("file", "")))
    return summaries


def period_file_summaries(
    snapshot: dict[str, Any],
    period_info: dict[str, str],
    vault_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for rel, file_info in (snapshot.get("files", {}) if isinstance(snapshot, dict) else {}).items():
        if file_in_period(file_info, period_info):
            summaries.append(build_changed_file_summary(rel, file_info, "modified_in_period", vault_profile))
    summaries.sort(key=lambda item: (-item.get("signal_score", 0), item.get("file", "")))
    return summaries


def build_changed_file_summary(
    rel: str,
    file_info: dict[str, Any],
    file_status: str,
    vault_profile: dict[str, Any],
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "file": rel,
        "file_status": file_status,
        "mtime": file_info.get("mtime"),
        "size": file_info.get("size"),
        "source_link": make_source_link(rel, []),
        "top_dir": top_dir_of(rel),
        "parent_dirs": rel.split("/")[:-1],
        "activities": {},
        "heading_paths": [],
        "representative_points": [],
        "todos": [],
        "blockers": [],
        "completed_or_outputs": [],
        "source_links": [],
        "_score": 0,
    }
    for block in file_info.get("blocks", []):
        activity = block.get("candidate_activity", "unknown")
        increment(item["activities"], activity)
        heading_path = block.get("heading_path") or []
        if heading_path and heading_path not in item["heading_paths"] and len(item["heading_paths"]) < 12:
            item["heading_paths"].append(heading_path)
        source_link = block.get("source_link")
        if source_link and source_link not in item["source_links"] and len(item["source_links"]) < 16:
            item["source_links"].append(source_link)
        text = clean_digest_text(block.get("text", ""))
        if not text or is_noise_block(block):
            continue
        score = block_signal_score(block)
        item["_score"] += score
        point = {
            "type": block.get("type"),
            "activity": activity,
            "heading_path": heading_path,
            "source_link": source_link,
            "text": truncate_text(text, 260),
        }
        if is_todo_text(text):
            append_limited(item["todos"], point, 10)
        if is_blocker_text(text):
            append_limited(item["blockers"], point, 10)
        if is_completed_or_output_text(text, block):
            append_limited(item["completed_or_outputs"], point, 10)
        append_ranked_point(item["representative_points"], point, score, 8)
    item["activities"] = dict(sorted(item["activities"].items()))
    item["representative_points"] = [point for _score, point in item["representative_points"]]
    profile_hint = confirmed_profile_hint(item, vault_profile)
    if profile_hint:
        item["topic_hint"] = profile_hint["topic"]
        item["topic_confidence"] = "confirmed_profile"
        item["profile_hint"] = profile_hint
    else:
        item["topic_hint"] = infer_topic_hint(item)
        item["topic_confidence"] = "low_candidate"
    item["signal_score"] = item.pop("_score", 0)
    return item


def index_blocks(files: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_struct: dict[str, list[dict[str, Any]]] = {}
    for file_info in files.values():
        for block in file_info.get("blocks", []):
            block_id = block.get("block_id")
            if block_id:
                by_id[block_id] = block
            by_struct.setdefault(block.get("structural_key", ""), []).append(block)
    return by_id, by_struct


def find_similar_previous(
    block: dict[str, Any],
    prev_blocks_by_struct: dict[str, list[dict[str, Any]]],
    matched_prev_ids: set[str],
) -> dict[str, Any] | None:
    candidates = [
        candidate
        for candidate in prev_blocks_by_struct.get(block.get("structural_key", ""), [])
        if candidate.get("block_id", "") not in matched_prev_ids
    ]
    min_score = 0.45 if len(candidates) == 1 else 0.72
    best_score = 0.0
    best: dict[str, Any] | None = None
    current_text = normalize_text(block.get("text", ""))
    for candidate in candidates:
        score = difflib.SequenceMatcher(None, current_text, normalize_text(candidate.get("text", ""))).ratio()
        if score > best_score:
            best_score = score
            best = candidate
    return best if best is not None and best_score >= min_score else None


def is_open_todo(block: dict[str, Any]) -> bool:
    text = block.get("text", "")
    match = TODO_RE.match(text)
    return bool(match and match.group(1) == " ")


def file_in_period(file_info: dict[str, Any], period_info: dict[str, str]) -> bool:
    try:
        mtime = datetime.fromisoformat(file_info.get("mtime", ""))
        start = datetime.fromisoformat(period_info["date_start"])
        end = datetime.fromisoformat(period_info["date_end"])
    except Exception:
        return False
    return start <= mtime <= end


def strip_internal_fields(block: dict[str, Any]) -> dict[str, Any]:
    result = dict(block)
    result.pop("structural_key", None)
    return result


def allocate_report_path(vault: Path, output_dir: Path, period: str, run_at: datetime) -> Path:
    date_part = run_at.date().isoformat()
    stem = f"{date_part}_{period}_周期复盘"
    candidate = output_dir / f"{stem}.md"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}_{index}.md"
        index += 1
    ensure_inside_vault(candidate, vault, "report output")
    return candidate


def merge_review_state(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in ("open_items", "blockers", "active_topics"):
        if key in update:
            merged[key] = unique_list(update.get(key, []))
        else:
            merged.setdefault(key, [])
    for key, value in update.items():
        if key not in {"open_items", "blockers", "active_topics", "latest_report", "last_run"}:
            merged[key] = value
    return merged


def unique_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[Any] = []
    for item in value:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key, "unknown"))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def attach_changed_block_stats(file_summaries: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> None:
    by_file: dict[str, dict[str, dict[str, int]]] = {}
    for block in blocks:
        rel = block.get("file", "")
        stats = by_file.setdefault(rel, {"changed_block_statuses": {}, "changed_block_activities": {}})
        increment(stats["changed_block_statuses"], block.get("status", "unknown"))
        increment(stats["changed_block_activities"], block.get("candidate_activity", "unknown"))
    for item in file_summaries:
        stats = by_file.get(item.get("file", ""), {})
        item["changed_block_statuses"] = dict(sorted(stats.get("changed_block_statuses", {}).items()))
        item["changed_block_activities"] = dict(sorted(stats.get("changed_block_activities", {}).items()))
        item.setdefault("topic_hint", infer_topic_hint(item))
        item.setdefault("topic_confidence", "low_candidate")
        item.setdefault("source_links", [])
        item.setdefault("representative_points", [])
        item.setdefault("todos", [])
        item.setdefault("blockers", [])
        item.setdefault("completed_or_outputs", [])


def file_summaries_from_changed_blocks(
    blocks: list[dict[str, Any]],
    vault_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for block in blocks:
        rel = block.get("file", "")
        item = files.setdefault(
            rel,
            {
                "file": rel,
                "source_link": make_source_link(rel, []),
                "top_dir": block.get("top_dir", ""),
                "parent_dirs": block.get("parent_dirs", []),
                "file_status": "changed",
                "changed_block_statuses": {},
                "activities": {},
                "heading_paths": [],
                "representative_points": [],
                "todos": [],
                "blockers": [],
                "completed_or_outputs": [],
                "source_links": [],
                "_score": 0,
            },
        )
        increment(item["changed_block_statuses"], block.get("status", "unknown"))
        increment(item["activities"], block.get("candidate_activity", "unknown"))
        heading_path = block.get("heading_path") or []
        if heading_path and heading_path not in item["heading_paths"] and len(item["heading_paths"]) < 8:
            item["heading_paths"].append(heading_path)
        source_link = block.get("source_link")
        if source_link and source_link not in item["source_links"] and len(item["source_links"]) < 12:
            item["source_links"].append(source_link)

        text = clean_digest_text(block.get("text", ""))
        if not text:
            continue
        score = block_signal_score(block)
        item["_score"] += score
        point = {
            "status": block.get("status"),
            "type": block.get("type"),
            "heading_path": heading_path,
            "source_link": source_link,
            "text": truncate_text(text, 260),
        }
        if is_todo_text(text):
            append_limited(item["todos"], point, 8)
        if is_blocker_text(text):
            append_limited(item["blockers"], point, 8)
        if is_completed_or_output_text(text, block):
            append_limited(item["completed_or_outputs"], point, 8)
        append_ranked_point(item["representative_points"], point, score, 6)

    file_summaries = []
    for item in files.values():
        item["changed_block_statuses"] = dict(sorted(item["changed_block_statuses"].items()))
        item["activities"] = dict(sorted(item["activities"].items()))
        profile_hint = confirmed_profile_hint(item, vault_profile)
        if profile_hint:
            item["topic_hint"] = profile_hint["topic"]
            item["topic_confidence"] = "confirmed_profile"
            item["profile_hint"] = profile_hint
        else:
            item["topic_hint"] = infer_topic_hint(item)
            item["topic_confidence"] = "low_candidate"
        item["representative_points"] = [p for _score, p in item["representative_points"]]
        score = item.pop("_score", 0)
        item["signal_score"] = score
        file_summaries.append(item)
    file_summaries.sort(key=lambda x: (-x.get("signal_score", 0), x.get("file", "")))
    return file_summaries


def build_review_digest(changed_payload: dict[str, Any]) -> dict[str, Any]:
    meta = changed_payload.get("meta", {})
    vault_profile = changed_payload.get("vault_profile") or {}
    blocks = [b for b in changed_payload.get("blocks", []) if not is_noise_block(b)]
    changed_files = changed_payload.get("changed_files") or []
    if changed_files:
        file_summaries = [dict(item) for item in changed_files if isinstance(item, dict)]
        attach_changed_block_stats(file_summaries, blocks)
    else:
        file_summaries = file_summaries_from_changed_blocks(blocks, vault_profile)

    topic_summaries: dict[str, dict[str, Any]] = {}
    for item in file_summaries:
        topic = item.get("topic_hint") or "其他"
        summary = topic_summaries.setdefault(
            topic,
            {
                "topic": topic,
                "files": [],
                "file_status_counts": {},
                "activity_counts": {},
                "changed_block_status_counts": {},
                "key_points": [],
                "todos": [],
                "blockers": [],
                "sources": [],
            },
        )
        append_limited(summary["files"], item["file"], 12)
        file_status = item.get("file_status") or "changed"
        summary["file_status_counts"][file_status] = summary["file_status_counts"].get(file_status, 0) + 1
        for key, value in item.get("activities", {}).items():
            summary["activity_counts"][key] = summary["activity_counts"].get(key, 0) + value
        for key, value in item.get("changed_block_statuses", {}).items():
            summary["changed_block_status_counts"][key] = summary["changed_block_status_counts"].get(key, 0) + value
        for point in item.get("representative_points", [])[:2]:
            append_limited(summary["key_points"], point, 10)
        for point in item.get("todos", [])[:3]:
            append_limited(summary["todos"], point, 10)
        for point in item.get("blockers", [])[:3]:
            append_limited(summary["blockers"], point, 10)
        for source in item.get("source_links", [])[:4]:
            append_limited(summary["sources"], source, 20)

    digest_meta = dict(meta)
    digest_meta["digest_note"] = (
        "This digest is the preferred LLM input. changed/new files are the primary evidence; changed_blocks.latest.json is detail evidence for lookup only."
    )
    digest_meta["file_summary_count"] = len(file_summaries)
    digest_meta["topic_summary_count"] = len(topic_summaries)
    digest_meta["review_digest_file"] = ".obsidian-review-agent/review_digest.latest.json"

    return {
        "meta": digest_meta,
        "executive_input": {
            "run_mode": meta.get("run_mode"),
            "period": meta.get("period"),
            "date_start": meta.get("date_start"),
            "date_end": meta.get("date_end"),
            "changed_files_count": meta.get("changed_files_count"),
            "changed_file_status_counts": meta.get("changed_file_status_counts"),
            "changed_blocks_count": meta.get("changed_blocks_count"),
            "first_baseline_warning": (
                "首次基线复盘只表示这些文件在本周期修改过；不要声称所有内容都是本周期新完成。"
                if meta.get("run_mode") == "first_baseline"
                else ""
            ),
        },
        "topic_summaries": sorted(topic_summaries.values(), key=lambda x: (-len(x.get("key_points", [])), x["topic"])),
        "vault_profile_context": compact_vault_profile(vault_profile),
        "profile_update_policy": (
            "Folder roles, long-term goals, and active mainlines from the confirmed profile must not be overwritten. "
            "Write only suggestions to vault_profile_update.latest.json when new evidence appears."
        ),
        "file_summaries": file_summaries[:80],
        "open_items": collect_points(file_summaries, "todos", 30),
        "blockers": collect_points(file_summaries, "blockers", 30),
        "completed_or_outputs": collect_points(file_summaries, "completed_or_outputs", 30),
        "source_index": build_source_index(file_summaries, 120),
        "report_outline": report_outline(),
        "writing_guidelines": digest_writing_guidelines(meta.get("run_mode", "")),
    }


def confirmed_profile_hint(file_summary: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(profile, dict):
        return None
    rel = str(file_summary.get("file", "")).replace("\\", "/")
    best: dict[str, Any] | None = None
    best_len = -1
    for entry in profile.get("folder_roles", []) or []:
        path = extract_profile_entry_path(entry)
        if not path:
            continue
        norm = normalize_path_part(path)
        if not norm:
            continue
        rel_norm = normalize_path_part(rel)
        if rel_norm == norm or rel_norm.startswith(norm + "/"):
            if len(norm) > best_len:
                best_len = len(norm)
                topic = extract_profile_entry_label(entry) or path
                best = {
                    "topic": topic,
                    "matched_folder": path,
                    "source": entry.get("source", "confirmed_profile") if isinstance(entry, dict) else "confirmed_profile",
                }
    return best


def extract_profile_entry_path(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in ("path", "folder", "file", "top_dir"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().strip("`")
        text = str(entry.get("text", "")).strip()
    else:
        text = str(entry or "").strip()
    match = re.search(r"`([^`]+)`", text)
    if match:
        return match.group(1).strip()
    match = re.match(r"([^=:：]+)\s*(?:=|:|：)", text)
    if match:
        return match.group(1).strip().strip("`")
    return ""


def extract_profile_entry_label(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in ("role", "role_candidate", "topic", "text", "type"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(entry or "").strip()


def compact_vault_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, dict) or not profile:
        return {}
    return {
        "confirmed_at": profile.get("confirmed_at"),
        "confirmation_source": profile.get("confirmation_source"),
        "user_calibration_priority": profile.get("user_calibration_priority"),
        "folder_roles": profile.get("folder_roles", [])[:30],
        "content_types": profile.get("content_types", [])[:30],
        "active_mainlines": profile.get("active_mainlines", [])[:30],
        "archive_or_ignore": profile.get("archive_or_ignore", [])[:30],
        "review_preferences": profile.get("review_preferences", [])[:30],
    }


def build_profile_update_suggestions(
    digest_payload: dict[str, Any],
    confirmed_profile: dict[str, Any],
    run_at: datetime,
) -> dict[str, Any]:
    suggestions = []
    confirmed_topics = {
        str(entry.get("topic") or entry.get("text") or entry.get("role") or entry.get("role_candidate", "")).strip()
        for entry in (confirmed_profile or {}).get("active_mainlines", []) or []
        if isinstance(entry, dict)
    }
    for topic in digest_payload.get("topic_summaries", []) or []:
        name = str(topic.get("topic", "")).strip()
        if name and name not in confirmed_topics:
            suggestions.append(
                {
                    "kind": "new_topic_candidate",
                    "topic": name,
                    "confidence": "low_candidate",
                    "sources": topic.get("sources", [])[:8],
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": run_at.isoformat(),
        "status": "suggested_only_not_merged",
        "policy": "Do not automatically merge folder roles, long-term goals, or active mainlines. User confirmation is required.",
        "suggestions": suggestions[:20],
    }


def increment(target: dict[str, int], key: Any) -> None:
    key = str(key or "unknown")
    target[key] = target.get(key, 0) + 1


def append_limited(items: list[Any], value: Any, limit: int) -> None:
    if value in items:
        return
    if len(items) < limit:
        items.append(value)


def append_ranked_point(items: list[tuple[int, dict[str, Any]]], point: dict[str, Any], score: int, limit: int) -> None:
    if any(existing.get("text") == point.get("text") for _score, existing in items):
        return
    items.append((score, point))
    items.sort(key=lambda x: -x[0])
    del items[limit:]


def is_noise_block(block: dict[str, Any]) -> bool:
    text = clean_digest_text(block.get("text", ""))
    if not text:
        return True
    if text in {"---", "..."}:
        return True
    if block.get("type") == "heading":
        return True
    if len(text) < 8 and not is_todo_text(text):
        return True
    if text.startswith("---\n") and "title:" in text[:120]:
        return True
    return False


def clean_digest_text(text: str) -> str:
    text = (text or "").replace("\ufeff", "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def truncate_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def block_signal_score(block: dict[str, Any]) -> int:
    text = block.get("text", "")
    score = 1
    if block.get("status") in {"added", "modified", "continued"}:
        score += 2
    if block.get("type") == "todo":
        score += 4
    if is_blocker_text(text):
        score += 4
    if is_completed_or_output_text(text, block):
        score += 3
    if block.get("candidate_activity") in {"paper_reading", "project_planning", "experiment_log", "interview_review"}:
        score += 2
    if len(text) > 80:
        score += 1
    return score


def is_todo_text(text: str) -> bool:
    return bool(TODO_RE.match(text or "")) or any(k in (text or "").lower() for k in ("todo", "待办", "未完成", "下一步"))


def is_blocker_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(k in lowered for k in ("blocked", "blocker", "阻塞", "卡住", "问题", "风险", "疑问", "todo?", "待解决"))


def is_completed_or_output_text(text: str, block: dict[str, Any]) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"\[[xX]\]", text or "")) or any(
        k in lowered
        for k in ("完成", "已完成", "done", "fixed", "resolved", "产出", "总结", "整理", "新增", "实现")
    )


def infer_topic_hint(file_summary: dict[str, Any]) -> str:
    path = file_summary.get("file", "").lower()
    activities = file_summary.get("activities", {})
    parent_text = "/".join(file_summary.get("parent_dirs", [])).lower()
    if "interview" in path or "面试" in path:
        return "面试复盘与准备"
    if "meeting" in path or "会议" in path:
        return "会议与沟通记录"
    if "project" in parent_text or "项目" in parent_text or activities.get("project_planning", 0) >= 3:
        return "项目开发与方案设计"
    if "paper" in path or "论文" in path or activities.get("paper_reading", 0) >= 3:
        if "agent" in path:
            return "Agent 论文与智能体学习"
        if "llm" in path:
            return "LLM 论文与模型学习"
        return "论文阅读与学习笔记"
    if activities.get("experiment_log", 0) >= 3:
        return "实验记录与结果分析"
    if activities.get("todo", 0) >= 3:
        return "任务推进与待办管理"
    top_dir = file_summary.get("top_dir") or "其他"
    return f"{top_dir} 相关内容"


def collect_points(file_summaries: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for item in file_summaries:
        for point in item.get(key, []):
            enriched = dict(point)
            enriched["file"] = item.get("file")
            enriched["topic_hint"] = item.get("topic_hint")
            append_limited(points, enriched, limit)
            if len(points) >= limit:
                return points
    return points


def build_source_index(file_summaries: list[dict[str, Any]], limit: int) -> list[str]:
    sources: list[str] = []
    for item in file_summaries:
        for source in item.get("source_links", []):
            append_limited(sources, source, limit)
            if len(sources) >= limit:
                return sources
    return sources


def report_outline() -> list[str]:
    return [
        "## 1. 本周期工作总览",
        "## 2. 本周期完成 / 产出事项",
        "## 3. 按主题分类复盘",
        "## 4. 项目 / 目标进展",
        "## 5. 未解决问题与阻塞事项",
        "## 6. 建议与下一步计划",
        "## 7. 来源与关联笔记",
    ]


def writing_guidelines(run_mode: str) -> list[str]:
    guidelines = [
        "以本周期新增/修改的文件为第一证据入口，必须覆盖这些文件；changed blocks 只用于定位文件内细节。",
        "每个有变化的 confirmed profile 主题都要总结一条逻辑线：本周期哪些文件变化、它们共同说明什么进展、下一步是什么。",
        "按主题串联工作脉络，不要按文件机械罗列。",
        "关键结论尽量引用 block.source_link。",
        "把未完成 checkbox、TODO、blocked、疑问和风险整理到阻塞或继承事项。",
        "内容产出只能表述为学习、整理、形成方案、记录实验等，不要过度声称完成。",
        "preferred_topics 是偏好，不是限制；最终主题应服从本周期新增/修改文件的证据。",
    ]
    if run_mode == "first_baseline":
        guidelines.append("这是首次基线复盘：只能说基于本周期修改文件生成，不要声称所有 blocks 都是本周期新增。")
    return guidelines


def digest_writing_guidelines(run_mode: str) -> list[str]:
    guidelines = [
        "优先阅读 review_digest.latest.json 中的 executive_input、topic_summaries、file_summaries；file_summaries 是主证据。",
        "不要把 changed blocks 当作报告基点；changed_blocks.latest.json 只用于必要时查证某个文件内的具体来源。",
        "必须总结所有 file_summaries 里的新增/修改文件；若同一主题下有多个文件，要写成一条逻辑线，而不是逐条罗列 block。",
        "禁止逐条罗列 block 或写成 `[[source]]: 原文片段` 清单。",
        "报告必须是复盘文章：先总览，再按主题串联进展、产出、问题和下一步。",
        "每个主题最多列 3-6 个关键来源，source_link 已经是 Obsidian 双链，必须原样使用，不要再包一层 [[...]]。",
        "不能留下 `[整体总结]`、`[待填写]` 等占位符；证据不足就写“本周期未从笔记中识别到明确证据”。",
        "完成事项要区分明确完成和内容产出，不要把阅读笔记夸大成项目完成。",
        "未完成 checkbox、blocked、疑问、风险应进入未解决问题或下周期继承事项。",
    ]
    if run_mode == "first_baseline":
        guidelines.insert(
            0,
            "这是首次基线复盘，不是真正精确 diff；要在总览里说明本报告基于本周期修改过的文件建立基线。",
        )
    return guidelines


def ensure_inside_vault(path: Path, vault: Path, label: str) -> None:
    try:
        path.resolve().relative_to(vault.resolve())
    except ValueError as exc:
        raise UserFacingError(f"{label} must be inside the vault: {path}") from exc


def rel_to_vault(path: Path, vault: Path) -> str:
    return path.resolve().relative_to(vault.resolve()).as_posix()


_MISSING = object()


def load_json(path: Path, default: Any = _MISSING) -> Any:
    if not path.exists():
        if default is not _MISSING:
            return default
        raise UserFacingError(f"JSON file does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise UserFacingError(f"Invalid JSON in {path}: {exc}") from exc


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        os.replace(str(tmp_path), str(path))
    except PermissionError:
        # Some Windows sandboxed environments allow normal writes but deny
        # rename/replace operations. Fall back to a direct write so the helper
        # remains usable in local GenericAgent workspaces.
        path.write_text(text, encoding="utf-8")
        try:
            tmp_path.unlink()
        except OSError:
            pass


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if text and not text.endswith("\n"):
        text += "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        os.replace(str(tmp_path), str(path))
    except PermissionError:
        path.write_text(text, encoding="utf-8")
        try:
            tmp_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
