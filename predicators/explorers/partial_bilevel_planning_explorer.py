"""An explorer that explores by solving tasks with bilevel planning."""

from typing import List, Set

from gym.spaces import Box

from predicators import utils
from predicators.explorers.bilevel_planning_explorer import \
    BilevelPlanningExplorer
from predicators.explorers.random_options_explorer import RandomOptionsExplorer
from predicators.planning import PlanningFailure, PlanningTimeout
from predicators.structs import ExplorationStrategy

class PartialBilevelPlanningExplorer(BilevelPlanningExplorer):
    """PartialBilevelPlanningExplorer implementation."""

    @classmethod
    def get_name(cls) -> str:
        return "partial_planning"

    def get_exploration_strategy(self, train_task_idx: int,
                                 timeout: int) -> ExplorationStrategy:
        task = self._train_tasks[train_task_idx]
        try:
            return self._solve(task, timeout)
        except (PlanningFailure, PlanningTimeout) as e:
            # print(f'Failed to refine plan, {len(e.info["partial_refinements"])} partial partial_refinements')
            skeleton, partial_plan = e.info["partial_refinements"][0]
            policy = utils.option_plan_to_policy(partial_plan)
            # When the policy finishes, an OptionExecutionFailure is raised
            # and caught, terminating the episode.
            termination_function = lambda _: False
            return policy, termination_function, skeleton
