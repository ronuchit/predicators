"""A bilevel planning approach that learns NSRTs from an offline dataset, and
continues learning options through reinforcement learning."""

from typing import Callable, List, Sequence, Set

from gym.spaces import Box

from predicators.src import utils
from predicators.src.approaches.base_approach import ApproachFailure, \
    ApproachTimeout
from predicators.src.approaches.nsrt_learning_approach import \
    NSRTLearningApproach
from predicators.src.settings import CFG
from predicators.src.structs import NSRT, Dataset, GroundAtom, \
    InteractionRequest, InteractionResult, LowLevelTrajectory, \
    ParameterizedOption, Predicate, State, Task, Type


class ReinforcementLearningApproach(NSRTLearningApproach):
    """A bilevel planning approach that learns NSRTs."""

    def __init__(self, initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task]) -> None:
        super().__init__(initial_predicates, initial_options, types,
                         action_space, train_tasks)
        self._nsrts: Set[NSRT] = set()
        self.online_learning_cycle = 0
        self.initial_dataset: List[LowLevelTrajectory] = []
        self.online_dataset: List[LowLevelTrajectory] = []
        # initialize option learner?

    @classmethod
    def get_name(cls) -> str:
        return "reinforcement_learning"

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        # The only thing we need to do here is learn NSRTs,
        # which we split off into a different function in case
        # subclasses want to make use of it.
        self.initial_dataset = dataset.trajectories
        self._learn_nsrts(dataset.trajectories, online_learning_cycle=None)

    @classmethod
    def _make_termination_fn(cls, goal: Set[GroundAtom]) \
            -> Callable[[State], bool]:

        def _termination_fn(s: State) -> bool:
            return all(goal_atom.holds(s) for goal_atom in goal)

        return _termination_fn

    def get_interaction_requests(self) -> List[InteractionRequest]:
        requests = []
        for i in range(len(self._train_tasks)):
            task = self._train_tasks[i]
            try:
                _act_policy = self.solve(task, CFG.timeout)
            except (ApproachTimeout, ApproachFailure) as e:
                partial_refinements = e.info.get("partial_refinements")
                assert partial_refinements is not None
                _, plan = max(partial_refinements, key=lambda x: len(x[1]))
                _act_policy = utils.option_plan_to_policy(plan)
            request = InteractionRequest(
                train_task_idx=i,
                act_policy=_act_policy,
                query_policy=lambda s: None,
                termination_function=ReinforcementLearningApproach.
                _make_termination_fn(task.goal)  # pylint: disable=line-too-long
            )
            requests.append(request)
        return requests

    def learn_from_interaction_results(
            self, results: Sequence[InteractionResult]) -> None:
        self.online_learning_cycle += 1
        # We get one result per training task.
        for i, result in enumerate(results):
            states = result.states
            actions = result.actions
            traj = LowLevelTrajectory(states,
                                      actions,
                                      _is_demo=False,
                                      _train_task_idx=i)
            self.online_dataset.append(traj)

        # Replace this with an _RLOptionLearner.
        self._learn_nsrts(self.initial_dataset + self.online_dataset,
                          online_learning_cycle=self.online_learning_cycle)
