"""Policy-guided planning for generalized policy generation (PG3).

PG3 requires known STRIPS operators. The command below uses oracle operators,
but it is also possible to use this approach with operators learned from
demonstrations.

Example command line:
    python predicators/main.py --approach pg3 --seed 0 \
        --env pddl_easy_delivery_procedural_tasks \
        --strips_learner oracle --num_train_tasks 10
"""
from __future__ import annotations

import abc
import functools
import logging
import time
from typing import Callable, Dict, FrozenSet, Iterator, List, Optional, \
    Sequence, Set, Tuple
from typing import Type as TypingType

import dill as pkl
from typing_extensions import TypeAlias

from predicators import utils
from predicators.approaches import ApproachFailure
from predicators.approaches.nsrt_learning_approach import NSRTLearningApproach
from predicators.approaches.llm_open_loop_approach import LLMOpenLoopApproach
from predicators.planning import PlanningFailure, run_low_level_search
from predicators.settings import CFG
from predicators.structs import NSRT, Action, Box, Dataset, GroundAtom, \
    LDLRule, LiftedAtom, LiftedDecisionList, Object, ParameterizedOption, \
    Predicate, State, Task, Type, Variable, _GroundNSRT


class PG3Approach(NSRTLearningApproach):
    """Policy-guided planning for generalized policy generation (PG3)."""

    def __init__(self, initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task]) -> None:
        super().__init__(initial_predicates, initial_options, types,
                         action_space, train_tasks)
        self._current_ldl = LiftedDecisionList([])

    @classmethod
    def get_name(cls) -> str:
        return "pg3"

    def _predict_ground_nsrt(self, atoms: Set[GroundAtom],
                             objects: Set[Object],
                             goal: Set[GroundAtom]) -> _GroundNSRT:
        """Predicts next GroundNSRT to be deployed based on the PG3 generated
        policy."""
        ground_nsrt = utils.query_ldl(self._current_ldl, atoms, objects, goal)
        if ground_nsrt is None:
            raise ApproachFailure("PG3 policy was not applicable!")
        return ground_nsrt

    def _solve(self, task: Task, timeout: int) -> Callable[[State], Action]:
        """Searches for a low level policy that satisfies PG3's abstract
        policy."""
        skeleton = []
        atoms_sequence = []
        atoms = utils.abstract(task.init, self._initial_predicates)
        atoms_sequence.append(atoms)
        current_objects = set(task.init)
        start_time = time.perf_counter()

        while not task.goal.issubset(atoms):
            if (time.perf_counter() - start_time) >= timeout:
                raise ApproachFailure("Timeout exceeded")
            ground_nsrt = self._predict_ground_nsrt(atoms, current_objects,
                                                    task.goal)
            atoms = utils.apply_operator(ground_nsrt, atoms)
            skeleton.append(ground_nsrt)
            atoms_sequence.append(atoms)
        try:
            option_list, succeeded = run_low_level_search(
                task, self._option_model, skeleton, atoms_sequence, self._seed,
                timeout - (time.perf_counter() - start_time), self._metrics,
                CFG.horizon)
        except PlanningFailure as e:
            raise ApproachFailure(e.args[0], e.info)
        if not succeeded:
            raise ApproachFailure("Low-level search failed")
        policy = utils.option_plan_to_policy(option_list)
        return policy

    def _learn_ldl(self, dataset: Dataset, online_learning_cycle: Optional[int]) -> None:
        """Learn a lifted decision list policy."""
        # Set up a search over LDL space.
        _S: TypeAlias = LiftedDecisionList
        # An "action" here is a search operator and an integer representing the
        # count of successors generated by that operator.
        _A: TypeAlias = Tuple[_PG3SearchOperator, int]

        # Create the PG3 search operators.
        search_operators = self._create_search_operators()

        # The heuristic is what distinguishes PG3 from baseline approaches.
        heuristic = self._create_heuristic(dataset)

        # Initialize the search with the best candidate.
        candidate_initial_states = self._get_policy_search_initial_ldls()
        initial_state = min(candidate_initial_states, key=heuristic)

        def get_successors(ldl: _S) -> Iterator[Tuple[_A, _S, float]]:
            for op in search_operators:
                for i, child in enumerate(op.get_successors(ldl)):
                    yield (op, i), child, 1.0  # cost always 1

        if CFG.pg3_search_method == "gbfs":
            # Terminate only after max expansions.
            path, _ = utils.run_gbfs(
                initial_state=initial_state,
                check_goal=lambda _: False,
                get_successors=get_successors,
                heuristic=heuristic,
                max_expansions=CFG.pg3_gbfs_max_expansions,
                lazy_expansion=True)

        elif CFG.pg3_search_method == "hill_climbing":
            # Terminate when no improvement is found.
            path, _, _ = utils.run_hill_climbing(
                initial_state=initial_state,
                check_goal=lambda _: False,
                get_successors=get_successors,
                heuristic=heuristic,
                early_termination_heuristic_thresh=0,
                enforced_depth=CFG.pg3_hc_enforced_depth)

        else:
            raise NotImplementedError("Unrecognized pg3_search_method "
                                      f"{CFG.pg3_search_method}.")

        # Save the best seen policy.
        self._current_ldl = path[-1]

        # TODO remove
        preds = self._get_current_predicates()
        nsrts = self._get_current_nsrts()
        new_heur = _DemoPlanComparisonAnyMatchPG3Heuristic(
            preds, nsrts, self._train_tasks)
        print("Final DEMO PLAN COMPARISON ANY MATCH heuristic evaluation:", new_heur(self._current_ldl))

        logging.info(f"Keeping best policy:\n{self._current_ldl}")
        save_path = utils.get_approach_save_path_str()
        with open(f"{save_path}_{online_learning_cycle}.ldl", "wb") as f:
            pkl.dump(self._current_ldl, f)

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        # First, learn NSRTs.
        self._learn_nsrts(dataset.trajectories, online_learning_cycle=None)
        # Now, learn the LDL policy.
        self._learn_ldl(dataset, online_learning_cycle=None)

    def load(self, online_learning_cycle: Optional[int]) -> None:
        # Load the NSRTs.
        super().load(online_learning_cycle)
        # Load the LDL policy.
        load_path = utils.get_approach_load_path_str()
        with open(f"{load_path}_{online_learning_cycle}.ldl", "rb") as f:
            self._current_ldl = pkl.load(f)

    def _create_search_operators(self) -> List[_PG3SearchOperator]:
        search_operator_classes = [
            _AddRulePG3SearchOperator,
            _AddConditionPG3SearchOperator,
        ]
        preds = self._get_current_predicates()
        nsrts = self._get_current_nsrts()
        return [cls(preds, nsrts) for cls in search_operator_classes]

    def _create_heuristic(self, dataset: Dataset) -> _PG3Heuristic:
        preds = self._get_current_predicates()
        nsrts = self._get_current_nsrts()
        heuristic_name_to_cls: Dict[str, TypingType[_PG3Heuristic]] = {
            "policy_guided": _PolicyGuidedPG3Heuristic,
            "policy_evaluation": _PolicyEvaluationPG3Heuristic,
            "demo_plan_comparison": _DemoPlanComparisonPG3Heuristic,
            "demo_plan_any_match": _DemoPlanComparisonAnyMatchPG3Heuristic,
        }
        cls = heuristic_name_to_cls[CFG.pg3_heuristic]
        heuristic = cls(preds, nsrts, self._train_tasks)
        # TODO: pay for my sins
        if CFG.pg3_plan_compare_demo_source == "llm":
            llm_planning_approach = LLMOpenLoopApproach(preds, self._initial_options, self._types, self._action_space, self._train_tasks)
            llm_planning_approach.learn_from_offline_dataset(dataset)
            heuristic.llm_planning_approach = llm_planning_approach
        return heuristic

    @staticmethod
    def _get_policy_search_initial_ldls() -> List[LiftedDecisionList]:
        # Initialize with an empty list by default, but subclasses may
        # override.
        return [LiftedDecisionList([])]


