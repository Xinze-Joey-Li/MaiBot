"""定制补丁的离线语法/应用验证及补丁中真实回戳方法的行为回归。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import ast
import re
import subprocess
import textwrap

import pytest

from src.plugin_runtime.runner.manifest_validator import VersionComparator

PATCH = Path(__file__).resolve().parents[2] / "adapter-patches/MaiBot-SnowLuma-Adapter/v0.1.1.patch"


@pytest.fixture
def applied_patch(tmp_path):
    """重建补丁原始上下文，离线检查每个 hunk；真实完整基线另在验收中验证。"""
    files = {}
    path = None
    old_remaining = new_remaining = 0
    old_lines = []
    start = 0
    for line in PATCH.read_bytes().splitlines():
        if line.startswith(b"diff --git "):
            assert old_remaining == new_remaining == 0
            path = line.split(b" b/", 1)[1].decode()
        match = re.match(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            assert old_remaining == new_remaining == 0
            start = int(match[1])
            old_remaining, new_remaining = int(match[2] or b"1"), int(match[4] or b"1")
            old_lines = []
            continue
        if old_remaining or new_remaining:
            assert line[:1] in (b" ", b"+", b"-", b"\\"), "补丁上下文缺少前缀"
            if line[:1] in (b" ", b"-"):
                old_remaining -= 1
                old_lines.append(line[1:])
            if line[:1] in (b" ", b"+"):
                new_remaining -= 1
            assert old_remaining >= 0 and new_remaining >= 0
            if old_remaining == new_remaining == 0:
                content = files.setdefault(path, [])
                content.extend([b""] * (start - 1 - len(content)))
                content.extend(old_lines)
    assert old_remaining == new_remaining == 0
    assert set(files) == {"CHANGELOG.md", "README.md", "_manifest.json", "config.py", "runtime/router.py"}
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\n".join(content) + b"\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    for args in (["--check"], []):
        subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(tmp_path), "apply", *args, str(PATCH)],
                       check=True, capture_output=True)
    return tmp_path


def test_intentional_version_and_update_order_are_preserved(applied_patch):
    manifest = (applied_patch / "_manifest.json").read_text(encoding="utf-8")
    assert '"version": "0.1.1"' in manifest
    assert VersionComparator.normalize_version("0.1.1") == "0.1.1"
    assert VersionComparator.compare("1.1.5", "0.1.1") > 0
    router = (applied_patch / "runtime/router.py").read_text(encoding="utf-8")
    assert router.index("if await self._handle_auto_poke_back") < router.index("await self.route_notice_payload")


@pytest.fixture
def poke_method(applied_patch):
    source = (applied_patch / "runtime/router.py").read_text(encoding="utf-8")
    start = source.index("    async def _handle_auto_poke_back(")
    end = source.index("    async def route_notice_payload(", start)
    tree = ast.parse(textwrap.dedent(source[start:end]))
    function = tree.body[0]
    function.returns = None
    for arg in function.args.args:
        arg.annotation = None
    namespace = {}
    exec(compile(ast.fix_missing_locations(tree), str(PATCH), "exec"), namespace)
    return namespace["_handle_auto_poke_back"]


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", [None, "303"])
async def test_group_private_and_repeated_events_poke_once_each(poke_method, group_id):
    sender = AsyncMock()
    runtime = SimpleNamespace(runtime_state=SimpleNamespace(account_id="101"), query_service=SimpleNamespace(send_poke=sender))
    router = SimpleNamespace(_require_runtime=lambda: runtime, _logger=Mock())
    settings = SimpleNamespace(notice=SimpleNamespace(enabled=True, auto_poke_back=True))
    payload = {"notice_type": "notify", "sub_type": "poke", "user_id": "202", "target_id": "101", "group_id": group_id}
    assert await poke_method(router, payload, "101", settings)
    assert await poke_method(router, payload, "101", settings)
    assert sender.await_count == 2
    sender.assert_awaited_with(user_id=202, group_id=303 if group_id else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,enabled,auto,result", [
    ({"user_id": "101"}, True, True, True),
    ({"target_id": "999"}, True, True, False),
    ({"sub_type": "other"}, True, True, False),
    ({}, False, True, False), ({}, True, False, False),
])
async def test_self_echo_other_target_and_switches_do_not_poke(poke_method, change, enabled, auto, result):
    sender = AsyncMock()
    runtime = SimpleNamespace(runtime_state=SimpleNamespace(account_id="101"), query_service=SimpleNamespace(send_poke=sender))
    router = SimpleNamespace(_require_runtime=lambda: runtime, _logger=Mock())
    settings = SimpleNamespace(notice=SimpleNamespace(enabled=enabled, auto_poke_back=auto))
    payload = {"notice_type": "notify", "sub_type": "poke", "user_id": "202", "target_id": "101", **change}
    assert await poke_method(router, payload, "101", settings) is result
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_poke_failure_is_visible_and_not_retried(poke_method):
    sender = AsyncMock(side_effect=RuntimeError("platform failed"))
    runtime = SimpleNamespace(runtime_state=SimpleNamespace(account_id="101"), query_service=SimpleNamespace(send_poke=sender))
    router = SimpleNamespace(_require_runtime=lambda: runtime, _logger=Mock())
    settings = SimpleNamespace(notice=SimpleNamespace(enabled=True, auto_poke_back=True))
    payload = {"notice_type": "notify", "sub_type": "poke", "user_id": "202", "target_id": "101"}
    with pytest.raises(RuntimeError, match="platform failed"):
        await poke_method(router, payload, "101", settings)
    sender.assert_awaited_once()
