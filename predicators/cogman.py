"""Cognitive manager (CogMan).

A wrapper around an approach that managers interaction with the environment.

Implements a perception module, which produces a State at each time step based
on the history of observations, and an execution monitor, which determines
whether to re-query the approach at each time step based on the states.

The name "CogMan" is due to Leslie Kaelbling.
"""
from typing import Callable, List, Optional, Set

from predicators.approaches import BaseApproach
from predicators.execution_monitoring import create_execution_monitor
from predicators.perception import create_perceiver
from predicators.settings import CFG
from predicators.structs import Action, GroundAtom, LowLevelTrajectory, \
    Observation, State, Task


class CogMan:
    """Cognitive manager."""

    def __init__(self, approach: BaseApproach) -> None:
        self._approach = approach
        self._perceiver = create_perceiver(CFG.perceiver)
        self._exec_monitor = create_execution_monitor(CFG.execution_monitor)
        self._current_policy: Optional[Callable[[State], Action]] = None
        self._current_goal: Optional[Set[GroundAtom]] = None
        self._current_state_history: List[State] = []
        self._current_action_history: List[Action] = []

    def reset(self, observation: Observation, goal: Set[GroundAtom]) -> None:
        """Start a new episode of environment interaction."""
        state = self._perceiver.reset(observation)
        self._current_goal = goal
        task = Task(state, self._current_goal)
        self._current_policy = self._approach.solve(task, timeout=CFG.timeout)
        self._exec_monitor.reset(task)
        self._current_state_history = []  # populated in step()
        self._current_action_history = []

    def step(self, observation: Observation) -> Action:
        """Receive an observation and produce an action."""
        state = self._perceiver.step(observation)
        # Check if we should replan.
        if self._exec_monitor.step(state):
            assert self._current_goal is not None
            task = Task(state, self._current_goal)
            new_policy = self._approach.solve(task, timeout=CFG.timeout)
            self._current_policy = new_policy
            self._exec_monitor.reset(task)
        assert self._current_policy is not None
        act = self._current_policy(state)
        self._current_state_history.append(state)
        self._current_action_history.append(act)
        return act

    def finish(self, observation: Observation) -> None:
        """Receive a final observation."""
        # If execution was interrupted, we may have already seen this.
        state_len = len(self._current_state_history)
        action_len = len(self._current_action_history)
        if state_len > action_len:
            assert state_len == action_len + 1
            return
        # Otherwise, this is a new observation.
        state = self._perceiver.step(observation)
        self._current_state_history.append(state)

    def get_history_trajectory(self) -> LowLevelTrajectory:
        """Get the history of observed states and actions."""
        return LowLevelTrajectory(list(self._current_state_history),
                                  list(self._current_action_history))
