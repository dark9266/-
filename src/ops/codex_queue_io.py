"""Codex 검증 큐(`reports/codex/backlog.json`) 잠금 I/O.

## 왜 flock 인가
두 프로세스가 각자 파일을 읽고 메모리에서 고친 뒤 순서 없이 덮어쓰면(lost update) 나중에
쓰는 쪽이 먼저 쓴 쪽의 추가분을 **지운다**. 큐잉과 드레인이 겹치면 항목이 조용히 증발한다.
`<큐>.lock` sidecar 에 독점 lock 을 잡은 채 read→modify→write 를 한 임계구역으로 묶는다.

★ **오래 걸리는 작업(`codex exec`, 최대 1800s)은 이 lock 을 잡은 채 하면 안 된다** —
  호출부가 lock 구간을 짧게(읽기만·쓰기만) 나눠 쓴다.

stdlib only. WSL/리눅스 전제(fcntl).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path


def _quarantine(path: Path) -> None:
    """계약(root=list)을 어긴 큐 파일을 조용히 버리지 않고 `.corrupt` 로 격리."""
    if path.exists():
        path.replace(path.with_name(path.name + ".corrupt"))


def _load(path: Path) -> list:
    """큐 로드 — 부재=빈 리스트, 파손=격리 후 빈 리스트."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        _quarantine(path)
        return []
    if not isinstance(data, list):
        _quarantine(path)
        return []
    return data


def _atomic_write(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        tmp = Path(f.name)
    tmp.replace(path)


@contextlib.contextmanager
def locked_queue(queue_path: Path) -> Iterator[tuple[list, Callable[[list], None]]]:
    """`queue_path` 에 독점 flock 을 잡은 채 `(items, save)` 를 제공하는 컨텍스트.

    `items` = 잠금 시점의 **최신** 큐(다른 프로세스의 동시 수정 반영 — lost update 없음).
    `save(items)` 는 잠금을 쥔 채 원자쓰기(임시파일 + rename).
    """
    lock_path = queue_path.with_name(queue_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            items = _load(queue_path)

            def save(data: list) -> None:
                _atomic_write(queue_path, data)

            yield items, save
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
