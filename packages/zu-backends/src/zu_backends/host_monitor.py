"""Host-effect monitors for the red-team container form (RED_TEAM_CONTAINER.md §3.3, P3).

The egress proxy answers "where did the target go?"; the host-effect monitor
answers "what did it do to the box?" — observed from *outside* the target's
userland, so an undeclared filesystem write or subprocess is a fact the plugin
cannot suppress. Both feed the same out-of-band judge: the monitor's facts become
``harness.host_effect.observed`` events, and ``HostEffect`` fires on them.

``DockerFsDiffMonitor`` is the pragmatic, dependency-free realisation: it reads the
container's filesystem diff (``docker diff``) and reports every write *outside* the
expected-writable scope. A plugin that declared no ``fs:write`` yet modified
``/etc`` is behaviour outside its envelope, and this is where it becomes visible.
A seccomp/audit source for subprocess/syscall facts plugs into the same
``{kind, path|argv}`` shape later; the observer already handles both.

Scope, honestly: ``docker diff`` reports changes to the container's own layer —
which catches the *behaviour signal* (this tool wrote files it never declared),
the thing the gate cares about. Detecting a write that escapes through a host
*mount* is a deeper, mount-specific concern noted for the live hardening pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Paths a sandboxed run may legitimately write (a browser needs a writable /tmp,
# the kernel pseudo-filesystems are not real writes). Anything else is reported.
_DEFAULT_WRITABLE = ("/tmp", "/var/tmp", "/proc", "/sys", "/dev", "/run", "/app/.cache")

# Linux syscall numbers we care about, mapped to the host-effect kind they signal.
# (x86_64 numbers; the audit record also carries the name on most distros, which
# we prefer when present.) Process creation -> subprocess; ptrace/mount/ns -> escape.
_SYSCALL_KIND = {
    "execve": "subprocess", "execveat": "subprocess",
    "fork": "subprocess", "vfork": "subprocess", "clone": "subprocess", "clone3": "subprocess",
    "ptrace": "ptrace", "mount": "mount", "umount2": "mount",
    "setns": "namespace", "unshare": "namespace",
}
_SYSCALL_NUM = {  # x86_64 fallbacks when the record carries only a number
    "59": "execve", "322": "execveat", "57": "fork", "58": "vfork",
    "56": "clone", "435": "clone3", "101": "ptrace", "165": "mount",
    "166": "umount2", "308": "setns", "272": "unshare",
}


def default_audit_profile_path() -> str:
    """The path to the shipped seccomp audit profile (RED_TEAM_CONTAINER.md P3),
    suitable for ``--security-opt seccomp=<path>`` / the launch spec's ``seccomp``
    key. It logs (does not block) process-creation/ptrace/mount/namespace syscalls."""
    return str(Path(__file__).with_name("seccomp") / "redteam-audit.json")


_FIELD = re.compile(r'(\w+)=("[^"]*"|\S+)')


def parse_seccomp_audit(text: str) -> list[dict]:
    """Parse Linux audit ``type=SECCOMP`` records (what ``SCMP_ACT_LOG`` emits via
    auditd) into host-effect facts ``{kind, syscall, path?, pid?}``.

    A record looks like::

        type=SECCOMP ... pid=1234 ... exe="/bin/sh" syscall=59 ...

    Only the recognised sensitive syscalls are surfaced; everything else is noise."""
    out: list[dict] = []
    for line in text.splitlines():
        if "type=SECCOMP" not in line:
            continue
        fields = {k: v.strip('"') for k, v in _FIELD.findall(line)}
        raw = fields.get("syscall", "")
        name = raw if raw in _SYSCALL_KIND else _SYSCALL_NUM.get(raw, "")
        kind = _SYSCALL_KIND.get(name)
        if kind is None:
            continue
        fact = {"kind": kind, "syscall": name}
        if fields.get("exe"):
            fact["path"] = fields["exe"]
        if fields.get("pid"):
            fact["pid"] = fields["pid"]
        out.append(fact)
    return out


@dataclass
class DockerFsDiffMonitor:
    """A ``HostEffectMonitor`` that flags filesystem writes outside the writable
    scope, read from the container's diff after the run."""

    name = "docker-fs-diff"
    writable: tuple[str, ...] = _DEFAULT_WRITABLE

    def _in_scope(self, path: str) -> bool:
        return any(path == w or path.startswith(w + "/") for w in self.writable)

    async def collect(self, sandbox: Any, backend: Any) -> list[dict]:
        diffs = await backend.fs_diff(sandbox)
        out: list[dict] = []
        for d in diffs:
            path = d.get("path") or ""
            if not path or self._in_scope(path):
                continue
            out.append({"kind": "fs:write", "path": path, "change": d.get("kind")})
        return out