############################## Search Operators ###############################


class _PG3SearchOperator(abc.ABC):
    """Given an LDL policy, generate zero or more successor LDL policies."""

    def __init__(self, predicates: Set[Predicate], nsrts: Set[NSRT]) -> None:
        self._predicates = predicates
        self._nsrts = nsrts

    @abc.abstractmethod
    def get_successors(
            self, ldl: LiftedDecisionList) -> Iterator[LiftedDecisionList]:
        """Generate zero or more successor LDL policies."""
        raise NotImplementedError("Override me!")


class _AddRulePG3SearchOperator(_PG3SearchOperator):
    """An operator that adds new rules to an existing LDL policy."""

    def get_successors(
            self, ldl: LiftedDecisionList) -> Iterator[LiftedDecisionList]:
        for idx in range(len(ldl.rules) + 1):
            for rule in self._get_candidate_rules():
                new_rules = list(ldl.rules)
                new_rules.insert(idx, rule)
                yield LiftedDecisionList(new_rules)

    @functools.lru_cache(maxsize=None)
    def _get_candidate_rules(self) -> List[LDLRule]:
        return [self._nsrt_to_rule(nsrt) for nsrt in sorted(self._nsrts)]

    @staticmethod
    def _nsrt_to_rule(nsrt: NSRT) -> LDLRule:
        """Initialize an LDLRule from an NSRT."""
        return LDLRule(
            name=nsrt.name,
            parameters=list(nsrt.parameters),
            pos_state_preconditions=set(nsrt.preconditions),
            neg_state_preconditions=set(),
            goal_preconditions=set(),
            nsrt=nsrt,
        )


