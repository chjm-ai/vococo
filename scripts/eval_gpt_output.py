"""用现有 Codex 订阅做单变量效果对比；只保存模型产出，不保存认证信息。"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

ENDPOINT = 'https://chatgpt.com/backend-api/codex/responses'
INSTRUCTIONS = '你是中文助手。回答用户的问题，不调用工具。'
QUESTIONS = (
    '数据库为什么要建索引？解释原理、适用场景和代价。',
    'Python 读取 CSV 报 FileNotFoundError，应该怎么排查？',
    '一家小型化工外贸公司准备开始用 AI 做客户开发，前两周怎么安排？',
)
SUMMARY_QUESTION = (
    '两个相同处理器执行不可中断任务，每个任务只能由一个处理器执行。'
    'A耗时3、B耗时4、C耗时2、D耗时5、E耗时3、F耗时4。'
    'C要等A完成，D要等A和B完成，E要等C完成，F要等D和E完成。'
    '求最短全部完工时间，给出一个排程并用下界说明为什么无法更短。只给结论和必要依据。'
)


def build_cases(model: str, repeats: int) -> list[dict]:
    cases = []
    for repeat in range(repeats):
        for index, prompt in enumerate(QUESTIONS):
            levels = ('low', 'medium', 'high') if repeat % 2 == 0 else ('high', 'medium', 'low')
            for level in levels:
                cases.append(dict(suite='verbosity', level=level, repeat=repeat + 1,
                                  question=index + 1, prompt=prompt, model=model))
        for level in ('auto', 'concise', 'detailed', 'none'):
            cases.append(dict(suite='summary', level=level, repeat=repeat + 1,
                              question=1, prompt=SUMMARY_QUESTION, model=model))
    return cases


def request_body(case: dict) -> dict:
    reasoning = {'effort': 'medium' if case['suite'] == 'verbosity' else 'high'}
    if case['suite'] == 'summary' and case['level'] != 'none':
        reasoning['summary'] = case['level']
    return {'model': case['model'], 'instructions': INSTRUCTIONS,
            'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': case['prompt']}]}],
            'stream': True, 'store': False, 'reasoning': reasoning,
            'text': {'verbosity': case['level'] if case['suite'] == 'verbosity' else 'medium'}}


def measure_response(response: dict) -> dict:
    if response.get('status') != 'completed':
        raise ValueError('响应未完成，不能作为长度样本')
    answer, summary = [], []
    for item in response.get('output', []):
        answer += [part['text'] for part in item.get('content', []) if part.get('type') == 'output_text']
        summary += [part['text'] for part in item.get('summary', []) if part.get('type') == 'summary_text']
    usage = response.get('usage') or {}
    reasoning_tokens = (usage.get('output_tokens_details') or {}).get('reasoning_tokens', 0)
    answer_text, summary_text = '\n'.join(answer), '\n'.join(summary)
    return {'answer': answer_text, 'summary': summary_text,
            'answer_chars': len(''.join(answer_text.split())),
            'summary_chars': len(''.join(summary_text.split())),
            'reasoning_tokens': reasoning_tokens,
            'visible_output_tokens': usage.get('output_tokens', 0) - reasoning_tokens,
            'model_echo': response.get('model'), 'text_echo': response.get('text'),
            'reasoning_echo': response.get('reasoning')}


def check_echo(case: dict, result: dict) -> None:
    body = request_body(case)
    if result['model_echo'] != case['model']:
        raise ValueError('返回模型与请求模型不一致')
    if result['text_echo'].get('verbosity') != body['text']['verbosity']:
        raise ValueError('返回输出详细程度与请求不一致')
    for key in ('effort', 'summary'):
        if key == 'summary' and body['reasoning'].get(key) == 'auto':
            if result['reasoning_echo'].get(key) in ('auto', 'concise', 'detailed'):
                continue
        if result['reasoning_echo'].get(key) != body['reasoning'].get(key):
            raise ValueError(f'返回 reasoning.{key} 与请求不一致')


async def request_response(client: httpx.AsyncClient, body: dict) -> dict:
    async with client.stream('POST', ENDPOINT, json=body) as response:
        if response.status_code != 200:
            raise ValueError(f'模型接口 HTTP {response.status_code}')
        async for line in response.aiter_lines():
            if not line.startswith('data: '):
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            if event.get('type') == 'response.completed':
                return event['response']
            if event.get('type') in ('response.failed', 'error'):
                raise ValueError('模型接口返回失败事件')
    raise ValueError('流结束但缺少 response.completed')


async def run_case(client: httpx.AsyncClient, semaphore: asyncio.Semaphore, case: dict) -> dict:
    async with semaphore:
        start = time.monotonic()
        result = {}
        try:
            response = await request_response(client, request_body(case))
            result = measure_response(response)
            check_echo(case, result)
            result.update(status='ok', seconds=round(time.monotonic() - start, 3))
        except (httpx.HTTPError, ValueError) as exc:
            result.update(status='error', error=type(exc).__name__ + ': ' + str(exc))
        row = {**case, **result}
        print(case['suite'], case['level'], case['question'], case['repeat'],
              result.get('status'), result.get('answer_chars'), result.get('summary_chars'), flush=True)
        return row


def paired_rows(rows: list[dict], suite: str) -> list[dict]:
    levels = {'low', 'medium', 'high'} if suite == 'verbosity' else {'auto', 'concise', 'detailed', 'none'}
    groups = {}
    for row in rows:
        if row['suite'] == suite and row['status'] == 'ok':
            groups.setdefault((row['question'], row['repeat']), set()).add(row['level'])
    complete = {key for key, present in groups.items() if present == levels}
    return [row for row in rows if row['suite'] == suite and row['status'] == 'ok'
            and (row['question'], row['repeat']) in complete]


def write_results(output: Path, rows: list[dict], model: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {'model': model, 'instructions': INSTRUCTIONS, 'endpoint': ENDPOINT,
                'tested_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
                'method': '单变量；不限制输出 token；字数去除空白；仅 completed 且回显符合请求的样本有效；auto 允许解析为具体摘要档位',
                'scope': '模型原生 Responses；不是运行中 vococo 服务的端到端测试', 'results': rows}
    output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    lines = [f'模型：{model}', metadata['method'], metadata['scope'], '',
             '分组 | 档位 | 配对样本 | 回答平均字数 | 摘要平均字数 | 平均秒数']
    for suite, levels in [('verbosity', ('low', 'medium', 'high')),
                          ('summary', ('auto', 'concise', 'detailed', 'none'))]:
        for level in levels:
            group = [row for row in paired_rows(rows, suite) if row['level'] == level]
            metrics = [round(statistics.mean(row[key] for row in group), 1) if group else '无' for key in ('answer_chars', 'summary_chars', 'seconds')]
            lines.append(' | '.join(map(str, [suite, level, len(group), *metrics])))
    lines += ['', '平均值仅使用同一道题、同一次重复且所有档位均成功的配对组，避免失败样本改变题目配比。', '小样本受随机波动影响；没有生成摘要时不能判断摘要档位的效果。',
              f'失败样本数：{sum(row["status"] != "ok" for row in rows)}', '', '完整原文与参数回显见同名 JSON 文件。']
    output.with_suffix('.txt').write_text('\n'.join(lines) + '\n')


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--repeats', type=int, default=2, choices=range(1, 6))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    tokens = json.loads((Path.home() / '.codex/auth.json').read_text())['tokens']
    headers = {'Authorization': 'Bearer ' + tokens['access_token'],
               'ChatGPT-Account-ID': tokens['account_id'], 'originator': 'codex_cli_rs'}
    async with httpx.AsyncClient(headers=headers, timeout=180) as client:
        semaphore = asyncio.Semaphore(2)
        rows = await asyncio.gather(*(run_case(client, semaphore, case) for case in build_cases(args.model, args.repeats)))
    write_results(args.output, rows, args.model)
    return int(any(row['status'] != 'ok' for row in rows))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
