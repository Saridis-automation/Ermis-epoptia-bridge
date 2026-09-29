"""Synthetic-only voice transport, routing, policy and HTTP checks."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import epoptia_read
from ermis_gateway import ACTIONS, Gateway, route
from ermis_gateway_server import BoundedHTTP, create_app
from ermis_gateway_voice import RealtimeHTTP, VoiceSessions, VoiceUnavailable, tool_schema


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.invoke = AsyncMock(return_value={"ok": True, "items": [{"workstation": "Strantza",
                                                                    "step_status": "paused"}]})
        self.now = 10
        self.gateway = Gateway(self.invoke, lambda: self.now)
        self.transport = Mock()
        self.transport.create.return_value = ("v=0\r\nanswer", "rtc_synthetic")
        self.voice = VoiceSessions(self.gateway, self.transport, lambda: self.now)
        self.app = create_app(self.gateway, self.voice)
        self.client = self.app.test_client()
        self.url = "http://127.0.0.1:8002"

    def post(self, path, body, **kwargs):
        return self.client.post(path, base_url=self.url, json=body,
                                headers={"Origin": self.url}, **kwargs)

    def session(self):
        result = self.post('/voice/session', {"sdp": "v=0\r\noffer"})
        self.assertEqual(result.status_code, 201)
        self.assertNotIn('call_id', result.json)
        return result.json['session_id']

    def tool(self, sid, name='station_wip', args=None, call_id='call_synthetic'):
        return self.post('/voice/tool', dict(session_id=sid, call_id=call_id,
                          name=name, arguments=args if args is not None else {"workstation": "Strantza"}))

    def test_strantza_routes_to_existing_tool_and_preserves_results(self):
        self.assertEqual(route('What is running in Strantza now?'),
                         ('station_wip', {'workstation': 'strantza'}))
        result = self.tool(self.session()).json
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result']['items'][0]['step_status'], 'paused')
        self.invoke.assert_awaited_once_with('Epoptia_MES', 'workstation_wip', {'workstation': 'Strantza'})

    def test_schema_only_exposes_allowlist(self):
        schemas = tool_schema()
        self.assertEqual({s['name'] for s in schemas}, set(ACTIONS))
        self.assertNotIn('confirm', {s['name'] for s in schemas})
        for schema in schemas:
            self.assertFalse(schema['parameters']['additionalProperties'])
            self.assertEqual(set(schema['parameters']['required']), set(ACTIONS[schema['name']].fields))

    def test_laser_voice_query_uses_existing_wip_logic(self):
        rows = [{'workorderline_id': index, 'erp_routing': [
            {'workstationName': station, 'status': status}]}
            for index, (station, status) in enumerate([
                ('LASER', 'started'), ('LASER', 'in_progress'), ('LASER', 'paused'),
                ('LASER', 'completed'), ('Strantza', 'started')], 1)]
        self.invoke.side_effect = lambda server, tool, args: epoptia_read.workstation_wip(rows, **args)
        sid = self.session()
        for index, station in enumerate(('LASER', 'laser')):
            result = self.tool(sid, 'station_wip', {'workstation': station}, f'laser_{index}').json
            self.assertEqual(result['status'], 'completed')
            self.assertEqual([item['step_status'] for item in result['result']['items']],
                             ['started', 'in_progress', 'paused'])
            self.assertEqual(result['result']['counts_by_workstation'], {'LASER': 3})
            self.invoke.assert_awaited_with('Epoptia_MES', 'workstation_wip', {'workstation': station})

    def test_wol_and_all_station_voice_queries_reuse_gateway_actions(self):
        sid = self.session()
        for name, args, upstream in [('wol_status', {'wol_id': 123}, 'get_wol_status'),
                                     ('workstation_wip', {}, 'workstation_wip')]:
            result = self.tool(sid, name, args, name).json
            self.assertEqual(result['result'], self.invoke.return_value)
            self.invoke.assert_awaited_with('Epoptia_MES', upstream, args)

    def test_voice_preserves_missing_data_and_upstream_failures(self):
        sid = self.session()
        for index, payload in enumerate([{'ok': False, 'error': 'WOL not found'},
                                        {'items': [], 'truncated': True}]):
            self.invoke.return_value = payload
            self.assertEqual(self.tool(sid, 'wol_status', {'wol_id': 123}, f'read_{index}').json['result'], payload)
        self.invoke.side_effect = TimeoutError('synthetic private detail')
        self.assertEqual(self.tool(sid, call_id='failed').json,
                         {'ok': False, 'status': 'upstream_unavailable', 'retry_safe': True,
                          'failure_stage': 'unexpected_error', 'error_category': 'read_timeout'})

    def test_unknown_and_malformed_tool_calls_are_denied(self):
        sid = self.session()
        for index, (name, args) in enumerate([
            ('confirm', {}), ('station_wip', {'workstation': True}),
            ('station_wip', {'workstation': ' ' }), ('station_wip', {'workstation': 'a' * 81}),
            ('station_wip', {'workstation': 'Strantza', 'approved': True}),
            ('restart_service', {'service': 'ssh.service'}),
        ]):
            self.assertFalse(self.tool(sid, name, args, f'call_{index}').json['ok'])
        self.assertEqual(self.tool('unrecognized_session').status_code, 404)
        for sid in ('', 'short', 'a' * 129, '../invalid_session'):
            self.assertEqual(self.tool(sid).status_code, 400)
        self.invoke.assert_not_called()

    def test_writes_need_explicit_single_use_session_bound_approval(self):
        sid = self.session()
        other = self.session()
        result = self.tool(sid, 'restart_service', {'service': 'ermis-system-mcp.service'}).json
        self.assertEqual(result['status'], 'confirmation_required')
        self.invoke.assert_not_called()
        body = dict(session_id=sid, confirmation_id=result['confirmation_id'], approved=True)
        self.assertFalse(self.post('/voice/confirm', body | {'session_id': other}).json['ok'])
        self.assertFalse(self.post('/voice/confirm', body | {'approved': 'yes'}).json['ok'])
        self.assertTrue(self.post('/voice/confirm', body).json['ok'])
        self.assertFalse(self.post('/voice/confirm', body).json['ok'])
        self.invoke.assert_awaited_once_with('Ermis_System', 'ermis_service_control',
                                            {'service': 'ermis-system-mcp.service', 'operation': 'restart'})

    def test_duplicate_call_is_cached_and_conflicting_call_rejected(self):
        sid = self.session()
        self.assertEqual(self.tool(sid).json, self.tool(sid).json)
        self.assertEqual(self.tool(sid, 'health', {}).status_code, 409)
        self.invoke.assert_awaited_once()

    def test_concurrent_duplicates_only_dispatch_once(self):
        sid = self.session()
        body = dict(session_id=sid, call_id='call_once', name='health', arguments={})
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.voice.handle('tool', body), range(4)))
        self.assertTrue(all(r[0]['ok'] for r in results))
        self.invoke.assert_awaited_once()

    def test_stop_clears_approvals_and_blocks_later_calls(self):
        sid = self.session()
        proposal = self.tool(sid, 'restart_service', {'service': 'ermis-system-mcp.service'}).json
        self.assertTrue(self.post('/voice/stop', {'session_id': sid}).json['ok'])
        self.assertFalse(self.gateway.pending)
        self.assertEqual(self.tool(sid).status_code, 404)
        self.assertFalse(asyncio.run(self.gateway.confirm(dict(session_id=sid,
            confirmation_id=proposal['confirmation_id'], approved=True)))['ok'])
        self.transport.stop.assert_called_once_with('rtc_synthetic')
        self.invoke.assert_not_called()

    def test_expiration_capacity_and_tool_budget(self):
        sid = self.session()
        for _ in range(3):
            self.session()
        self.assertEqual(self.post('/voice/session', {'sdp': 'v=0'}).status_code, 429)
        self.voice.sessions[sid]['calls'] = dict.fromkeys([str(i) for i in range(128)])
        self.assertEqual(self.tool(sid).status_code, 429)
        self.now += 600
        self.voice.reap()
        self.assertFalse(self.voice.sessions)
        self.assertEqual(self.transport.stop.call_count, 4)
        self.assertEqual(self.tool(sid).status_code, 404)

    def test_transport_failures_are_sanitized_and_stop_invalidates_session(self):
        self.transport.create.side_effect = RuntimeError('synthetic private detail')
        result = self.post('/voice/session', {'sdp': 'v=0'})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json, {'ok': False, 'status': 'voice_unavailable'})
        self.transport.create.side_effect = None
        sid = self.session()
        self.transport.stop.side_effect = RuntimeError('synthetic private detail')
        self.assertEqual(self.post('/voice/stop', {'session_id': sid}).json['status'], 'stop_unverified')
        self.assertEqual(self.tool(sid).status_code, 404)

    def test_health_static_guards_logging_and_body_limits(self):
        with self.assertLogs('ermis.gateway', level='INFO') as captured:
            health = self.client.get('/health?secret=synthetic-private', base_url=self.url)
            self.assertEqual(health.json['status'], 'alive')
            for path in ('/', '/voice.js', '/voice.css'):
                with self.client.get(path, base_url=self.url) as response:
                    self.assertEqual(response.status_code, 200)
        for line in captured.output:
            self.assertNotIn('synthetic-private', line)
            entry = json.loads(line.split('ermis.gateway:', 1)[1])
            self.assertEqual(set(entry), {'event', 'endpoint', 'status', 'duration_ms'})
        self.assertEqual(health.headers['Cache-Control'], 'no-store')
        self.transport.create.assert_not_called()
        self.invoke.assert_not_called()
        for options in [dict(headers={'Origin': 'https://evil.invalid'}),
                        dict(headers={'Origin': 'null'}),
                        dict(headers={'Sec-Fetch-Site': 'cross-site'}),
                        dict(base_url='http://evil.invalid:8002'),
                        dict(environ_overrides={'REMOTE_ADDR': '192.0.2.1'})]:
            result = self.client.post('/voice/session', json={'sdp': 'v=0'},
                                      **({'base_url': self.url} | options))
            self.assertEqual(result.status_code, 403)
        self.assertEqual(self.post('/request', {'session_id': 'a' * 20, 'text': 'git status'}).status_code, 403)
        for body in [None, [], {}, {'sdp': False}, {'sdp': 'bad'}, {'sdp': 'v=0', 'tools': []}]:
            result = self.client.post('/voice/session', base_url=self.url,
                                      data=json.dumps(body), content_type='application/json')
            self.assertEqual(result.status_code, 400)
        self.assertEqual(self.post('/voice/session', {'sdp': 'v=0' + 'a' * 66000}).status_code, 413)
        self.assertEqual(self.post('/voice/session', {'sdp': 'v=0' + 'a' * 5000}).status_code, 201)
        self.assertEqual(self.post('/voice/tool', {'x': 'a' * 5000}).status_code, 413)

    def test_unexpected_errors_never_log_exception_contents(self):
        self.transport.create.side_effect = None
        with patch.object(self.voice, 'create', side_effect=RuntimeError('synthetic private detail')), \
                self.assertLogs('ermis.gateway', level='INFO') as captured:
            result = self.post('/voice/session', {'sdp': 'v=0'})
        self.assertEqual(result.status_code, 500)
        self.assertNotIn('synthetic private detail', str(captured.output) + result.get_data(as_text=True))


class BodyBoundsTest(unittest.IsolatedAsyncioTestCase):
    async def check_body(self, path, chunks, expected, headers=(), timeout=10):
        completed = []

        async def buffered_app(scope, receive, send):
            while True:
                message = await receive()
                if not message.get('more_body', False):
                    break
            completed.append(True)
            await send({'type': 'http.response.start', 'status': 200})

        receive = AsyncMock(side_effect=chunks)
        send = AsyncMock()
        await BoundedHTTP(buffered_app, timeout)(
            {'type': 'http', 'path': path, 'headers': headers}, receive, send)
        self.assertEqual(send.await_args_list[0].args[0]['status'], expected)
        self.assertEqual(bool(completed), expected == 200)

    async def test_limits_apply_before_buffering_including_chunked_health(self):
        chunk = lambda body, more=False: {'type': 'http.request', 'body': body, 'more_body': more}
        await self.check_body('/health', [chunk(b'x' * 4096, True), chunk(b'x')], 413)
        await self.check_body('/voice/session', [chunk(b'x' * 65536)], 200)
        await self.check_body('/voice/session', [chunk(b'x' * 65536, True), chunk(b'x')], 413)
        await self.check_body('/voice/session', [], 413, [(b'content-length', b'65537')])
        await self.check_body('/health', [], 400, [(b'content-length', b'invalid')])

    async def test_body_timeout_and_disconnect_do_not_reach_flask(self):
        await self.check_body('/voice/session', [], 408, timeout=0)
        await self.check_body('/voice/session', [{'type': 'http.disconnect'}], 400)


class HTTPSContractTest(unittest.TestCase):
    def test_invalid_answer_attempts_hangup_without_exposing_provider_data(self):
        for answer in ('provider diagnostic', 'v=0invalid'):
            with patch.object(RealtimeHTTP, '_post', return_value=(
                    answer, '/v1/realtime/calls/rtc_synthetic')), \
                    patch.object(RealtimeHTTP, 'stop') as stop:
                with self.assertRaises(VoiceUnavailable):
                    RealtimeHTTP().create('v=0')
                stop.assert_called_once_with('rtc_synthetic')

    def test_malformed_call_location_never_becomes_a_hangup_path(self):
        with patch.object(RealtimeHTTP, '_post', return_value=('v=0\r\nanswer', '/calls/..')), \
                patch.object(RealtimeHTTP, 'stop') as stop:
            with self.assertRaises(VoiceUnavailable):
                RealtimeHTTP().create('v=0')
            stop.assert_not_called()

    def test_key_missing_and_failures_do_not_escape(self):
        with patch('ermis_gateway_voice.os.getenv', return_value=None), \
                patch('ermis_gateway_voice.requests.Session') as client:
            with self.assertRaises(VoiceUnavailable):
                RealtimeHTTP().create('v=0')
            client.assert_not_called()

    def test_unified_multipart_and_hangup_without_sdk_or_redirects(self):
        response = Mock(status_code=201, headers={'Location': '/v1/realtime/calls/rtc_synthetic'})
        response.iter_content.return_value = [b'v=0\r\nanswer']
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        client = Mock()
        client.post.return_value = response
        with patch('ermis_gateway_voice.os.getenv', return_value='synthetic-key'), \
                patch('ermis_gateway_voice.requests.Session') as factory:
            factory.return_value.__enter__.return_value = client
            transport = RealtimeHTTP()
            self.assertEqual(transport.create('v=0'), ('v=0\r\nanswer', 'rtc_synthetic'))
            call = client.post.call_args
            self.assertFalse(client.trust_env)
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['timeout'], (5, 30))
            self.assertEqual(call.args[0], 'https://api.openai.com/v1/realtime/calls')
            config = json.loads(call.kwargs['files']['session'][1])
            self.assertEqual(config['type'], 'realtime')
            self.assertEqual(config['tools'], tool_schema())
            self.assertIn('τι τρέχει στο laser;', config['instructions'])
            self.assertIn('with workstation LASER', config['instructions'])
            self.assertIn('call wol_status with wol_id', config['instructions'])
            self.assertNotIn('synthetic-key', json.dumps(config))
            transport.stop('rtc_synthetic')
            self.assertTrue(client.post.call_args.args[0].endswith('/rtc_synthetic/hangup'))
            for status in (301, 401, 429, 500):
                response.status_code = status
                with self.assertRaises(VoiceUnavailable):
                    transport.create('v=0')
            response.status_code = 201
            response.iter_content.return_value = [b'x' * 65537]
            with self.assertRaises(VoiceUnavailable):
                transport.create('v=0')
            with self.assertRaises(VoiceUnavailable):
                transport.stop('../unsafe')