class _AddConditionPG3SearchOperator(_PG3SearchOperator):
    """An operator that adds new preconditions to existing LDL rules."""

    def get_successors(
            self, ldl: LiftedDecisionList) -> Iterator[LiftedDecisionList]:
        for rule_idx, rule in enumerate(ldl.rules):
            rule_vars = frozenset(rule.parameters)
            for condition in self._get_candidate_conditions(rule_vars):
                # Consider adding new condition to positive preconditions,
                # negative preconditions, or goal preconditions.
                for destination in ["pos", "neg", "goal"]:
                    new_pos = set(rule.pos_state_preconditions)
                    new_neg = set(rule.neg_state_preconditions)
                    new_goal = set(rule.goal_preconditions)
                    if destination == "pos":
                        dest_set = new_pos
                    elif destination == "neg":
                        dest_set = new_neg
                    else:
                        assert destination == "goal"
                        dest_set = new_goal
                    # If the condition already exists, skip.
                    if condition in dest_set:
                        continue
                    # Special case: if the condition already exists in the
                    # positive preconditions, don't add to the negative
                    # preconditions, and vice versa.
                    if destination in ("pos", "neg") and condition in \
                        new_pos | new_neg:
                        continue
                    dest_set.add(condition)
                    parameters = sorted({
                        v
                        for c in new_pos | new_neg | new_goal
                        for v in c.variables
                    } | set(rule.nsrt.parameters))
                    # Create the new rule.
                    new_rule = LDLRule(
                        name=rule.name,
                        parameters=parameters,
                        pos_state_preconditions=new_pos,
                        neg_state_preconditions=new_neg,
                        goal_preconditions=new_goal,
                        nsrt=rule.nsrt,
                    )
                    # Create the new LDL.
                    new_rules = list(ldl.rules)
                    new_rules[rule_idx] = new_rule
                    yield LiftedDecisionList(new_rules)

    @functools.lru_cache(maxsize=None)
    def _get_candidate_conditions(
            self, variables: FrozenSet[Variable]) -> List[LiftedAtom]:
        conditions = []
        for pred in sorted(self._predicates):
            if CFG.pg3_add_condition_allow_new_vars:
                # Create fresh variables for the predicate to complement the
                # variables that already exist in the rule.
                new_vars = utils.create_new_variables(pred.types, variables)
                condition_vars = variables | frozenset(new_vars)
            else:
                condition_vars = variables
            for condition in utils.get_all_lifted_atoms_for_predicate(
                    pred, condition_vars):
                conditions.append(condition)
        return conditions


