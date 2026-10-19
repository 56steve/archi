"""CPU thread count for torch in the containers that embed text.

Unset, ``OMP_NUM_THREADS`` lets torch start one intra-op thread per visible core,
and a container sees every core on its host (CPU quotas such as Docker ``--cpus``
or Kubernetes CPU limits do not change that count). On a shared host each small
embedding call then waits on its slowest thread: ~3 s instead of ~16 ms per
MiniLM call on a busy 32-core machine. The compose file and the Helm chart set the
variable for the data-manager and chatbot from ``data_manager.torch_threads``.
"""

from typing import Any, Mapping, Optional

DEFAULT_TORCH_THREADS = 8


def resolve_torch_threads(config: Optional[Mapping[str, Any]]) -> int:
    """Return the configured thread count, or the default when unset.

    Raises ValueError for anything but a positive integer, so a bad value fails
    ``archi create`` instead of reaching a container.
    """
    data_manager = (config or {}).get("data_manager") or {}
    value = data_manager.get("torch_threads")
    if value is None:
        return DEFAULT_TORCH_THREADS
    # bool is an int subclass; True must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"data_manager.torch_threads must be a positive integer, got {value!r}"
        )
    return value
