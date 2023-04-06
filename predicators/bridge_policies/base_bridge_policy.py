"""Base class for a bridge policy."""

import abc
from typing import Callable, List, Set

import numpy as np

from predicators.settings import CFG
from predicators.structs import NSRT, Object, ParameterizedOption, Predicate, \
    State, Type, _Option


class BridgePolicyDone(Exception):
    """Raised when a bridge policy is done executing."""


class BaseBridgePolicy(abc.ABC):
    """Base bridge policy."""

    def __init__(self, types: Set[Type], predicates: Set[Predicate],
                 options: Set[ParameterizedOption], nsrts: Set[NSRT]) -> None:
        self._types = types
        self._predicates = predicates
        self._options = options
        self._nsrts = nsrts
        self._rng = np.random.default_rng(CFG.seed)
        self._failed_options: List[_Option] = []
        self._offending_objects: Set[Object] = set()

    @classmethod
    @abc.abstractmethod
    def get_name(cls) -> str:
        """Get the unique name of this bridge policy, for future use as the
        argument to `--bridge_policy`."""
        raise NotImplementedError("Override me!")

    @property
    @abc.abstractmethod
    def is_learning_based(self) -> bool:
        """Does the bridge policy learn from interaction data?"""
        raise NotImplementedError("Override me!")

    @abc.abstractmethod
    def get_option_policy(self) -> Callable[[State], _Option]:
        """The main method creating the bridge policy."""
        raise NotImplementedError("Override me!")

    def reset(self) -> None:
        """Called at the beginning of a new task."""
        self._failed_options = []
        self._offending_objects = set()

    def record_failed_option(self, failed_option: _Option) -> None:
        """Called when an option has failed."""
        self._failed_options.append(failed_option)

    def record_offending_objects(self, offending_objects: Set[Object]) -> None:
        """Called when there were some offending objects."""
        self._offending_objects.update(offending_objects)
