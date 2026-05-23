import time
from collections import OrderedDict
from collections.abc import Callable, Iterable


class TtlByteCache:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_items: int,
        max_bytes: int,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_items = max(1, int(max_items))
        self.max_bytes = max(1, int(max_bytes))
        self._time_fn = time_fn or time.monotonic
        self._items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0

    def __len__(self) -> int:
        self.prune()
        return len(self._items)

    @property
    def total_bytes(self) -> int:
        self.prune()
        return self._bytes

    def get(self, key: str) -> bytes | None:
        self.prune()
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= self._time_fn():
            self._drop(key)
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: str, value: bytes) -> bool:
        self.prune()
        value = bytes(value)
        if len(value) > self.max_bytes:
            return False
        is_new = key not in self._items
        if not is_new:
            self._drop(key)
        self._items[key] = (self._time_fn() + self.ttl_seconds, value)
        self._bytes += len(value)
        self._evict_over_limit()
        return is_new and key in self._items

    def put_many(self, entries: Iterable[tuple[str, bytes]]) -> list[str]:
        stored: list[str] = []
        for key, value in entries:
            if self.put(key, value):
                stored.append(key)
        return [key for key in stored if key in self._items]

    def prune(self) -> None:
        now = self._time_fn()
        expired = [key for key, (expires_at, _) in self._items.items() if expires_at <= now]
        for key in expired:
            self._drop(key)

    def _drop(self, key: str) -> None:
        item = self._items.pop(key, None)
        if item is not None:
            self._bytes -= len(item[1])

    def _evict_over_limit(self) -> None:
        while len(self._items) > self.max_items or self._bytes > self.max_bytes:
            key, _ = next(iter(self._items.items()))
            self._drop(key)