class _DeleteConditionPG3SearchOperator(_PG3SearchOperator):
    """An operator that removes conditions from existing LDL rules."""

    def get_successors(
            self, ldl: LiftedDecisionList) -> Iterator[LiftedDecisionList]:
        for rule_idx, rule in enumerate(ldl.rules):
            for condition in rule.pos_state_preconditions | \
                rule.neg_state_preconditions | rule.goal_preconditions:

                # If the condition to be removed is a
                # precondition of an nsrt, don't remove it.
                if condition in rule.nsrt.preconditions:
                    continue

                # Recreate new preconditions.
                # Assumes that a condition can appear only in one set
                new_pos = rule.pos_state_preconditions - {condition}
                new_neg = rule.neg_state_preconditions - {condition}
                new_goal = rule.goal_preconditions - {condition}

                # Reconstruct parameters from the other
                # components of the LDL.
                all_atoms = new_pos | new_neg | new_goal
                new_rule_params_set = \
                    {v for a in all_atoms for v in a.variables}
                new_rule_params_set.update(rule.nsrt.parameters)
                new_rule_params = sorted(new_rule_params_set)

                # Create the new rule.
                new_rule = LDLRule(
                    name=rule.name,
                    parameters=new_rule_params,
                    pos_state_preconditions=new_pos,
                    neg_state_preconditions=new_neg,
                    goal_preconditions=new_goal,
                    nsrt=rule.nsrt,
                )
                # Create the new LDL.
                new_rules = list(ldl.rules)
                new_rules[rule_idx] = new_rule
                yield LiftedDecisionList(new_rules)


class _DeleteRulePG3SearchOperator(_PG3SearchOperator):
    """An operator that removes entire rules from existing LDL rules."""

    def get_successors(
            self, ldl: LiftedDecisionList) -> Iterator[LiftedDecisionList]:
        for rule_idx in range(len(ldl.rules)):
            new_rules = [r for i, r in enumerate(ldl.rules) if i != rule_idx]
            yield LiftedDecisionList(new_rules)


################################ Heuristics ###################################


class _PG3Heuristic(abc.ABC):
    """Given an LDL policy, produce a score, with lower better."""

    def __init__(
        self,
        predicates: Set[Predicate],
        nsrts: Set[NSRT],
        train_tasks: Sequence[Task],
    ) -> None:
        self._predicates = predicates
        self._nsrts = nsrts
        # Convert each train task into (object set, init atoms, goal).
        self._abstract_train_tasks = [(set(task.init),
                                       utils.abstract(task.init,
                                                      predicates), task.goal)
                                      for task in train_tasks]

    def __call__(self, ldl: LiftedDecisionList) -> float:
        """Compute the heuristic value for the given LDL policy."""
        score = 0.0
        for idx in range(len(self._abstract_train_tasks)):
            score += self._get_score_for_task(ldl, idx)
        logging.debug(f"Scoring:\n{ldl}\nScore: {score}")
        return score

    @abc.abstractmethod
    def _get_score_for_task(self, ldl: LiftedDecisionList,
                            task_idx: int) -> float:
        """Produce a score, with lower better."""
        raise NotImplementedError("Override me!")


class _PolicyEvaluationPG3Heuristic(_PG3Heuristic):
    """Score a policy based on the number of train tasks it solves at the
    abstract level."""

    def _get_score_for_task(self, ldl: LiftedDecisionList,
                            task_idx: int) -> float:
        objects, atoms, goal = self._abstract_train_tasks[task_idx]
        if self._ldl_solves_abstract_task(ldl, atoms, objects, goal):
            return 0.0
        return 1.0

    @staticmethod
    def _ldl_solves_abstract_task(ldl: LiftedDecisionList,
                                  atoms: Set[GroundAtom], objects: Set[Object],
                                  goal: Set[GroundAtom]) -> bool:
        for _ in range(CFG.horizon):
            if goal.issubset(atoms):
                return True
            ground_nsrt = utils.query_ldl(ldl, atoms, objects, goal)
            if ground_nsrt is None:
                return False
            atoms = utils.apply_operator(ground_nsrt, atoms)
        return goal.issubset(atoms)


