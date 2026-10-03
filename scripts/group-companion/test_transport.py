import unittest
from unittest.mock import patch

import transport


class TransportTokenTests(unittest.TestCase):
    def setUp(self):
        transport._tokens.clear()

    def test_invalid_access_token_refreshes_once_then_retries(self):
        calls = []

        def fake_http(method, url, data=None, token=None):
            calls.append((method, url, token))
            if url == '/auth/v3/tenant_access_token/internal':
                return {'tenant_access_token': 'fresh-token'}
            if token == 'stale-token':
                raise RuntimeError('Feishu HTTP 400: Invalid access token for authorization. Please make a request with token attached.')
            return {'code': 0, 'data': {'message_id': 'om_new'}}

        with (patch.object(transport, 'credentials', return_value=('app', 'secret')),
              patch.object(transport, '_http', side_effect=fake_http)):
            transport._tokens['xiaowen'] = 'stale-token'
            result = transport.request('xiaowen', 'POST', '/im/v1/messages', {})

        self.assertEqual(result['data']['message_id'], 'om_new')
        self.assertEqual([c[2] for c in calls], ['stale-token', None, 'fresh-token'])


if __name__ == '__main__':
    unittest.main()
