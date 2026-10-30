"""部署测试只加载虚构配置，不访问实例 data。"""
import copy

import pytest

from vococo import deployment_settings

FAKE_SETTINGS = {
    'brain': {'origin': 'https://mcp.example.com'},
    'publishing': {
        'share_origin': 'https://share.example.com',
        'kimmy': {
            'ssh_host': 'fake-publisher', 'deploy_root': '/srv/fake-deploy',
            'web_root': '/srv/fake-www', 'snippet_root': '/srv/fake-snippets',
            'public_origin': 'https://pub.example.com', 'script_sha256': 'a' * 64,
            'contract_lines': ['DOMAIN=pub.example.com', 'URL_MODE=path',
                               'SSL_CERT=/etc/letsencrypt/live/example.com/fullchain.pem',
                               'SSL_KEY=/etc/letsencrypt/live/example.com/privkey.pem'],
        },
    },
}


@pytest.fixture(autouse=True)
def deployment_config(monkeypatch):
    settings = copy.deepcopy(FAKE_SETTINGS)
    monkeypatch.setattr(deployment_settings, '_section', lambda name: copy.deepcopy(settings.get(name, {})))
    return settings