class _PlanComparisonPG3Heuristic(_PG3Heuristic):
    """Score a policy based on agreement with certain plans.

    Which plans are used to compute agreement is defined by subclasses.
    """

    def __init__(
        self,
        predicates: Set[Predicate],
        nsrts: Set[NSRT],
        train_tasks: Sequence[Task],
    ) -> None:
        super().__init__(predicates, nsrts, train_tasks)
        # Ground the NSRTs once per task and save them.
        self._train_task_idx_to_ground_nsrts = {
            idx: [
                ground_nsrt for nsrt in nsrts
                for ground_nsrt in utils.all_ground_nsrts(nsrt, objects)
            ]
            for idx, (objects, _, _) in enumerate(self._abstract_train_tasks)
        }

    def _get_score_for_task(self, ldl: LiftedDecisionList,
                            task_idx: int) -> float:
        try:
            atom_plan = self._get_atom_plan_for_task(ldl, task_idx)
        except PlanningFailure:
            return CFG.horizon  # worst possible score
        # Note: we need the goal because it's an input to the LDL policy.
        objects, _, goal = self._abstract_train_tasks[task_idx]
        assert goal.issubset(atom_plan[-1])
        return self._count_missed_steps(ldl, atom_plan, objects, goal)

    @abc.abstractmethod
    def _get_atom_plan_for_task(self, ldl: LiftedDecisionList,
                                task_idx: int) -> Sequence[Set[GroundAtom]]:
        """Given a task, get the plan with which we will compare the policy.

        If no plan can be found, a PlanningFailure exception is raised.
        """
        raise NotImplementedError("Override me!")

    @staticmethod
    def _count_missed_steps(ldl: LiftedDecisionList,
                            atoms_seq: Sequence[Set[GroundAtom]],
                            objects: Set[Object],
                            goal: Set[GroundAtom]) -> float:
        missed_steps = 0.0
        for t in range(len(atoms_seq) - 1):
            ground_nsrt = utils.query_ldl(ldl, atoms_seq[t], objects, goal)
            if ground_nsrt is None:
                missed_steps += CFG.pg3_plan_compare_inapplicable_cost
            else:
                predicted_atoms = utils.apply_operator(ground_nsrt,
                                                       atoms_seq[t])
                if predicted_atoms != atoms_seq[t + 1]:
                    missed_steps += 1
        return missed_steps


class _DemoPlanComparisonPG3Heuristic(_PlanComparisonPG3Heuristic):
    """Score a policy based on agreement with demo plans.

    The demos are generated with a planner, once per train task.
    """

    def _get_atom_plan_for_task(self, ldl: LiftedDecisionList,
                                task_idx: int) -> Sequence[Set[GroundAtom]]:
        del ldl  # unused
        return self._get_demo_atom_plan_for_task(task_idx)

    @functools.lru_cache(maxsize=None)
    def _get_demo_atom_plan_for_task(
            self, task_idx: int) -> Sequence[Set[GroundAtom]]:
        # Run planning once per task and cache the result.
        if CFG.pg3_plan_compare_demo_source == "planner":
            return self._get_demo_atom_plan_for_task_from_planner(task_idx)
        assert CFG.pg3_plan_compare_demo_source == "llm"
        return self._get_demo_atom_plan_for_task_from_llm(task_idx)

    def _get_demo_atom_plan_for_task_from_planner(
            self, task_idx: int) -> Sequence[Set[GroundAtom]]:
        objects, init, goal = self._abstract_train_tasks[task_idx]
        ground_nsrts = self._train_task_idx_to_ground_nsrts[task_idx]

        # Set up an A* search.
        _S: TypeAlias = FrozenSet[GroundAtom]
        _A: TypeAlias = _GroundNSRT

        def check_goal(atoms: _S) -> bool:
            return goal.issubset(atoms)

        def get_successors(atoms: _S) -> Iterator[Tuple[_A, _S, float]]:
            for op in utils.get_applicable_operators(ground_nsrts, atoms):
                next_atoms = utils.apply_operator(op, set(atoms))
                yield (op, frozenset(next_atoms), 1.0)

        heuristic = utils.create_task_planning_heuristic(
            heuristic_name=CFG.pg3_task_planning_heuristic,
            init_atoms=init,
            goal=goal,
            ground_ops=ground_nsrts,
            predicates=self._predicates,
            objects=objects,
        )

        planned_frozen_atoms_seq, _ = utils.run_astar(
            initial_state=frozenset(init),
            check_goal=check_goal,
            get_successors=get_successors,
            heuristic=heuristic)

        if not check_goal(planned_frozen_atoms_seq[-1]):
            raise PlanningFailure("Could not find plan for train task.")

        return [set(atoms) for atoms in planned_frozen_atoms_seq]

    def _get_demo_atom_plan_for_task_from_llm(
            self, task_idx: int) -> Sequence[Set[GroundAtom]]:
        objects, init, goal = self._abstract_train_tasks[task_idx]
        plan = self.llm_planning_approach._get_llm_based_plan(objects, init, goal)
        if not plan:
            raise PlanningFailure("Could not find plan for train task.")
        atoms = set(init)
        atoms_seq = [atoms]
        for ground_nsrt in plan:
            if not ground_nsrt.preconditions.issubset(atoms):
                break
            atoms = utils.apply_operator(ground_nsrt, atoms)
            atoms_seq.append(atoms)
        return atoms_seq



