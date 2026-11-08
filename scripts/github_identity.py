"""Obtain an audience-bound identity from the current GitHub Actions job."""
import http.client
import json
import re
import urllib.parse


def identity(environment, audience):
    if (not isinstance(audience, str) or not audience.startswith(('komizo-request:sha256:', 'komizo-release:sha256:'))
            or not re.fullmatch('[a-f0-9]{64}', audience.rsplit(':', 1)[-1])):
        raise ValueError('Actions identity requires a bound Komizo audience')
    url = urllib.parse.urlsplit(environment.get('ACTIONS_ID_TOKEN_REQUEST_URL', ''))
    token = environment.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')
    if (url.scheme != 'https' or not url.hostname or not url.hostname.endswith('.actions.githubusercontent.com')
            or url.username or url.password or url.port not in (None, 443) or url.fragment
            or not token or len(token) > 8192 or any(char in token for char in '\r\n\0')):
        raise ValueError('GitHub OIDC request authority is unavailable')
    query = [(key, value) for key, value in urllib.parse.parse_qsl(url.query) if key != 'audience']
    path = urllib.parse.urlunsplit(('', '', url.path,
        urllib.parse.urlencode(query + [('audience', audience)]), ''))
    connection = http.client.HTTPSConnection(url.hostname, port=url.port or 443, timeout=10)
    try:
        connection.request('GET', path, headers={'Authorization': 'Bearer ' + token})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('GitHub OIDC request refused')
        body = response.read((32 << 10) + 1)
        if len(body) > 32 << 10:
            raise ValueError('GitHub OIDC response exceeds its bound')
        data = json.loads(body)
        value = data.get('value') if isinstance(data, dict) else None
        if (not isinstance(value, str) or not 0 < len(value) <= 24 << 10
                or value.count('.') != 2 or any(char in value for char in '\r\n\0')):
            raise ValueError('GitHub OIDC response has no bounded token')
        return value
    except (OSError, http.client.HTTPException):
        raise ValueError('GitHub OIDC exchange failed') from None
    finally:
        connection.close()
