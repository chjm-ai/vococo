"""标题栏显示仓库名，分支标签仅隐藏 vococo/ 前缀。"""
from pathlib import Path
import json
import shutil
import subprocess

import pytest


STATIC = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static"


def test_git_header_repo_name_and_branch_prefix():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行真实前端标题栏代码")
    script = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(STATIC+'/sidebar.js','utf8').split('// ── 会话列表')[0];
const nodes={};
const ctx={S:{conv:'main'},document:{addEventListener(){}},
  $:s=>nodes[s]||(nodes[s]={hidden:true,classList:{toggle(){}}}),
  agentForConv:()=>({name:'Vococo 开发',main_conv:'main'}),
  esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;')};
vm.createContext(ctx);
vm.runInContext(source,ctx);
const data={is_project:true,is_repo:true,name:'vococo',branch:'vococo/abc',
  dirty:2,unmerged:1,added:3,removed:4};
ctx.applyGitBtn(data);
assert.equal(nodes['#convProjName'].textContent,'vococo');
assert.equal(nodes['#convProjName'].hidden,false,'Agent 主会话也显示仓库');
assert.ok(nodes['#convGit'].innerHTML.includes('⎇ abc'));
assert.ok(!nodes['#convGit'].innerHTML.includes('vococo/'));
for(const text of ['↑1','•2','+3','-4']) assert.ok(nodes['#convGit'].innerHTML.includes(text));
assert.equal(data.branch,'vococo/abc','不能改变真实分支名');
for(const branch of ['main','feature/vococo/abc','my-vococo/abc']){
  assert.ok(ctx.gitBtnLabel({...data,branch}).includes('⎇ '+branch));
}
assert.ok(ctx.gitBtnLabel({...data,branch:'vococo/<test>'}).includes('&lt;test>'));
ctx.applyGitBtn({...data,name:'other-repo'});
assert.equal(nodes['#convProjName'].textContent,'other-repo');
ctx.applyGitBtn({...data,is_repo:false});
assert.equal(nodes['#convGit'].textContent,'⎇ 非 git 仓库');
ctx.applyGitBtn(null);
assert.equal(nodes['#convProjName'].hidden,true);
assert.equal(nodes['#convGit'].hidden,true);
'''
    result = subprocess.run(
        [node, "-e", "const STATIC=" + json.dumps(str(STATIC)) + ";\n" + script],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
