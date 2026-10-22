from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest

from hysight.config import ConfigurationError, Rule, Settings, load_rules
from hysight.runtime import Runtime, evidence_refs
from hysight.store import Store, WriterLock


RULE = {"id": "share", "source_bank": "executor", "source_model_id": "shared-lessons",
        "target_bank": "collective", "target_tags": ["team:hylab"]}


class FakeBackend:
    def __init__(self):
        self.models = {("executor", "shared-lessons"): {
            "id": "shared-lessons", "bank_id": "executor", "content": "A tested lesson with its limits.",
            "last_refreshed_at": "2026-10-22T01:00:00Z",
            "source_query": "Private query that must not be copied",
            "reflect_response": {"based_on": {"memories": [
                {"id": "fact-1", "document_id": "original-1", "text": "Private raw text", "metadata": {"secret": "canary"}}
            ]}, "trace": {"private": "canary"}}
        }}
        self.banks = {"collective": {"bank_id": "collective"}}
        self.docs = {}
        self.retains = []
        self.deletes = []
        self.fail_before = False
        self.fail_after = False
        self.after_write = None

    async def model(self, bank, model_id):
        return copy.deepcopy(self.models.get((bank, model_id)))

    async def bank(self, bank):
        return self.banks.get(bank)

    async def document(self, bank, document_id):
        return copy.deepcopy(self.docs.get((bank, document_id)))

    async def retain(self, bank, item):
        self.retains.append(copy.deepcopy(item))
        if self.fail_before:
            raise RuntimeError("secret provider detail must never be stored")
        self.docs[(bank, item["document_id"])] = {
            "document_metadata": copy.deepcopy(item["metadata"]), "memory_unit_count": 1,
            "original_text": item["content"],
        }
        if self.after_write:
            action, self.after_write = self.after_write, None
            await action()
        if self.fail_after:
            raise TimeoutError("commit happened, acknowledgement lost")
        return [["unit-1"]]

    async def delete(self, bank, document_id):
        self.deletes.append((bank, document_id))
        self.docs.pop((bank, document_id), None)


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rules.json"

    def load(self, rules, max_hops=1):
        self.path.write_text(json.dumps({"schema_version": 1, "rules": rules}))
        return load_rules(self.path, max_hops)

    def test_reject_cycles(self):
        with self.assertRaisesRegex(ConfigurationError, "cycle"):
            self.load([RULE, {**RULE, "id": "return", "source_bank": "collective", "target_bank": "executor"}], 2)

    def test_default_one_hop_and_explicit_dag_limit(self):
        rules = [RULE, {**RULE, "id": "next", "source_bank": "collective", "target_bank": "universal"}]
        with self.assertRaisesRegex(ConfigurationError, "max_hops"):
            self.load(rules)
        self.assertEqual(len(self.load(rules, 2)), 2)

    def test_duplicate_rule_or_route(self):
        for rules in ([RULE, RULE], [RULE, {**RULE, "id": "duplicate"}]):
            with self.assertRaises(ConfigurationError):
                self.load(rules)

    def test_reject_unknown_fields_paths_and_reserved_tags(self):
        for values in ({"target_bank": "../wrong"}, {"surprise": 3}, {"enabled": "false"}, {"target_tags": ["hysight:fake"]}):
            with self.assertRaises(ConfigurationError):
                self.load([{**RULE, **values}])

    def test_disabled_route_is_not_a_graph_edge(self):
        self.assertEqual(len(self.load([RULE, {**RULE, "id": "off", "source_bank": "collective", "target_bank": "executor", "enabled": False}])), 2)

    def test_nonfinite_settings_rejected(self):
        for value in ("nan", "inf", "-1", "0"):
            with self.assertRaises(ConfigurationError):
                Settings.parse({"rules_file": str(self.path), "state_path": str(self.path.parent / 'state.db'), "poll_seconds": value})

    def test_evidence_does_not_export_raw_text_or_metadata(self):
        refs = evidence_refs(FakeBackend().models[("executor", "shared-lessons")])
        self.assertEqual(refs, {"memories": [{"id": "fact-1", "document_id": "original-1"}]})

    def test_foreign_sqlite_database_is_not_modified(self):
        path = self.path.parent / 'foreign.sqlite3'
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE valuable(data TEXT)')
        con.commit()
        con.close()
        with self.assertRaisesRegex(ConfigurationError, 'non-Hysight'):
            Store(path).initialize()
        con = sqlite3.connect(path)
        try:
            self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('valuable',)])
        finally:
            con.close()

    @unittest.skipIf(os.name == 'nt', 'POSIX permission test')
    def test_shared_directory_permissions_are_not_changed(self):
        parent = self.path.parent / 'shared'
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        with self.assertRaisesRegex(ConfigurationError, '0700'):
            Store(parent / 'state.sqlite3').initialize()
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "rules.json"
        self.write_rules([RULE])
        self.settings = Settings(self.path, self.root / "state.sqlite3", retry_base_seconds=0.001, max_attempts=2)
        self.backend = FakeBackend()
        self.runtime = self.manual_runtime()

    def write_rules(self, rules):
        self.path.write_text(json.dumps({"schema_version": 1, "rules": rules}))

    def manual_runtime(self):
        # Exercise the durable state machine one job at a time; lifecycle is tested separately.
        runtime = Runtime(self.settings)
        runtime.lock.acquire()
        runtime.store.initialize()
        runtime.store.register(runtime.rules, runtime.settings.schema)
        runtime.started = True
        runtime.backend = self.backend
        return runtime

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.tmp.cleanup()

    def job(self):
        return self.runtime.store.snapshot()["jobs"][0]

    async def changed(self, content):
        self.backend.models[("executor", "shared-lessons")]["content"] = content
        await self.runtime.hook("executor", "shared-lessons", success=True)
        await self.runtime.tick()

    async def test_publish_and_unchanged_refresh_no_extra_inference(self):
        await self.runtime.tick()
        self.backend.models[("executor", "shared-lessons")]["last_refreshed_at"] = "2026-10-23T00:00:00Z"
        await self.runtime.hook("executor", "shared-lessons", success=True)
        await self.runtime.tick()
        self.assertEqual(len(self.backend.retains), 1)
        self.assertEqual(self.job()["last_action"], "unchanged")

    async def test_content_change_replaces_one_stable_document(self):
        await self.runtime.tick()
        await self.changed("A corrected lesson")
        self.assertEqual(len(self.backend.retains), 2)
        self.assertEqual(len(self.backend.docs), 1)
        self.assertEqual(self.backend.retains[0]["document_id"], self.backend.retains[1]["document_id"])
        self.assertNotIn("Private raw text", self.backend.retains[-1]["content"])
        self.assertNotIn("Private query", self.backend.retains[-1]["content"])

    async def test_evidence_change_also_updates_publication(self):
        await self.runtime.tick()
        self.backend.models[("executor", "shared-lessons")]["reflect_response"]["based_on"]["memories"][0]["id"] = "fact-2"
        await self.runtime.hook("executor", "shared-lessons", success=True)
        await self.runtime.tick()
        self.assertEqual(len(self.backend.retains), 2)

    async def test_unselected_or_failed_hook_is_ignored(self):
        await self.runtime.tick()
        self.assertEqual(await self.runtime.hook("other", "shared-lessons", success=True), 0)
        self.assertEqual(await self.runtime.hook("executor", "shared-lessons", success=False), 0)
        self.assertFalse(await self.runtime.tick())

    async def test_empty_source_keeps_previous_and_missing_source_retracts(self):
        await self.runtime.tick()
        await self.changed("")
        self.assertEqual(self.job()["last_action"], "source_not_ready")
        self.assertEqual(len(self.backend.docs), 1)
        self.backend.models.clear()
        await self.runtime.reconcile()
        await self.runtime.tick()
        self.assertEqual(len(self.backend.docs), 0)
        self.assertEqual(self.job()["last_action"], "retracted")

    async def test_no_evidence_means_no_inference(self):
        self.backend.models[("executor", "shared-lessons")]["reflect_response"] = {}
        await self.runtime.tick()
        self.assertEqual(self.job()["last_action"], "source_without_evidence")
        self.assertEqual(self.backend.retains, [])

    async def test_missing_bank_not_automatically_created(self):
        self.backend.banks.clear()
        await self.runtime.tick()
        self.assertEqual(self.job()["state"], "retry")
        self.assertEqual(self.backend.retains, [])

    async def test_document_collision_never_overwritten_or_deleted(self):
        self.backend.docs[("collective", "hysight/v1/share")] = {"document_metadata": {"owner": "someone-else"}}
        await self.runtime.tick()
        self.assertEqual(self.backend.retains, [])
        self.assertEqual(self.backend.deletes, [])

    async def test_terminal_failure_does_not_repeat_inference_until_source_changes(self):
        self.backend.fail_before = True
        await self.runtime.tick()
        await asyncio.sleep(0.01)
        await self.runtime.tick()
        self.assertEqual(len(self.backend.retains), 2)
        await self.runtime.reconcile()
        await self.runtime.tick()
        self.assertEqual(len(self.backend.retains), 2)
        self.assertEqual(self.job()["last_action"], "retry_exhausted")
        self.assertNotIn("secret provider detail", json.dumps(self.runtime.store.snapshot()))
        self.backend.fail_before = False
        await self.changed("New evidence-backed correction")
        self.assertEqual(self.job()["state"], "done")
        self.assertEqual(len(self.backend.retains), 3)

    async def test_crash_after_commit_recovers_without_second_retain(self):
        self.backend.fail_after = True
        claim = self.runtime.store.claim()
        with self.assertRaises(TimeoutError):
            await self.runtime._process(claim)
        self.assertEqual(self.job()["state"], "running")
        await self.runtime.stop()
        self.backend.fail_after = False
        self.runtime = self.manual_runtime()
        await self.runtime.tick()
        self.assertEqual(len(self.backend.retains), 1)
        self.assertEqual(self.job()["state"], "done")

    async def test_new_event_during_write_is_not_lost(self):
        async def event():
            self.backend.models[("executor", "shared-lessons")]["content"] = "Latest version"
            await self.runtime.hook("executor", "shared-lessons", success=True)
        self.backend.after_write = event
        await self.runtime.tick()
        self.assertEqual(self.job()["state"], "pending")
        await self.runtime.tick()
        self.assertEqual(self.job()["state"], "done")
        self.assertIn("Latest version", self.backend.retains[-1]["content"])

    async def test_removed_rule_retracts_after_restart(self):
        await self.runtime.tick()
        await self.runtime.stop()
        self.write_rules([])
        self.runtime = self.manual_runtime()
        await self.runtime.tick()
        self.assertEqual(self.backend.docs, {})

    async def test_disabled_rule_retracts_after_restart(self):
        await self.runtime.tick()
        await self.runtime.stop()
        self.write_rules([{**RULE, "enabled": False}])
        self.runtime = self.manual_runtime()
        await self.runtime.tick()
        self.assertEqual(self.backend.docs, {})

    async def test_external_deletion_of_owned_document_is_repaired(self):
        await self.runtime.tick()
        self.backend.docs.clear()
        await self.runtime.reconcile()
        await self.runtime.tick()
        self.assertEqual(len(self.backend.retains), 2)

    async def test_second_writer_rejected(self):
        other = WriterLock(Path(str(self.settings.state_path) + '.lock'))
        with self.assertRaises(RuntimeError):
            other.acquire()

    async def test_namespace_reuse_and_rule_retarget_rejected(self):
        with self.assertRaises(ConfigurationError):
            self.runtime.store.register(self.runtime.rules, "another_tenant")
        with self.assertRaises(ConfigurationError):
            self.runtime.store.register([Rule.parse({**RULE, "target_bank": "different"})])

    async def test_real_lifecycle_consumes_jobs(self):
        await self.runtime.stop()
        self.runtime = Runtime(dataclasses.replace(self.settings, poll_seconds=0.01))
        await self.runtime.start(self.backend)
        for _ in range(100):
            if self.job()["state"] == "done":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.job()["state"], "done")


if __name__ == '__main__':
    unittest.main()
