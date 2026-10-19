"""The CPU thread count compose gives torch in the embedding containers.

Unset, torch starts one thread per visible core, and a container sees every host
core. On the 32-core dev host that made each embedding call ~3 s instead of
~16 ms, so the default is a bounded 8.
"""

import pytest

from src.cli.utils.torch_threads import (DEFAULT_TORCH_THREADS,
                                         resolve_torch_threads)


class TestDefault:
    def test_the_default_is_eight(self):
        assert DEFAULT_TORCH_THREADS == 8

    @pytest.mark.parametrize(
        "config",
        [
            None,
            {},
            {"data_manager": None},
            {"data_manager": {}},
            {"data_manager": {"torch_threads": None}},
        ],
    )
    def test_absent_key_gives_the_default(self, config):
        assert resolve_torch_threads(config) == DEFAULT_TORCH_THREADS


class TestOverride:
    @pytest.mark.parametrize("value", [1, 4, 16, 64])
    def test_a_positive_integer_is_used(self, value):
        assert (
            resolve_torch_threads({"data_manager": {"torch_threads": value}}) == value
        )


class TestRefusal:
    @pytest.mark.parametrize("value", [0, -1, True, False, 8.0, "8", [8]])
    def test_anything_but_a_positive_integer_is_refused(self, value):
        with pytest.raises(ValueError, match=r"data_manager\.torch_threads"):
            resolve_torch_threads({"data_manager": {"torch_threads": value}})
