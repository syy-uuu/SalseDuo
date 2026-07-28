"""读取/渲染 prompts/*.prompt 文件——frontmatter（YAML 元数据）+ 纯文本正文。

被 src/graph/*.py（运行时）和 ops/、tests/eval/*.py（非运行时）共同 import。放在
prompts/ 自己底下、不放进 src/：这个模块唯一的职责就是读它自己所在的这个文件夹，
跟 db_client.py/config.py 那种"src/ 里的全项目通用基础设施"不是一回事——如果放进
src/，ops/ 为了加载一个 prompt 就要反过来依赖运行时包，方向别扭。

.prompt 文件格式（YAML frontmatter + 纯文本正文，用两个 `---` 分隔）：
---
name: xxx
version: 1
description: 一句话说明这个 prompt 是干什么的
variables: [foo, bar]      # 正文里用 {foo}/{bar} 占位，没有变量就写 []
---
正文，从这里开始往下全是纯文本，不需要额外缩进或转义，怎么写 prompt 就怎么写。

部署时的前提：ops/deploy_model.py 的 code_paths 除了 src/ 还要包含 prompts/，否则
router.prompt/finalize.prompt/history_framing.prompt 这三个运行时会用到的文件不会被
打进部署产物。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_PROMPTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PromptFile:
    name: str
    version: int
    description: str
    variables: list[str]
    body: str


def load_prompt(name: str) -> PromptFile:
    """读 prompts/{name}.prompt，切开 frontmatter + 正文，返回原始正文（不做变量替换）。"""
    path = _PROMPTS_DIR / f"{name}.prompt"
    if not path.exists():
        raise FileNotFoundError(f"未找到 prompt 文件: {path}")
    raw = path.read_text(encoding="utf-8")

    if not raw.startswith("---"):
        raise ValueError(f"{path} 缺少 frontmatter（文件应以 --- 开头）")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path} frontmatter 格式不对，应该有两个 --- 分隔符")
    _, frontmatter_raw, body = parts
    meta = yaml.safe_load(frontmatter_raw) or {}

    return PromptFile(
        name=meta.get("name", name),
        version=meta.get("version", 1),
        description=meta.get("description", ""),
        variables=list(meta.get("variables", []) or []),
        body=body.strip("\n"),
    )


def render_prompt(name: str, **kwargs) -> str:
    """load_prompt 之后校验 kwargs 跟 frontmatter 里声明的 variables 是否一一对上
    （多传/少传都报错，不是静默忽略——这样 variables 字段才是真的在被校验，不只是文档），
    再用 body.format(**kwargs) 替换占位符。variables 声明为空的 prompt 也走这个函数，
    kwargs 留空即可，所有调用方统一用这一个入口。"""
    prompt = load_prompt(name)
    declared = set(prompt.variables)
    provided = set(kwargs.keys())
    if declared != provided:
        problems = []
        missing = declared - provided
        extra = provided - declared
        if missing:
            problems.append(f"缺少: {sorted(missing)}")
        if extra:
            problems.append(f"多余: {sorted(extra)}")
        raise ValueError(
            f"prompt '{name}' 的 variables 声明是 {sorted(declared)}，"
            f"但调用时传的是 {sorted(provided)}（{'; '.join(problems)}）"
        )
    return prompt.body.format(**kwargs) if kwargs else prompt.body
