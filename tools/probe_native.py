"""Run inside the deployed Hindsight image; no new model inference.

The native refresh task handler dispatches the real hook. Refresh inference is
stubbed with an already-stored synthetic model. Destination API/storage is real.
Only a uniquely named temporary destination bank is written and removed.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from unittest.mock import AsyncMock
import uuid

from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.extensions import HttpExtension, OperationValidatorExtension
from hindsight_api.extensions.loader import load_extension
from hysight.runtime import PromotionError

SOURCE = 'ltm-exp-20261021-01-user-demo'
MODEL = 'preferencias-contextuais'
TARGET = os.environ.get('HYSIGHT_PROBE_TARGET_BANK', 'hysight-v1-probe-' + uuid.uuid4().hex[:12])
KEY = Path(os.environ.get('HYSIGHT_PROBE_API_KEY_FILE', '/run/hysight-api-key')).read_text().strip()
BASE = os.environ.get('HYSIGHT_PROBE_API_URL', 'http://127.0.0.1:8888')
assert TARGET.startswith('hysight-v1-probe-') and TARGET != SOURCE


def request(method, path, data=None, missing=False):
    req = Request(BASE + path, data=None if data is None else json.dumps(data).encode(), method=method,
                  headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        if missing and exc.code == 404:
            return None
        raise PromotionError(f'Probe API call failed: HTTP {exc.code} {method} {path}') from None


def call_total(bank):
    data = request('GET', '/v1/default/banks/' + bank + '/llm-requests/stats?period=1d')
    return sum(b['total'] for b in data['buckets'])


class APIEngine:
    def __init__(self):
        self.visible = False
        self.suffix = ''
        self.retains = 0
        self.deletes = 0
        self.model_read_ms = []

    async def get_mental_model(self, bank, model_id, **kwargs):
        if not self.visible:
            return None
        start = time.perf_counter()
        model = await asyncio.to_thread(request, 'GET', f'/v1/default/banks/{bank}/mental-models/{model_id}')
        self.model_read_ms.append(round((time.perf_counter() - start) * 1000, 3))
        result = copy.deepcopy(model)
        result['content'] += self.suffix
        return result

    async def get_bank_profile(self, bank, **kwargs):
        # The HTTP profile route is retired (410), while the public engine method
        # remains available. Emulate its read-only existence lookup through listing.
        page = await asyncio.to_thread(request, 'GET', '/v1/default/banks?limit=1000')
        return next((row for row in page['banks'] if row['bank_id'] == bank), None)

    async def get_document(self, document_id, bank, **kwargs):
        return await asyncio.to_thread(request, 'GET', f'/v1/default/banks/{bank}/documents/{quote(document_id, safe="")}', None, True)

    async def retain_batch_async(self, bank_id, contents, **kwargs):
        assert bank_id == TARGET
        self.retains += 1
        return await asyncio.to_thread(request, 'POST', '/v1/default/banks/' + bank_id + '/memories', {'items': contents, 'async': False})

    async def delete_document(self, document_id, bank, **kwargs):
        assert bank == TARGET
        self.deletes += 1
        return await asyncio.to_thread(request, 'DELETE', f'/v1/default/banks/{bank}/documents/{quote(document_id, safe="")}')


async def wait_for(runtime, action, generation=1):
    for _ in range(600):
        snapshot = await asyncio.to_thread(runtime.store.snapshot)
        job = snapshot['jobs'][0]
        if job['state'] == 'done' and job['last_action'] == action and job['generation'] >= generation:
            return snapshot
        if job['state'] in ('failed', 'retry'):
            raise RuntimeError('Promotion probe failed: ' + str(job['last_error']))
        await asyncio.sleep(0.05)
    raise TimeoutError('Promotion probe did not complete')


async def main():
    source_before = request('GET', f'/v1/default/banks/{SOURCE}/mental-models/{MODEL}')
    before_calls = call_total(SOURCE)
    request('PUT', '/v1/default/banks/' + TARGET,
            {'name': '[TEMP] Hysight V1 native contract probe', 'retain_extraction_mode': 'chunks', 'enable_observations': False})
    request('PATCH', '/v1/default/banks/' + TARGET + '/config',
            {'updates': {'retain_extraction_mode': 'chunks', 'enable_observations': False, 'enable_auto_consolidation': False, 'store_document_text': True}})
    lifecycle = None
    report = {'probe': 'hysight-v1-native', 'target_bank': TARGET,
              'scope': 'Real installed refresh handler and hook; inference stubbed; real destination API/storage'}
    try:
        with tempfile.TemporaryDirectory(prefix='hysight-probe-') as temp:
            root = Path(temp)
            rules = root / 'rules.json'
            rules.write_text(json.dumps({'schema_version': 1, 'rules': [{
                'id': 'probe', 'source_bank': SOURCE, 'source_model_id': MODEL, 'target_bank': TARGET,
                'target_tags': ['dataset:hysight-probe'], 'require_evidence': True}]}))
            os.environ.update({
                'HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION': 'hysight.extension:PromotionExtension',
                'HINDSIGHT_API_OPERATION_VALIDATOR_RULES_FILE': str(rules),
                'HINDSIGHT_API_OPERATION_VALIDATOR_STATE_PATH': str(root / 'state.sqlite3'),
                'HINDSIGHT_API_OPERATION_VALIDATOR_POLL_SECONDS': '0.01',
                'HINDSIGHT_API_OPERATION_VALIDATOR_RECONCILE_SECONDS': '3600',
                'HINDSIGHT_API_HTTP_EXTENSION': 'hysight.extension:PromotionLifecycle',
            })
            validator = load_extension('OPERATION_VALIDATOR', OperationValidatorExtension)
            lifecycle = load_extension('HTTP', HttpExtension)
            facade = APIEngine()
            router = lifecycle.get_router(facade)
            assert router.routes == [], 'The lifecycle adapter must expose no administrative routes'
            await lifecycle.on_startup()
            await wait_for(validator.runtime, 'absent')
            facade.visible = True

            # Execute the exact installed handler, replacing only inference/persistence
            # of the source refresh. It constructs the real RequestContext and result.
            engine = MemoryEngine.__new__(MemoryEngine)
            engine._operation_validator = validator
            engine.refresh_mental_model = AsyncMock(side_effect=lambda **kwargs: None)
            async def refreshed(**kwargs):
                return await facade.get_mental_model(SOURCE, MODEL)
            engine.refresh_mental_model.side_effect = refreshed
            engine._write_refresh_outcome_metadata = AsyncMock()
            engine._write_refresh_failure_metadata = AsyncMock()
            engine._record_mental_model_refresh_failure = AsyncMock()
            task = {'bank_id': SOURCE, 'mental_model_id': MODEL, 'operation_id': 'probe-task',
                    '_operation_id': 'probe-operation', '_tenant_id': 'probe-tenant', '_api_key_id': 'probe-key-id', '_retry_count': 2}
            await MemoryEngine._handle_refresh_mental_model(engine, task)
            snapshot = await wait_for(validator.runtime, 'published', 2)
            first_doc = await facade.get_document('hysight/v1/probe', TARGET)
            assert first_doc['memory_unit_count'] > 0
            assert facade.retains == 1

            await MemoryEngine._handle_refresh_mental_model(engine, task)
            await wait_for(validator.runtime, 'unchanged', 3)
            assert facade.retains == 1, 'Duplicate event incurred another retain'

            facade.suffix = '\n\n[SYNTHETIC PROBE] A correction for adapter verification only.'
            await MemoryEngine._handle_refresh_mental_model(engine, task)
            snapshot = await wait_for(validator.runtime, 'published', 4)
            updated_doc = await facade.get_document('hysight/v1/probe', TARGET)
            assert updated_doc['document_metadata']['hysight_revision'] != first_doc['document_metadata']['hysight_revision']
            assert facade.retains == 2
            docs = request('GET', '/v1/default/banks/' + TARGET + '/documents?limit=100')
            report['destination_documents_response_keys'] = list(docs)
            report['destination_stable_id'] = 'hysight/v1/probe'

            events = [json.loads(e['details']) for e in snapshot['events'] if e['kind'] == 'refresh_hook']
            assert events and events[0]['internal'] is True
            assert KEY not in json.dumps(snapshot)
            report['observed_request_context'] = events[0]
            report['source_provenance_is_separate'] = 'evidence_refs' in json.loads(updated_doc['original_text'])
            report['first_memory_units'] = first_doc['memory_unit_count']
            report['model_read_ms'] = facade.model_read_ms

            facade.visible = False  # Simulated source deletion; actual source is untouched.
            await validator.runtime.reconcile()
            await wait_for(validator.runtime, 'retracted', 5)
            assert await facade.get_document('hysight/v1/probe', TARGET) is None
            report.update({'retains': facade.retains, 'retractions': facade.deletes,
                           'duplicate_refresh_skipped': True, 'correction_replaced_stable_document': True})
            report['source_llm_calls_delta'] = call_total(SOURCE) - before_calls
            report['target_llm_calls'] = call_total(TARGET)
            assert report['source_llm_calls_delta'] == report['target_llm_calls'] == 0
            source_after = request('GET', f'/v1/default/banks/{SOURCE}/mental-models/{MODEL}')
            assert source_after['content'] == source_before['content']
            report['existing_source_unchanged'] = True
            await lifecycle.on_shutdown()
            lifecycle = None
    finally:
        if lifecycle is not None:
            await lifecycle.on_shutdown()
        assert TARGET.startswith('hysight-v1-probe-') and TARGET != SOURCE
        request('DELETE', '/v1/default/banks/' + TARGET)
        report['temporary_bank_removed'] = True
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
