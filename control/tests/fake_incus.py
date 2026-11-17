"""An Incus the target lives in, in memory: for brain's time travel tests.

Its answers follow what the real one does (checked against Incus 6.0 in the
sandbox): a missing snapshot is an IncusError "Snapshot desk/NAME does not
exist"; a graceful restart the guest ignores fails after the timeout; a
stopped container stays stopped when restored, and can't be restarted.
"""

from chatuses.incus import Incus, IncusError


class FakeTarget(Incus):
    def __init__(self, instance: str = "desk") -> None:  # no certificates needed
        self.instance = instance
        self.there: list[str] = []  # the instance's snapshots, oldest first
        self.restored: list[str] = []
        self.restarts: list[bool] = []  # force?
        self.deleted: list[str] = []
        self.ignores_shutdown = False  # a graceful reboot times out, as with an init that ignores it
        self.running = True  # False: switched off (poweroff from chat, incus stop)
        self.starts = 0
        self.error: Exception | None = None  # every call fails with it

    def _check(self, instance: str) -> None:
        if self.error:
            raise self.error
        if instance != self.instance:
            raise IncusError(f"Instance not found: {instance}")

    async def find(self, address: str) -> str:
        if self.error:
            raise self.error
        return self.instance

    async def state(self, instance: str) -> str:
        self._check(instance)
        return "Running" if self.running else "Stopped"

    async def start(self, instance: str) -> None:
        self._check(instance)
        if self.running:
            raise IncusError("The instance is already running")
        self.running = True
        self.starts += 1

    async def snapshot(self, name: str, *, instance: str = "", address: str = "") -> str:
        instance = instance or self.instance
        self._check(instance)
        self.there.append(name)
        return instance

    async def snapshots(self, instance: str) -> list[str]:
        self._check(instance)
        return list(self.there)

    async def restore(self, instance: str, name: str) -> None:
        self._check(instance)
        if name not in self.there:
            raise IncusError(f"Snapshot {instance}/{name} does not exist")
        self.restored.append(name)

    async def delete_snapshot(self, instance: str, name: str) -> None:
        self._check(instance)
        if name not in self.there:
            raise IncusError(f"Snapshot {instance}/{name} does not exist")
        self.there.remove(name)
        self.deleted.append(name)

    async def restart(self, instance: str, *, force: bool) -> None:
        self._check(instance)
        if not self.running:
            raise IncusError("The instance is already stopped")
        if not force and self.ignores_shutdown:
            raise IncusError('Failed shutting down instance, status is "Running": context deadline exceeded')
        self.restarts.append(force)
