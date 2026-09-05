#!/usr/bin/env python3
"""仓库专用静态检查：不是通用 YAML 解析器，也不是模型行为评测器。"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys
from typing import Any

import mingbian_session as session

REQUIRED = (
    'SKILL.md', 'README.md', 'PRODUCT_INTRO.md', 'CHANGELOG.md', 'LICENSE',
    'agents/openai.yaml', 'references/foundations.md', 'references/interview-protocol.md',
    'references/session-state.md', 'references/deliverable-template.md',
    'references/examples.md', 'references/evaluation.md', 'test-prompts.json',
    'evals/behavior-cases.json', 'scripts/mingbian_session.py',
    'scripts/test_mingbian_session.py', 'examples/tree.json', 'examples/empty-tree.json',
    'examples/sample-input.json', 'examples/sample-plan.md', 'examples/changes.json',
    'examples/mingbian-record.md', 'examples/review-checks.json',
)


def validate(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    checks: dict[str, Any] = {}
    for filename in REQUIRED:
        if not (root / filename).is_file():
            errors.append(f'缺少必需文件：{filename}')
    try:
        content = (root / 'SKILL.md').read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        return {'ok': False, 'errors': errors + [str(exc)], 'checks': checks}
    parts = content.split('---', 2)
    if len(parts) != 3 or parts[0].strip():
        errors.append('SKILL.md 必须以 YAML frontmatter 开始并完整闭合')
        front = ''
    else:
        front = parts[1]
    name = re.search(r'^name:\s*([a-z0-9-]+)\s*$', front, re.M)
    if name is None or name.group(1) != 'mingbian' or root.name != 'mingbian':
        errors.append('name 与父目录名必须都是 mingbian')
    description = re.search(r'^description:\s*>-?\s*\n((?:[ \t]+[^\n]*\n?)+)', front, re.M)
    if description is None:
        errors.append('本仓库 description 应采用非空 YAML 折叠块')
    else:
        text = ' '.join(line.strip() for line in description.group(1).splitlines()).strip()
        checks['description_characters'] = len(text)
        if not 1 <= len(text) <= 1024:
            errors.append('description 必须为 1—1024 字符')
    if f'version: "{session.VERSION}"' not in front:
        errors.append('主文件版本与状态脚本不一致')
    checks['skill_lines'] = len(content.splitlines())
    checks['skill_bytes'] = len(content.encode('utf-8'))
    if checks['skill_lines'] >= 500:
        errors.append('本仓库主文件应少于 500 行')

    # Ignore external links and anchors; all local Markdown resources must exist.
    link_count = 0
    for path in sorted(root.rglob('*.md')):
        try:
            markdown = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            errors.append(str(exc)); continue
        for target in re.findall(r'(?<!!)\[[^\]]+\]\(([^\s)]+)(?:\s+"[^"]*")?\)', markdown):
            if target.startswith(('#', 'http://', 'https://', 'mailto:')):
                continue
            relative = target.split('#', 1)[0]
            if not relative:
                continue
            resolved = (path.parent / relative).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                errors.append(f'{path.relative_to(root)} 中的本地引用不存在或越界：{target}')
            link_count += 1
    checks['local_markdown_links'] = link_count

    checked_python = 0
    for path in sorted((root / 'scripts').glob('*.py')):
        try:
            # Parse against the declared minimum syntax level; not an OS/runtime matrix.
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3, 10))
            checked_python += 1
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(f'Python 语法检查失败：{path.name}：{exc}')
    checks['python_files_parsed'] = checked_python

    parsed: dict[str, Any] = {}
    for path in sorted(root.rglob('*.json')):
        try:
            parsed[str(path.relative_to(root))] = session.read_json(path)
        except session.UserError as exc:
            errors.append(str(exc))
    checks['json_files_parsed'] = len(parsed)
    for path in ('examples/tree.json', 'examples/empty-tree.json'):
        if path in parsed:
            try:
                session.load_tree(root / path)
            except session.UserError as exc:
                errors.append(f'{path}：{exc}')
    try:
        fixture = parsed.get('examples/review-checks.json')
        if fixture is not None:
            session.validate_checks(fixture, 'pilot')
    except session.UserError as exc:
        errors.append(f'示例复核无效：{exc}')

    triggers = parsed.get('test-prompts.json', {})
    behavior = parsed.get('evals/behavior-cases.json', {})
    if isinstance(triggers, dict):
        if triggers.get('skill_version') != session.VERSION:
            errors.append('触发样例版本不一致')
        if triggers.get('evaluation_status') != 'inputs_only_not_model_tested':
            errors.append('触发样例须明确不是已执行的模型测试结果')
        count = 0
        ids: set[str] = set()
        for group in ('should_trigger', 'should_not_trigger'):
            items = triggers.get(group)
            if not isinstance(items, list) or not items:
                errors.append(f'触发样例 {group} 缺失'); continue
            for case in items:
                if not isinstance(case, dict) or not isinstance(case.get('prompt'), str) or not case['prompt'].strip():
                    errors.append(f'触发样例 {group} 有无效内容'); continue
                identifier = case.get('id', case['prompt'])
                if not isinstance(identifier, str) or identifier in ids:
                    errors.append(f'触发样例标识重复或无效：{identifier}')
                else:
                    ids.add(identifier)
                count += 1
        checks['trigger_inputs_prepared_not_run'] = count
    else:
        errors.append('触发样例必须是 JSON 对象')
    if isinstance(behavior, dict):
        if behavior.get('status') != 'test_inputs_only_not_executed_against_model':
            errors.append('行为样例须明确不是已执行的模型测试结果')
        cases = behavior.get('cases')
        if not isinstance(cases, list) or not cases:
            errors.append('行为测试 cases 缺失')
        else:
            ids = set()
            for case in cases:
                if not isinstance(case, dict):
                    errors.append('行为用例必须是对象'); continue
                identifier = case.get('id')
                if not isinstance(identifier, str) or identifier in ids:
                    errors.append('行为用例 id 缺失或重复')
                else:
                    ids.add(identifier)
                for field in ('prompt', 'title'):
                    if not isinstance(case.get(field), str) or not case[field].strip():
                        errors.append(f'{identifier} 缺少 {field}')
                for field in ('must', 'forbidden'):
                    values = case.get(field)
                    if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                        errors.append(f'{identifier} 缺少有效 {field}')
            checks['behavior_inputs_prepared_not_run'] = len(cases)
    else:
        errors.append('行为样例必须是 JSON 对象')
    if (root / 'agents/openai.yaml').is_file():
        agent = (root / 'agents/openai.yaml').read_text(encoding='utf-8')
        if '$mingbian' not in agent or 'display_name: "明辨"' not in agent:
            errors.append('展示配置名称或默认触发提示不一致')
    checks['scope'] = 'repository_specific_static_checks_not_model_evaluation'
    return {'ok': not errors, 'version': session.VERSION, 'errors': errors, 'checks': checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = validate(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
