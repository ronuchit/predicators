"""A bilevel planning approach that uses hand-specified NSRTs.

The approach is aware of the initial predicates and options. Predicates
that are not in the initial predicates are excluded from the ground
truth NSRTs. If an NSRT's option is not included, that NSRT will not be
generated at all.
"""

from typing import Set, List
from predicators.src.structs import NSRT, _Option
from predicators.src.approaches.bilevel_planning_approach import \
    BilevelPlanningApproach
from predicators.src.ground_truth_nsrts import get_gt_nsrts


class OracleApproach(BilevelPlanningApproach):
    """A bilevel planning approach that uses hand-specified NSRTs."""

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
        return get_gt_nsrts(self._initial_predicates, self._initial_options)
