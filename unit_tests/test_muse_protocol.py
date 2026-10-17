"""Transport tests, not evidence of model access or coding performance."""
import asyncio
import sys
import unittest
import uuid

from ttakkari.agents.muse_protocol import (
    MuseConnection,
    MuseProtocolError,
    new_command_id,
)

HOST = '''
import sys,json
for line in sys.stdin:
 q=json.loads(line)
 if 'id' not in q: continue
 if q['method']=='die': break
 if q['method']=='hang': continue
 if q['method']=='bad': print('not json',flush=True); continue
 if q['method']=='error':
  print(json.dumps({'jsonrpc':'2.0','id':q['id'],'error':{'code':-32602,'message':'invalid','data':{'kind':'invalidParams'}}}),flush=True)
  continue
 print(json.dumps({'jsonrpc':'2.0','method':'session/statusChanged','params':{'status':'idle'}}),flush=True)
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':{'method':q['method'],'params':q.get('params'),'commandId':q.get('params',{}).get('commandId')}}),flush=True)
'''


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.conn = await MuseConnection.start([sys.executable, '-u', '-c', HOST], on_notification=self.events.append)

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_notification_does_not_consume_response(self):
        result = await self.conn.request('model/list', {})
        self.assertEqual(result['method'], 'model/list')
        self.assertEqual(self.events[0]['method'], 'session/statusChanged')

    async def test_concurrent_request_ids_are_correlated(self):
        replies = await asyncio.gather(*(self.conn.request(str(i), {}) for i in range(4)))
        self.assertEqual([r['method'] for r in replies], ['0', '1', '2', '3'])

    async def test_command_uses_uuid7_on_python313(self):
        reply = await self.conn.command('turn/start', {'sessionId': 'test'})
        self.assertEqual(uuid.UUID(reply['commandId']).version, 7)

    async def test_uuid7_ids_are_unique(self):
        values = [new_command_id() for _ in range(200)]
        self.assertEqual(len(set(values)), 200)
        self.assertTrue(all(uuid.UUID(value).version == 7 for value in values))

    async def test_typed_error_is_not_a_successful_empty_result(self):
        with self.assertRaises(MuseProtocolError) as ctx:
            await self.conn.request('error', {})
        self.assertEqual(ctx.exception.kind, 'invalidParams')

    async def test_host_death_fails_pending_requests(self):
        with self.assertRaises(MuseProtocolError):
            await self.conn.request('die', {})

    async def test_malformed_frame_fails_closed(self):
        with self.assertRaises(MuseProtocolError):
            await self.conn.request('bad', {})

    async def test_timeout_does_not_retry_command(self):
        with self.assertRaises(TimeoutError):
            await self.conn.request('hang', {}, timeout=0.05)
        result = await self.conn.request('model/list', {})
        self.assertEqual(result['method'], 'model/list')


if __name__ == '__main__':
    unittest.main()
