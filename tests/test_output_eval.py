"""效果对比保持单变量，统计回答和摘要分开。"""
import importlib.util
from pathlib import Path


def load_eval():
    spec = importlib.util.spec_from_file_location('output_eval', Path(__file__).parents[1] / 'scripts/eval_gpt_output.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_suite_only_changes_one_parameter_at_a_time():
    module = load_eval()
    cases = module.build_cases('gpt-6.1-sol', 2)
    assert len(cases) == 26
    for case in cases:
        body = module.request_body(case)
        assert body['model'] == 'gpt-6.1-sol'
        if case['suite'] == 'verbosity':
            assert body['reasoning'] == {'effort': 'medium'}
        else:
            assert body['text']['verbosity'] == 'medium'
            assert body['reasoning']['effort'] == 'high'
            assert body['reasoning'].get('summary') == (None if case['level'] == 'none' else case['level'])


def test_lengths_are_measured_independently_and_incomplete_is_rejected():
    module = load_eval()
    sample = {'status':'completed', 'model':'gpt-6.1-sol', 'text':{'verbosity':'low'},
              'reasoning':{'effort':'medium'}, 'usage':{'output_tokens':9,'output_tokens_details':{'reasoning_tokens':3}},
              'output':[{'type':'message','content':[{'type':'output_text','text':'答案 一'}]},
                        {'type':'reasoning','summary':[{'type':'summary_text','text':'摘要 二'}]}]}
    row = module.measure_response(sample)
    assert row['answer_chars'] == 3 and row['summary_chars'] == 3
    assert row['reasoning_tokens'] == 3
    assert row['visible_output_tokens'] == 6
    sample['status'] = 'incomplete'
    try:
        module.measure_response(sample)
    except ValueError:
        pass
    else:
        raise AssertionError('不能把被截断的回答算作更简洁')


def test_auto_summary_may_resolve_to_detailed():
    module = load_eval()
    case = next(case for case in module.build_cases('gpt-6.1-sol', 1)
                if case['suite'] == 'summary' and case['level'] == 'auto')
    module.check_echo(case, {'model_echo':'gpt-6.1-sol', 'text_echo':{'verbosity':'medium'},
                             'reasoning_echo':{'effort':'high','summary':'detailed'}})


def test_failed_long_question_cannot_reverse_length_comparison():
    module = load_eval()
    rows = []
    for question, lengths in [(1, (10, 15, 20)), (2, (100, 150, None))]:
        for level, length in zip(('low','medium','high'),lengths):
            rows.append(dict(suite='verbosity',question=question,repeat=1,level=level,
                             status='error' if length is None else 'ok',answer_chars=length))
    paired = module.paired_rows(rows, 'verbosity')
    assert len(paired) == 3
    assert {row['question'] for row in paired} == {1}
