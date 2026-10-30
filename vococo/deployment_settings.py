"""实例部署配置；缺项拒绝使用，不提供个人部署的隐式默认值。"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit


def _section(name: str) -> dict:
    from .instance_config import section
    return section(name)


def _origin(value, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/?", value):
        raise ValueError(f"{field} 必须配置为 HTTPS origin（不能含路径、凭据或查询参数）")
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.port == 0:
        raise ValueError(f"{field} 的主机或端口无效")
    return value.rstrip('/').lower()


def brain_origin() -> str:
    return _origin(_section('brain').get('origin'), 'brain.origin')


def share_origin() -> str:
    return _origin(_section('publishing').get('share_origin'), 'publishing.share_origin')


def _path(value, field: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r'/(?!/)[A-Za-z0-9_./-]+', value)
            or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts or value == '/'):
        raise ValueError(f"{field} 必须是规范的非根绝对路径")
    return value


def kimmy_target() -> dict:
    raw = _section('publishing').get('kimmy')
    if not isinstance(raw, dict):
        raise ValueError('缺少 publishing.kimmy 配置，拒绝远端发布')
    host = raw.get('ssh_host')
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host):
        raise ValueError('publishing.kimmy.ssh_host 必须是安全 SSH 主机名或别名')
    target = {'ssh_host': host, 'public_origin': _origin(raw.get('public_origin'), 'publishing.kimmy.public_origin')}
    for name in ('deploy_root', 'web_root', 'snippet_root'):
        target[name] = _path(raw.get(name), f'publishing.kimmy.{name}')
    paths = [PurePosixPath(target[name]) for name in ('deploy_root', 'web_root', 'snippet_root')]
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError('发布根目录不能互相重叠')
    digest = raw.get('script_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('缺少有效的部署脚本 SHA-256，须先审计契约')
    target['script_sha256'] = digest
    target['contract_lines'] = _contract(raw.get('contract_lines'), target['public_origin'])
    return target


def _contract(lines, origin: str) -> list[str]:
    if not isinstance(lines, list) or len(lines) != 4 or not all(isinstance(line, str) for line in lines):
        raise ValueError('contract_lines 必须保留完整四行部署契约')
    values = dict(line.split('=', 1) for line in lines if '=' in line)
    parsed = urlsplit(origin)
    if (set(values) != {'DOMAIN', 'URL_MODE', 'SSL_CERT', 'SSL_KEY'} or parsed.port is not None
            or values['DOMAIN'] != parsed.hostname or values['URL_MODE'] != 'path'):
        raise ValueError('部署契约必须匹配公开域名和 path 模式')
    cert = _path(values['SSL_CERT'], 'SSL_CERT')
    key = _path(values['SSL_KEY'], 'SSL_KEY')
    if (not cert.startswith('/etc/letsencrypt/live/') or not cert.endswith('/fullchain.pem')
            or key != str(PurePosixPath(cert).parent / 'privkey.pem')):
        raise ValueError('证书契约必须是同目录的受控 fullchain.pem/privkey.pem')
    return sorted(lines)


def target_fingerprint(target: dict) -> str:
    return hashlib.sha256(json.dumps(target, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def require_target_binding(state: dict | None, target: dict) -> None:
    if state is None:
        return
    fingerprint = state.get('deployment_fingerprint')
    if not fingerprint:
        fingerprint = state.get('receipt', {}).get('metadata', {}).get('deployment_fingerprint')
    if not fingerprint:
        fingerprint = _section('publishing').get('legacy_kimmy_fingerprint')
    if fingerprint != target_fingerprint(target):
        raise ValueError('发布记录未绑定当前部署配置；须核对旧目标并迁移绑定，拒绝连接新目标')
