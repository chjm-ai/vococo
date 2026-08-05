# agents/skills — 服务器版平台发行 skill 目录

VOCOCO_MODE=server 时,agent 的 skill 从这里加载(不再是主人个人的 ~/.claude/skills):
- 随仓库/镜像走,与服务器上任何用户的 home 目录无关(物理隔离);
- 放哪个 skill、给哪个 agent 用,由平台在 agents_manifest.yaml(P2)统一声明,
  租户只能在已开通集合内开关,不能自由挂载;
- 每个 skill 一个子目录,内含 SKILL.md(格式与 ~/.claude/skills 一致)。

personal 模式本目录不被读取。
