import unittest
from browser_error_contract import cancelled_poll_error


class BrowserErrorContractTests(unittest.TestCase):
    def setUp(self):
        self.base = 'http://127.0.0.1:12345'
        self.url = self.base + '/api/tv-pages/state?mode=controls'
        self.error = {'stack': 'Fetch API cannot load ' + self.url + ' due to access control checks.\n at api', 'url': self.base + '/tv-controls', 'at': 11}
        self.lifecycle = [{'event': 'beforeunload', 'url': self.error['url'], 'at': 10.5}]
        self.methods = {self.url: 'GET'}

    def classified(self, **changes):
        return cancelled_poll_error(self.error, engine=changes.get('engine', 'webkit'), base=self.base, lifecycle=changes.get('lifecycle', self.lifecycle), request_methods=changes.get('request_methods', self.methods))

    def test_exact_webkit_cancelled_read_during_navigation_is_identified(self):
        self.assertTrue(self.classified())

    def test_same_policy_error_during_normal_view_is_not_ignored(self):
        self.assertFalse(self.classified(lifecycle=[]))
        self.lifecycle[0]['at'] = 1
        self.assertFalse(self.classified())

    def test_commands_external_origins_and_other_browsers_fail_closed(self):
        self.assertFalse(self.classified(request_methods={self.url: 'POST'}))
        self.assertFalse(self.classified(engine='chromium'))
        self.error['stack'] = 'Fetch API cannot load https://other.example/api/tv-pages/state due to access control checks.'
        self.assertFalse(self.classified())

    def test_other_endpoint_or_real_exception_is_not_ignored(self):
        self.error['stack'] = 'Fetch API cannot load ' + self.base + '/api/tvs/foyer/power due to access control checks.'
        self.assertFalse(self.classified())
        self.error['stack'] = 'TypeError: genuine application bug'
        self.assertFalse(self.classified())

    def test_another_document_navigation_cannot_excuse_error(self):
        self.lifecycle[0]['url'] = self.base + '/different-page'
        self.assertFalse(self.classified())

    def test_new_document_at_same_url_cannot_reuse_old_navigation(self):
        self.lifecycle.append({'event': 'document-start', 'url': self.error['url'], 'at': 10.8})
        self.assertFalse(self.classified())
