"""Distinguish WebKit native navigation noise from real page exceptions."""
import re
from urllib.parse import urlparse


def cancelled_poll_error(error, *, engine, base, lifecycle, request_methods):
    if engine != 'webkit':
        return False
    first_line = str(error.get('stack', '')).splitlines()[0:1]
    match = re.fullmatch(r'Fetch API cannot load (https?://\S+) due to access control checks\.', first_line[0] if first_line else '')
    if not match:
        return False
    url = match.group(1)
    parsed, origin = urlparse(url), urlparse(base)
    if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
        return False
    if parsed.path not in ('/auth/ping', '/api/tv-pages/state') or request_methods.get(url) != 'GET':
        return False
    at = error.get('at', 0)
    # Require a real document navigation signal, not just an AbortError-like
    # string. Normal in-page policy failures remain failures. Window errors and
    # unhandled rejections are independently asserted empty by the browser test.
    documents = [event for event in lifecycle if event['event'] in ('beforeunload', 'document-start')
                 and event['url'] == error.get('url') and event['at'] <= at]
    latest = max(documents, key=lambda event: event['at']) if documents else None
    return bool(latest and latest['event'] == 'beforeunload' and 0 <= at - latest['at'] <= 2)
