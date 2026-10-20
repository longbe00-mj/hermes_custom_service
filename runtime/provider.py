import os

import httpx
from hermes_cli.dashboard_auth import DashboardAuthProvider, ProviderError, RefreshExpiredError, Session


class CentralAuth(DashboardAuthProvider):
    name = 'central'
    display_name = 'Central account'

    def start_login(self, *, redirect_uri):
        raise ProviderError('Use the central service login page.')

    def complete_login(self, **kwargs):
        raise ProviderError('Use the central service login page.')

    def verify_session(self, *, access_token):
        try:
            response = httpx.post('http://control:8000/internal/runtime-auth',
                                  headers={'Authorization': 'Bearer ' + os.environ['RUNTIME_TOKEN']},
                                  json={'owner_id': os.environ['OWNER_ID'], 'access_token': access_token},
                                  timeout=5, trust_env=False)
        except httpx.HTTPError as exc:
            raise ProviderError('Central account service unavailable') from exc
        if response.status_code in (401, 403):
            return None
        if response.status_code != 200:
            raise ProviderError('Central account service unavailable')
        data = response.json()
        return Session(**data, access_token=access_token, refresh_token='')

    def refresh_session(self, *, refresh_token):
        raise RefreshExpiredError('Refresh at the central service.')

    def revoke_session(self, *, refresh_token):
        return None


def register(ctx):
    ctx.register_dashboard_auth_provider(CentralAuth())
