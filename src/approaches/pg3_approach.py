"""Policy-guided planning for generalized policy generation (PG3).

PG3 requires known STRIPS operators. The command below uses oracle operators,
but it is also possible to use this approach with operators learned from
demonstrations.

Example command line:
    python src/main.py --approach pg3 --seed 0 \
        --env pddl_easy_delivery_procedural_tasks \
        --strips_learner oracle --num_train_tasks 10
"""
from __future__ import annotations

import abc
import functools
import logging
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence, Set, \
    Tuple
from typing import Type as TypingType

import dill as pkl
from typing_extensions import TypeAlias

from predicators.src import utils
from predicators.src.planning import PlanningFailure
from predicators.src.approaches import ApproachFailure
from predicators.src.approaches.nsrt_metacontroller_approach import \
    NSRTMetacontrollerApproach
from predicators.src.settings import CFG
from predicators.src.structs import NSRT, Box, Dataset, GroundAtom, LDLRule, \
    LiftedAtom, LiftedDecisionList, ParameterizedOption, Predicate, State, \
    Task, Type, Variable, _GroundNSRT


class PG3Approach(NSRTMetacontrollerApproach):
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

    def _predict(self, state: State, atoms: Set[GroundAtom],
                 goal: Set[GroundAtom]) -> _GroundNSRT:
        del state  # unused
        ground_nsrt = utils.query_ldl(self._current_ldl, atoms, goal)
        if ground_nsrt is None:
            raise ApproachFailure("PG3 policy was not applicable!")
        return ground_nsrt

    def _learn_ldl(self, online_learning_cycle: Optional[int]) -> None:
        """Learn a lifted decision list policy."""

        # Set up a search over LDL space.
        _S: TypeAlias = LiftedDecisionList
        # An "action" here is a search operator and an integer representing the
        # count of successors generated by that operator.
        _A: TypeAlias = Tuple[_PG3SearchOperator, int]

        # Create the PG3 search operators.
        search_operators = self._create_search_operators()

        # The heuristic is what distinguishes PG3 from baseline approaches.
        heuristic = self._create_heuristic()

        def get_successors(ldl: _S) -> Iterator[Tuple[_A, _S, float]]:
            for op in search_operators:
                for i, child in enumerate(op.get_successors(ldl)):
                    yield (op, i), child, 1.0  # cost always 1

        if CFG.pg3_search_method == "gbfs":
            # Terminate only after max expansions.
            path, _ = utils.run_gbfs(
                initial_state=self._current_ldl,
                check_goal=lambda _: False,
                get_successors=get_successors,
                heuristic=heuristic,
                max_expansions=CFG.pg3_gbfs_max_expansions,
                lazy_expansion=True)

        elif CFG.pg3_search_method == "hill_climbing":
            # Terminate when no improvement is found.
            path, _, _ = utils.run_hill_climbing(
                initial_state=self._current_ldl,
                check_goal=lambda _: False,
                get_successors=get_successors,
                heuristic=heuristic,
                enforced_depth=CFG.pg3_hc_enforced_depth)

        else:
            raise NotImplementedError("Unrecognized pg3_search_method "
                                      f"{CFG.pg3_search_method}.")

        # Save the best seen policy.
        self._current_ldl = path[-1]
        logging.info(f"Keeping best policy:\n{self._current_ldl}")
        save_path = utils.get_approach_save_path_str()
        with open(f"{save_path}_{online_learning_cycle}.ldl", "wb") as f:
            pkl.dump(self._current_ldl, f)

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        # First, learn NSRTs.
        self._learn_nsrts(dataset.trajectories, online_learning_cycle=None)
        # Now, learn the LDL policy.
        self._learn_ldl(online_learning_cycle=None)

    def load(self, online_learning_cycle: Optional[int]) -> None:
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

    def _create_heuristic(self) -> _PG3Heuristic:
        preds = self._get_current_predicates()
        nsrts = self._get_current_nsrts()
        heuristic_name_to_cls: Dict[str, TypingType[_PG3Heuristic]] = {
            "policy_guided": _PolicyGuidedPG3Heuristic,
            "policy_evaluation": _PolicyEvaluationPG3Heuristic,
            "demo_plan_comparison": _DemoPlanComparisonPG3Heuristic,
        }
        cls = heuristic_name_to_cls[CFG.pg3_heuristic]
        return cls(preds, nsrts, self._train_tasks)


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
            # Create fresh variables for the predicate to complement the
            # variables that already exist in the rule.
            new_vars = utils.create_new_variables(pred.types, variables)
            for condition in utils.get_all_lifted_atoms_for_predicate(
                    pred, variables | frozenset(new_vars)):
                conditions.append(condition)
        return conditions


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
        _, atoms, goal = self._abstract_train_tasks[task_idx]
        if self._ldl_solves_abstract_task(ldl, atoms, goal):
            return 0.0
        return 1.0

    @staticmethod
    def _ldl_solves_abstract_task(ldl: LiftedDecisionList,
                                  atoms: Set[GroundAtom],
                                  goal: Set[GroundAtom]) -> bool:
        for _ in range(CFG.horizon):
            if goal.issubset(atoms):
                return True
            ground_nsrt = utils.query_ldl(ldl, atoms, goal)
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
        _, _, goal = self._abstract_train_tasks[task_idx]
        assert goal.issubset(atom_plan[-1])
        return self._count_missed_steps(ldl, atom_plan, goal)

    @abc.abstractmethod
    def _get_atom_plan_for_task(self, ldl: LiftedDecisionList,
                                task_idx: int) -> Sequence[Set[GroundAtom]]:
        """Given a task, get the plan with which we will compare the policy."""
        raise NotImplementedError("Override me!")

    @staticmethod
    def _count_missed_steps(ldl: LiftedDecisionList,
                            atoms_seq: Sequence[Set[GroundAtom]],
                            goal: Set[GroundAtom]) -> float:
        missed_steps = 0.0
        for t in range(len(atoms_seq) - 1):
            ground_nsrt = utils.query_ldl(ldl, atoms_seq[t], goal)
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

        atom_plan = [set(atoms) for atoms in planned_frozen_atoms_seq]

        if not check_goal(atom_plan[-1]):
            raise PlanningFailure("Could not find plan for train task.")

        return atom_plan


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
            return utils.query_ldl(ldl, set(atoms), goal)

        planned_frozen_atoms_seq, _ = utils.run_policy_guided_astar(
            initial_state=frozenset(init),
            check_goal=check_goal,
            get_valid_actions=get_valid_actions,
            get_next_state=get_next_state,
            heuristic=heuristic,
            policy=policy,
            num_rollout_steps=CFG.pg3_max_policy_guided_rollout,
            rollout_step_cost=0)

        atom_plan = [set(atoms) for atoms in planned_frozen_atoms_seq]

        if not check_goal(atom_plan[-1]):
            raise PlanningFailure("Could not find plan for train task.")

        return atom_plan
