"""An approach that invents predicates by searching over candidate sets, with
the candidates proposed from a grammar.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Set, Callable, List, Optional, DefaultDict, Dict, Sequence, \
    Any, FrozenSet, Iterator, Tuple
import numpy as np
from gym.spaces import Box
from predicators.src import utils
from predicators.src.approaches import NSRTLearningApproach
from predicators.src.nsrt_learning import segment_trajectory, \
    learn_strips_operators
from predicators.src.structs import State, Predicate, ParameterizedOption, \
    Type, Task, Action, Dataset, GroundAtom, LiftedAtom, Array, Object, \
    GroundAtomTrajectory, STRIPSOperator
from predicators.src.torch_models import LearnedPredicateClassifier, \
    MLPClassifier
from predicators.src.settings import CFG


@dataclass(frozen=True, eq=False, repr=False)
class _SingleAttributeGEClassifier:
    """Check whether a single attribute value on an object is >= some value.
    """
    object_index: int
    attribute_name: str
    value: float

    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        obj = o[self.object_index]
        return s.get(obj, self.attribute_name) >= self.value


class GrammarSearchInventionApproach(NSRTLearningApproach):
    """An approach that invents predicates by searching over candidate sets,
    with the candidates proposed from a grammar.
    """
    def __init__(self, simulator: Callable[[State, Action], State],
                 all_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption],
                 types: Set[Type],
                 action_space: Box,
                 train_tasks: List[Task]) -> None:
        super().__init__(simulator, all_predicates, initial_options,
                         types, action_space, train_tasks)
        self._learned_predicates: Set[Predicate] = set()
        self._num_inventions = 0

    def _get_current_predicates(self) -> Set[Predicate]:
        return self._initial_predicates | self._learned_predicates

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        # Generate a large candidate set of predicates.
        candidates = self._generate_candidate_predicates()
        # Apply the candidate predicates to the data.
        atom_dataset = utils.create_ground_atom_dataset(dataset,
            candidates | self._initial_predicates)
        # Select a subset of the candidates to keep.
        self._learned_predicates = self._select_predicates_to_keep(candidates,
            atom_dataset)
        # Finally, learn NSRTs via superclass, using all the kept predicates.
        self._learn_nsrts(dataset)

    def _generate_candidate_predicates(self) -> Set[Predicate]:
        # TODO
        # Testing: python src/main.py --env cover --approach grammar_search_invention --seed 0 --excluded_predicates Holding

        candidates = set()

        # A necessary predicate
        name = "InventedHolding"
        block_type = [t for t in self._types if t.name == "block"][0]
        types = [block_type]
        classifier = _SingleAttributeGEClassifier(0, "grasp", -0.9)
        predicate = Predicate(name, types, classifier)
        candidates.add(predicate)

        # An unnecessary predicate (because it's redundant)
        name = "InventedDummy"
        block_type = [t for t in self._types if t.name == "block"][0]
        types = [block_type]
        classifier = _SingleAttributeGEClassifier(0, "is_block", 0.5)
        predicate = Predicate(name, types, classifier)
        candidates.add(predicate)

        return candidates

    def _select_predicates_to_keep(self, candidates: Set[Predicate],
                                   atom_dataset: List[GroundAtomTrajectory]
                                   ) -> Set[Predicate]:
        # Perform a greedy search over predicate sets.
        # Successively consider small predicate sets.
        def _get_successors(s: FrozenSet[Predicate]
                ) -> Iterator[Tuple[None, FrozenSet[Predicate], float]]:
            for predicate in sorted(s):  # sorting for determinism
                # Actions not needed. Frozensets for hashing.
                yield (None, frozenset(s - {predicate}), 1.)

        # The heuristic is where the action happens...
        def _heuristic(s: FrozenSet[Predicate]) -> float:
            # Relearn operators with the current predicates.
            kept_preds = s | self._initial_predicates
            pruned_atom_data = utils.prune_ground_atom_dataset(atom_dataset,
                                                               kept_preds)
            segments = [seg for traj in pruned_atom_data
                        for seg in segment_trajectory(traj)]
            strips_ops, _ = learn_strips_operators(segments)
            # Score based on how well the operators fit the data.
            num_true_positives, num_false_positives = \
                self._count_positives_for_ops(strips_ops, pruned_atom_data)
            # Also add a size penalty.
            op_size = self._get_operators_size(strips_ops)
            # Lower is better.
            return CFG.grammar_search_false_pos_weight * num_false_positives + \
                CFG.grammar_search_true_pos_weight * (-num_true_positives) + \
                CFG.grammar_search_size_weight * (op_size)

        # There are no goal states for this search; run until exhausted.
        def _check_goal(s: FrozenSet[Predicate]) -> bool:
            return False

        # Start the search with all of the candidates.
        init = frozenset(candidates)

        # Greedy best first search.
        path, _ = utils.run_gbfs(
            init, _check_goal, _get_successors, _heuristic,
            max_expansions=CFG.grammar_search_max_expansions)
        kept_predicates = path[-1]

        print(f"Selected {len(kept_predicates)} predicates out of "
              f"{len(candidates)} candidates:")
        for pred in kept_predicates:
            print(pred)

        return set(kept_predicates)

    def _count_positives_for_ops(self, strips_ops: List[STRIPSOperator],
                                 pruned_atom_data: List[GroundAtomTrajectory]
                                 ) -> Tuple[int, int]:
        """Returns num true positives, num false positives.
        """
        num_true_positives = 0
        num_false_positives = 0
        for (states, _, atom_sequence) in pruned_atom_data:
            if len(atom_sequence) == 0:
                continue
            objects = set(states[0])
            ground_ops = [o for op in strips_ops
                          for o in utils.all_ground_operators(op, objects)]
            for s, ns in zip(atom_sequence[1:], atom_sequence[:-1]):
                for ground_op in ground_ops:
                    if not ground_op.preconditions.issubset(s):
                        continue
                    if ground_op.add_effects == ns - s and \
                       ground_op.delete_effects == s - ns:
                        num_true_positives += 1
                    else:
                        num_false_positives += 1
        return num_true_positives, num_false_positives

    def _get_operators_size(self, strips_ops: List[STRIPSOperator]) -> int:
        size = 0
        for op in strips_ops:
            size += len(op.parameters) + len(op.preconditions) + \
                    len(op.add_effects) + len(op.delete_effects)
        return size