@dataclass
class SeccompAuditMonitor:
    """A ``HostEffectMonitor`` that reads kernel audit ``SECCOMP`` records (emitted
    when the shipped ``redteam-audit.json`` profile's ``SCMP_ACT_LOG`` fires) and
    surfaces the sensitive ones — process creation, ptrace, mount, namespace — as
    host-effect facts. This is the subprocess/syscall source the fs-diff monitor
    cannot see.

    ``source`` supplies the audit text: a callable returning it, or a path to the
    audit log (default ``/var/log/audit/audit.log``). The parser is exercised in
    CI with sample records; **live reading requires a Linux host with auditd**, so
    on a daemon without it (e.g. Docker Desktop's VM) ``collect`` simply yields
    nothing rather than failing.

    ``exclude_exes`` are expected program paths (the runner's own interpreter) so a
    profile that logs *all* execs doesn't drown the signal in the runner's startup;
    leave empty to report everything."""

    name = "seccomp-audit"
    source: Any = "/var/log/audit/audit.log"
    exclude_exes: tuple[str, ...] = ()
    only_kinds: tuple[str, ...] = ()  # empty = all sensitive kinds

    def _read(self) -> str:
        if callable(self.source):
            return self.source() or ""
        try:
            return Path(self.source).read_text()
        except OSError:
            return ""  # no audit log here (no auditd) — yield nothing, never fail

    async def collect(self, sandbox: Any = None, backend: Any = None) -> list[dict]:
        facts = parse_seccomp_audit(self._read())
        out: list[dict] = []
        for f in facts:
            if self.only_kinds and f["kind"] not in self.only_kinds:
                continue
            if f.get("path") and f["path"] in self.exclude_exes:
                continue
            out.append(f)
        return out


@dataclass
class DockerTopMonitor:
    """A ``HostEffectMonitor`` that reads the target's process table (``docker
    top``) and reports any process outside a baseline allowlist — a tool that
    spawned a process it never declared. **Portable** (any Docker host, including
    Docker Desktop), so it is the everywhere-available subprocess source; it sees
    *persistent* processes (a backdoor/beacon that keeps running), while a
    transient exec that exits between reads is the seccomp-audit source's job.

    ``allow_substrings`` are commands expected in a runner container — the
    keep-alive and the runner itself; a process whose command matches none is
    surfaced as a ``subprocess`` host-effect."""

    name = "docker-top"
    allow_substrings: tuple[str, ...] = (
        "sleep infinity", "zu-redteam-run", "/bin/sh\x00-c", "ps -ef", "[ps]")

    async def collect(self, sandbox: Any, backend: Any) -> list[dict]:
        out: list[dict] = []
        for proc in await backend.top(sandbox):
            cmd = (proc.get("cmd") or "").strip()
            if not cmd or any(s in cmd for s in self.allow_substrings):
                continue
            out.append({"kind": "subprocess", "argv": cmd, "pid": proc.get("pid")})
        return out


@dataclass
class MountEscapeMonitor:
    """A ``HostEffectMonitor`` that flags a **writable host bind-mount** on the
    target — a path through which a write escapes the container onto the host, the
    one thing ``docker diff`` (container-layer only) cannot see. The gate should
    never grant one; this asserts it, and catches a misconfiguration that did.

    ``allow_destinations`` are the controlled, expected mounts (e.g. the read-only
    MITM CA at ``/ca``); anything else that is a writable bind is a breach."""

    name = "mount-escape"
    allow_destinations: tuple[str, ...] = ("/ca",)

    async def collect(self, sandbox: Any, backend: Any) -> list[dict]:
        out: list[dict] = []
        for m in await backend.mounts(sandbox):
            if m.get("Type") != "bind":
                continue  # tmpfs/volume are not host-filesystem escapes
            dest = m.get("Destination", "")
            if any(dest == a or dest.startswith(a.rstrip("/") + "/") for a in self.allow_destinations):
                continue
            if m.get("RW"):
                out.append({
                    "kind": "mount", "path": dest, "source": m.get("Source"),
                    "detail": "writable host bind-mount — a filesystem-escape path",
                })
        return out


@dataclass
class CompositeHostMonitor:
    """Run several ``HostEffectMonitor``s and concatenate their facts — e.g. the
    fs-diff monitor plus the seccomp audit monitor, so one run reports both
    filesystem writes and subprocess/syscall effects."""

    monitors: list[Any] = field(default_factory=list)

    async def collect(self, sandbox: Any, backend: Any) -> list[dict]:
        out: list[dict] = []
        for m in self.monitors:
            out.extend(await m.collect(sandbox, backend))
        return out
