"""An explorer that takes random NSRTs."""

from typing import List, Set

from gym.spaces import Box

from predicators import utils
from predicators.explorers.base_explorer import BaseExplorer
from predicators.structs import NSRT, Action, DummyOption, \
    ExplorationStrategy, ParameterizedOption, Predicate, State, Task, Type, \
    _GroundNSRT


class RandomNSRTsExplorer(BaseExplorer):
    """RandomNSRTsExplorer implementation.

    Similar to RandomOptionsExplorer in that it chooses
    uniformly at random out of whichever ground NSRTs are
    applicable in the current state, but different in that
    the continuous parameter of the parameterized option is
    generated by the NSRT's sampler rather than by sampling
    uniformly at random from the option's parameter space.

    This explorer is intended to be used when learning options
    via reinforcement learning with oracle samplers. In this
    setting, planning (e.g. using BilevelPlanningExplorer) is
    not feasible because refinement is slow or fails when the
    learned options aren't good enough yet.

    Note that the sampler requires the current low-level state
    as input. An alternative approach would generate a plan of
    options by planning towards the goal with the ground NSRTs
    and avoid refinement by (1) inferring the terminal low-level
    state of each option (the initial low-level state of the
    subsequent option) from the option's sample, which is
    proposing a low-level subgoal, and (2) setting the simulator
    state directly to that state. Because applying dimensionality
    reduction techniques on the sample makes this approach infeasible,
    and this is something we may want to do, we avoid this approach
    for now.
    """

    def __init__(self, predicates: Set[Predicate],
                 options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task],
                 nsrts: Set[NSRT]) -> None:

        super().__init__(predicates, options, types, action_space, train_tasks)
        self._nsrts = nsrts

    @classmethod
    def get_name(cls) -> str:
        return "random_nsrts"

    def get_exploration_strategy(self, train_task_idx: int,
                                 timeout: int) -> ExplorationStrategy:
        cur_option = DummyOption
        task = self._train_tasks[train_task_idx]

        def fallback_policy(state: State) -> Action:
            del state  # unused
            print("request act policy failure!")
            raise utils.RequestActPolicyFailure(
                "No applicable NSRT in this state!")

        def policy(state: State) -> Action:
            nonlocal cur_option

            if cur_option is DummyOption or cur_option.terminal(state):
                # Create all applicable ground NSRTs.
                ground_nsrts: List[_GroundNSRT] = []
                for nsrt in sorted(self._nsrts):
                    ground_nsrts.extend(
                        utils.all_ground_nsrts(nsrt, list(state)))

                # Sample an applicable NSRT.
                ground_nsrt = utils.sample_applicable_ground_nsrt(
                    state, ground_nsrts, self._predicates, self._rng)
                if ground_nsrt is None:
                    return fallback_policy(state)
                assert all(a.holds for a in ground_nsrt.preconditions)

                print(f"sampled {ground_nsrt.name} {ground_nsrt.objects}")

                # Sample an option.
                option = ground_nsrt.sample_option(state,
                                                   goal=task.goal,
                                                   rng=self._rng)
                cur_option = option
                assert cur_option.initiable(state)

            act = cur_option.policy(state)
            return act

        # Never terminate (until the interaction budget is exceeded).
        termination_function = lambda _: False
        return policy, termination_function
