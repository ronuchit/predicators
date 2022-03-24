"""Environments that are derived from PDDL. There are no continuous aspects
of the state or action space. These environments are similar to PDDLGym."""

import abc
from typing import Callable, List, Optional, Set, Tuple

import numpy as np
from gym.spaces import Box
from pyperplan.pddl.parser import parse_domain_def, parse_lisp_iterator, \
    TraversePDDLDomain

from predicators.src.envs import BaseEnv
from predicators.src.settings import CFG
from predicators.src.structs import Action, DefaultState, DefaultTask, Image, \
    ParameterizedOption, Predicate, State, Task, Type, STRIPSOperator, \
    _GroundSTRIPSOperator, GroundAtom
from predicators.src import utils


# Given a desired number of problems and an rng, returns a list of that many
# PDDL problem strs.
PDDLProblemGenerator = Callable[[int, np.random.Generator], List[str]]


class PDDLEnv(BaseEnv):
    """An environment induced by PDDL.

    The action space is hacked to conform to our convention that actions are
    fixed dimensional vectors. Users of this class should not need to worry
    about the action space because it would never make sense to learn anything
    using this action space. The dimensionality is 1 + max operator arity. The
    first dimension encodes the operator. The next dimensions encode the object
    used to ground the operator. The encoding assumes a fixed ordering over
    operators and objects in the state.
    """

    def __init__(self) -> None:
        super().__init__()
        # Parse the domain str.
        self._types, self._predicates, self._strips_operators = \
            _parse_pddl_domain(self._domain_str)

    @property
    @abc.abstractmethod
    def _domain_str(self) -> str:
        raise NotImplementedError("Override me!")

    @property
    @abc.abstractmethod
    def _train_problem_generator(self) -> PDDLProblemGenerator:
        raise NotImplementedError("Override me!")

    @property
    @abc.abstractmethod
    def _test_problem_generator(self) -> PDDLProblemGenerator:
        raise NotImplementedError("Override me!")

    def simulate(self, state: State, action: Action) -> State:
        # Convert the state into a Set[GroundAtom].
        ground_atoms = self._state_to_ground_atoms(state)
        # Convert the action into a _GroundSTRIPSOperator.
        ground_op = self._action_to_ground_strips_op(action)
        # Apply the operator.
        next_ground_atoms = utils.apply_operator(ground_op, ground_atoms)
        # Convert back into a State.
        next_state = self._ground_atoms_to_state(next_ground_atoms)
        return next_state

    def _generate_train_tasks(self) -> List[Task]:
        return self._generate_tasks(CFG.num_train_tasks,
                                    self._train_problem_generator,
                                    self._train_rng)

    def _generate_test_tasks(self) -> List[Task]:
        return self._generate_tasks(CFG.num_test_tasks,
                                    self._test_problem_generator,
                                    self._test_rng)

    def _generate_tasks(self, num_tasks: int,
                        problem_gen: PDDLProblemGenerator,
                        rng: np.random.Generator) -> List[Task]:
        tasks = []
        for pddl_problem_str in problem_gen(num_tasks, rng):
            task = self._pddl_problem_str_to_task(pddl_problem_str)
            tasks.append(task)
        return tasks

    @property
    def predicates(self) -> Set[Predicate]:
        return self._predicates

    @property
    def goal_predicates(self) -> Set[Predicate]:
        # For now, we assume that all predicates may be included as part of the
        # goals. This is not important because these environments are not
        # currently used for predicate invention. If we do want to use these
        # for predicate invention in the future, we can revisit this, and
        # try to automatically detect which predicates appear in problem goals.
        return self._predicates

    @property
    def types(self) -> Set[Type]:
        return self._types

    @property
    def options(self) -> Set[ParameterizedOption]:
        raise NotImplementedError("Override me!")

    @property
    def action_space(self) -> Box:
        # See class docstring for explanation.
        num_operators = len(self._strips_operators)
        max_arity = max(len(op.parameters) for op in self._strips_operators)
        lb = [0.0 for _ in range(max_arity + 1)]
        ub = [num_operators - 1.0] + [np.inf for _ in range(max_arity)]
        return Box(lb, ub, dtype=np.float32)

    def render_state(self,
                     state: State,
                     task: Task,
                     action: Optional[Action] = None,
                     caption: Optional[str] = None) -> List[Image]:
        raise NotImplementedError("Render not implemented for PDDLEnv.")

    def _state_to_ground_atoms(self, state: State) -> Set[GroundAtom]:
        import ipdb; ipdb.set_trace()

    def _ground_atoms_to_state(self, ground_atoms: Set[GroundAtom]) -> State:
        import ipdb; ipdb.set_trace()

    def _action_to_ground_strips_op(self, action: Action) -> _GroundSTRIPSOperator:
        import ipdb; ipdb.set_trace()

    def _pddl_problem_str_to_task(self, pddl_problem_str: str) -> Task:
        import ipdb; ipdb.set_trace()



class BlocksPDDLEnv(PDDLEnv):
    """The IPC 4-operator blocks world domain."""

    @classmethod
    def get_name(cls) -> str:
        return f"pddl_blocks"

    @property
    def _domain_str(self) -> str:
        path = utils.get_env_asset_path("pddl/blocks/domain.pddl")
        with open(path, encoding="utf-8") as f:
            domain_str = f.read()
        return domain_str

    @property
    def _train_problem_generator(self) -> PDDLProblemGenerator:
        import ipdb; ipdb.set_trace()

    @property
    def _test_problem_generator(self) -> PDDLProblemGenerator:
        import ipdb; ipdb.set_trace()


def _parse_pddl_domain(domain_str: str) -> Tuple[Set[Type], Set[Predicate], Set[STRIPSOperator]]:
    # Let pyperplan do the heavy lifting.
    split_domain_str = domain_str.split("\n")
    domain_ast = parse_domain_def(parse_lisp_iterator(split_domain_str))
    visitor = TraversePDDLDomain()
    domain_ast.accept(visitor)
    pyperplan_domain = visitor.domain
    import ipdb; ipdb.set_trace()
