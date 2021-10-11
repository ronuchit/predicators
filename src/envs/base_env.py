"""Base class for an environment.
"""

import abc
from typing import List, Set
import numpy as np
from gym.spaces import Box
from predicators.src.structs import State, Task, Predicate, \
    ParameterizedOption, Type, Action, Image


class BaseEnv:
    """Base environment.
    """
    def __init__(self) -> None:
        self.seed(0)

    @abc.abstractmethod
    def simulate(self, state: State, action: Action) -> State:
        """Get the next state, given a state and an action. Note that this
        action is a low-level action (i.e., its array representation is
        a member of self.action_space), NOT an option.
        """
        raise NotImplementedError("Override me!")

    @abc.abstractmethod
    def get_train_tasks(self) -> List[Task]:
        """Get an ordered list of tasks for training.
        """
        raise NotImplementedError("Override me!")

    @abc.abstractmethod
    def get_test_tasks(self) -> List[Task]:
        """Get an ordered list of tasks for testing / evaluation.
        """
        raise NotImplementedError("Override me!")

    @property
    @abc.abstractmethod
    def predicates(self) -> Set[Predicate]:
        """Get the set of predicates that are given with this environment.
        """
        raise NotImplementedError("Override me!")

    @property
    @abc.abstractmethod
    def types(self) -> Set[Type]:
        """Get the set of types that are given with this environment.
        """
        raise NotImplementedError("Override me!")

    @property
    @abc.abstractmethod
    def options(self) -> Set[ParameterizedOption]:
        """Get the set of parameterized options that are given with
        this environment.
        """
        raise NotImplementedError("Override me!")

    @property
    @abc.abstractmethod
    def action_space(self) -> Box:
        """Get the action space of this environment.
        """
        raise NotImplementedError("Override me!")

    @abc.abstractmethod
    def render(self, state: State) -> Image:
        """Render a state into an image.
        """
        raise NotImplementedError("Override me!")

    def seed(self, seed: int) -> None:
        """Reset seed and rngs.
        """
        self._seed = seed
        # The train/test rng should be used when generating
        # train/test tasks respectively.
        self._train_rng = np.random.default_rng(self._seed)
        self._test_rng = np.random.default_rng(self._seed)
