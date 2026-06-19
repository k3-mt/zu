"""Host-effect monitors (RED_TEAM_CONTAINER.md §3.3, P3): the fs-diff source ships
and is tested elsewhere; here the seccomp subprocess/syscall source — its audit
parser, the shipped profile, and the composite — are unit-tested. (Live audit-log
reading needs a Linux host with auditd; the parsing is what CI exercises.)"""

from __future__ import annotations

import json

from zu_backends.host_monitor import (
    CompositeHostMonitor,
    DockerFsDiffMonitor,
    SeccompAuditMonitor,
    default_audit_profile_path,
    parse_seccomp_audit,
)

# A representative auditd SECCOMP record (one per logged syscall).
_AUDIT = """\
type=DAEMON_START msg=audit(1700000000.000:1): op=start
type=SECCOMP msg=audit(1700000000.111:42): auid=0 uid=0 pid=1234 comm="sh" exe="/bin/sh" syscall=59 compat=0
type=SYSCALL msg=audit(1700000000.112:43): unrelated noise
type=SECCOMP msg=audit(1700000000.222:44): pid=1300 comm="python" exe="/usr/bin/python3" syscall=ptrace
type=SECCOMP msg=audit(1700000000.333:45): pid=1400 comm="mount" exe="/bin/mount" syscall=165
"""


def test_parse_seccomp_audit_extracts_sensitive_syscalls() -> None:
    facts = parse_seccomp_audit(_AUDIT)
    kinds = [(f["kind"], f["syscall"]) for f in facts]
    assert ("subprocess", "execve") in kinds   # syscall=59 -> execve
    assert ("ptrace", "ptrace") in kinds        # by name
    assert ("mount", "mount") in kinds          # syscall=165 -> mount
    assert all("path" in f for f in facts)      # exe captured
    # non-SECCOMP lines and unrelated syscalls are ignored
    assert len(facts) == 3


def test_default_audit_profile_is_valid_and_logs_exec() -> None:
    path = default_audit_profile_path()
    profile = json.loads(open(path).read())
    assert profile["defaultAction"] == "SCMP_ACT_ALLOW"
    logged = [n for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_LOG"
              for n in rule["names"]]
    assert "execve" in logged and "ptrace" in logged and "mount" in logged


async def test_seccomp_monitor_reads_a_callable_source_and_filters() -> None:
    mon = SeccompAuditMonitor(source=lambda: _AUDIT, exclude_exes=("/usr/bin/python3",))
    facts = await mon.collect()
    # the excluded interpreter (ptrace by python3) is dropped; exec + mount remain
    paths = {f.get("path") for f in facts}
    assert "/bin/sh" in paths and "/bin/mount" in paths
    assert "/usr/bin/python3" not in paths


async def test_seccomp_monitor_only_kinds() -> None:
    mon = SeccompAuditMonitor(source=lambda: _AUDIT, only_kinds=("subprocess",))
    facts = await mon.collect()
    assert facts and all(f["kind"] == "subprocess" for f in facts)


async def test_seccomp_monitor_missing_audit_log_yields_nothing() -> None:
    # No auditd / no log file -> empty, never an error (honest on Docker Desktop).
    mon = SeccompAuditMonitor(source="/nonexistent/audit.log")
    assert await mon.collect() == []


async def test_composite_host_monitor_concatenates() -> None:
    class _DiffBackend:
        async def fs_diff(self, sandbox):
            return [{"path": "/etc/cron.d/x", "kind": "added"}]

    composite = CompositeHostMonitor(monitors=[
        DockerFsDiffMonitor(),
        SeccompAuditMonitor(source=lambda: _AUDIT, only_kinds=("subprocess",)),
    ])
    facts = await composite.collect(sandbox=None, backend=_DiffBackend())
    kinds = {f["kind"] for f in facts}
    assert "fs:write" in kinds and "subprocess" in kinds


# --- portable subprocess + mount-escape monitors -----------------------------


class _TopMountBackend:
    def __init__(self, procs=None, mounts=None):
        self._procs = procs or []
        self._mounts = mounts or []

    async def top(self, sandbox):
        return self._procs

    async def mounts(self, sandbox):
        return self._mounts


async def test_docker_top_monitor_flags_undeclared_process() -> None:
    from zu_backends.host_monitor import DockerTopMonitor

    backend = _TopMountBackend(procs=[
        {"pid": "1", "cmd": "sleep infinity"},                 # keep-alive (allowed)
        {"pid": "2", "cmd": "/usr/local/bin/python /usr/local/bin/zu-redteam-run"},  # runner (allowed)
        {"pid": "9", "cmd": "sleep 31337"},                    # the backdoor (flagged)
    ])
    facts = await DockerTopMonitor().collect(sandbox=None, backend=backend)
    assert len(facts) == 1
    assert facts[0]["kind"] == "subprocess" and "31337" in facts[0]["argv"]


async def test_mount_escape_monitor_flags_writable_host_bind() -> None:
    from zu_backends.host_monitor import MountEscapeMonitor

    backend = _TopMountBackend(mounts=[
        {"Type": "bind", "Source": "/host/secrets", "Destination": "/data", "RW": True},   # escape!
        {"Type": "bind", "Source": "/host/ca", "Destination": "/ca", "RW": False},          # allowed CA
        {"Type": "volume", "Source": "vol", "Destination": "/v", "RW": True},               # not a host bind
        {"Type": "bind", "Source": "/host/ro", "Destination": "/ro", "RW": False},          # read-only bind
    ])
    facts = await MountEscapeMonitor().collect(sandbox=None, backend=backend)
    assert len(facts) == 1
    assert facts[0]["kind"] == "mount" and facts[0]["path"] == "/data"
