"""插件 enabled 的受限业务入口；复用运行时串行生命周期，不提供安装或任意配置写入。"""

from pathlib import Path
from typing import Any, Callable, Dict
import asyncio
import hashlib
import logging

import tomlkit

logger = logging.getLogger("owner_admin.plugins")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def catalog(runtime: Any) -> Dict[str, Any]:
    from src.plugin_runtime.runner.manifest_validator import ManifestValidator

    roots = list(runtime._iter_plugin_dirs())
    validator = ManifestValidator(validate_python_package_dependencies=False, log_errors=False, log_compat_warnings=False)
    found: Dict[str, Any] = {}
    builtin_roots = [path.resolve() for path in runtime._get_builtin_plugin_dirs()]
    for path, manifest in validator.iter_plugin_manifests(roots, require_entrypoint=True):
        if manifest.id in found:
            raise ValueError("插件ID重复，不能安全管理")
        config = path / "config.toml"
        doc = tomlkit.parse(config.read_text(encoding="utf-8")) if config.exists() else tomlkit.document()
        enabled = doc.get("plugin", {}).get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("插件 enabled 配置格式无效")
        found[manifest.id] = {"path": path, "type": manifest.plugin_type, "dependencies": set(manifest.plugin_dependency_ids),
                              "protected": manifest.plugin_type != "extension" or bool(manifest.llm_providers)
                              or any(path.resolve().is_relative_to(root) for root in builtin_roots),
                              "enabled": enabled, "digest": digest(config)}
    return found


def protected_target(runtime: Any, entries: Dict[str, Any], plugin_id: str, *, enabling: bool = False) -> None:
    from src.platform_io import DriverKind, get_platform_io_manager

    if plugin_id not in entries:
        raise ValueError("未找到唯一有效的插件清单")
    protected = {key for key, row in entries.items() if row["protected"]}
    protected.update(driver.descriptor.plugin_id for driver in get_platform_io_manager().driver_registry.list(kind=DriverKind.PLUGIN))
    dependencies = {key: set(row["dependencies"]) for key, row in entries.items()}
    for key, values in runtime._build_registered_dependency_map().items():
        dependencies.setdefault(key, set()).update(values)
    pending = list(protected)
    while pending:
        key = pending.pop()
        for dependency in dependencies.get(key, set()) - protected:
            protected.add(dependency)
            pending.append(dependency)
    if plugin_id in protected:
        raise ValueError("关键插件、适配器、认证/管理入口、模型供应组件及其依赖受保护，不能通过QQ变更")
    loaded = {key for supervisor in runtime.supervisors for key in supervisor.get_loaded_plugin_ids()}
    # 所有声明的依赖方均保护，避免 runtime 的依赖联动重载隐式改变其它插件。
    impacted = runtime._collect_reverse_dependents({plugin_id}, dependencies) - {plugin_id}
    if impacted:
        raise ValueError("该插件有已声明依赖方，拒绝隐式联动变更")
    if enabling:
        required = set(dependencies.get(plugin_id, set()))
        pending = list(required)
        while pending:
            dependency = pending.pop()
            for nested in dependencies.get(dependency, set()) - required:
                required.add(nested)
                pending.append(nested)
        if required - loaded:
            raise ValueError("必要依赖未加载，不能通过启用目标插件隐式启动其它组件")


def _replace(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(".owner.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


class PluginChangeError(RuntimeError):
    pass


async def change_enabled(runtime: Any, plugin_id: str, enabled: bool, expected_digest: str,
                         authorize: Callable[[], bool]) -> str:
    if not (await runtime.get_plugin_state_snapshot())["running"]:
        raise ValueError("插件运行时未启动；未将配置值冒充加载成功")
    entries = await asyncio.to_thread(catalog, runtime)
    protected_target(runtime, entries, plugin_id, enabling=enabled)
    row = entries[plugin_id]
    path = row["path"] / "config.toml"
    backup = None
    written = None
    loaded_before = any(plugin_id in supervisor.get_loaded_plugin_ids() for supervisor in runtime.supervisors)
    touched = False

    async def write() -> None:
        nonlocal backup, written, touched
        if not authorize():
            raise PermissionError("主人授权已撤销")
        # 在既有 apply_plugin_config 的同一文件更新锁内重新验证拓扑与文件。
        current = await asyncio.to_thread(catalog, runtime)
        protected_target(runtime, current, plugin_id, enabling=enabled)
        def persist() -> None:
            nonlocal backup, written, touched
            if digest(path) != expected_digest:
                raise ValueError("插件配置已变化，必须重新预览")
            backup = path.read_bytes() if path.exists() else None
            doc = tomlkit.parse(backup.decode("utf-8")) if backup is not None else tomlkit.document()
            if "plugin" not in doc:
                doc["plugin"] = tomlkit.table()
            doc["plugin"]["enabled"] = enabled
            written = tomlkit.dumps(doc).encode("utf-8")
            _replace(path, written)
            touched = True
        await asyncio.to_thread(persist)

    try:
        status = await runtime.apply_plugin_config(plugin_id, write)
        loaded = any(plugin_id in supervisor.get_loaded_plugin_ids() for supervisor in runtime.supervisors)
        final = await asyncio.to_thread(catalog, runtime)
        if status != ("success" if enabled else "inactive") or loaded != enabled or final[plugin_id]["enabled"] != enabled:
            raise RuntimeError("插件实际加载状态与配置不一致")
        return status
    except Exception as error:
        if not touched:
            raise
        logger.exception("插件变更失败，尝试恢复原配置及运行态: %s", plugin_id)
        async def restore() -> None:
            def persist() -> None:
                if path.read_bytes() != written:
                    raise RuntimeError("并发配置已改变，不能覆盖回滚")
                if backup is None:
                    path.unlink()
                else:
                    _replace(path, backup)
            await asyncio.to_thread(persist)
        try:
            await runtime.apply_plugin_config(plugin_id, restore)
            loaded = any(plugin_id in supervisor.get_loaded_plugin_ids() for supervisor in runtime.supervisors)
            if loaded != loaded_before:
                raise RuntimeError("旧运行态未恢复")
        except Exception as rollback_error:
            logger.exception("插件回滚未完成: %s", plugin_id)
            raise PluginChangeError("插件变更失败且回滚未完成；状态不确定，需本地检查；未报告成功") from rollback_error
        raise PluginChangeError("插件变更失败，原配置与加载状态已恢复") from error
