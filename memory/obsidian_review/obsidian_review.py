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
    finalize_parser.add_argument("--review-id", required=True, help="Review run id returned by prepare.")
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
        "next": "Maintain Reviews/_AgentProfile/vault_profile.draft.md 用户确认区 in Obsidian, then run /obsidian-review confirm-profile --vault <path> once to establish the initial snapshot.",
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
    l1_path = write_runtime_l1_context(vault, config, confirmed_profile, run_at)

    return {
        "ok": True,
        "action": "profile-confirm",
        "vault_path": str(vault),
        "config_path": str(config_path),
        "profile_draft": str(draft_path),
        "confirmed_profile": str(confirmed_path),
        "l1_context": str(l1_path),
        "snapshot": str(snapshot_path),
        "markdown_files": summary["overview"]["markdown_files"],
        "skipped": skipped,
        "next": "Initial profile baseline saved. Future periodic /obsidian-review runs will re-read \u6211\u7684\u590d\u76d8\u8bb0\u5fc6 and refresh runtime memory automatically.",
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
    review_id = make_review_id(run_at, vault, period_info)
    run_dir = review_run_dir(state_dir, review_id)
    run_dir.mkdir(parents=True, exist_ok=False)

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
    current_snapshot, skipped = build_snapshot(vault, config, run_at)
    current_snapshot["review_id"] = review_id
    previous_state = load_json(state_dir / "review_state.json", default={})
    profile_draft_path = vault / config.get("profile_draft_dir", "Reviews/_AgentProfile") / "vault_profile.draft.md"
    memory_dir = state_dir / "memory"
    draft_memory_sync = empty_draft_memory_sync()
    if profile_draft_path.exists() and profile_draft_path.is_file():
        draft_text = profile_draft_path.read_text(encoding="utf-8-sig", errors="replace")
        draft_memory_sync = sync_draft_memory_decisions(profile_draft_path, memory_dir, vault, run_at)
        draft_text = profile_draft_path.read_text(encoding="utf-8-sig", errors="replace")
        summary = build_vault_profile_summary(current_snapshot, skipped, config, run_at)
        confirmed_profile = build_confirmed_profile(draft_text, summary, config, vault, profile_draft_path, run_at)
        merge_confirmed_memory_updates(
            confirmed_profile,
            load_confirmed_memory_updates(memory_dir / "profile_updates.history.jsonl"),
        )
        confirmed_profile["profile_source_mode"] = "draft_markdown_runtime_source"
        atomic_write_json(confirmed_profile_path, confirmed_profile)
    else:
        confirmed_profile["profile_source_mode"] = "confirmed_json_cache"
    l1_path = write_runtime_l1_context(vault, config, confirmed_profile, run_at)

    if isinstance(previous_state, dict) and not previous_state.get("last_run"):
        changed_files = period_file_summaries(current_snapshot, period_info, confirmed_profile)
        changed_blocks = first_baseline_blocks(current_snapshot, period_info)
        run_mode = "first_baseline"
    else:
        changed_files = diff_file_summaries(previous_snapshot, current_snapshot, period_info, confirmed_profile)
        changed_blocks = diff_snapshots(previous_snapshot, current_snapshot, period_info)
        run_mode = "block_diff"

    suggested_report = allocate_report_path(vault, output_dir, period_info["period"], run_at, review_id)
    changed_path = run_dir / "changed_blocks.json"
    digest_path = run_dir / "review_digest.json"
    pending_path = run_dir / "pending_snapshot.json"
    state_update_path = run_dir / "review_state_update.json"
    proposals_path = run_dir / "memory_proposals.json"
    manifest_path = run_dir / "run_manifest.json"
    meta = {
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "source": "GenericAgent",
        "vault_path": str(vault),
        "config_path": str(config_path),
        "state_dir": rel_to_vault(state_dir, vault),
        "run_dir": rel_to_vault(run_dir, vault),
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
        "vault_profile_draft_file": rel_to_vault(profile_draft_path, vault) if profile_draft_path.exists() else "",
        "vault_profile_source_mode": confirmed_profile.get("profile_source_mode"),
        "vault_profile_confirmed_at": confirmed_profile.get("confirmed_at"),
        "vault_profile_update_file": rel_to_vault(profile_update_path, vault),
        "suggested_report": rel_to_vault(suggested_report, vault),
        "suggested_report_path": str(suggested_report),
        "pending_snapshot": rel_to_vault(pending_path, vault),
        "changed_blocks_file": rel_to_vault(changed_path, vault),
        "review_digest_file": rel_to_vault(digest_path, vault),
        "review_state_update_file": rel_to_vault(state_update_path, vault),
        "memory_proposals_file": rel_to_vault(proposals_path, vault),
        "run_manifest_file": rel_to_vault(manifest_path, vault),
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
    state_update_payload = {
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "open_items": [],
        "blockers": [],
        "active_topics": [],
    }
    proposals_payload = {
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "proposals": [],
    }
    manifest_payload = {
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "created_at": run_at.isoformat(),
        "vault_path": str(vault),
        "period": period_info["period"],
        "date_start": period_info["date_start"],
        "date_end": period_info["date_end"],
        "run_mode": run_mode,
        "suggested_report": str(suggested_report),
        "suggested_report_rel": rel_to_vault(suggested_report, vault),
        "changed_blocks_file": str(changed_path),
        "review_digest_file": str(digest_path),
        "pending_snapshot": str(pending_path),
        "review_state_update_file": str(state_update_path),
        "memory_proposals_file": str(proposals_path),
    }
    atomic_write_json(changed_path, payload)
    atomic_write_json(digest_path, digest_payload)
    atomic_write_json(pending_path, current_snapshot)
    atomic_write_json(state_update_path, state_update_payload)
    atomic_write_json(proposals_path, proposals_payload)
    atomic_write_json(manifest_path, manifest_payload)
    atomic_write_json(state_dir / "changed_blocks.latest.json", payload)
    atomic_write_json(state_dir / "review_digest.latest.json", digest_payload)
    atomic_write_json(state_dir / "pending_snapshot.latest.json", current_snapshot)
    atomic_write_json(state_dir / "review_state_update.latest.json", state_update_payload)
    atomic_write_json(state_dir / "memory_proposals.latest.json", proposals_payload)
    atomic_write_json(state_dir / "latest_run_manifest.json", manifest_payload)
    atomic_write_json(profile_update_path, profile_update_payload)
    draft_candidates_added = 0

    return {
        "ok": True,
        "action": "prepare",
        "review_id": review_id,
        "vault_path": str(vault),
        "run_dir": str(run_dir),
        "run_manifest": str(manifest_path),
        "run_mode": run_mode,
        "period": period_info["period"],
        "date_start": period_info["date_start"],
        "date_end": period_info["date_end"],
        "changed_files": len(changed_files),
        "changed_blocks": len(changed_blocks),
        "changed_blocks_file": str(changed_path),
        "review_digest_file": str(digest_path),
        "pending_snapshot": str(pending_path),
        "review_state_update_file": str(state_update_path),
        "memory_proposals_file": str(proposals_path),
        "confirmed_profile": str(confirmed_profile_path),
        "profile_draft": str(profile_draft_path) if profile_draft_path.exists() else "",
        "l1_context": str(l1_path),
        "vault_profile_update_file": str(profile_update_path),
        "profile_draft_candidates_added": draft_candidates_added,
        "draft_memory_sync": draft_memory_sync,
        "suggested_report": str(suggested_report),
        "next": "Generate the Markdown report from this run's review_digest.json, write this run's review_state_update.json and memory_proposals.json, then run finalize with --review-id.",
    }


def cmd_finalize(args: argparse.Namespace) -> dict[str, Any]:
    config, _config_path, vault = load_config_from_args(args)
    state_dir = vault / config["state_dir"]
    review_id = args.review_id
    run_dir = review_run_dir(state_dir, review_id)
    if not run_dir.exists() or not run_dir.is_dir():
        raise UserFacingError(f"Review run does not exist: {run_dir}")

    marker_path = finalize_marker_path(run_dir)
    if marker_path.exists():
        marker = load_json(marker_path)
        if isinstance(marker, dict):
            marker["idempotent"] = True
            return marker

    manifest_path = run_dir / "run_manifest.json"
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("review_id") != review_id:
        raise UserFacingError(f"Run manifest review_id mismatch: {manifest_path}")

    pending_path = run_dir / "pending_snapshot.json"
    snapshot_path = state_dir / "review_snapshot.json"
    changed_path = run_dir / "changed_blocks.json"
    digest_path = run_dir / "review_digest.json"
    state_path = state_dir / "review_state.json"
    update_path = run_dir / "review_state_update.json"
    proposals_path = run_dir / "memory_proposals.json"

    report_path = Path(args.report).expanduser()
    if not report_path.is_absolute():
        report_path = (Path.cwd() / report_path).resolve()
    else:
        report_path = report_path.resolve()
    if not report_path.exists() or not report_path.is_file():
        raise UserFacingError(f"Report file does not exist: {report_path}")
    ensure_inside_vault(report_path, vault, "report")
    expected_report = Path(str(manifest.get("suggested_report", ""))).expanduser()
    if not expected_report.is_absolute():
        expected_report = (vault / expected_report).resolve()
    else:
        expected_report = expected_report.resolve()
    if report_path != expected_report:
        raise UserFacingError(
            f"Report path does not match this review run. Expected {expected_report}, got {report_path}"
        )
    if not pending_path.exists():
        raise UserFacingError(f"Missing pending snapshot. Run prepare first: {pending_path}")

    pending_snapshot = load_json(pending_path)
    if not isinstance(pending_snapshot, dict) or pending_snapshot.get("review_id") != review_id:
        raise UserFacingError(f"pending_snapshot.json review_id mismatch: {pending_path}")
    changed_payload = load_json(changed_path, default={})
    changed_meta = changed_payload.get("meta", {}) if isinstance(changed_payload, dict) else {}
    if changed_meta.get("review_id") != review_id:
        raise UserFacingError(f"changed_blocks.json review_id mismatch: {changed_path}")
    digest_payload = load_json(digest_path)
    digest_meta = digest_payload.get("meta", {}) if isinstance(digest_payload, dict) else {}
    if digest_meta.get("review_id") != review_id:
        raise UserFacingError(f"review_digest.json review_id mismatch: {digest_path}")
    proposals_payload = load_json(proposals_path)
    normalized_proposals = normalize_memory_proposals(proposals_payload, review_id)
    atomic_write_json(
        proposals_path,
        {"schema_version": SCHEMA_VERSION, "review_id": review_id, "proposals": normalized_proposals},
    )

    memory_dir = state_dir / "memory"
    pending_updates_path = memory_dir / "profile_updates.pending.jsonl"
    history_updates_path = memory_dir / "profile_updates.history.jsonl"
    review_history_path = memory_dir / "review_history.jsonl"
    profile_draft_path = vault / config.get("profile_draft_dir", "Reviews/_AgentProfile") / "vault_profile.draft.md"
    normalized_proposals = filter_new_memory_proposals(
        normalized_proposals,
        pending_updates_path,
        history_updates_path,
        profile_draft_path,
    )
    atomic_write_json(
        proposals_path,
        {"schema_version": SCHEMA_VERSION, "review_id": review_id, "proposals": normalized_proposals},
    )
    proposal_records = [
        proposal_to_pending_record(proposal, review_id, report_path, digest_path, run_dir, vault)
        for proposal in normalized_proposals
    ]
    appended_proposal_ids = append_jsonl_once(pending_updates_path, proposal_records, "proposal_id")
    draft_candidates_added = append_memory_proposals_to_draft(profile_draft_path, proposal_records, run_at=datetime.now(get_timezone(config.get("timezone", "Asia/Shanghai"))))
    touched_mainlines = []
    for proposal in normalized_proposals:
        if append_mainline_candidate_once(memory_dir, proposal, review_id, report_path, digest_path, vault):
            touched_mainlines.append(proposal.get("target_id", "uncategorized"))
    touched_mainlines = unique_list(touched_mainlines)

    existing_state = load_json(state_path, default={})
    if not isinstance(existing_state, dict):
        existing_state = {}
    state_update = load_json(update_path, default={})
    if not isinstance(state_update, dict):
        state_update = {}
    if state_update.get("review_id") not in (None, "", review_id):
        raise UserFacingError(f"review_state_update.json review_id mismatch: {update_path}")
    next_state = merge_review_state(existing_state, state_update)
    next_state["latest_report"] = rel_to_vault(report_path, vault)
    next_state["last_run"] = {
        "review_id": review_id,
        "period": changed_meta.get("period"),
        "run_mode": changed_meta.get("run_mode"),
        "date_start": changed_meta.get("date_start"),
        "date_end": changed_meta.get("date_end"),
        "changed_blocks": changed_meta.get("changed_blocks_count", 0),
    }
    history_record = {
        "id": review_id,
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "period": changed_meta.get("period"),
        "date_start": changed_meta.get("date_start"),
        "date_end": changed_meta.get("date_end"),
        "report_path": rel_to_vault(report_path, vault),
        "digest_path": rel_to_vault(digest_path, vault),
        "changed_files": changed_meta.get("changed_files_count", 0),
        "changed_blocks": changed_meta.get("changed_blocks_count", 0),
        "mainlines_touched": touched_mainlines,
        "created_pending_updates": [record["proposal_id"] for record in proposal_records],
        "finalized_at": datetime.now(get_timezone(config.get("timezone", "Asia/Shanghai"))).isoformat(),
    }
    appended_review_ids = append_jsonl_once(review_history_path, [history_record], "review_id")

    atomic_write_json(snapshot_path, pending_snapshot)
    atomic_write_json(state_path, next_state)

    marker = {
        "ok": True,
        "action": "finalize",
        "review_id": review_id,
        "report": str(report_path),
        "snapshot": str(snapshot_path),
        "state": str(state_path),
        "latest_report": next_state["latest_report"],
        "proposal_count": len(normalized_proposals),
        "appended_proposal_ids": appended_proposal_ids,
        "profile_draft_candidates_added": draft_candidates_added,
        "review_history_appended": bool(appended_review_ids),
        "mainlines_touched": touched_mainlines,
    }
    atomic_write_json(marker_path, marker)
    return marker


def make_review_id(run_at: datetime, vault: Path, period_info: dict[str, str]) -> str:
    base = "|".join(
        [
            str(vault.resolve()),
            period_info.get("period", ""),
            period_info.get("date_start", ""),
            period_info.get("date_end", ""),
            run_at.isoformat(),
        ]
    )
    suffix = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"review_{run_at.strftime('%Y%m%d_%H%M%S')}_{suffix}"


def review_run_dir(state_dir: Path, review_id: str) -> Path:
    if not re.fullmatch(r"review_\d{8}_\d{6}_[0-9a-f]{8}", str(review_id or "")):
        raise UserFacingError(f"Invalid review_id: {review_id}")
    return state_dir / "runs" / review_id


def finalize_marker_path(run_dir: Path) -> Path:
    return run_dir / "finalize_marker.json"


ALLOWED_PROPOSAL_KINDS = {
    "mainline_progress",
    "mainline_gap",
    "mainline_next_step",
    "new_mainline_candidate",
    "workflow_preference_candidate",
}


def normalize_memory_proposals(payload: Any, review_id: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise UserFacingError("memory_proposals.json must be a JSON object.")
    if payload.get("review_id") != review_id:
        raise UserFacingError("memory_proposals.json review_id mismatch.")
    proposals = payload.get("proposals", [])
    if not isinstance(proposals, list):
        raise UserFacingError("memory_proposals.json proposals must be a list.")
    if len(proposals) > 5:
        raise UserFacingError("memory_proposals.json may contain at most 5 proposals.")

    normalized = []
    for index, raw in enumerate(proposals, start=1):
        if not isinstance(raw, dict):
            raise UserFacingError(f"Proposal #{index} must be a JSON object.")
        kind = str(raw.get("kind", "")).strip()
        if kind not in ALLOWED_PROPOSAL_KINDS:
            raise UserFacingError(f"Unsupported proposal kind: {kind}")
        target_id = str(raw.get("target_id") or "uncategorized").strip() or "uncategorized"
        proposal_text = str(raw.get("proposal", "")).strip()
        if not proposal_text:
            raise UserFacingError(f"Proposal #{index} is missing proposal text.")
        if len(proposal_text) > 180:
            raise UserFacingError(f"Proposal #{index} exceeds 180 characters.")
        evidence = normalize_proposal_evidence(raw.get("evidence", []), index)
        expected_id = proposal_key(review_id, kind, target_id, proposal_text)
        provided_id = str(raw.get("proposal_id", "")).strip()
        if provided_id and provided_id != expected_id:
            raise UserFacingError(f"Proposal #{index} proposal_id mismatch. Expected {expected_id}.")
        normalized.append(
            {
                "proposal_id": expected_id,
                "kind": kind,
                "target_id": target_id,
                "proposal": proposal_text,
                "confidence": "agent_candidate",
                "evidence": evidence,
            }
        )
    payload["proposals"] = normalized
    return normalized


def normalize_proposal_evidence(value: Any, proposal_index: int) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise UserFacingError(f"Proposal #{proposal_index} evidence must be a list.")
    result: list[dict[str, str]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            raise UserFacingError(f"Proposal #{proposal_index} evidence entries must be objects.")
        entry_type = str(item.get("type") or "source").strip()[:40]
        path = str(item.get("path") or item.get("link") or "").strip()
        if not path:
            continue
        result.append({"type": entry_type, "path": path[:240]})
    return result


def proposal_key(review_id: str, kind: str, target_id: str, proposal: str) -> str:
    raw = "|".join([review_id, kind, target_id, proposal])
    return "prop_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def proposal_to_pending_record(
    proposal: dict[str, Any],
    review_id: str,
    report_path: Path,
    digest_path: Path,
    run_dir: Path,
    vault: Path,
) -> dict[str, Any]:
    evidence = list(proposal.get("evidence", []))
    evidence.extend(
        [
            {"type": "review_report", "path": rel_to_vault(report_path, vault)},
            {"type": "review_digest", "path": rel_to_vault(digest_path, vault)},
        ]
    )
    return {
        "id": proposal["proposal_id"],
        "proposal_id": proposal["proposal_id"],
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "status": "pending",
        "kind": proposal["kind"],
        "target_layer": "L3",
        "target_id": proposal.get("target_id", "uncategorized"),
        "proposal": proposal["proposal"],
        "confidence": "agent_candidate",
        "evidence": evidence,
        "run_dir": rel_to_vault(run_dir, vault),
        "created_at": datetime.now().isoformat(),
    }


def append_jsonl_once(path: Path, records: list[dict[str, Any]], key: str) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_jsonl_keys(path, key)
    additions = []
    added_keys = []
    for record in records:
        value = str(record.get(key, "")).strip()
        if not value or value in existing:
            continue
        additions.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        added_keys.append(value)
        existing.add(value)
    if additions:
        with path.open("a", encoding="utf-8") as handle:
            for line in additions:
                handle.write(line + "\n")
    return added_keys


def read_jsonl_keys(path: Path, key: str) -> set[str]:
    if not path.exists():
        return set()
    result: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UserFacingError(f"Invalid JSONL in {path} at line {lineno}: {exc}") from exc
        value = str(item.get(key, "")).strip()
        if value:
            result.add(value)
    return result


def append_mainline_candidate_once(
    memory_dir: Path,
    proposal: dict[str, Any],
    review_id: str,
    report_path: Path,
    digest_path: Path,
    vault: Path,
) -> bool:
    target_id = str(proposal.get("target_id") or "uncategorized").strip() or "uncategorized"
    mainline_id = safe_mainline_id(target_id)
    mainline_dir = memory_dir / "mainlines"
    mainline_path = mainline_dir / f"{mainline_id}.md"
    existing = mainline_path.read_text(encoding="utf-8-sig", errors="replace") if mainline_path.exists() else ""
    proposal_id = proposal["proposal_id"]
    if proposal_id in existing:
        return False
    if not existing:
        existing = f"# {target_id}\n\n## Agent 候选更新\n"
    elif "## Agent 候选更新" not in existing:
        existing = existing.rstrip() + "\n\n## Agent 候选更新\n"
    addition = (
        f"\n- `{proposal_id}` [{proposal['kind']}] {proposal['proposal']}\n"
        f"  - review_id: `{review_id}`\n"
        f"  - report: {rel_to_vault(report_path, vault)}\n"
        f"  - digest: {rel_to_vault(digest_path, vault)}\n"
    )
    atomic_write_text(mainline_path, existing.rstrip() + "\n" + addition)
    return True



DRAFT_PROPOSAL_ID_RE = re.compile(r"`?(prop_[0-9a-f]{16})`?")
DRAFT_STATUS_RE = re.compile("(?:status|\u72b6\u6001|\u5904\u7406)\s*[:\uff1a]\s*`?([^`|,;\s]+)`?", re.IGNORECASE)


def empty_draft_memory_sync() -> dict[str, Any]:
    return {
        "confirmed": [],
        "rejected": [],
        "pending": 0,
        "restored_to_draft": 0,
        "history_appended": [],
    }


def sync_draft_memory_decisions(draft_path: Path, memory_dir: Path, vault: Path, run_at: datetime) -> dict[str, Any]:
    sync = empty_draft_memory_sync()
    if not draft_path.exists() or not draft_path.is_file():
        return sync

    pending_path = memory_dir / "profile_updates.pending.jsonl"
    history_path = memory_dir / "profile_updates.history.jsonl"
    draft_text = draft_path.read_text(encoding="utf-8-sig", errors="replace")
    decisions = parse_draft_candidate_decisions(draft_text)
    pending_records = read_jsonl_records(pending_path)
    history_ids = read_jsonl_keys(history_path, "proposal_id")
    history_records: list[dict[str, Any]] = []
    remaining_pending: list[dict[str, Any]] = []
    seen_pending_ids: set[str] = set()

    for record in pending_records:
        proposal_id = str(record.get("proposal_id") or record.get("id") or "").strip()
        if not proposal_id:
            continue
        seen_pending_ids.add(proposal_id)
        decision = decisions.get(proposal_id)
        status = (decision or {}).get("status", "pending")
        if proposal_id in history_ids:
            continue
        if status == "confirmed":
            history_record = decision_history_record(record, decision, "confirmed", draft_path, vault, run_at)
            history_records.append(history_record)
            append_confirmed_memory_once(memory_dir, history_record, vault, run_at)
            sync["confirmed"].append(proposal_id)
        elif status == "rejected":
            history_records.append(decision_history_record(record, decision, "rejected", draft_path, vault, run_at))
            sync["rejected"].append(proposal_id)
        else:
            remaining_pending.append(record)

    for proposal_id, decision in decisions.items():
        status = decision.get("status", "pending")
        if status not in {"confirmed", "rejected"} or proposal_id in seen_pending_ids or proposal_id in history_ids:
            continue
        draft_record = {
            "id": proposal_id,
            "proposal_id": proposal_id,
            "schema_version": SCHEMA_VERSION,
            "review_id": decision.get("review_id", ""),
            "status": "pending",
            "kind": decision.get("kind", "draft_candidate"),
            "target_layer": "L3",
            "target_id": decision.get("target_id", "uncategorized"),
            "proposal": decision.get("proposal", ""),
            "confidence": "user_edited_draft",
            "evidence": [],
            "run_dir": "",
        }
        history_record = decision_history_record(draft_record, decision, status, draft_path, vault, run_at)
        history_records.append(history_record)
        if status == "confirmed":
            append_confirmed_memory_once(memory_dir, history_record, vault, run_at)
            sync["confirmed"].append(proposal_id)
        else:
            sync["rejected"].append(proposal_id)

    appended = append_jsonl_once(history_path, history_records, "proposal_id")
    sync["history_appended"] = appended
    write_jsonl_records(pending_path, remaining_pending)
    sync["pending"] = len(remaining_pending)
    sync["restored_to_draft"] = append_memory_proposals_to_draft(draft_path, remaining_pending, run_at)
    return sync


def parse_draft_candidate_decisions(text: str) -> dict[str, dict[str, Any]]:
    lines = (text or "").splitlines()
    decisions: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(lines):
        match = DRAFT_PROPOSAL_ID_RE.search(lines[index])
        if not match:
            index += 1
            continue
        proposal_id = match.group(1)
        block = [lines[index]]
        index += 1
        while index < len(lines):
            next_line = lines[index]
            next_match = DRAFT_PROPOSAL_ID_RE.search(next_line)
            if next_match and re.match(r"\s*[-*+]\s+", next_line):
                break
            if re.match(r"^#{1,3}\s+", next_line) and any(DRAFT_PROPOSAL_ID_RE.search(item) for item in block):
                break
            block.append(next_line)
            index += 1
        parsed = parse_draft_candidate_block(proposal_id, block)
        if parsed:
            decisions[proposal_id] = parsed
    return decisions


def parse_draft_candidate_block(proposal_id: str, block: list[str]) -> dict[str, Any]:
    block_text = "\n".join(block)
    first_line = block[0] if block else ""
    status = parse_draft_candidate_status(block_text, first_line)
    proposal = parse_draft_field(block, ("\u5efa\u8bae\u5185\u5bb9", "\u5185\u5bb9", "proposal", "text")) or fallback_draft_proposal_text(first_line)
    kind = (
        parse_draft_field(block, ("\u5efa\u8bae\u7c7b\u578b", "kind", "\u7c7b\u578b"))
        or parse_inline_meta(first_line, "kind")
        or "draft_candidate"
    )
    target_id = (
        parse_draft_field(block, ("\u76f8\u5173\u5bf9\u8c61", "\u76f8\u5173\u4e3b\u7ebf", "target", "target_id", "\u4e3b\u7ebf"))
        or parse_inline_meta(first_line, "target")
        or "uncategorized"
    )
    review_id = parse_draft_field(block, ("\u590d\u76d8\u7f16\u53f7", "review_id", "review")) or parse_inline_meta(first_line, "review_id")
    return {
        "proposal_id": proposal_id,
        "status": status,
        "kind": clean_draft_value(kind),
        "target_id": clean_draft_value(target_id) or "uncategorized",
        "proposal": clean_draft_value(proposal),
        "review_id": clean_draft_value(review_id),
    }

def parse_draft_candidate_status(block_text: str, first_line: str) -> str:
    match = DRAFT_STATUS_RE.search(block_text or "")
    if match:
        status = normalize_draft_status(match.group(1))
        if status:
            return status
    if re.search(r"\[[xX]\]", first_line or ""):
        return "confirmed"
    if re.search(r"\[\s\]", first_line or ""):
        return "pending"
    return "pending"


def normalize_draft_status(value: str) -> str:
    lowered = str(value or "").strip().strip("`[](){}.,;:").lower()
    confirmed = {"confirmed", "confirm", "approved", "approve", "accepted", "accept", "done", "yes", "y", "\u786e\u8ba4", "\u5df2\u786e\u8ba4", "\u901a\u8fc7", "\u63a5\u53d7", "\u5bf9", "\u662f"}
    rejected = {"rejected", "reject", "declined", "decline", "refused", "refuse", "no", "n", "\u62d2\u7edd", "\u5df2\u62d2\u7edd", "\u4e22\u5f03", "\u653e\u5f03", "\u4e0d\u5bf9", "\u5426"}
    pending = {"pending", "todo", "open", "wait", "waiting", "\u5f85\u5b9a", "\u5f85\u5904\u7406", "\u672a\u5904\u7406", "\u4fdd\u7559", "\u5148\u7559\u7740"}
    if lowered in confirmed:
        return "confirmed"
    if lowered in rejected:
        return "rejected"
    if lowered in pending:
        return "pending"
    return ""

def parse_draft_field(block: list[str], labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"^\s*(?:[-*+]\s*)?(?:{label_pattern})\s*[:\uff1a]\s*(.+?)\s*$", re.IGNORECASE)
    for line in block:
        match = pattern.match(line)
        if match:
            return match.group(1)
    return ""


def parse_inline_meta(line: str, key: str) -> str:
    match = re.search(rf"(?:^|\|)\s*{re.escape(key)}\s*[:\uff1a]\s*`?([^`|]+)`?", line or "", flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def fallback_draft_proposal_text(line: str) -> str:
    text = re.sub(r"^\s*[-*+]\s+\[[ xX]\]\s*", "", line or "").strip()
    text = DRAFT_PROPOSAL_ID_RE.sub("", text)
    text = re.sub(r"(?:proposal_id|\u5efa\u8bae\u7f16\u53f7)\s*[:\uff1a]\s*", "", text, flags=re.IGNORECASE)
    parts = [part.strip() for part in text.split("|")]
    meta_keys = r"status|\u72b6\u6001|\u5904\u7406|kind|\u7c7b\u578b|\u5efa\u8bae\u7c7b\u578b|target|target_id|\u76f8\u5173\u5bf9\u8c61|\u76f8\u5173\u4e3b\u7ebf|review_id|\u590d\u76d8\u7f16\u53f7"
    kept = [part for part in parts if not re.match(rf"^(?:{meta_keys})\s*[:\uff1a]", part, flags=re.IGNORECASE)]
    return " | ".join(kept).strip()

def clean_draft_value(value: Any) -> str:
    text = str(value or "").strip()
    text = text.strip("`").strip()
    text = re.sub("\uff08.*?\uff09", "", text).strip()
    return text[:300]


def decision_history_record(
    pending_record: dict[str, Any],
    decision: dict[str, Any] | None,
    status: str,
    draft_path: Path,
    vault: Path,
    run_at: datetime,
) -> dict[str, Any]:
    decision = decision or {}
    original = str(pending_record.get("proposal", "")).strip()
    user_proposal = str(decision.get("proposal") or original).strip()
    record = dict(pending_record)
    record.update(
        {
            "id": pending_record.get("proposal_id") or pending_record.get("id"),
            "proposal_id": pending_record.get("proposal_id") or pending_record.get("id"),
            "status": status,
            "decision": status,
            "proposal": user_proposal,
            "original_proposal": original,
            "user_modified": bool(user_proposal and original and user_proposal != original),
            "decision_source": "vault_profile_draft",
            "decision_draft": rel_to_vault(draft_path, vault),
            "decided_at": run_at.isoformat(),
        }
    )
    if decision.get("kind"):
        record["kind"] = decision["kind"]
    if decision.get("target_id"):
        record["target_id"] = decision["target_id"]
    if decision.get("review_id") and not record.get("review_id"):
        record["review_id"] = decision["review_id"]
    return record


def load_confirmed_memory_updates(history_path: Path) -> list[dict[str, Any]]:
    return [record for record in read_jsonl_records(history_path) if record.get("status") == "confirmed"]


def merge_confirmed_memory_updates(profile: dict[str, Any], updates: list[dict[str, Any]]) -> None:
    confirmed = []
    seen_ids = set()
    for record in updates:
        proposal_id = str(record.get("proposal_id", "")).strip()
        proposal = str(record.get("proposal", "")).strip()
        if not proposal or proposal_id in seen_ids:
            continue
        seen_ids.add(proposal_id)
        confirmed.append(record)
    profile["confirmed_memory_updates"] = confirmed
    for record in confirmed:
        kind = str(record.get("kind", "")).strip()
        entry = {
            "source": "confirmed_memory_proposal",
            "proposal_id": record.get("proposal_id"),
            "text": record.get("proposal", ""),
        }
        if kind == "new_mainline_candidate":
            append_profile_entry_once(profile.setdefault("active_mainlines", []), entry)
        elif kind == "workflow_preference_candidate":
            append_profile_entry_once(profile.setdefault("review_preferences", []), entry)


def append_profile_entry_once(entries: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    label = normalize_memory_text(entry.get("text", ""))
    proposal_id = str(entry.get("proposal_id", "")).strip()
    for existing in entries:
        if not isinstance(existing, dict):
            continue
        if proposal_id and existing.get("proposal_id") == proposal_id:
            return
        if label and normalize_memory_text(extract_profile_entry_label(existing)) == label:
            return
    entries.append(entry)


def append_confirmed_memory_once(memory_dir: Path, record: dict[str, Any], vault: Path, run_at: datetime) -> bool:
    if record.get("status") != "confirmed":
        return False
    target_id = str(record.get("target_id") or "uncategorized").strip() or "uncategorized"
    mainline_path = memory_dir / "mainlines" / f"{safe_mainline_id(target_id)}.md"
    existing = mainline_path.read_text(encoding="utf-8-sig", errors="replace") if mainline_path.exists() else ""
    proposal_id = str(record.get("proposal_id", "")).strip()
    if proposal_id and proposal_id in existing:
        return False
    heading = "## User confirmed updates"
    if not existing:
        existing = f"# {target_id}\n\n{heading}\n"
    elif heading not in existing:
        existing = existing.rstrip() + f"\n\n{heading}\n"
    addition = (
        f"\n- `{proposal_id}` [{record.get('kind', 'candidate')}] {record.get('proposal', '')}\n"
        f"  - status: confirmed\n"
        f"  - decided_at: {run_at.isoformat()}\n"
        f"  - source: {record.get('decision_draft', '')}\n"
    )
    atomic_write_text(mainline_path, existing.rstrip() + "\n" + addition)
    return True


def filter_new_memory_proposals(
    proposals: list[dict[str, Any]],
    pending_path: Path,
    history_path: Path,
    draft_path: Path,
) -> list[dict[str, Any]]:
    known_ids = read_jsonl_keys(pending_path, "proposal_id") | read_jsonl_keys(history_path, "proposal_id")
    known_texts = known_memory_proposal_texts(pending_path, history_path, draft_path)
    result = []
    seen_texts = set(known_texts)
    for proposal in proposals:
        proposal_id = str(proposal.get("proposal_id", "")).strip()
        text_key = normalize_memory_text(proposal.get("proposal", ""))
        if not proposal_id or proposal_id in known_ids or (text_key and text_key in seen_texts):
            continue
        result.append(proposal)
        if text_key:
            seen_texts.add(text_key)
    return result


def known_memory_proposal_texts(pending_path: Path, history_path: Path, draft_path: Path) -> set[str]:
    texts = set()
    for path in (pending_path, history_path):
        for record in read_jsonl_records(path):
            value = normalize_memory_text(record.get("proposal", ""))
            if value:
                texts.add(value)
            original = normalize_memory_text(record.get("original_proposal", ""))
            if original:
                texts.add(original)
    if draft_path.exists() and draft_path.is_file():
        for decision in parse_draft_candidate_decisions(draft_path.read_text(encoding="utf-8-sig", errors="replace")).values():
            value = normalize_memory_text(decision.get("proposal", ""))
            if value:
                texts.add(value)
    return texts


def normalize_memory_text(value: Any) -> str:
    text = str(value or "").strip().strip("`").lower()
    text = re.sub(r"\s+", " ", text)
    return text


def append_memory_proposals_to_draft(draft_path: Path, records: list[dict[str, Any]], run_at: datetime) -> int:
    if not draft_path.exists() or not draft_path.is_file() or not records:
        return 0
    text = draft_path.read_text(encoding="utf-8-sig", errors="replace")
    existing_ids = set(DRAFT_PROPOSAL_ID_RE.findall(text))
    new_blocks = []
    for record in records:
        proposal_id = str(record.get("proposal_id") or record.get("id") or "").strip()
        if not proposal_id or proposal_id in existing_ids:
            continue
        new_blocks.append(format_draft_candidate_record(record))
        existing_ids.add(proposal_id)
    if not new_blocks:
        return 0
    if "## \u5f85\u786e\u8ba4\u7684\u65b0\u53d1\u73b0" not in text and "## Agent " + "\u5019\u9009\u533a" not in text:
        text = text.rstrip() + "\n\n## \u5f85\u786e\u8ba4\u7684\u65b0\u53d1\u73b0\n\n\u8fd9\u91cc\u4f1a\u51fa\u73b0\u590d\u76d8\u52a9\u624b\u6839\u636e\u65b0\u7b14\u8bb0\u53d1\u73b0\u7684\u53ef\u80fd\u8bb0\u5fc6\u3002\u4f60\u53ef\u4ee5\u628a\u201c\u72b6\u6001\u201d\u6539\u6210\u201c\u786e\u8ba4\u201d\u3001\u201c\u62d2\u7edd\u201d\u6216\u201c\u5f85\u5b9a\u201d\uff1b\u786e\u8ba4\u524d\u53ef\u4ee5\u76f4\u63a5\u6539\u201c\u5efa\u8bae\u5185\u5bb9\u201d\u3002\u5220\u9664\u4e0d\u4f1a\u88ab\u5f53\u6210\u62d2\u7edd\u3002\n"
    stamp = run_at.strftime("%Y-%m-%d %H:%M")
    addition = "\n\n### \u65b0\u5efa\u8bae " + stamp + "\n\n" + "\n".join(new_blocks)
    atomic_write_text(draft_path, text.rstrip() + addition + "\n")
    return len(new_blocks)

def format_draft_candidate_record(record: dict[str, Any]) -> str:
    proposal_id = str(record.get("proposal_id") or record.get("id") or "").strip()
    kind = str(record.get("kind") or "candidate").strip()
    target_id = str(record.get("target_id") or "uncategorized").strip() or "uncategorized"
    proposal = str(record.get("proposal") or "").strip()
    review_id = str(record.get("review_id") or "").strip()
    evidence = record.get("evidence", [])
    sources = []
    if isinstance(evidence, list):
        for item in evidence[:4]:
            if isinstance(item, dict) and item.get("path"):
                sources.append(str(item.get("path")))
    source_text = "; ".join(sources)
    lines = [
        f"- \u5efa\u8bae\u7f16\u53f7\uff1a`{proposal_id}`",
        "  - \u72b6\u6001\uff1a\u5f85\u5b9a",
        f"  - \u5efa\u8bae\u7c7b\u578b\uff1a{format_candidate_kind_label(kind)}",
        f"  - \u5efa\u8bae\u5185\u5bb9\uff1a{proposal}",
    ]
    if target_id and target_id != "uncategorized":
        lines.append(f"  - \u76f8\u5173\u5bf9\u8c61\uff1a`{target_id}`")
    if source_text:
        lines.append(f"  - \u6765\u6e90\uff1a{source_text}")
    if review_id:
        lines.append(f"  - \u590d\u76d8\u7f16\u53f7\uff1a`{review_id}`")
    return "\n".join(lines)


def format_candidate_kind_label(kind: str) -> str:
    labels = {
        "mainline_progress": "\u4e3b\u7ebf\u8fdb\u5c55",
        "mainline_gap": "\u4e3b\u7ebf\u7a7a\u7f3a",
        "mainline_next_step": "\u4e0b\u4e00\u6b65\u7ebf\u7d22",
        "new_mainline_candidate": "\u65b0\u4e3b\u7ebf",
        "workflow_preference_candidate": "\u590d\u76d8\u504f\u597d",
        "candidate": "\u5f85\u786e\u8ba4\u5efa\u8bae",
        "draft_candidate": "\u5f85\u786e\u8ba4\u5efa\u8bae",
    }
    return labels.get(str(kind or "").strip(), str(kind or "\u5f85\u786e\u8ba4\u5efa\u8bae"))

def read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UserFacingError(f"Invalid JSONL in {path} at line {lineno}: {exc}") from exc
        if isinstance(item, dict):
            records.append(item)
    return records


def write_jsonl_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        atomic_write_text(path, "")
        return
    text = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records) + "\n"
    atomic_write_text(path, text)


def safe_mainline_id(value: str) -> str:
    raw = str(value or "uncategorized").strip()
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_").lower()
    if slug:
        return slug[:80]
    return "mainline_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


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
            append_limited(item["sample_files"], rel, 12)
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
                        12,
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
                        8,
                    )

    folders = []
    for item in folder_summaries.values():
        item["activity_counts"] = dict(sorted(item["activity_counts"].items()))
        item["sample_points"] = [point for _score, point in item["sample_points"]]
        item["content_keywords"] = infer_folder_content_keywords(item)
        item["content_overview"] = infer_folder_content_overview(item)
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


ACTIVITY_LABELS = {
    "todo": "\u5f85\u529e/\u672a\u5b8c\u6210\u4e8b\u9879",
    "meeting_note": "\u4f1a\u8bae\u4e0e\u8ba8\u8bba\u8bb0\u5f55",
    "experiment_log": "\u5b9e\u9a8c\u8fc7\u7a0b\u548c\u7ed3\u679c\u5206\u6790",
    "interview_review": "\u9762\u8bd5\u51c6\u5907\u548c\u590d\u76d8",
    "paper_reading": "\u8bba\u6587/\u8d44\u6599\u9605\u8bfb",
    "reading_note": "\u9605\u8bfb\u6458\u5f55\u548c\u7b14\u8bb0",
    "daily_log": "\u65e5\u8bb0\u6216\u5468\u671f\u6027\u8bb0\u5f55",
    "project_planning": "\u9879\u76ee\u65b9\u6848/\u8bbe\u8ba1/\u63a8\u8fdb",
    "unknown": "\u672a\u5206\u7c7b\u5185\u5bb9",
}

THEME_KEYWORDS = [
    (("agent", "multi-agent", "function calling", "tool", "memory", "\u667a\u80fd\u4f53", "\u5de5\u5177\u8c03\u7528", "\u8bb0\u5fc6"), "Agent/\u667a\u80fd\u4f53"),
    (("rag", "retrieval", "\u68c0\u7d22", "\u77e5\u8bc6\u5e93", "\u5411\u91cf"), "RAG/\u68c0\u7d22\u589e\u5f3a"),
    (("llm", "transformer", "prompt", "\u5927\u6a21\u578b", "\u6a21\u578b", "\u63d0\u793a\u8bcd"), "LLM/\u5927\u6a21\u578b"),
    (("paper", "arxiv", "\u8bba\u6587", "\u6587\u732e", "\u9605\u8bfb"), "\u8bba\u6587\u9605\u8bfb"),
    (("project", "roadmap", "design", "plan", "\u9879\u76ee", "\u65b9\u6848", "\u8bbe\u8ba1", "\u89c4\u5212"), "\u9879\u76ee\u63a8\u8fdb"),
    (("interview", "\u9762\u8bd5", "\u516b\u80a1", "\u9898"), "\u9762\u8bd5\u51c6\u5907"),
    (("experiment", "ablation", "result", "\u5b9e\u9a8c", "\u6307\u6807", "\u7ed3\u679c"), "\u5b9e\u9a8c\u5206\u6790"),
    (("daily", "journal", "diary", "\u65e5\u8bb0", "\u65e5\u62a5"), "\u65e5\u5e38\u8bb0\u5f55"),
]


def infer_folder_content_keywords(folder_summary: dict[str, Any]) -> list[str]:
    corpus = folder_text_corpus(folder_summary).lower()
    labels: list[str] = []
    for needles, label in THEME_KEYWORDS:
        if any(needle.lower() in corpus for needle in needles):
            append_limited(labels, label, 6)
    for name in note_name_samples(folder_summary, 8):
        for token in re.split(r"[-_\s./\\]+", name):
            token = token.strip("`#()[]{}:,.;!????????")
            if len(token) >= 3 and not token.isdigit() and token.lower() not in {"md", "note", "paper", "project"}:
                append_limited(labels, token[:28], 6)
    return labels[:6]


def infer_folder_content_overview(folder_summary: dict[str, Any]) -> str:
    parts: list[str] = []
    activity_labels = top_activity_labels(folder_summary.get("activity_counts", {}), 3)
    keywords = folder_summary.get("content_keywords") or infer_folder_content_keywords(folder_summary)
    notes = note_name_samples(folder_summary, 4)
    headings = heading_title_samples(folder_summary, 4)
    points = point_text_samples(folder_summary, 3)
    if activity_labels:
        parts.append("\u5185\u5bb9\u7c7b\u578b\uff1a" + "\u3001".join(activity_labels))
    if keywords:
        parts.append("\u5173\u952e\u7ebf\u7d22\uff1a" + "\u3001".join(keywords[:5]))
    if notes:
        parts.append("\u4ee3\u8868\u7b14\u8bb0\uff1a" + "\u3001".join(notes[:3]))
    if headings:
        parts.append("\u5e38\u89c1\u6807\u9898\uff1a" + "\u3001".join(headings[:3]))
    if points:
        parts.append("\u5185\u5bb9\u6837\u4f8b\uff1a" + "\uff1b".join(points[:2]))
    return truncate_text("\uff1b".join(parts), 260)


def folder_text_corpus(folder_summary: dict[str, Any]) -> str:
    chunks = [str(folder_summary.get("path", ""))]
    chunks.extend(str(item) for item in folder_summary.get("sample_files", []) or [])
    for item in folder_summary.get("sample_headings", []) or []:
        if isinstance(item, dict):
            chunks.extend(str(part) for part in item.get("heading_path", []) or [])
    for item in folder_summary.get("sample_points", []) or []:
        if isinstance(item, dict):
            chunks.append(str(item.get("text", "")))
    return "\n".join(chunks)


def top_activity_labels(counts: dict[str, int], limit: int) -> list[str]:
    ranked = sorted(
        ((key, value) for key, value in (counts or {}).items() if value and key != "unknown"),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return [ACTIVITY_LABELS.get(key, key) for key, _value in ranked[:limit]]


def note_name_samples(folder_summary: dict[str, Any], limit: int) -> list[str]:
    names: list[str] = []
    for rel in folder_summary.get("sample_files", []) or []:
        name = Path(str(rel)).stem.strip()
        if name:
            append_limited(names, truncate_text(name, 34), limit)
    return names


def heading_title_samples(folder_summary: dict[str, Any], limit: int) -> list[str]:
    titles: list[str] = []
    for item in folder_summary.get("sample_headings", []) or []:
        if not isinstance(item, dict):
            continue
        heading_path = item.get("heading_path", []) or []
        if heading_path:
            title = str(heading_path[-1]).strip()
            if title:
                append_limited(titles, truncate_text(title, 34), limit)
    return titles


def point_text_samples(folder_summary: dict[str, Any], limit: int) -> list[str]:
    points: list[str] = []
    for item in folder_summary.get("sample_points", []) or []:
        if not isinstance(item, dict):
            continue
        text = clean_profile_sample_text(str(item.get("text", "")))
        if text:
            append_limited(points, truncate_text(text, 54), limit)
    return points


def clean_profile_sample_text(text: str) -> str:
    text = re.sub(r"^\s*[-*+]\s+(?:\[[ xX]\]\s*)?", "", text or "").strip()
    text = re.sub(r"[`*_>#]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def enrich_folder_role(base: str, folder_summary: dict[str, Any]) -> str:
    keywords = list(folder_summary.get("content_keywords", []) or [])[:3]
    activity_labels = top_activity_labels(folder_summary.get("activity_counts", {}), 2)
    hints = keywords or activity_labels
    if not hints:
        return base
    return truncate_text(base.rstrip("\u3002") + "\uff1b\u91cd\u70b9\u7ebf\u7d22\uff1a" + "\u3001".join(hints), 170)


def infer_folder_role_candidate(folder_summary: dict[str, Any]) -> str:
    path = str(folder_summary.get("path", ""))
    activities = folder_summary.get("activity_counts", {})
    lowered = path.lower()
    normalized = path.replace("\\", "/")
    path_roles = {
        "AI-Agent": "Agent/RAG \u76f8\u5173\u5b66\u4e60\u3001\u8bba\u6587\u7b14\u8bb0\u548c\u9762\u8bd5\u51c6\u5907\u7684\u7efc\u5408\u533a",
        "AI-Agent/Paper": "Agent \u65b9\u5411\u8bba\u6587\u9605\u8bfb\u7b14\u8bb0",
        "AI-Agent/Paper/Agent": "Agent \u673a\u5236\u3001\u5de5\u5177\u8c03\u7528\u3001\u8bb0\u5fc6\u7b49\u65b9\u5411\u7684\u8bba\u6587\u7b14\u8bb0",
        "AI-Agent/Paper/Multi-Agent": "Multi-Agent \u65b9\u5411\u8bba\u6587\u9605\u8bfb\u7b14\u8bb0",
        "AI-Agent/interview": "Agent/RAG \u9762\u8bd5\u51c6\u5907\u3001\u9762\u8bd5\u8bb0\u5f55\u548c\u590d\u76d8",
        "AI-Agent/knowledge": "Agent \u5de5\u7a0b\u77e5\u8bc6\u3001\u6846\u67b6\u8d44\u6599\u548c\u5b66\u4e60\u6574\u7406",
        "AI-Agent/knowledge/Codex": "Codex/Agent \u5de5\u5177\u4f7f\u7528\u3001\u6e90\u7801\u7406\u89e3\u548c\u5de5\u4f5c\u6d41\u6574\u7406",
        "Clippings": "\u7f51\u9875\u526a\u85cf\u548c\u5f85\u8bfb\u8d44\u6599\uff0c\u901a\u5e38\u4e0d\u7b49\u540c\u4e8e\u5df2\u7ecf\u5b8c\u6210\u7684\u5b66\u4e60\u6210\u679c",
        "LLM": "LLM \u57fa\u7840\u77e5\u8bc6\u548c\u8bba\u6587\u9605\u8bfb\u7b14\u8bb0",
        "LLM/Paper": "LLM \u8bba\u6587\u9605\u8bfb\u7b14\u8bb0",
        "MISGL": "MISGL/\u56fe\u795e\u7ecf\u7f51\u7edc\u76f8\u5173\u5b9e\u9a8c\u3001\u7ed3\u679c\u548c\u65b9\u6cd5\u8bb0\u5f55",
        "RAG": "RAG \u9879\u76ee\u3001\u8bba\u6587\u548c\u9762\u8bd5\u8868\u8fbe\u6574\u7406",
        "RAG/Paper": "RAG \u8bba\u6587\u9605\u8bfb\u7b14\u8bb0",
    }
    if normalized in path_roles:
        return enrich_folder_role(path_roles[normalized], folder_summary)
    if "interview" in lowered or "\u9762\u8bd5" in path:
        return enrich_folder_role("\u9762\u8bd5\u51c6\u5907\u3001\u9762\u8bd5\u8bb0\u5f55\u548c\u590d\u76d8", folder_summary)
    if "clippings" in lowered or "\u526a\u85cf" in path:
        return enrich_folder_role("\u7f51\u9875\u526a\u85cf\u548c\u5f85\u8bfb\u8d44\u6599", folder_summary)
    if "paper" in lowered or "\u8bba\u6587" in path:
        return enrich_folder_role("\u8bba\u6587\u9605\u8bfb\u7b14\u8bb0", folder_summary)
    if "project" in lowered or "\u9879\u76ee" in path:
        return enrich_folder_role("\u9879\u76ee\u65b9\u6848\u3001\u8bbe\u8ba1\u601d\u8def\u548c\u63a8\u8fdb\u8bb0\u5f55", folder_summary)
    if activities.get("paper_reading", 0) >= 3:
        return enrich_folder_role("\u8bba\u6587\u6216\u8d44\u6599\u9605\u8bfb\u7b14\u8bb0", folder_summary)
    if activities.get("project_planning", 0) >= 3:
        return enrich_folder_role("\u9879\u76ee\u65b9\u6848\u3001\u8bbe\u8ba1\u601d\u8def\u548c\u63a8\u8fdb\u8bb0\u5f55", folder_summary)
    if activities.get("experiment_log", 0) >= 3:
        return enrich_folder_role("\u5b9e\u9a8c\u8fc7\u7a0b\u3001\u7ed3\u679c\u548c\u5206\u6790\u8bb0\u5f55", folder_summary)
    if activities.get("interview_review", 0) >= 3:
        return enrich_folder_role("\u9762\u8bd5\u51c6\u5907\u548c\u590d\u76d8\u8bb0\u5f55", folder_summary)
    if activities.get("daily_log", 0) >= 3:
        return enrich_folder_role("\u65e5\u8bb0\u6216\u5468\u671f\u6027\u8bb0\u5f55", folder_summary)
    overview = str(folder_summary.get("content_overview", "")).strip()
    if overview:
        return "\u57fa\u4e8e\u6587\u4ef6\u5185\u5bb9\u7684\u5019\u9009\u7528\u9014\uff1a" + overview
    return "\u7528\u9014\u4e0d\u660e\u786e\uff0c\u9700\u8981\u7528\u6237\u8865\u5145\u8bf4\u660e"


def format_profile_table_cell(value: Any, limit: int = 180) -> str:
    text = truncate_text(str(value or "").replace("\n", " ").strip(), limit)
    text = text.replace("|", "\\|")
    return text or "\u5f85\u8865\u5145"


def folder_display_role(folder_summary: dict[str, Any]) -> str:
    base = concise_role_base(folder_summary)
    focus = concise_folder_focus(folder_summary)
    if focus and focus not in base:
        return truncate_text(f"{base}\uff1a{focus}", 150)
    return truncate_text(base, 150)


def concise_role_base(folder_summary: dict[str, Any]) -> str:
    path = str(folder_summary.get("path", ""))
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    activities = folder_summary.get("activity_counts", {}) or {}
    if "clippings" in lowered or "\u526a\u85cf" in path:
        return "\u7f51\u9875\u526a\u85cf\u4e0e\u5f85\u8bfb\u8d44\u6599\u6c60"
    if "\u65e5\u8bb0" in path or "diary" in lowered or "journal" in lowered:
        return "\u65e5\u8bb0\u4e0e\u9636\u6bb5\u6027\u8bb0\u5f55"
    if "interview" in lowered or "\u9762\u8bd5" in path:
        return "\u9762\u8bd5\u51c6\u5907\u4e0e\u590d\u76d8"
    if normalized == "2-AI-Agent":
        return "Agent \u5b66\u4e60\u3001\u8bba\u6587\u4e0e\u9762\u8bd5\u6750\u6599"
    if normalized.startswith("2-AI-Agent/2-Paper"):
        return "Agent \u8bba\u6587\u9605\u8bfb"
    if normalized.startswith("2-AI-Agent/knowledge"):
        return "Agent \u5de5\u7a0b\u77e5\u8bc6\u4e0e\u5de5\u5177\u6574\u7406"
    if normalized == "3-RAG":
        return "RAG \u8bba\u6587\u4e0e\u9879\u76ee\u6750\u6599"
    if normalized.startswith("3-RAG/Paper"):
        return "RAG \u8bba\u6587\u9605\u8bfb"
    if normalized == "4-LLM":
        return "LLM \u57fa\u7840\u4e0e\u8bba\u6587\u9605\u8bfb"
    if normalized.startswith("4-LLM/Paper"):
        return "LLM \u8bba\u6587\u9605\u8bfb"
    if "MISGL" in normalized or "misgl" in lowered:
        return "MISGL/\u56fe\u5b66\u4e60\u5b9e\u9a8c\u4e0e\u65b9\u6cd5\u5206\u6790"
    if normalized == "1-Project":
        return "\u9879\u76ee\u65b9\u6848\u3001\u8bbe\u8ba1\u4e0e\u8fdb\u5c55"
    if normalized.startswith("1-Project/1-personal-review-agent"):
        return "Personal Review Agent \u9879\u76ee\u8bbe\u8ba1\u4e0e\u8fed\u4ee3"
    if normalized.startswith("1-Project/2-Paper-RAG"):
        return "RAG \u9879\u76ee\u8bba\u6587\u4e0e\u65b9\u6848\u6750\u6599"
    if "project" in lowered or "\u9879\u76ee" in path or activities.get("project_planning", 0) >= 3:
        return "\u9879\u76ee\u65b9\u6848\u3001\u8bbe\u8ba1\u4e0e\u8fdb\u5c55"
    if "paper" in lowered or "\u8bba\u6587" in path or activities.get("paper_reading", 0) >= 3:
        return "\u8bba\u6587\u4e0e\u8d44\u6599\u9605\u8bfb"
    if activities.get("experiment_log", 0) >= 3:
        return "\u5b9e\u9a8c\u8bb0\u5f55\u4e0e\u7ed3\u679c\u5206\u6790"
    if activities.get("daily_log", 0) >= 3:
        return "\u65e5\u5e38\u8bb0\u5f55\u4e0e\u5468\u671f\u590d\u76d8"
    return "\u7efc\u5408\u7b14\u8bb0\u4e0e\u5b66\u4e60\u6574\u7406"


def concise_folder_focus(folder_summary: dict[str, Any]) -> str:
    corpus = folder_text_corpus(folder_summary).lower()
    labels = []
    if "personal-review-agent" in corpus or "review agent" in corpus:
        labels.append("Personal Review Agent")
    if any(k in corpus for k in ("agent", "multi-agent", "\u667a\u80fd\u4f53")):
        labels.append("Agent")
    if any(k in corpus for k in ("rag", "retrieval", "\u68c0\u7d22", "\u77e5\u8bc6\u5e93")):
        labels.append("RAG")
    if any(k in corpus for k in ("llm", "transformer", "\u5927\u6a21\u578b")):
        labels.append("LLM")
    if any(k in corpus for k in ("misgl", "gnn", "\u56fe\u795e\u7ecf")):
        labels.append("\u56fe\u5b66\u4e60/MISGL")
    deduped = []
    for label in labels:
        if label not in deduped:
            deduped.append(label)
    if deduped:
        return " / ".join(deduped[:3])
    notes = note_name_samples(folder_summary, 2)
    return "\u3001".join(notes[:2])


def mainline_description_for_folder(folder_summary: dict[str, Any], cluster: str | None = None) -> str:
    path = str(folder_summary.get("path") or "").strip()
    focus = concise_folder_focus(folder_summary)
    base = concise_role_base(folder_summary)
    source = f"\u6765\u6e90\uff1a`{path}`" if path else "\u6765\u6e90\uff1a\u521d\u59cb\u626b\u63cf"
    cluster = cluster or mainline_cluster_for_folder(folder_summary, infer_mainline_name_for_folder(folder_summary))
    special = {
        "graph_learning": "\u56f4\u7ed5 MISGL/\u56fe\u5b66\u4e60\u5b9e\u9a8c\u3001\u65b9\u6cd5\u5206\u6790\u548c\u7ed3\u679c\u8bb0\u5f55\uff1b\u4e0e Agent/RAG \u4e3b\u7ebf\u5206\u5f00\u8ddf\u8e2a\u3002",
        "interview_preparation": "\u56f4\u7ed5\u9762\u8bd5\u51c6\u5907\u3001\u9898\u76ee\u6574\u7406\u3001\u8868\u8fbe\u6253\u78e8\u548c\u9636\u6bb5\u6027\u590d\u76d8\u3002",
        "personal_review_agent_project": "\u56f4\u7ed5 Personal Review Agent \u7684\u9879\u76ee\u8bbe\u8ba1\u3001\u8bb0\u5fc6\u673a\u5236\u548c\u8fed\u4ee3\u63a8\u8fdb\u3002",
        "rag_project_learning": "\u56f4\u7ed5 RAG \u9879\u76ee\u65b9\u6848\u3001\u8bba\u6587\u9605\u8bfb\u548c\u68c0\u7d22\u589e\u5f3a\u76f8\u5173\u6750\u6599\u3002",
        "agent_paper_learning": "\u56f4\u7ed5 Agent/Multi-Agent \u8bba\u6587\u9605\u8bfb\u3001\u673a\u5236\u7406\u89e3\u548c\u65b9\u6cd5\u6574\u7406\u3002",
        "agent_engineering": "\u56f4\u7ed5 Agent \u5de5\u7a0b\u77e5\u8bc6\u3001\u5de5\u5177\u94fe\u3001\u6846\u67b6\u8d44\u6599\u548c\u5b9e\u8df5\u6574\u7406\u3002",
        "llm_learning": "\u56f4\u7ed5 LLM \u57fa\u7840\u3001\u8bba\u6587\u9605\u8bfb\u548c\u6a21\u578b\u673a\u5236\u5b66\u4e60\u3002",
        "project_planning": "\u56f4\u7ed5\u9879\u76ee\u65b9\u6848\u3001\u8bbe\u8ba1\u51b3\u7b56\u3001\u63a8\u8fdb\u8bb0\u5f55\u548c\u9636\u6bb5\u6210\u679c\u3002",
    }.get(cluster)
    if special:
        return truncate_text(f"{source}\uff1b{special}", 180)
    if focus:
        return truncate_text(f"{source}\uff1b\u4e3b\u8981\u56f4\u7ed5 {focus}\uff1b\u7528\u9014\u503e\u5411\uff1a{base}\u3002", 170)
    return truncate_text(f"{source}\uff1b\u7528\u9014\u503e\u5411\uff1a{base}\u3002", 170)

def render_vault_profile_draft(summary: dict[str, Any]) -> str:
    folders = summary.get("folder_summaries", [])
    mainlines = infer_active_mainlines_for_draft(folders)
    lines = [
        "# \u6211\u7684\u590d\u76d8\u8bb0\u5fc6",
        "",
        "\u8fd9\u4efd\u6587\u4ef6\u7528\u6765\u6821\u51c6\u590d\u76d8\u52a9\u624b\u5bf9\u4f60 Obsidian \u7684\u7406\u89e3\u3002\u4f60\u53ef\u4ee5\u76f4\u63a5\u4fee\u6539\u4e0b\u9762\u7684\u5185\u5bb9\uff1b\u4e4b\u540e\u7684\u590d\u76d8\u4f1a\u4f18\u5148\u6309\u8fd9\u91cc\u6765\u7406\u89e3\u4f60\u7684\u9879\u76ee\u3001\u5b66\u4e60\u4e3b\u9898\u548c\u957f\u671f\u4e3b\u7ebf\u3002",
        "",
        "## \u5df2\u786e\u8ba4\u7684\u7406\u89e3",
        "",
        "### \u6587\u4ef6\u5939\u7528\u9014",
        "",
        "| \u6587\u4ef6\u5939 | \u8fd9\u4e2a\u6587\u4ef6\u5939\u4e3b\u8981\u653e\u4ec0\u4e48 |",
        "|---|---|",
    ]
    for item in folders:
        lines.append(f"| `{item.get('path')}` | {format_profile_table_cell(folder_display_role(item))} |")
    lines += [
        "",
        "### \u957f\u671f\u4e3b\u7ebf",
        "",
    ]
    for item in mainlines:
        lines.extend(format_mainline_draft_item(item))
    lines += [
        "",
        "## \u5f85\u786e\u8ba4\u7684\u65b0\u53d1\u73b0",
        "",
        "\u8fd9\u91cc\u4f1a\u51fa\u73b0\u590d\u76d8\u52a9\u624b\u6839\u636e\u65b0\u7b14\u8bb0\u53d1\u73b0\u7684\u53ef\u80fd\u8bb0\u5fc6\u3002\u4f60\u53ef\u4ee5\u628a\u201c\u72b6\u6001\u201d\u6539\u6210\u201c\u786e\u8ba4\u201d\u3001\u201c\u62d2\u7edd\u201d\u6216\u201c\u5f85\u5b9a\u201d\uff1b\u786e\u8ba4\u524d\u53ef\u4ee5\u76f4\u63a5\u6539\u201c\u5efa\u8bae\u5185\u5bb9\u201d\u3002\u5220\u9664\u4e0d\u4f1a\u88ab\u5f53\u6210\u62d2\u7edd\u3002",
        "",
        "### \u65b0\u6587\u4ef6\u5939",
        "",
        "\u6682\u65e0\u65b0\u7684\u5f85\u786e\u8ba4\u5efa\u8bae\u3002",
        "",
        "### \u65b0\u4e3b\u7ebf",
        "",
        "\u6682\u65e0\u65b0\u7684\u5f85\u786e\u8ba4\u5efa\u8bae\u3002",
        "",
        "### \u5bf9\u5df2\u6709\u7406\u89e3\u7684\u4fee\u6539\u5efa\u8bae",
        "",
        "\u6682\u65e0\u65b0\u7684\u5f85\u786e\u8ba4\u5efa\u8bae\u3002",
    ]
    return "\n".join(lines)


def format_mainline_draft_item(item: dict[str, str]) -> list[str]:
    name = item.get("name", "").strip()
    description = item.get("description", "").strip()
    source = ""
    body = description
    match = re.match(r"\u6765\u6e90\uff1a(`[^`]+`|[^\uff1b]+)\uff1b(.+)", description)
    if match:
        source = match.group(1).strip()
        body = match.group(2).strip()
    body = body.rstrip("\u3002") + "\u3002" if body else "\u5f85\u4f60\u8865\u5145\u8fd9\u6761\u4e3b\u7ebf\u7684\u542b\u4e49\u3002"
    lines = [f"- **{name}**", f"  - \u590d\u76d8\u52a9\u624b\u7684\u7406\u89e3\uff1a{body}"]
    if source:
        lines.append(f"  - \u76f8\u5173\u6587\u4ef6\u5939\uff1a{source}")
    return lines

def infer_active_mainlines_for_draft(folders: list[dict[str, Any]]) -> list[dict[str, str]]:
    candidates: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    has_specific_candidates = any(
        int(item.get("depth") or 0) >= 2
        and (item.get("sample_points") or item.get("sample_headings"))
        and not is_low_value_mainline_folder(str(item.get("path") or ""))
        for item in folders
    )
    for item in folders:
        if not item.get("sample_points") and not item.get("sample_headings"):
            continue
        path = str(item.get("path") or "")
        if is_low_value_mainline_folder(path):
            continue
        if has_specific_candidates and is_broad_mainline_parent(path):
            continue
        name = infer_mainline_name_for_folder(item)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        cluster = mainline_cluster_for_folder(item, name)
        candidates.append(
            {
                "score": mainline_candidate_score(item),
                "cluster": cluster,
                "name": name,
                "path": str(item.get("path") or ""),
                "description": mainline_description_for_folder(item, cluster),
            }
        )
    candidates.sort(key=lambda item: (-int(item["score"]), mainline_cluster_rank(str(item["cluster"])), item["name"]))
    selected: list[dict[str, str]] = []
    selected_clusters: set[str] = set()
    for item in candidates:
        cluster = str(item["cluster"])
        if cluster in selected_clusters:
            continue
        selected.append({"name": item["name"], "description": item["description"]})
        selected_clusters.add(cluster)
        if len(selected) >= 6:
            break
    if len(selected) < 4:
        for item in candidates:
            pair = {"name": item["name"], "description": item["description"]}
            if pair in selected:
                continue
            selected.append(pair)
            if len(selected) >= 4:
                break
    if selected:
        return selected[:6]
    return [
        {
            "name": "\u4e3b\u7ebf\u5f85\u786e\u8ba4",
            "description": "\u5df2\u5c1d\u8bd5\u626b\u63cf\u6587\u4ef6\u5185\u5bb9\uff0c\u4f46\u672a\u627e\u5230\u8db3\u591f\u7a33\u5b9a\u7684\u4e3b\u7ebf\u7ebf\u7d22\uff1b\u8bf7\u5728\u8fd9\u91cc\u6539\u6210\u4f60\u7684\u771f\u5b9e\u4e3b\u7ebf\u3002",
        }
    ]


def is_low_value_mainline_folder(path: str) -> bool:
    lowered = str(path or "").lower()
    return any(token in lowered for token in ("clippings", "archive", "trash", "\u526a\u85cf", "\u5f52\u6863", "\u56de\u6536"))


def is_broad_mainline_parent(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/")
    return normalized in {"1-Project", "2-AI-Agent", "3-RAG", "4-LLM"}


def infer_mainline_name_for_folder(folder_summary: dict[str, Any]) -> str:
    corpus = mainline_signal_corpus(folder_summary).lower()
    path = str(folder_summary.get("path") or "").strip()
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    if is_interview_folder(folder_summary):
        return "\u9762\u8bd5\u51c6\u5907\u4e0e\u590d\u76d8"
    if is_graph_learning_folder(folder_summary):
        return "\u56fe\u5b66\u4e60/MISGL \u5b9e\u9a8c\u4e0e\u65b9\u6cd5\u5206\u6790"
    if "personal-review-agent" in lowered or "review agent" in corpus:
        return "Personal Review Agent \u9879\u76ee\u8bbe\u8ba1\u4e0e\u8fed\u4ee3"
    if normalized.startswith("1-Project/2-Paper-RAG"):
        return "RAG \u9879\u76ee\u8bba\u6587\u4e0e\u65b9\u6848\u6750\u6599"
    if normalized == "3-RAG" or normalized.startswith("3-RAG/"):
        return "RAG \u8bba\u6587\u4e0e\u9879\u76ee\u6574\u7406"
    if normalized.startswith("2-AI-Agent/2-Paper"):
        return "Agent \u8bba\u6587\u4e0e\u673a\u5236\u5b66\u4e60"
    if normalized.startswith("2-AI-Agent/knowledge"):
        return "Agent \u5de5\u7a0b\u77e5\u8bc6\u4e0e\u5de5\u5177\u6574\u7406"
    if normalized == "4-LLM" or normalized.startswith("4-LLM/"):
        return "LLM \u8bba\u6587\u4e0e\u57fa\u7840\u5b66\u4e60"
    if any(k in corpus for k in ("experiment", "ablation", "\u5b9e\u9a8c", "\u6307\u6807", "\u7ed3\u679c")):
        return "\u5b9e\u9a8c\u8bb0\u5f55\u4e0e\u65b9\u6cd5\u5206\u6790"
    if any(k in corpus for k in ("project", "roadmap", "design", "\u9879\u76ee", "\u65b9\u6848", "\u8bbe\u8ba1")):
        return "\u9879\u76ee\u65b9\u6848\u4e0e\u63a8\u8fdb"
    if any(k in corpus for k in ("agent", "multi-agent", "\u667a\u80fd\u4f53", "tool", "memory")):
        return "Agent \u673a\u5236\u3001\u5de5\u5177\u548c\u8bb0\u5fc6\u65b9\u5411"
    if any(k in corpus for k in ("rag", "retrieval", "\u68c0\u7d22", "\u77e5\u8bc6\u5e93")):
        return "RAG \u9879\u76ee\u4e0e\u8bba\u6587\u6574\u7406"
    if any(k in corpus for k in ("llm", "transformer", "\u5927\u6a21\u578b", "\u6a21\u578b")):
        return "LLM \u57fa\u7840\u4e0e\u8bba\u6587\u9605\u8bfb"
    role = str(folder_summary.get("role_candidate") or "").split("\u3002", 1)[0].strip()
    if role and "\u7528\u9014\u4e0d\u660e\u786e" not in role:
        return truncate_text(role, 32)
    if path and path != "(vault root)":
        return truncate_text(path.replace("/", " / ") + " \u76f8\u5173\u6574\u7406", 32)
    return ""


def mainline_cluster_for_folder(folder_summary: dict[str, Any], name: str = "") -> str:
    path = str(folder_summary.get("path") or "").replace("\\", "/")
    lowered = path.lower()
    corpus = mainline_signal_corpus(folder_summary).lower()
    if is_interview_folder(folder_summary):
        return "interview_preparation"
    if is_graph_learning_folder(folder_summary):
        return "graph_learning"
    if "personal-review-agent" in lowered or "review agent" in corpus:
        return "personal_review_agent_project"
    if path.startswith("1-Project/2-Paper-RAG") or path == "3-RAG" or path.startswith("3-RAG/"):
        return "rag_project_learning"
    if path.startswith("2-AI-Agent/2-Paper"):
        return "agent_paper_learning"
    if path.startswith("2-AI-Agent/knowledge"):
        return "agent_engineering"
    if path == "4-LLM" or path.startswith("4-LLM/"):
        return "llm_learning"
    if "project" in lowered or "\u9879\u76ee" in path:
        return "project_planning"
    if "paper" in lowered or "\u8bba\u6587" in path:
        return "paper_learning"
    if any(k in corpus for k in ("rag", "retrieval", "\u68c0\u7d22", "\u77e5\u8bc6\u5e93")):
        return "rag_project_learning"
    if any(k in corpus for k in ("agent", "multi-agent", "\u667a\u80fd\u4f53")):
        return "agent_general"
    if name:
        return safe_mainline_id(name)
    return "general"


def mainline_signal_corpus(folder_summary: dict[str, Any]) -> str:
    chunks = [str(folder_summary.get("path", ""))]
    for item in folder_summary.get("sample_headings", []) or []:
        if isinstance(item, dict):
            chunks.extend(str(part) for part in item.get("heading_path", []) or [])
    for item in folder_summary.get("sample_points", []) or []:
        if isinstance(item, dict):
            chunks.append(str(item.get("text", "")))
    return "\n".join(chunks)


def is_graph_learning_folder(folder_summary: dict[str, Any]) -> bool:
    path = str(folder_summary.get("path") or "")
    lowered = path.lower()
    corpus = mainline_signal_corpus(folder_summary).lower()
    depth = int(folder_summary.get("depth") or 0)
    graph_terms = ("misgl", "gnn", "graph learning", "\u56fe\u5b66\u4e60", "\u56fe\u795e\u7ecf", "\u56fe\u795e\u7ecf\u7f51\u7edc")
    if any(term in lowered for term in ("misgl", "gnn")) or "\u56fe\u5b66\u4e60" in path or "\u56fe\u795e\u7ecf" in path:
        return True
    return depth >= 2 and any(k in corpus for k in graph_terms)


def is_interview_folder(folder_summary: dict[str, Any]) -> bool:
    path = str(folder_summary.get("path") or "")
    lowered = path.lower()
    corpus = mainline_signal_corpus(folder_summary).lower()
    activities = folder_summary.get("activity_counts", {}) or {}
    block_count = max(int(folder_summary.get("block_count") or 0), 1)
    if "interview" in lowered or "\u9762\u8bd5" in path:
        return True
    if any(k in corpus for k in ("interview", "\u9762\u8bd5", "\u516b\u80a1", "\u590d\u76d8\u9898")):
        return int(activities.get("interview_review", 0)) * 2 >= block_count
    return int(activities.get("interview_review", 0)) >= 3 and int(activities.get("interview_review", 0)) * 2 >= block_count


def mainline_cluster_rank(cluster: str) -> int:
    ranks = {
        "personal_review_agent_project": 0,
        "graph_learning": 1,
        "rag_project_learning": 2,
        "agent_paper_learning": 3,
        "interview_preparation": 4,
        "agent_engineering": 5,
        "llm_learning": 6,
        "project_planning": 7,
    }
    return ranks.get(cluster, 20)


def folder_name_semantic_score(folder_summary: dict[str, Any]) -> int:
    path = str(folder_summary.get("path") or "").replace("\\", "/")
    lowered = path.lower()
    leaf = lowered.rsplit("/", 1)[-1]
    score = 0
    weighted_terms = [
        (("misgl", "gnn", "\u56fe\u5b66\u4e60", "\u56fe\u795e\u7ecf"), 22),
        (("interview", "\u9762\u8bd5", "\u516b\u80a1"), 22),
        (("personal-review-agent", "review-agent"), 20),
        (("paper-rag",), 16),
        (("knowledge", "\u77e5\u8bc6"), 14),
        (("multi-agent",), 14),
        (("paper", "\u8bba\u6587"), 12),
        (("rag", "retrieval", "\u68c0\u7d22"), 12),
        (("llm", "\u5927\u6a21\u578b"), 12),
        (("project", "\u9879\u76ee"), 8),
    ]
    for terms, value in weighted_terms:
        if any(term in lowered for term in terms):
            score = max(score, value)
        if any(term in leaf for term in terms):
            score = max(score, value + 4)
    if path and path != "(vault root)":
        score += min(path.count("/") + 1, 3) * 2
    return score


def mainline_candidate_score(folder_summary: dict[str, Any]) -> int:
    block_count = min(int(folder_summary.get("block_count") or 0), 40)
    score = block_count
    activities = folder_summary.get("activity_counts", {}) or {}
    for key in ("project_planning", "paper_reading", "experiment_log", "interview_review"):
        score += min(int(activities.get(key, 0)), 10) * 4
    score += len(folder_summary.get("sample_points", []) or []) * 2
    score += folder_name_semantic_score(folder_summary)
    path = str(folder_summary.get("path") or "").replace("\\", "/")
    depth = int(folder_summary.get("depth") or 0)
    if depth <= 1:
        score -= 24
    elif depth == 2:
        score += 8
    else:
        score += 10
    if is_graph_learning_folder(folder_summary) or is_interview_folder(folder_summary):
        score += 18
    if "personal-review-agent" in path:
        score += 28
    if any(token in path for token in ("Paper-RAG", "MISGL", "interview", "knowledge")):
        score += 10
    if path in {"1-Project", "2-AI-Agent", "3-RAG", "4-LLM"}:
        score -= 24
    return score

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
    calibration["folder_roles"] = calibration.get("folder_roles") or extract_markdown_subsection(draft_text, "文件夹用途")
    calibration["active_mainlines"] = calibration.get("active_mainlines") or extract_markdown_subsection(draft_text, "长期主线")
    calibration["review_preferences"] = calibration.get("review_preferences") or extract_markdown_subsection(draft_text, "复盘偏好")
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


def allocate_report_path(vault: Path, output_dir: Path, period: str, run_at: datetime, review_id: str = "") -> Path:
    date_part = run_at.date().isoformat()
    id_part = review_id.rsplit("_", 1)[-1] if review_id else ""
    stem = f"{date_part}_{period}_{id_part}_review" if id_part else f"{date_part}_{period}_review"
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
            "Folder roles, long-term goals, and active mainlines from vault_profile.draft.md 用户确认区 must not be overwritten. "
            "Write new evidence only as suggestions in vault_profile_update.latest.json and Agent 候选区."
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


def write_runtime_l1_context(
    vault: Path,
    config: dict[str, Any],
    profile: dict[str, Any],
    run_at: datetime,
) -> Path:
    state_dir = vault / config.get("state_dir", ".obsidian-review-agent")
    memory_dir = state_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / "l1_context.md"
    draft_rel = config.get("profile_draft_dir", "Reviews/_AgentProfile") + "/vault_profile.draft.md"
    mainlines = profile.get("active_mainlines", []) if isinstance(profile, dict) else []
    prefs = profile.get("review_preferences", []) if isinstance(profile, dict) else []

    lines = [
        "# Review Agent L1",
        "",
        "L0: memory/obsidian_memory_management_sop.md",
        f"ProfileDraft: {draft_rel}",
        "VaultProfileCache: .obsidian-review-agent/vault_profile.confirmed.json",
        "Mainlines: .obsidian-review-agent/memory/mainlines_registry.json",
        "SOPs: .obsidian-review-agent/memory/sops/*.md",
        "Pending: .obsidian-review-agent/memory/profile_updates.pending.jsonl",
        "History: .obsidian-review-agent/memory/profile_updates.history.jsonl",
        f"GeneratedAt: {run_at.isoformat()}",
        "",
        "RULES:",
        "- ProfileDraft 用户确认区才是最高优先级事实；Agent 候选区只是弱信号。",
        "- 候选决策以 proposal_id + status 为准：confirmed/rejected/pending；删除不等于拒绝。",
        "- 新主线/文件夹用途/用户偏好/长期目标只能追加到 Agent 候选区，不能自动改用户确认区。",
        "- 周复盘先读 review_digest.latest.json；证据不清时再查 changed_blocks.latest.json。",
        "",
        "活跃主线:",
    ]
    if mainlines:
        for entry in mainlines[:20]:
            label = extract_profile_entry_label(entry)
            if label:
                lines.append(f"- {label}")
    else:
        lines.append("- 暂无已确认主线")

    lines += ["", "复盘偏好:"]
    if prefs:
        for entry in prefs[:10]:
            label = extract_profile_entry_label(entry)
            if label:
                lines.append(f"- {label}")
    else:
        lines += [
            "- 结论先行",
            "- 按长期主线组织",
            "- 避免逐条 block 罗列",
        ]
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path


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
        "status": "suggested_and_may_be_appended_to_profile_draft",
        "policy": "Append candidates to Reviews/_AgentProfile/vault_profile.draft.md Agent 候选区. Do not modify 用户确认区 automatically.",
        "suggestions": suggestions[:20],
    }


def append_profile_candidates_to_draft(
    draft_path: Path,
    suggestions_payload: dict[str, Any],
    run_at: datetime,
) -> int:
    if not draft_path.exists() or not draft_path.is_file():
        return 0
    suggestions = suggestions_payload.get("suggestions", [])
    if not isinstance(suggestions, list) or not suggestions:
        return 0

    text = draft_path.read_text(encoding="utf-8-sig", errors="replace")
    new_lines: list[str] = []
    for item in suggestions:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "candidate")).strip()
        topic = str(item.get("topic") or item.get("path") or item.get("proposal") or "").strip()
        if not topic or topic in text:
            continue
        sources = item.get("sources", [])
        source_text = ""
        if isinstance(sources, list) and sources:
            source_text = "；来源：" + "、".join(str(s) for s in sources[:3])
        if kind == "new_topic_candidate":
            new_lines.append(f"- [ ] 新主线候选：`{topic}`（低置信，需要你按需修改/删除）{source_text}")
        else:
            new_lines.append(f"- [ ] {kind}：`{topic}`{source_text}")

    if not new_lines:
        return 0

    if "## Agent 候选区" not in text:
        text = text.rstrip() + "\n\n## Agent 候选区\n\n> 以下内容由 Agent 自动追加。确认请把 status 改为 confirmed，拒绝请改为 rejected；保留 pending 表示暂不处理。删除不等于拒绝。\n"

    stamp = run_at.strftime("%Y-%m-%d %H:%M")
    addition = "\n\n### 自动候选 " + stamp + "\n\n" + "\n".join(new_lines)
    atomic_write_text(draft_path, text.rstrip() + addition + "\n")
    return len(new_lines)


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
        "每个有变化的用户确认区主题都要总结一条逻辑线：本周期哪些文件变化、它们共同说明什么进展、下一步是什么。",
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