class _DemoPlanComparisonAnyMatchPG3Heuristic(_DemoPlanComparisonPG3Heuristic):
    """Similar to DemoPlanComparisonPG3Heuristic, except rather than checking
    if the exact action returned by the policy matches the demo, we check to
    see if grounding the LDL rules in _any_ order would match the demo at each
    step.

    This removes the influence of the arbitrary order in the case where
    multiple groundings of a rule satisfy the preconditions.
    """

    @staticmethod
    def _count_missed_steps(ldl: LiftedDecisionList,
                            atoms_seq: Sequence[Set[GroundAtom]],
                            objects: Set[Object],
                            goal: Set[GroundAtom]) -> float:
        # This requires a different implementation because we can no longer
        # check just the single action returned by the policy.
        missed_steps = 0.0
        total_steps = 0.0
        print("Goal", goal)
        for t in range(len(atoms_seq) - 1):
            print(f"Step {t} added {atoms_seq[t + 1] - atoms_seq[t]}")
            print("Full state:", atoms_seq[t])
            total_steps += 1
            ground_nsrts = utils.query_ldl_all(ldl, atoms_seq[t], objects,
                                               goal)
            candidate_found = False
            match_found = False
            for ground_nsrt in ground_nsrts:
                candidate_found = True
                predicted_atoms = utils.apply_operator(ground_nsrt,
                                                       atoms_seq[t])
                if predicted_atoms == atoms_seq[t + 1]:
                    match_found = True
                    break
            if not match_found:
                if candidate_found:
                    print("No match found...")
                    print("But found candidate:")
                    print(ground_nsrt.name, ground_nsrt.objects)
                    missed_steps += 1
                else:
                    print("No match found.")
                    missed_steps += CFG.pg3_plan_compare_inapplicable_cost
            else:
                print("Match found.")
        import ipdb; ipdb.set_trace()
        return missed_steps / total_steps

    @functools.lru_cache(maxsize=None)
    def _get_demo_atom_plan_for_task(
            self, task_idx: int) -> Sequence[Set[GroundAtom]]:
        return self._get_demo_atom_plan_for_task_from_planner(task_idx)


class _PolicyGuidedPG3Heuristic(_PlanComparisonPG3Heuristic):
    """Score a policy based on agreement with policy-guided plans."""

    def _get_atom_plan_for_task(self, ldl: LiftedDecisionList,
                                task_idx: int) -> Sequence[Set[GroundAtom]]:

        objects, init, goal = self._abstract_train_tasks[task_idx]
        ground_nsrts = self._train_task_idx_to_ground_nsrts[task_idx]

        # Set up a policy-guided A* search.
        _S: TypeAlias = FrozenSet[GroundAtom]
        _A: TypeAlias = _GroundNSRT

        def check_goal(atoms: _S) -> bool:
            return goal.issubset(atoms)

        def get_valid_actions(atoms: _S) -> Iterator[Tuple[_A, float]]:
            for op in utils.get_applicable_operators(ground_nsrts, atoms):
                yield (op, 1.0)

        def get_next_state(atoms: _S, ground_nsrt: _A) -> _S:
            return frozenset(utils.apply_operator(ground_nsrt, set(atoms)))

        heuristic = utils.create_task_planning_heuristic(
            heuristic_name=CFG.pg3_task_planning_heuristic,
            init_atoms=init,
            goal=goal,
            ground_ops=ground_nsrts,
            predicates=self._predicates,
            objects=objects,
        )

        def policy(atoms: _S) -> Optional[_A]:
            return utils.query_ldl(ldl, set(atoms), objects, goal)

        planned_frozen_atoms_seq, _ = utils.run_policy_guided_astar(
            initial_state=frozenset(init),
            check_goal=check_goal,
            get_valid_actions=get_valid_actions,
            get_next_state=get_next_state,
            heuristic=heuristic,
            policy=policy,
            num_rollout_steps=CFG.pg3_max_policy_guided_rollout,
            rollout_step_cost=0)

        if not check_goal(planned_frozen_atoms_seq[-1]):
            raise PlanningFailure("Could not find plan for train task.")

        return [set(atoms) for atoms in planned_frozen_atoms_seq]
