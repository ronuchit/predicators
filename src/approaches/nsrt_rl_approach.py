"""A bilevel planning approach that learns NSRTs from an offline dataset, and
continues learning options through reinforcement learning."""

from typing import List, Sequence, Set

from gym.spaces import Box

from predicators.src import utils
from predicators.src.approaches.base_approach import ApproachFailure, \
    ApproachTimeout
from predicators.src.approaches.nsrt_learning_approach import \
    NSRTLearningApproach
from predicators.src.settings import CFG
from predicators.src.structs import NSRT, Dataset, InteractionRequest, \
    InteractionResult, LowLevelTrajectory, ParameterizedOption, Predicate, \
    Task, Type, Object, Array, State
import numpy as np


class NSRTReinforcementLearningApproach(NSRTLearningApproach):
    """A bilevel planning approach that learns NSRTs from an offline dataset,
    and continues learning options through reinforcement learning."""

    def __init__(self, initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task]) -> None:
        super().__init__(initial_predicates, initial_options, types,
                         action_space, train_tasks)
        self._nsrts: Set[NSRT] = set()
        self._online_learning_cycle = 0
        self._initial_trajectories: List[LowLevelTrajectory] = []
        self._train_task_to_online_traj: Dict[int, List[LowLevelTrajectory]] = {}
        self._train_task_to_option_plan: Dict[int, List[_Option]] = {}
        self._reward_epsilon = CFG.reward_epsilon
        self._pos_reward = CFG.pos_reward
        self._neg_reward = CFG.neg_reward

    @classmethod
    def get_name(cls) -> str:
        return "nsrt_rl"

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        self._initial_trajectories = dataset.trajectories
        super().learn_from_offline_dataset(dataset)

    def get_interaction_requests(self) -> List[InteractionRequest]:
        # For each training task, try to solve the task to get a policy. If the
        # task can't be solved, construct a policy from the sequence of _Option
        # objects that achieves the longest partial refinement of a valid plan
        # skeleton. The teacher will collect a trajectory on the training task
        # using this policy.
        requests = []
        for i in range(len(self._train_tasks)):
            task = self._train_tasks[i]
            try:
                _act_policy = self.solve(task, CFG.timeout)
                # Store the list of _Option objects corresponding to this policy.
                self._train_task_to_option_plan[i] = self._last_plan
            except (ApproachTimeout, ApproachFailure) as e:
                partial_refinements = e.info.get("partial_refinements")
                assert partial_refinements is not None
                _, plan = max(partial_refinements, key=lambda x: len(x[1]))
                _act_policy = utils.option_plan_to_policy(plan)
                # Store the list of _Option objects corresponding to this policy.
                self._train_task_to_option_plan[i] = plan
            request = InteractionRequest(train_task_idx=i,
                                         act_policy=_act_policy,
                                         query_policy=lambda s: None,
                                         termination_function=task.goal_holds)
            requests.append(request)
        return requests

    @classmethod
    def infer_delta(cls, object: Object, states: List[State], features: List[str]) -> List[float]:
        return [states[-1].get(object, feat) - states[0].get(object, feat) for feat in features]

    def learn_from_interaction_results(
            self, results: Sequence[InteractionResult]) -> None:
        self._online_learning_cycle += 1
        # We get one result per training task.
        for i, result in enumerate(results):
            states = result.states
            actions = result.actions
            traj = LowLevelTrajectory(states,
                                      actions,
                                      _is_demo=False,
                                      _train_task_idx=i)
            self._train_task_to_online_traj[i] = traj


        option_to_data = {} # option_name -> online experience data

        # for each task:
        #    for each _Option involved in the trajectory:
        #       compute (s, a, s', r) tuples
        for i in range(len(self._train_tasks)):
            plan = self._train_task_to_option_plan[i]
            traj = self._train_task_to_online_traj[i]

            curr_option_idx = 0
            curr_option = plan[curr_option_idx]
            if curr_option.name not in option_to_data:
                option_to_data[curr_option.name] = []
            curr_states = [traj.states[0]]
            curr_actions = []
            curr_rewards = []
            curr_relative_params = []
            actions = (a for a in traj.actions)
            block = [b for b in curr_option.objects if b.type.name=='block'][0]
            robot = [r for r in curr_option.objects if r.type.name=='robot'][0]

            # Generate transition data, (s, a, s', r, relative_param). The
            # reward R(s, a, s') = neg_reward if s' is not within epsilon of the
            # subgoal, and pos_reward otherwise.
            for j, s in enumerate(traj.states[1:]):
                curr_states.append(s)
                curr_actions.append(next(actions))
                curr_relative_params.append(curr_option.memory["absolute_params"] - s.vec(curr_option.objects))

                if curr_option.terminal(s):
                    # TODO: make this not hardcoded to be environment specific,
                    # or implement specifically per environment and throw an
                    # error for environments where this is not implemented.

                    # Check if we reached our subgoal within a tolerance by
                    # checking the difference between our proposed subgoal
                    # (which is expressed relatively) and the relative changes
                    # that actually happened.
                    if curr_option.params[-1] > 0:  # if holding becomes true
                        dblock = self.infer_delta(block, curr_states, ['grasp'])
                        drobot = self.infer_delta(robot, curr_states, ['x', 'y', 'grip', 'holding'])
                    else:
                        dblock = self.infer_delta(block, curr_states, ['x', 'grasp'])
                        drobot = self.infer_delta(robot, curr_states, ['x', 'grip', 'holding'])
                    actual_delta = np.array(dblock + drobot)
                    if np.allclose(curr_option.params, actual_delta, atol=self._reward_epsilon):
                        reward = self._pos_reward
                    else:
                        reward = self._neg_reward
                    curr_rewards.append(reward)

                    # Store transition data.
                    option_to_data[curr_option.name].append(list(curr_states, curr_actions, curr_rewards, curr_relative_params))

                    # Advance to next option.
                    curr_option_idx += 1
                    if curr_option_idx < len(plan):
                        curr_option = plan[curr_option_idx]
                    else:
                        # If we run out of options in the plan, there should be
                        # an _OptionPlanExhausted exception, and so there is
                        # nothing more in the trajectory that we have not yet
                        # assigned to an _Option already.
                        # TODO: maybe add an assert to confirm the above ^
                        pass

                    # Initialize trajectory for next option.
                    curr_states = [s]
                    curr_actions = []
                    curr_rewards = []
                    curr_relative_params = []

                else:  # case where terminal state not reached.
                    curr_rewards.append(self._neg_reward)
                    # Handle the case where we are at the last state in the
                    # trajectory, but it is not a terminal state of the current
                    # option. This occurs when the plan we are executing is a
                    # partial refinement.
                    if j+1 == len(traj.states) - 1:
                        # Store transition data.
                        option_to_data[curr_option.name].append(list(curr_states, curr_actions, curr_rewards, curr_relative_params))

        import pdb; pdb.set_trace()

        # Associate each unique option we see in the data (that we
        # identify by the option's name) with an nsrt's parameterized option. We
        # need to do this because the RLOptionLearner updates the parameterized
        # options. The RLOptionLearner will receive a parameterized option to
        # update, and all the data associated with it from the online learning
        # cycle that just happened.
        option_to_parent_and_nsrt = {}
        parameterized_options = [(nsrt.option, nsrt.name) for nsrt in self._nsrts]
        for option_name in option_to_data.keys():


        # TODO: call RL option learner's update method, passing in (s, a, s', r)
        # TODO: replace the corresponding parameterized option
