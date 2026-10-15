"""Read-only historical tag cost/coverage replay. No network or model calls.

Run with uv run --no-sync python scripts/replay_tag_jobs.py --output <report.json>.
Payload sizes are measured characters/UTF-8 bytes, never provider token estimates.
"""
from argparse import ArgumentParser
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

import asyncio
import hashlib
import json
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main() -> None:
    from src.services.tag_conflict_service import decide_tag_conflicts, plan_conflict_pairs
    from src.services.tag_memory_store import TagCandidate, TagMemoryStore

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/qq-agent/memory_index.db")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-source", type=Path, required=True,
                        help="Backup tag_conflict_service.py used before this change")
    parser.add_argument("--legacy-prompt", type=Path, required=True)
    args = parser.parse_args()
    # Import baseline from its verified backup without changing the working tree.
    import importlib.util
    spec = importlib.util.spec_from_file_location("src.services._tag_conflict_baseline", args.legacy_source)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    old_template = args.legacy_prompt.read_text(encoding="utf-8")
    new_template = (ROOT / "prompts/zh-CN/tag_conflict.prompt").read_text(encoding="utf-8")
    real_usage: Dict[str, Dict[str, int]] = {}
    failed_ids = set()
    for name in ("app_20261015_161020.log.jsonl", "app_20261015_161021.log.jsonl", "app_20261015_161029.log.jsonl"):
        for line in (ROOT / "logs" / name).read_text(encoding="utf-8").splitlines():
            event = json.loads(line).get("event", "")
            match = re.search(r"类型: ([^,]+), 提示词: (\d+), 完成: (\d+), 总计: (\d+)", event)
            if match:
                task, input_, output, total = match.groups()
                usage = real_usage.setdefault(task, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                for key, value in zip(usage, (1, int(input_), int(output), int(total)), strict=True):
                    usage[key] += value
            match = re.search(r"批量标签提取失败: session_id=\w+ job_id=(\w+)", event)
            if match:
                failed_ids.add(match.group(1))
    with TemporaryDirectory(prefix="tag-offline-replay-") as directory:
        path = Path(directory) / "memory.db"
        with closing(sqlite3.connect("file:" + str(args.database.resolve()) + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(path)) as target:
                source.backup(target)
        store = TagMemoryStore(path)
        store.initialize()  # Migrate only the temporary copy.
        with store._connect() as connection:
            historical_failures = []
            for job_id in sorted(failed_ids):
                job = connection.execute("SELECT * FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
                if job is None:
                    historical_failures.append({"job_id": job_id, "status": "not_in_queue", "requires_batch_lookup": True})
                    continue
                payload = json.loads(job["payload"])
                ids = [message["message_id"] for message in payload["messages"]]
                params = (job["session_id"], json.dumps(ids))
                pending = connection.execute("SELECT COUNT(*) FROM tag_inbox WHERE session_id=? AND message_id IN (SELECT value FROM json_each(?))", params).fetchone()[0]
                committed = connection.execute("SELECT COUNT(DISTINCT s.message_id) FROM batch_sources s JOIN batches b ON b.id=s.batch_id WHERE b.session_id=? AND s.message_id IN (SELECT value FROM json_each(?))", params).fetchone()[0]
                frozen = store.freeze_extraction_job(job_id)  # Copy only; production remains failed.
                snapshot = json.loads(frozen["payload"])
                historical_failures.append({"job_id": job_id, "session_id": job["session_id"], "attempts": job["attempts"],
                    "status": job["status"], "message_count": len(ids), "pending_messages": pending,
                    "messages_found_in_any_committed_batch": committed, "previous_batch_id": payload["previous_batch_id"],
                    "legacy_snapshot_migrates": snapshot["snapshot_origin"] == "legacy_first_resume"})
        # Each sample is reconstructed at a batch's creation time. Exact historical prompts and
        # tag status/revision snapshots are unavailable; do not compare these sizes to billed tokens.
        samples: List[Dict[str, Any]] = []
        with store._connect() as connection:
            # Explicit date conversion avoids depending on the shell timezone.
            from datetime import datetime
            begin = datetime.fromisoformat("2026-10-15T16:10:20+08:00").timestamp()
            end = datetime.fromisoformat("2026-10-16T03:36:00+08:00").timestamp()
            batches = [dict(row) for row in connection.execute("SELECT * FROM batches WHERE external_id LIKE 'extract:%' AND created_at BETWEEN ? AND ? ORDER BY created_at", (begin, end))]
        for batch in batches:
            # Include superseded/disputed history too for a conservative candidate coverage check.
            with store._connect() as connection:
                old_rows = connection.execute("SELECT t.*,c.path FROM tags t JOIN categories c ON c.id=t.category_id WHERE t.session_id=? AND t.created_at<?", (batch["session_id"], batch["created_at"])).fetchall()
                old_tags = []
                for row in old_rows:
                    item = dict(row)
                    item["category_path"] = json.loads(item.pop("path"))
                    item["evidence"] = [dict(value) for value in connection.execute("SELECT message_id,quote FROM tag_sources WHERE tag_id=?", (item["id"],))]
                    old_tags.append(item)
                new_rows = connection.execute("SELECT t.*,c.path FROM tags t JOIN categories c ON c.id=t.category_id WHERE t.batch_id=? ORDER BY t.id", (batch["id"],)).fetchall()
                new = []
                new_ids = []
                for row in new_rows:
                    quotes = {value["message_id"]: value["quote"] for value in connection.execute("SELECT message_id,quote FROM tag_sources WHERE tag_id=?", (row["id"],))}
                    new.append(TagCandidate(row["name"], row["summary"], tuple(json.loads(row["path"])), list(quotes), evidence_quotes=quotes))
                    new_ids.append(row["id"])
                recorded = [dict(row) for row in connection.execute("SELECT old_tag_id,new_tag_id,decision,reason FROM tag_conflicts WHERE batch_id=?", (batch["id"],))]
            if not new or not old_tags:
                continue
            old_prompts = []
            async def legacy_generate(prompt, capture=old_prompts):
                capture.append(prompt)
                from types import SimpleNamespace
                return SimpleNamespace(generation_attempts=(), response='{"decisions":[]}', prompt_tokens=0, completion_tokens=0, total_tokens=0)
            new_prompts = []
            async def new_generate(prompt, capture=new_prompts, recorded=recorded, new_ids=new_ids):
                capture.append(prompt)
                data = json.loads(prompt.split("输入：\n", 1)[1])
                actions = {(row["new_tag_id"], row["old_tag_id"]): row for row in recorded}
                results = []
                for pair in data["pairs"]:
                    action = actions.get((new_ids[pair["new_index"]], pair["old_tag_id"]), {})
                    results.append({"candidate_id": pair["candidate_id"], "decision": action.get("decision", "coexist"), "reason": action.get("reason", "No recorded conflict; fixture only")})
                from types import SimpleNamespace
                return SimpleNamespace(generation_attempts=(), response=json.dumps({"results": results}), prompt_tokens=0, completion_tokens=0, total_tokens=0)
            from types import SimpleNamespace
            from unittest.mock import patch
            async def run_model(function, generate, template, batch=batch, new=new, old_tags=old_tags):
                modules = {"src.services.llm_service": SimpleNamespace(LLMServiceClient=lambda **kw: SimpleNamespace(generate_response=generate)),
                           "src.common.prompt_i18n": SimpleNamespace(load_prompt=lambda name: template)}
                with patch.dict(sys.modules, modules):
                    return await function(batch["session_id"], new, old_tags, max_requests=10000)
            asyncio.run(run_model(baseline.decide_tag_conflicts, legacy_generate, old_template))
            actual = asyncio.run(run_model(decide_tag_conflicts, new_generate, new_template))
            pairs, local = plan_conflict_pairs(batch["session_id"], new, old_tags)
            retained = {(new_ids[pair["new_index"]], pair["old"]["id"]) for pair in pairs}
            known = {(row["new_tag_id"], row["old_tag_id"]) for row in recorded}
            reproduced = {(new_ids[row["new_index"]], row["old_tag_id"], row["decision"]) for row in actual}
            expected = {(row["new_tag_id"], row["old_tag_id"], row["decision"]) for row in recorded}
            samples.append({"batch_id": batch["id"], "new_tags": len(new), "old_tags": len(old_tags),
                "baseline_calls": len(old_prompts), "new_calls": len(new_prompts),
                "baseline_prompt_chars": sum(map(len, old_prompts)), "new_prompt_chars": sum(map(len, new_prompts)),
                "baseline_prompt_bytes": sum(len(value.encode()) for value in old_prompts),
                "new_prompt_bytes": sum(len(value.encode()) for value in new_prompts),
                "local_duplicates": local, "known_conflicts": len(known), "known_conflicts_filtered_out": len(known-retained),
                "recorded_actions_preserved_by_protocol": reproduced == expected})
        totals = {key: sum(row[key] for row in samples) for key in ("baseline_calls", "new_calls", "baseline_prompt_chars", "new_prompt_chars", "baseline_prompt_bytes", "new_prompt_bytes", "known_conflicts", "known_conflicts_filtered_out", "local_duplicates")}
        report = {"method": "Offline reconstruction, recorded-decision fixture, no API calls. No independent semantic accuracy evaluation. All same-category candidate pairs retained except exact same-source duplicates. Historical status/revision and extraction previous_tag_id snapshots are unavailable. Character/byte measurements are not tokens.",
                  "historical_actual_api_usage": real_usage, "historical_failures": historical_failures,
                  "samples": samples, "totals": totals,
                  "new_actual_api_tokens": None, "token_reduction_target_verified": False,
                  "baseline_source_sha256": hashlib.sha256(args.legacy_source.read_bytes()).hexdigest()}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"samples": len(samples), "totals": totals, "failures": historical_failures, "actual_usage": real_usage}, ensure_ascii=False))


if __name__ == "__main__":
    main()
