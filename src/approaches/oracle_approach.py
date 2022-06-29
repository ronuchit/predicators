"""A bilevel planning approach that uses hand-specified NSRTs.

The approach is aware of the initial predicates and options. Predicates
that are not in the initial predicates are excluded from the ground
truth NSRTs. If an NSRT's option is not included, that NSRT will not be
generated at all.
"""

from typing import List, Set

from gym.spaces import Box

from predicators.src.approaches.bilevel_planning_approach import \
    BilevelPlanningApproach
from predicators.src.ground_truth_nsrts import get_gt_nsrts
from predicators.src.option_model import _OptionModelBase
from predicators.src.structs import NSRT, ParameterizedOption, Predicate, \
    Task, Type, _Option


class OracleApproach(BilevelPlanningApproach):
    """A bilevel planning approach that uses hand-specified NSRTs."""

    def __init__(self,
                 initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption],
                 types: Set[Type],
                 action_space: Box,
                 train_tasks: List[Task],
                 task_planning_heuristic: str = "default",
                 max_skeletons_optimized: int = -1) -> None:
        super().__init__(initial_predicates, initial_options, types,
                         action_space, train_tasks, task_planning_heuristic,
                         max_skeletons_optimized)
        self._nsrts = get_gt_nsrts(self._initial_predicates,
                                   self._initial_options)

    def get_last_plan(self) -> List[_Option]:
        """For ONLY an oracle approach, we allow the user to get the plan that
        was most recently generated by a call to solve().

        Note that this doesn't fit into the standard API for an
        Approach, since solve() returns a policy, which abstracts away
        the details of whether that policy is actually a plan under the
        hood.
        """
        return self._last_plan

    @classmethod
    def get_name(cls) -> str:
        return "oracle"

    @property
    def is_learning_based(self) -> bool:
        return False

    def _get_current_nsrts(self) -> Set[NSRT]:
        return self._nsrts

    def get_option_model(self) -> _OptionModelBase:
        """For ONLY an oracle approach, we allow the user to get the current
        option model.

        Note that this doesn't fit into the standard API for an
        Approach, since solve() returns a policy, which abstracts away
        the details of whether that policy is actually a plan under the
        hood.
        """
        return self._option_model  # pragma: no cover
