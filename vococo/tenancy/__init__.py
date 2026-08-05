"""多租户层(仅 VOCOCO_MODE=server 激活)。

personal 模式下本层全部透明回落:context.current() 恒为 "local",
paths.* 解析到 config 里现有的全局路径——行为与引入本层之前完全一致。
server 模式下按请求/任务注入 tenant_id,数据落到 data/tenants/<tid>/ 物理隔离。

设计约束(见 docs/design/server-edition-tech-plan.md §1 铁律):
业务代码只调本包的 context/paths 抽象,不各自判断 config.IS_SERVER。
"""
from .. import config as _config  # noqa: F401 —— 让 import tenancy 处可先确认模式
from . import context, paths  # noqa: F401
