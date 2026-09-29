"""Synthetic-only admin-write policy checks; no credentials or live transport."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from epoptia_write import CONTRACTS, execute_write
from ermis_gateway import ACTIONS, Gateway
import ermis_system_server as system


class AdminWriteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 10
        self.invoke = AsyncMock()
        self.gateway = Gateway(self.invoke, lambda: self.now)
        self.session = 'admin_session_0001'

    def args(self, name):
        c = CONTRACTS[name]
        return {c.id_field: 12, c.expected_field: 'Old', c.new_field: 'New'}

    async def request(self, name, args=None):
        return await self.gateway.request(dict(session_id=self.session, action=name,
                                              arguments=self.args(name) if args is None else args))

    async def confirm(self, proposal, **extra):
        return await self.gateway.confirm(dict(session_id=self.session,
            confirmation_id=proposal['confirmation_id'], approved=True) | extra)

    async def test_proposals_no_dispatch_and_confirm_fails_closed(self):
        for name in CONTRACTS:
            self.assertTrue(ACTIONS[name].write)
            proposal = await self.request(name)
            self.assertEqual(proposal['status'], 'confirmation_required')
            self.assertEqual(proposal['arguments'], self.args(name))
            for field, value in self.args(name).items():
                self.assertIn(field, proposal['prompt'])
                self.assertIn(str(value), proposal['prompt'])
            self.invoke.assert_not_called()
            outcome = await self.confirm(proposal)
            self.assertEqual(outcome['status'], 'auth_required')
            self.assertFalse(outcome['ok'])
            self.assertFalse(outcome['write_performed'])
            self.assertEqual((await self.confirm(proposal))['status'], 'invalid_confirmation')
        self.invoke.assert_not_called()

    async def test_wrong_expired_tampered_and_cancelled_confirmation(self):
        for name in CONTRACTS:
            proposal = await self.request(name)
            for extra in ({'session_id': 'wrong_session_001'}, {'approved': 'true'},
                          {'confirmation_id': 'wrong'}, {'arguments': self.args(name)}):
                self.assertEqual((await self.confirm(proposal, **extra))['status'], 'invalid_confirmation')
            self.now += 120
            self.assertEqual((await self.confirm(proposal))['status'], 'invalid_confirmation')
            proposal = await self.request(name)
            self.assertEqual((await self.confirm(proposal, approved=False))['status'], 'cancelled')
        self.invoke.assert_not_called()

    async def test_strict_fields_ids_and_values(self):
        for name, c in CONTRACTS.items():
            invalid = [self.args(name) | {'extra': 'synthetic-secret'}]
            for key in self.args(name):
                invalid.append({k: v for k, v in self.args(name).items() if k != key})
            for value in (True, 0, -1, 1000000000000000, '12', 1.5, None):
                invalid.append(self.args(name) | {c.id_field: value})
            for key in (c.expected_field, c.new_field):
                for value in ('', '  ', 'x' * (c.limit + 1), None, 1, 'x\n', 'x\x7f'):
                    invalid.append(self.args(name) | {key: value})
            for args in invalid:
                result = await self.request(name, args)
                self.assertEqual(result, {'ok': False, 'status': 'unsupported_request'})
                self.assertNotIn('synthetic-secret', json.dumps(result))
            valid = self.args(name) | {c.new_field: 'x' * c.limit}
            self.assertEqual((await self.request(name, valid))['status'], 'confirmation_required')
        self.invoke.assert_not_called()

    async def test_read_preflight_never_writes_or_exposes_transport_data(self):
        for name in CONTRACTS:
            transport = SimpleNamespace(authenticated=True, csrf_ready=True,
                read_current=AsyncMock(return_value='synthetic-secret'), post=AsyncMock())
            for value, status in [('synthetic-secret', 'conflict'), (None, 'read_unverified'),
                                  ('Old', 'write_transport_unverified')]:
                transport.read_current.return_value = value
                result = await execute_write(name, self.args(name), read_transport=transport)
                self.assertEqual(result['status'], status)
                self.assertFalse(result['write_performed'])
                self.assertNotIn('synthetic-secret', json.dumps(result))
            transport.read_current.side_effect = RuntimeError('synthetic-secret session cookie csrf')
            result = await execute_write(name, self.args(name), read_transport=transport)
            self.assertEqual(result['status'], 'read_unverified')
            self.assertNotIn('synthetic-secret', json.dumps(result))
            transport.post.assert_not_called()
            for authenticated, csrf in ((False, True), (True, False), ('true', True)):
                transport.authenticated, transport.csrf_ready = authenticated, csrf
                transport.read_current.reset_mock()
                result = await execute_write(name, self.args(name), read_transport=transport)
                self.assertEqual(result['status'], 'auth_required')
                transport.read_current.assert_not_called()

    async def test_mcp_actions_use_same_confirmation_flow(self):
        with patch.object(system, 'gateway', self.gateway):
            for name in CONTRACTS:
                result = await system.mcp.call_tool('ermis_gateway_execute', {
                    'operation': 'request', 'payload': dict(session_id=self.session,
                        action=name, arguments=self.args(name))})
                proposal = result.structured_content
                self.assertEqual(proposal['status'], 'confirmation_required')
                result = await system.mcp.call_tool('ermis_gateway_execute', {
                    'operation': 'confirm', 'payload': dict(session_id=self.session,
                        confirmation_id=proposal['confirmation_id'], approved=True)})
                self.assertEqual(result.structured_content['status'], 'auth_required')
        self.invoke.assert_not_called()
