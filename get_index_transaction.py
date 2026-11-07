#!/usr/bin/env python3
"""Recoverable multi-file replacement with a final commit-marker file."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat as stat_module
from typing import Callable, Iterator


JOURNAL_KEYS = {
    "schema_version",
    "transaction_id",
    "commit_marker",
    "state_dev",
    "state_ino",
    "backup_dir",
    "backup_dev",
    "backup_ino",
    "entries",
}
ENTRY_KEYS = {
    "target",
    "target_name",
    "parent_dev",
    "parent_ino",
    "stage_name",
    "backup_name",
    "old_sha256",
    "new_sha256",
    "mode",
    "atime_ns",
    "mtime_ns",
}
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
MAX_MANAGED_FILE_BYTES = 50 * 1024 * 1024


class TransactionError(ValueError):
    """A fail-closed transaction error safe to surface to the operator."""


class _JournalWriteError(Exception):
    def __init__(self, published: bool) -> None:
        super().__init__("journal write failed")
        self.published = published


class LockedDirectories:
    """Identity guard for every directory participating in one apply run."""

    def __init__(self, identities: dict[Path, tuple[int, int]]) -> None:
        self.identities = dict(identities)

    def verify(self) -> None:
        for path, (device, inode) in self.identities.items():
            _verify_visible_directory(path, device, inode)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _directory_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


def _open_verified_directory(path: Path) -> tuple[int, os.stat_result]:
    try:
        visible = path.lstat()
    except OSError as exc:
        raise TransactionError("受控更新目录无法检查") from exc
    if stat_module.S_ISLNK(visible.st_mode) or not stat_module.S_ISDIR(visible.st_mode):
        raise TransactionError("受控更新目录必须是真实目录")
    try:
        descriptor = os.open(path, _directory_flags())
    except OSError as exc:
        raise TransactionError("受控更新目录无法安全打开") from exc
    opened = os.fstat(descriptor)
    if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(descriptor)
        raise TransactionError("受控更新目录在检查期间发生变化")
    return descriptor, opened


def _open_verified_child_directory(
    parent_descriptor: int,
    name: str,
    expected_dev: int,
    expected_ino: int,
) -> int:
    try:
        visible = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise TransactionError("应用事务备份目录无法检查") from exc
    if (
        stat_module.S_ISLNK(visible.st_mode)
        or not stat_module.S_ISDIR(visible.st_mode)
        or (visible.st_dev, visible.st_ino) != (expected_dev, expected_ino)
    ):
        raise TransactionError("应用事务备份目录身份不匹配")
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        raise TransactionError("应用事务备份目录无法安全打开") from exc
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (expected_dev, expected_ino):
        os.close(descriptor)
        raise TransactionError("应用事务备份目录在检查期间发生变化")
    return descriptor


def _verify_visible_directory(path: Path, expected_dev: int, expected_ino: int) -> None:
    try:
        visible = path.lstat()
    except OSError as exc:
        raise TransactionError("受控更新目录在提交前不可用") from exc
    if (
        stat_module.S_ISLNK(visible.st_mode)
        or not stat_module.S_ISDIR(visible.st_mode)
        or (visible.st_dev, visible.st_ino) != (expected_dev, expected_ino)
    ):
        raise TransactionError("受控更新目录在提交前发生变化")


def _open_bound_directory(
    path: Path,
    expected_directory_identities: dict[Path, tuple[int, int]] | None,
) -> tuple[int, os.stat_result]:
    descriptor, directory_stat = _open_verified_directory(path)
    expected = (
        expected_directory_identities.get(path)
        if expected_directory_identities is not None
        else None
    )
    if expected is not None and (directory_stat.st_dev, directory_stat.st_ino) != expected:
        os.close(descriptor)
        raise TransactionError("受控更新目录与已锁定目录不一致")
    return descriptor, directory_stat


def _verify_bound_directories(
    expected_directory_identities: dict[Path, tuple[int, int]] | None,
) -> None:
    if expected_directory_identities is None:
        return
    for path, (device, inode) in expected_directory_identities.items():
        _verify_visible_directory(path, device, inode)


def _read_regular_at(directory_descriptor: int, name: str) -> tuple[bytes, int, int, int]:
    try:
        file_stat = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise TransactionError("受控目标文件不存在或无法检查") from exc
    if (
        stat_module.S_ISLNK(file_stat.st_mode)
        or not stat_module.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
    ):
        raise TransactionError("受控目标文件必须是单硬链接普通文件")
    if file_stat.st_size > MAX_MANAGED_FILE_BYTES:
        raise TransactionError("受控目标文件超过大小限制")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        opened_stat = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (file_stat.st_dev, file_stat.st_ino)
        ):
            raise TransactionError("受控目标文件在打开期间发生变化")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(MAX_MANAGED_FILE_BYTES + 1)
    except OSError as exc:
        raise TransactionError("受控目标文件无法读取") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(content) > MAX_MANAGED_FILE_BYTES:
        raise TransactionError("受控目标文件超过大小限制")
    return (
        content,
        file_stat.st_mode & 0o777,
        file_stat.st_atime_ns,
        file_stat.st_mtime_ns,
    )


def _read_managed_file(path: Path) -> bytes:
    directory_descriptor, _ = _open_verified_directory(path.parent)
    try:
        content, _, _, _ = _read_regular_at(directory_descriptor, path.name)
        return content
    finally:
        os.close(directory_descriptor)


def _create_file_at(
    directory_descriptor: int,
    name: str,
    content: bytes,
    mode: int,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, mode, dir_fd=directory_descriptor)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise TransactionError("受控临时文件无法写入") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_json_at(directory_descriptor: int, name: str, payload: dict) -> None:
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temp_name = f".get-index-journal-{secrets.token_hex(12)}.tmp"
    published = False
    try:
        _create_file_at(directory_descriptor, temp_name, serialized, 0o600)
        try:
            os.replace(
                temp_name,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            published = True
            os.fsync(directory_descriptor)
        except BaseException as exc:
            if not published:
                try:
                    current, _, _, _ = _read_regular_at(directory_descriptor, name)
                    published = current == serialized
                except TransactionError:
                    pass
            raise _JournalWriteError(published) from exc
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            pass


@contextmanager
def transaction_lock(*directories: Path) -> Iterator[LockedDirectories]:
    if not directories:
        raise TransactionError("GET 索引应用缺少需要锁定的目录")
    normalized_paths = sorted(
        {Path(os.path.abspath(path)) for path in directories},
        key=str,
    )
    identities: dict[Path, tuple[int, int]] = {}
    descriptors_by_identity: dict[tuple[int, int], int] = {}
    acquired: list[int] = []
    try:
        for path in normalized_paths:
            descriptor, directory_stat = _open_verified_directory(path)
            identity = (directory_stat.st_dev, directory_stat.st_ino)
            identities[path] = identity
            if identity in descriptors_by_identity:
                os.close(descriptor)
            else:
                descriptors_by_identity[identity] = descriptor
        for identity in sorted(descriptors_by_identity):
            descriptor = descriptors_by_identity[identity]
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise TransactionError("已有另一个 GET 索引应用正在运行") from exc
            acquired.append(descriptor)
        locked = LockedDirectories(identities)
        locked.verify()
        yield locked
        locked.verify()
    finally:
        for descriptor in reversed(acquired):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        for descriptor in descriptors_by_identity.values():
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_journal(
    raw: object,
    allowed_targets: set[Path],
    commit_marker: Path,
) -> dict:
    if not isinstance(raw, dict) or set(raw) != JOURNAL_KEYS:
        raise TransactionError("遗留应用事务日志结构无效")
    if raw["schema_version"] != 1 or not isinstance(raw["transaction_id"], str):
        raise TransactionError("遗留应用事务日志版本无效")
    if not HEX_32.fullmatch(raw["transaction_id"]):
        raise TransactionError("遗留应用事务编号无效")
    expected_backup_dir = f"apply-transaction-{raw['transaction_id']}"
    if raw["backup_dir"] != expected_backup_dir:
        raise TransactionError("遗留应用事务备份目录无效")
    if any(
        type(raw[key]) is not int or raw[key] < 0
        for key in ("state_dev", "state_ino", "backup_dev", "backup_ino")
    ):
        raise TransactionError("遗留应用事务目录身份无效")
    if raw["commit_marker"] != str(commit_marker):
        raise TransactionError("遗留应用事务提交标记不匹配")
    entries = raw["entries"]
    if not isinstance(entries, list) or not entries:
        raise TransactionError("遗留应用事务没有受控目标")
    normalized_allowed = {str(path): path for path in allowed_targets}
    seen_targets: set[str] = set()
    seen_names: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            raise TransactionError("遗留应用事务目标结构无效")
        target_text = entry["target"]
        if (
            not isinstance(target_text, str)
            or target_text not in normalized_allowed
            or target_text in seen_targets
        ):
            raise TransactionError("遗留应用事务包含未授权或重复目标")
        target = normalized_allowed[target_text]
        if (
            not isinstance(entry["target_name"], str)
            or entry["target_name"] != target.name
            or "/" in entry["target_name"]
        ):
            raise TransactionError("遗留应用事务目标名称无效")
        expected_stage_name = (
            f".get-index-apply-{raw['transaction_id']}-{index}.tmp"
        )
        expected_backup_name = f"old-{index}.bin"
        if (
            not isinstance(entry["stage_name"], str)
            or not isinstance(entry["backup_name"], str)
            or entry["stage_name"] != expected_stage_name
            or entry["backup_name"] != expected_backup_name
            or entry["stage_name"] in seen_names
            or entry["backup_name"] in seen_names
        ):
            raise TransactionError("遗留应用事务临时名称无效")
        seen_names.update((entry["stage_name"], entry["backup_name"]))
        for key in ("old_sha256", "new_sha256"):
            if not isinstance(entry[key], str) or not HEX_64.fullmatch(entry[key]):
                raise TransactionError("遗留应用事务文件摘要无效")
        if (
            type(entry["parent_dev"]) is not int
            or type(entry["parent_ino"]) is not int
            or entry["parent_dev"] < 0
            or entry["parent_ino"] < 0
        ):
            raise TransactionError("遗留应用事务目录身份无效")
        if type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o777:
            raise TransactionError("遗留应用事务权限无效")
        if any(
            type(entry[key]) is not int or entry[key] < 0
            for key in ("atime_ns", "mtime_ns")
        ):
            raise TransactionError("遗留应用事务时间信息无效")
        seen_targets.add(target_text)
    if str(commit_marker) not in seen_targets:
        raise TransactionError("遗留应用事务缺少增量快照提交标记")
    if entries[-1]["target"] != str(commit_marker):
        raise TransactionError("遗留应用事务提交标记顺序无效")
    return raw


def _read_journal(path: Path, allowed_targets: set[Path], commit_marker: Path) -> dict:
    directory_descriptor, _ = _open_verified_directory(path.parent)
    try:
        content, _, _, _ = _read_regular_at(directory_descriptor, path.name)
    finally:
        os.close(directory_descriptor)
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransactionError("遗留应用事务日志无法解析") from exc
    return _validate_journal(raw, allowed_targets, commit_marker)


def _open_journal_state_directory(journal_path: Path, journal: dict) -> int:
    descriptor, directory_stat = _open_verified_directory(journal_path.parent)
    if (directory_stat.st_dev, directory_stat.st_ino) != (
        journal["state_dev"],
        journal["state_ino"],
    ):
        os.close(descriptor)
        raise TransactionError("遗留应用事务状态目录身份不匹配")
    return descriptor


def _open_journal_backup_directory(
    journal_path: Path,
    journal: dict,
) -> tuple[int, int]:
    state_descriptor = _open_journal_state_directory(journal_path, journal)
    try:
        backup_descriptor = _open_verified_child_directory(
            state_descriptor,
            journal["backup_dir"],
            journal["backup_dev"],
            journal["backup_ino"],
        )
    except BaseException:
        os.close(state_descriptor)
        raise
    return state_descriptor, backup_descriptor


def _entry_current_digest(entry: dict) -> str:
    target = Path(entry["target"])
    directory_descriptor, directory_stat = _open_verified_directory(target.parent)
    try:
        if (directory_stat.st_dev, directory_stat.st_ino) != (
            entry["parent_dev"],
            entry["parent_ino"],
        ):
            raise TransactionError("遗留应用事务目标目录身份不匹配")
        content, _, _, _ = _read_regular_at(directory_descriptor, entry["target_name"])
        return sha256_bytes(content)
    finally:
        os.close(directory_descriptor)


def _backup_bytes(backup_descriptor: int, entry: dict) -> bytes:
    content, _, _, _ = _read_regular_at(backup_descriptor, entry["backup_name"])
    if sha256_bytes(content) != entry["old_sha256"]:
        raise TransactionError("遗留应用事务备份摘要不匹配")
    return content


def _restore_entry(
    entry: dict,
    backup_content: bytes,
    expected_current_sha256: str,
) -> None:
    target = Path(entry["target"])
    directory_descriptor, directory_stat = _open_verified_directory(target.parent)
    recovery_name = f".get-index-recovery-{secrets.token_hex(12)}.tmp"
    try:
        if (directory_stat.st_dev, directory_stat.st_ino) != (
            entry["parent_dev"],
            entry["parent_ino"],
        ):
            raise TransactionError("遗留应用事务目标目录身份不匹配")
        current, _, _, _ = _read_regular_at(
            directory_descriptor,
            entry["target_name"],
        )
        if sha256_bytes(current) != expected_current_sha256:
            raise TransactionError("遗留应用事务目标在恢复期间发生变化")
        _create_file_at(directory_descriptor, recovery_name, backup_content, entry["mode"])
        current, _, _, _ = _read_regular_at(
            directory_descriptor,
            entry["target_name"],
        )
        if sha256_bytes(current) != expected_current_sha256:
            raise TransactionError("遗留应用事务目标在恢复期间发生变化")
        _verify_visible_directory(target.parent, entry["parent_dev"], entry["parent_ino"])
        os.replace(
            recovery_name,
            entry["target_name"],
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.utime(
            entry["target_name"],
            ns=(entry["atime_ns"], entry["mtime_ns"]),
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        restored_descriptor = os.open(
            entry["target_name"],
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_descriptor,
        )
        try:
            os.fsync(restored_descriptor)
        finally:
            os.close(restored_descriptor)
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise TransactionError("遗留应用事务无法回滚") from exc
    finally:
        try:
            os.unlink(recovery_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(directory_descriptor)


def _unlink_verified_optional_file(
    directory_descriptor: int,
    name: str,
    expected_digest: str,
    label: str,
) -> None:
    try:
        file_stat = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TransactionError(f"{label}无法检查") from exc
    if (
        stat_module.S_ISLNK(file_stat.st_mode)
        or not stat_module.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
    ):
        raise TransactionError(f"{label}不是安全的单硬链接普通文件")
    content, _, _, _ = _read_regular_at(directory_descriptor, name)
    if sha256_bytes(content) != expected_digest:
        raise TransactionError(f"{label}摘要不匹配")
    try:
        os.unlink(name, dir_fd=directory_descriptor)
    except OSError as exc:
        raise TransactionError(f"{label}无法清理") from exc


def _cleanup_transaction(journal_path: Path, journal: dict) -> None:
    state_descriptor = _open_journal_state_directory(journal_path, journal)
    backup_descriptor = -1
    try:
        try:
            backup_descriptor = _open_verified_child_directory(
                state_descriptor,
                journal["backup_dir"],
                journal["backup_dev"],
                journal["backup_ino"],
            )
        except TransactionError as exc:
            try:
                os.stat(
                    journal["backup_dir"],
                    dir_fd=state_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                backup_descriptor = -1
            else:
                raise exc

        for entry in journal["entries"]:
            target = Path(entry["target"])
            directory_descriptor, directory_stat = _open_verified_directory(target.parent)
            try:
                if (directory_stat.st_dev, directory_stat.st_ino) != (
                    entry["parent_dev"],
                    entry["parent_ino"],
                ):
                    raise TransactionError("遗留应用事务目标目录身份不匹配")
                _unlink_verified_optional_file(
                    directory_descriptor,
                    entry["stage_name"],
                    entry["new_sha256"],
                    "遗留应用事务暂存文件",
                )
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            if backup_descriptor >= 0:
                _unlink_verified_optional_file(
                    backup_descriptor,
                    entry["backup_name"],
                    entry["old_sha256"],
                    "遗留应用事务备份文件",
                )

        if backup_descriptor >= 0:
            os.fsync(backup_descriptor)
            os.close(backup_descriptor)
            backup_descriptor = -1
            visible_backup = os.stat(
                journal["backup_dir"],
                dir_fd=state_descriptor,
                follow_symlinks=False,
            )
            if (
                stat_module.S_ISLNK(visible_backup.st_mode)
                or not stat_module.S_ISDIR(visible_backup.st_mode)
                or (visible_backup.st_dev, visible_backup.st_ino)
                != (journal["backup_dev"], journal["backup_ino"])
            ):
                raise TransactionError("应用事务备份目录在清理前发生变化")
            os.rmdir(journal["backup_dir"], dir_fd=state_descriptor)
        else:
            try:
                os.stat(
                    journal["backup_dir"],
                    dir_fd=state_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise TransactionError("应用事务备份目录在清理期间重新出现")
        try:
            os.unlink(journal_path.name, dir_fd=state_descriptor)
        except FileNotFoundError:
            pass
        os.fsync(state_descriptor)
    except OSError as exc:
        raise TransactionError("遗留应用事务清理失败") from exc
    finally:
        if backup_descriptor >= 0:
            os.close(backup_descriptor)
        os.close(state_descriptor)


def recover_transaction(
    journal_path: Path,
    allowed_targets: set[Path],
    commit_marker: Path,
    expected_directory_identities: dict[Path, tuple[int, int]] | None = None,
    validate_committed: Callable[[], None] | None = None,
) -> str:
    _verify_bound_directories(expected_directory_identities)
    try:
        journal_path.lstat()
    except FileNotFoundError:
        return "none"
    except OSError as exc:
        raise TransactionError("遗留应用事务日志无法检查") from exc
    journal = _read_journal(journal_path, allowed_targets, commit_marker)
    state_descriptor = _open_journal_state_directory(journal_path, journal)
    os.close(state_descriptor)
    digests = {
        entry["target"]: _entry_current_digest(entry)
        for entry in journal["entries"]
    }
    for entry in journal["entries"]:
        if digests[entry["target"]] not in {entry["old_sha256"], entry["new_sha256"]}:
            raise TransactionError("遗留应用事务目标出现未知内容，停止自动恢复")
    marker_entry = next(
        entry for entry in journal["entries"] if entry["target"] == str(commit_marker)
    )
    all_new = all(digests[entry["target"]] == entry["new_sha256"] for entry in journal["entries"])
    if digests[str(commit_marker)] == marker_entry["new_sha256"] and all_new:
        _verify_bound_directories(expected_directory_identities)
        if validate_committed is not None:
            validate_committed()
        _verify_bound_directories(expected_directory_identities)
        _cleanup_transaction(journal_path, journal)
        return "committed"
    if digests[str(commit_marker)] == marker_entry["new_sha256"]:
        raise TransactionError("已提交事务的受控目标出现混合状态，停止自动恢复")
    all_old = all(digests[entry["target"]] == entry["old_sha256"] for entry in journal["entries"])
    if all_old:
        _verify_bound_directories(expected_directory_identities)
        _cleanup_transaction(journal_path, journal)
        return "rolled_back"
    state_descriptor, backup_descriptor = _open_journal_backup_directory(
        journal_path,
        journal,
    )
    try:
        backups = {
            entry["target"]: _backup_bytes(backup_descriptor, entry)
            for entry in journal["entries"]
            if digests[entry["target"]] != entry["old_sha256"]
        }
    finally:
        os.close(backup_descriptor)
        os.close(state_descriptor)
    for entry in journal["entries"]:
        if digests[entry["target"]] != entry["old_sha256"]:
            _verify_bound_directories(expected_directory_identities)
            _restore_entry(
                entry,
                backups[entry["target"]],
                digests[entry["target"]],
            )
    if any(_entry_current_digest(entry) != entry["old_sha256"] for entry in journal["entries"]):
        raise TransactionError("遗留应用事务回滚后校验失败")
    _verify_bound_directories(expected_directory_identities)
    _cleanup_transaction(journal_path, journal)
    return "rolled_back"


def _prepare_transaction(
    contents: dict[Path, bytes],
    commit_marker: Path,
    journal_path: Path,
    expected_old_sha256: dict[Path, str],
    expected_directory_identities: dict[Path, tuple[int, int]] | None = None,
) -> tuple[dict, dict[Path, int]]:
    transaction_id = secrets.token_hex(16)
    backup_dir_name = f"apply-transaction-{transaction_id}"
    state_descriptor, state_stat = _open_bound_directory(
        journal_path.parent,
        expected_directory_identities,
    )
    backup_descriptor = -1
    backup_created = False
    try:
        try:
            os.mkdir(backup_dir_name, 0o700, dir_fd=state_descriptor)
            backup_created = True
            os.fsync(state_descriptor)
            backup_visible = os.stat(
                backup_dir_name,
                dir_fd=state_descriptor,
                follow_symlinks=False,
            )
            if not stat_module.S_ISDIR(backup_visible.st_mode):
                raise TransactionError("应用事务备份目录创建结果无效")
            try:
                backup_descriptor = os.open(
                    backup_dir_name,
                    _directory_flags(),
                    dir_fd=state_descriptor,
                )
            except OSError as exc:
                raise TransactionError("应用事务备份目录无法安全打开") from exc
            backup_stat = os.fstat(backup_descriptor)
            if (backup_stat.st_dev, backup_stat.st_ino) != (
                backup_visible.st_dev,
                backup_visible.st_ino,
            ):
                raise TransactionError("应用事务备份目录在打开期间发生变化")
        except BaseException as exc:
            if backup_descriptor >= 0:
                os.close(backup_descriptor)
                backup_descriptor = -1
            if backup_created:
                try:
                    os.rmdir(backup_dir_name, dir_fd=state_descriptor)
                    os.fsync(state_descriptor)
                except OSError:
                    pass
            if isinstance(exc, OSError):
                raise TransactionError("应用事务备份目录无法准备") from exc
            raise
    finally:
        os.close(state_descriptor)
    _verify_bound_directories(expected_directory_identities)
    directory_descriptors: dict[Path, int] = {}
    entries: list[dict] = []
    try:
        for index, (target, new_content) in enumerate(contents.items(), start=1):
            if target.parent not in directory_descriptors:
                descriptor, _ = _open_bound_directory(
                    target.parent,
                    expected_directory_identities,
                )
                directory_descriptors[target.parent] = descriptor
            directory_descriptor = directory_descriptors[target.parent]
            directory_stat = os.fstat(directory_descriptor)
            old_content, mode, atime_ns, mtime_ns = _read_regular_at(
                directory_descriptor,
                target.name,
            )
            if sha256_bytes(old_content) != expected_old_sha256[target]:
                raise TransactionError("受控目标在校验后发生变化")
            stage_name = f".get-index-apply-{transaction_id}-{index}.tmp"
            backup_name = f"old-{index}.bin"
            entry = {
                "target": str(target),
                "target_name": target.name,
                "parent_dev": directory_stat.st_dev,
                "parent_ino": directory_stat.st_ino,
                "stage_name": stage_name,
                "backup_name": backup_name,
                "old_sha256": sha256_bytes(old_content),
                "new_sha256": sha256_bytes(new_content),
                "mode": mode,
                "atime_ns": atime_ns,
                "mtime_ns": mtime_ns,
            }
            entries.append(entry)
            _create_file_at(directory_descriptor, stage_name, new_content, mode)
            _create_file_at(backup_descriptor, backup_name, old_content, 0o600)
            os.fsync(directory_descriptor)
        os.fsync(backup_descriptor)
        journal = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "commit_marker": str(commit_marker),
            "state_dev": state_stat.st_dev,
            "state_ino": state_stat.st_ino,
            "backup_dir": backup_dir_name,
            "backup_dev": backup_stat.st_dev,
            "backup_ino": backup_stat.st_ino,
            "entries": entries,
        }
        journal_state_stat = os.fstat(directory_descriptors[journal_path.parent])
        if (journal_state_stat.st_dev, journal_state_stat.st_ino) != (
            state_stat.st_dev,
            state_stat.st_ino,
        ):
            raise TransactionError("应用事务状态目录在准备期间发生变化")
        _write_json_at(
            directory_descriptors[journal_path.parent],
            journal_path.name,
            journal,
        )
        return journal, directory_descriptors
    except BaseException as exc:
        journal_preserved = isinstance(exc, _JournalWriteError) and exc.published
        if not journal_preserved:
            for entry in entries:
                target = Path(entry["target"])
                descriptor = directory_descriptors.get(target.parent)
                if descriptor is not None:
                    try:
                        os.unlink(entry["stage_name"], dir_fd=descriptor)
                    except OSError:
                        pass
                try:
                    os.unlink(entry["backup_name"], dir_fd=backup_descriptor)
                except OSError:
                    pass
            cleanup_descriptor = -1
            try:
                cleanup_descriptor, cleanup_stat = _open_bound_directory(
                    journal_path.parent,
                    expected_directory_identities,
                )
                if (cleanup_stat.st_dev, cleanup_stat.st_ino) != (
                    state_stat.st_dev,
                    state_stat.st_ino,
                ):
                    raise TransactionError("应用事务状态目录在清理前发生变化")
                os.rmdir(backup_dir_name, dir_fd=cleanup_descriptor)
                os.fsync(cleanup_descriptor)
            except (OSError, TransactionError):
                pass
            finally:
                if cleanup_descriptor >= 0:
                    os.close(cleanup_descriptor)
        for descriptor in directory_descriptors.values():
            os.close(descriptor)
        if isinstance(exc, _JournalWriteError):
            if exc.published:
                raise TransactionError(
                    "应用事务日志已写入但持久化未确认；事务日志已保留，请重新运行以恢复"
                ) from exc
            raise TransactionError("应用事务日志无法写入") from exc
        if isinstance(exc, OSError):
            raise TransactionError("应用事务准备失败") from exc
        raise
    finally:
        if backup_descriptor >= 0:
            os.close(backup_descriptor)


def replace_files_transactionally(
    contents: dict[Path, bytes],
    *,
    commit_marker: Path,
    journal_path: Path,
    before_commit: Callable[[], None] | None = None,
    after_replace: Callable[[Path, int], None] | None = None,
    expected_old_sha256: dict[Path, str] | None = None,
    expected_directory_identities: dict[Path, tuple[int, int]] | None = None,
) -> list[Path]:
    _verify_bound_directories(expected_directory_identities)
    if expected_old_sha256 is not None and set(expected_old_sha256) != set(contents):
        raise TransactionError("受控目标的校验摘要集合不完整")
    changed: dict[Path, bytes] = {}
    observed_old_sha256: dict[Path, str] = {}
    for target, content in contents.items():
        current = _read_managed_file(target)
        current_digest = sha256_bytes(current)
        if (
            expected_old_sha256 is not None
            and current_digest != expected_old_sha256[target]
        ):
            raise TransactionError("受控目标在校验后发生变化")
        observed_old_sha256[target] = current_digest
        if current != content:
            changed[target] = content
    if not changed:
        return []
    if commit_marker not in changed:
        raise TransactionError("受控更新缺少变化后的增量快照提交标记")
    ordered = {
        **{path: content for path, content in changed.items() if path != commit_marker},
        commit_marker: changed[commit_marker],
    }
    expected_for_changed = {
        path: (
            expected_old_sha256[path]
            if expected_old_sha256 is not None
            else observed_old_sha256[path]
        )
        for path in ordered
    }
    journal, directory_descriptors = _prepare_transaction(
        ordered,
        commit_marker,
        journal_path,
        expected_for_changed,
        expected_directory_identities,
    )
    marker_committed = False
    changed_paths = [Path(entry["target"]) for entry in journal["entries"]]
    try:
        replacement_index = 0
        for entry in journal["entries"]:
            if entry["target"] == str(commit_marker):
                continue
            target = Path(entry["target"])
            descriptor = directory_descriptors[target.parent]
            current, _, _, _ = _read_regular_at(descriptor, entry["target_name"])
            if sha256_bytes(current) != entry["old_sha256"]:
                raise TransactionError("受控目标文件在提交前发生变化")
            _verify_bound_directories(expected_directory_identities)
            _verify_visible_directory(target.parent, entry["parent_dev"], entry["parent_ino"])
            os.replace(
                entry["stage_name"],
                entry["target_name"],
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            os.fsync(descriptor)
            replacement_index += 1
            if after_replace is not None:
                after_replace(target, replacement_index)
        if before_commit is not None:
            before_commit()
        for entry in journal["entries"]:
            if entry["target"] == str(commit_marker):
                continue
            if _entry_current_digest(entry) != entry["new_sha256"]:
                raise TransactionError("受控目标文件在增量快照提交前发生变化")
        marker_entry = next(
            entry for entry in journal["entries"] if entry["target"] == str(commit_marker)
        )
        marker_descriptor = directory_descriptors[commit_marker.parent]
        marker_current, _, _, _ = _read_regular_at(
            marker_descriptor,
            marker_entry["target_name"],
        )
        if sha256_bytes(marker_current) != marker_entry["old_sha256"]:
            raise TransactionError("增量快照在提交前发生变化")
        _verify_bound_directories(expected_directory_identities)
        _verify_visible_directory(
            commit_marker.parent,
            marker_entry["parent_dev"],
            marker_entry["parent_ino"],
        )
        os.replace(
            marker_entry["stage_name"],
            marker_entry["target_name"],
            src_dir_fd=marker_descriptor,
            dst_dir_fd=marker_descriptor,
        )
        marker_committed = True
        os.fsync(marker_descriptor)
        replacement_index += 1
        if after_replace is not None:
            after_replace(commit_marker, replacement_index)
        if any(_entry_current_digest(entry) != entry["new_sha256"] for entry in journal["entries"]):
            raise TransactionError("受控更新提交后校验失败")
        _cleanup_transaction(journal_path, journal)
        return changed_paths
    except Exception as exc:
        try:
            recovery = recover_transaction(
                journal_path,
                set(ordered),
                commit_marker,
                expected_directory_identities,
            )
            all_new = all(
                _entry_current_digest(entry) == entry["new_sha256"]
                for entry in journal["entries"]
            )
        except TransactionError as recovery_exc:
            if marker_committed:
                raise TransactionError(
                    "受控更新已提交，但提交后核验或清理未完成；事务日志已保留"
                ) from recovery_exc
            raise TransactionError(
                "受控更新状态无法自动确定；事务日志已保留"
            ) from recovery_exc
        if recovery in {"committed", "none"} and all_new:
            return changed_paths
        if recovery != "rolled_back":
            raise TransactionError("受控更新后的状态不一致") from exc
        if isinstance(exc, TransactionError):
            raise
        raise TransactionError("受控更新失败，已恢复原状态") from exc
    finally:
        for descriptor in directory_descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
