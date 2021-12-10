"""An approach that invents predicates by searching over candidate sets, with
the candidates proposed from a grammar.
"""

import abc
from dataclasses import dataclass
from functools import cached_property, partial
import itertools
from operator import ge, le
from typing import Set, Callable, List, Sequence, FrozenSet, Iterator, Tuple, \
    Dict
from gym.spaces import Box
import numpy as np
from predicators.src import utils
from predicators.src.approaches import NSRTLearningApproach
from predicators.src.nsrt_learning import segment_trajectory, \
    learn_strips_operators
from predicators.src.structs import State, Predicate, ParameterizedOption, \
    Type, Task, Action, Dataset, Object, GroundAtomTrajectory, STRIPSOperator, \
    OptionSpec, Segment
from predicators.src.settings import CFG


################################################################################
#                          Programmatic classifiers                            #
################################################################################

class _ProgrammaticClassifier(abc.ABC):
    """A classifier implemented as an arbitrary program.
    """
    @abc.abstractmethod
    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        """All programmatic classifiers are functions of state and objects.

        The objects are the predicate arguments.
        """
        raise NotImplementedError("Override me!")

    @abc.abstractmethod
    def __str__(self) -> str:
        raise NotImplementedError("Override me!")


class _NullaryClassifier(_ProgrammaticClassifier):
    """A classifier on zero objects.
    """
    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        assert len(o) == 0
        return self._classify_state(s)

    @abc.abstractmethod
    def _classify_state(self, s: State) -> bool:
        raise NotImplementedError("Override me!")


class _UnaryClassifier(_ProgrammaticClassifier):
    """A classifier on one object.
    """
    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        assert len(o) == 1
        return self._classify_object(s, o[0])

    @abc.abstractmethod
    def _classify_object(self, s: State, obj: Object) -> bool:
        raise NotImplementedError("Override me!")


@dataclass(frozen=True, eq=False, repr=False)
class _SingleAttributeCompareClassifier(_UnaryClassifier):
    """Compare a single feature value with a constant value.
    """
    object_index: int
    object_type: Type
    attribute_name: str
    constant: float
    compare: Callable[[float, float], bool]
    compare_str: str

    def _classify_object(self, s: State, obj: Object) -> bool:
        assert obj.type == self.object_type
        return self.compare(s.get(obj, self.attribute_name), self.constant)

    def __str__(self) -> str:
        return (f"(({self.object_index}:{self.object_type.name})."
                f"{self.attribute_name}{self.compare_str}{self.constant:.3})")


@dataclass(frozen=True, eq=False, repr=False)
class _NegationClassifier(_ProgrammaticClassifier):
    """Negate a given classifier.
    """
    body: Predicate

    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        return not self.body.holds(s, o)

    def __str__(self) -> str:
        return f"NOT-{self.body}"


@dataclass(frozen=True, eq=False, repr=False)
class _ForallClassifier(_NullaryClassifier):
    """Apply a predicate to all objects.
    """
    body: Predicate

    def _classify_state(self, s: State) -> bool:
        for o in utils.get_object_combinations(set(s), self.body.types):
            if not self.body.holds(s, o):
                return False
        return True

    def __str__(self) -> str:
        types = self.body.types
        type_sig = ",".join(f"{i}:{t.name}" for i, t in enumerate(types))
        objs = ",".join(str(i) for i in range(len(types)))
        return f"Forall[{type_sig}].[{str(self.body)}({objs})]"


@dataclass(frozen=True, eq=False, repr=False)
class _UnaryFreeForallClassifier(_UnaryClassifier):
    """Universally quantify all but one variable in a multi-arity predicate.

    Examples:
        - ForAll ?x. On(?x, ?y)
        - Forall ?y. On(?x, ?y)
        - ForAll ?x, ?y. Between(?x, ?z, ?y)
    """
    body: Predicate  # Must be arity 2 or greater.
    free_variable_idx: int

    def __post_init__(self) -> None:
        assert self.body.arity >= 2
        assert self.free_variable_idx < self.body.arity

    @cached_property
    def _quantified_types(self) -> List[Type]:
        return [t for i, t in enumerate(self.body.types)
                if i != self.free_variable_idx]

    def _classify_object(self, s: State, obj: Object) -> bool:
        assert obj.type == self.body.types[self.free_variable_idx]
        for o in utils.get_object_combinations(set(s), self._quantified_types):
            o_lst = list(o)
            o_lst.insert(self.free_variable_idx, obj)
            if not self.body.holds(s, o_lst):
                return False
        return True

    def __str__(self) -> str:
        types = self.body.types
        type_sig = ",".join(f"{i}:{t.name}" for i, t in enumerate(types)
                            if i != self.free_variable_idx)
        objs = ",".join(str(i) for i in range(len(types)))
        return f"Forall[{type_sig}].[{str(self.body)}({objs})]"


################################################################################
#                             Predicate grammars                               #
################################################################################

@dataclass(frozen=True, eq=False, repr=False)
class _PredicateGrammar:
    """A grammar for generating predicate candidates.
    """
    def generate(self, max_num: int) -> Dict[Predicate, float]:
        """Generate candidate predicates from the grammar.
        The dict values are costs, e.g., negative log prior probability for the
        predicate in a PCFG.
        """
        candidates: Dict[Predicate, float] = {}
        if max_num == 0:
            return candidates
        assert max_num > 0
        for candidate, cost in self.enumerate():
            candidates[candidate] = cost
            if len(candidates) == max_num:
                break
        return candidates

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        """Iterate over candidate predicates from less to more cost.
        """
        raise NotImplementedError("Override me!")


@dataclass(frozen=True, eq=False, repr=False)
class _DataBasedPredicateGrammar(_PredicateGrammar):
    """A predicate grammar that uses a dataset.
    """
    dataset: Dataset

    @cached_property
    def types(self) -> Set[Type]:
        """Infer types from the dataset.
        """
        types: Set[Type] = set()
        for traj in self.dataset:
            types.update(o.type for o in traj.states[0])
        return types

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        """Iterate over candidate predicates from less to more cost.
        """
        raise NotImplementedError("Override me!")


@dataclass(frozen=True, eq=False, repr=False)
class _HoldingDummyPredicateGrammar(_DataBasedPredicateGrammar):
    """A hardcoded cover-specific grammar.

    Good for testing with:
        python src/main.py --env cover --approach grammar_search_invention \
            --seed 0 --excluded_predicates Holding
    """
    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        # A necessary predicate.
        block_type = [t for t in self.types if t.name == "block"][0]
        types = [block_type]
        classifier = _SingleAttributeCompareClassifier(
            0, block_type, "grasp", -0.9, ge, ">=")
        # The name of the predicate is derived from the classifier.
        # In this case, the name will be (0.grasp>=-0.9). The "0" at the
        # beginning indicates that the classifier is indexing into the
        # first object argument and looking at its grasp feature. For
        # example, (0.grasp>=-0.9)(block1) would look be a function of
        # state.get(block1, "grasp").
        yield (Predicate(str(classifier), types, classifier), 1.0)

        # An unnecessary predicate (because it's redundant).
        classifier = _SingleAttributeCompareClassifier(
            0, block_type, "is_block", 0.5, ge, ">=")
        yield (Predicate(str(classifier), types, classifier), 1.0)


def _halving_constant_generator(lo: float, hi: float) -> Iterator[float]:
    mid = (hi + lo) / 2.
    yield mid
    left_gen = _halving_constant_generator(lo, mid)
    right_gen = _halving_constant_generator(mid, hi)
    for l, r in zip(left_gen, right_gen):
        yield l
        yield r


@dataclass(frozen=True, eq=False, repr=False)
class _SingleFeatureInequalitiesPredicateGrammar(_DataBasedPredicateGrammar):
    """Generates features of the form "0.feature >= c" or "0.feature <= c".
    """
    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        # Get ranges of feature values from data.
        feature_ranges = self._get_feature_ranges()
        # 0.5, 0.25, 0.75, 0.125, 0.375, ...
        constant_generator = _halving_constant_generator(0.0, 1.0)
        for c in constant_generator:
            for t in sorted(self.types):
                for f in t.feature_names:
                    lb, ub = feature_ranges[t][f]
                    # Optimization: if lb == ub, there is no variation
                    # among this feature, so there's no point in trying to
                    # learn a classifier with it. So, skip the feature.
                    if abs(lb - ub) < 1e-6:
                        continue
                    # Scale the constant by the feature range.
                    k = (c + lb) / (ub - lb)
                    # Only need one of (ge, le) because we can use negations
                    # to get the other (modulo equality, which we shouldn't
                    # rely on anyway because of precision issues).
                    comp, comp_str = le, "<="
                    classifier = _SingleAttributeCompareClassifier(
                        0, t, f, k, comp, comp_str)
                    name = str(classifier)
                    types = [t]
                    yield (Predicate(name, types, classifier), 1.0)


    def _get_feature_ranges(self) -> Dict[Type, Dict[str, Tuple[float, float]]]:
        feature_ranges: Dict[Type, Dict[str, Tuple[float, float]]] = {}
        for traj in self.dataset:
            for state in traj.states:
                for obj in state:
                    if obj.type not in feature_ranges:
                        feature_ranges[obj.type] = {}
                        for f in obj.type.feature_names:
                            v = state.get(obj, f)
                            feature_ranges[obj.type][f] = (v, v)
                    else:
                        for f in obj.type.feature_names:
                            mn, mx = feature_ranges[obj.type][f]
                            v = state.get(obj, f)
                            feature_ranges[obj.type][f] = (min(mn, v),
                                                           max(mx, v))
        return feature_ranges


@dataclass(frozen=True, eq=False, repr=False)
class _GivenPredicateGrammar(_PredicateGrammar):
    """Enumerates a given set of predicates.
    """
    given_predicates: Set[Predicate]

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        for predicate in sorted(self.given_predicates):
            yield (predicate, 1.0)


@dataclass(frozen=True, eq=False, repr=False)
class _ChainPredicateGrammar(_PredicateGrammar):
    """Chains together multiple predicate grammars in sequence.
    """
    base_grammars: Sequence[_PredicateGrammar]

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        return itertools.chain.from_iterable(
            g.enumerate() for g in self.base_grammars)


@dataclass(frozen=True, eq=False, repr=False)
class _SkipGrammar(_PredicateGrammar):
    """A grammar that omits given predicates from being enumerated.
    """
    base_grammar: _PredicateGrammar
    omitted_predicates: Set[Predicate]

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        for (predicate, cost) in self.base_grammar.enumerate():
            if predicate in self.omitted_predicates:
                continue
            yield (predicate, cost)


@dataclass(frozen=True, eq=False, repr=False)
class _NegationPredicateGrammarWrapper(_PredicateGrammar):
    """For each x generated by the base grammar, also generates not(x).
    """
    base_grammar: _PredicateGrammar

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        for (predicate, cost) in self.base_grammar.enumerate():
            yield (predicate, cost)
            classifier = _NegationClassifier(predicate)
            negated_predicate = Predicate(str(classifier), predicate.types,
                                          classifier)
            yield (negated_predicate, cost)


@dataclass(frozen=True, eq=False, repr=False)
class _ForallPredicateGrammarWrapper(_PredicateGrammar):
    """For each x generated by the base grammar, also generates forall(x).
    """
    base_grammar: _PredicateGrammar

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        for (predicate, cost) in self.base_grammar.enumerate():
            yield (predicate, cost)
            if predicate.arity == 0:
                continue
            classifier = _ForallClassifier(predicate)
            yield (Predicate(str(classifier), [], classifier), cost)
            if predicate.arity >= 2:
                for idx in range(predicate.arity):
                    uff_classifier = _UnaryFreeForallClassifier(predicate, idx)
                    uff_predicate = Predicate(str(uff_classifier),
                                              [predicate.types[idx]],
                                              uff_classifier)
                    yield (uff_predicate, cost)


def _create_grammar(grammar_name: str, dataset: Dataset,
                    given_predicates: Set[Predicate]) -> _PredicateGrammar:
    if grammar_name == "holding_dummy":
        return _HoldingDummyPredicateGrammar(dataset)
    if grammar_name == "single_feat_ineqs":
        sfi_grammar = _SingleFeatureInequalitiesPredicateGrammar(dataset)
        return _NegationPredicateGrammarWrapper(sfi_grammar)
    if grammar_name == "forall_single_feat_ineqs":
        # We start with the given predicates because we want to allow
        # negated and quantified versions of the given predicates, in
        # addition to negated and quantified versions of new predicates.
        given_grammar = _GivenPredicateGrammar(given_predicates)
        sfi_grammar = _SingleFeatureInequalitiesPredicateGrammar(dataset)
        # This chained grammar has the effect of enumerating first the
        # given predicates, then the single feature inequality ones.
        chained_grammar = _ChainPredicateGrammar([given_grammar, sfi_grammar])
        # For each predicate enumerated by the chained grammar, we also
        # enumerate the negation of that predicate.
        negated_grammar = _NegationPredicateGrammarWrapper(chained_grammar)
        # For each predicate enumerated, we also enumerate foralls for
        # that predicate.
        forall_grammar = _ForallPredicateGrammarWrapper(negated_grammar)
        # Finally, we don't actually need to enumerate the given predicates
        # because we already have them in the initial predicate set,
        # so we just filter them out from actually being enumerated.
        # But remember that we do want to enumerate their negations
        # and foralls, which is why they're included originally.
        return _SkipGrammar(forall_grammar, given_predicates)
    raise NotImplementedError(f"Unknown grammar name: {grammar_name}.")


################################################################################
#                                 Approach                                     #
################################################################################

class GrammarSearchInventionApproach(NSRTLearningApproach):
    """An approach that invents predicates by searching over candidate sets,
    with the candidates proposed from a grammar.
    """
    def __init__(self, simulator: Callable[[State, Action], State],
                 initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption],
                 types: Set[Type],
                 action_space: Box) -> None:
        super().__init__(simulator, initial_predicates, initial_options,
                         types, action_space)
        self._learned_predicates: Set[Predicate] = set()
        self._num_inventions = 0

    def _get_current_predicates(self) -> Set[Predicate]:
        return self._initial_predicates | self._learned_predicates

    def learn_from_offline_dataset(self, dataset: Dataset,
                                   train_tasks: List[Task]) -> None:
        self._dataset.extend(dataset)
        del dataset
        # Generate a candidate set of predicates.
        print("Generating candidate predicates...")
        grammar = _create_grammar(CFG.grammar_search_grammar_name,
                                  self._dataset, self._initial_predicates)
        candidates = grammar.generate(max_num=CFG.grammar_search_max_predicates)
        print(f"Done: created {len(candidates)} candidates:")
        for predicate in candidates:
            print(predicate)
        # Apply the candidate predicates to the data.
        print("Applying predicates to data...")
        atom_dataset = utils.create_ground_atom_dataset(
            self._dataset, set(candidates) | self._initial_predicates)
        print("Done.")
        # Create the heuristic function that will be used to guide search.
        heuristic = _create_heuristic_function(self._initial_predicates,
            atom_dataset, candidates)
        # Select a subset of the candidates to keep.
        print("Selecting a subset...")
        self._learned_predicates = _select_predicates_to_keep(candidates,
                                                              heuristic)
        print("Done.")
        # Finally, learn NSRTs via superclass, using all the kept predicates.
        self._learn_nsrts()


def _create_heuristic_function(initial_predicates: Set[Predicate],
        atom_dataset: List[GroundAtomTrajectory],
        candidates: Dict[Predicate, float]
        ) -> Callable[[FrozenSet[Predicate]], float]:
    """Returns a function that takes a frozenset of predicates and returns a
    heuristic score, where lower is better.
    """
    if CFG.grammar_search_heuristic == "prediction_error":
        return partial(_prediction_error_heuristic,
                       initial_predicates,
                       atom_dataset,
                       candidates)
    if CFG.grammar_search_heuristic == "hadd_match":
        return partial(_hadd_match_heuristic,
                       initial_predicates,
                       atom_dataset,
                       candidates)
    if CFG.grammar_search_heuristic == "hadd_lookahead_match":
        return partial(_hadd_lookahead_match,
                       initial_predicates,
                       atom_dataset,
                       candidates)
    raise NotImplementedError(
        f"Unknown heuristic: {CFG.grammar_search_heuristic}.")


def _learn_operators_from_atom_dataset(atom_dataset: List[GroundAtomTrajectory]
        ) -> Tuple[List[Segment], List[STRIPSOperator], List[OptionSpec]]:
    """Relearn operators and option specs with the given dataset, which may
    be pruned to include only a subset of the predicates.
    """
    segments = [seg for traj in atom_dataset
                for seg in segment_trajectory(traj)]
    strips_ops, partitions = learn_strips_operators(segments,
                                                    verbose=False)
    option_specs = [p.option_spec for p in partitions]
    return (segments, strips_ops, option_specs)


def _prediction_error_heuristic(initial_predicates: Set[Predicate],
                                atom_dataset: List[GroundAtomTrajectory],
                                candidates: Dict[Predicate, float],
                                s: FrozenSet[Predicate]) -> float:
    """Score a predicate set by learning operators and counting false positives.
    """
    print("Scoring predicates:", s)
    kept_predicates = s | initial_predicates
    pruned_atom_data = utils.prune_ground_atom_dataset(
        atom_dataset, kept_predicates)
    segments, strips_ops, option_specs = \
        _learn_operators_from_atom_dataset(pruned_atom_data)
    num_true_positives, num_false_positives, _, _ = \
        _count_positives_for_ops(strips_ops, option_specs, segments)
    # Also add a size penalty.
    op_size = _get_operators_size(strips_ops)
    # Also add a penalty based on predicate complexity.
    pred_complexity = sum(candidates[p] for p in s)
    # Lower is better.
    total_score = \
        CFG.grammar_search_false_pos_weight * num_false_positives + \
        CFG.grammar_search_true_pos_weight * (-num_true_positives) + \
        CFG.grammar_search_size_weight * op_size + \
        CFG.grammar_search_pred_complexity_weight * pred_complexity
    # Useful for debugging:
    # print("TP/FP/S/C/Total:", num_true_positives, num_false_positives,
    #       op_size, pred_complexity, total_score)
    return total_score


def _hadd_match_heuristic(initial_predicates: Set[Predicate],
                          atom_dataset: List[GroundAtomTrajectory],
                          candidates: Dict[Predicate, float],
                          s: FrozenSet[Predicate]) -> float:
    """Score predicates by comparing the hadd values of the induced operators
    to the groundtruth values inferred from demonstration data.
    """
    del candidates  # not currently used, but maybe later
    print("Scoring predicates:", s)
    score = 0.0  # lower is better
    kept_predicates = s | initial_predicates
    pruned_atom_data = utils.prune_ground_atom_dataset(
        atom_dataset, kept_predicates)
    _, strips_ops, _ =_learn_operators_from_atom_dataset(pruned_atom_data)
    for traj, atoms_sequence in pruned_atom_data:
        if not traj.is_demo:  # we only care about demonstrations
            continue
        # Create hAdd heuristic for this trajectory.
        objects = set(traj.states[0])
        init_atoms = atoms_sequence[0]
        goal_atoms = traj.goal
        ground_ops = {op for strips_op in strips_ops
                      for op in utils.all_ground_operators(strips_op, objects)}
        relaxed_operators = frozenset({utils.RelaxedOperator(
            op.name, utils.atoms_to_tuples(op.preconditions),
            utils.atoms_to_tuples(op.add_effects)) for op in ground_ops})
        hadd_fn = utils.HAddHeuristic(
            utils.atoms_to_tuples(init_atoms),
            utils.atoms_to_tuples(goal_atoms),
            relaxed_operators)
        for i, atoms in enumerate(atoms_sequence):
            # Assumes optimal demonstrations.
            ideal_heuristic = len(atoms_sequence) - i - 1
            hadd_heuristic = hadd_fn(utils.atoms_to_tuples(atoms))
            score += abs(hadd_heuristic - ideal_heuristic)
    print("score:", score)
    return score


def _hadd_lookahead_match(initial_predicates: Set[Predicate],
                          atom_dataset: List[GroundAtomTrajectory],
                          candidates: Dict[Predicate, float],
                          s: FrozenSet[Predicate]) -> float:
    """Score predicates by using the hadd values of the induced operators
    to compute an energy-based policy, and comparing that policy to demos.
    """
    print("Scoring predicates:", s)
    score = 0.0  # lower is better
    kept_predicates = s | initial_predicates
    pruned_atom_data = utils.prune_ground_atom_dataset(
        atom_dataset, kept_predicates)
    _, strips_ops, _ =_learn_operators_from_atom_dataset(pruned_atom_data)
    for traj, atoms_sequence in pruned_atom_data:
        if not traj.is_demo:  # we only care about demonstrations
            continue
        # Create hAdd heuristic for this trajectory.
        objects = set(traj.states[0])
        init_atoms = atoms_sequence[0]
        goal_atoms = traj.goal
        ground_ops = {op for strips_op in strips_ops
                      for op in utils.all_ground_operators(strips_op, objects)}
        relaxed_operators = frozenset({utils.RelaxedOperator(
            op.name, utils.atoms_to_tuples(op.preconditions),
            utils.atoms_to_tuples(op.add_effects)) for op in ground_ops})
        hadd_fn = utils.HAddHeuristic(
            utils.atoms_to_tuples(init_atoms),
            utils.atoms_to_tuples(goal_atoms),
            relaxed_operators)
        for i in range(len(atoms_sequence)-1):
            atoms, next_atoms = atoms_sequence[i], atoms_sequence[i+1]
            # Record the heuristic value for each ground op.
            ground_op_to_heur = {}
            # Record whether the ground op matches the demonstration.
            ground_op_to_match = {}
            for ground_op in ground_ops:
                # Only care about applicable ground ops.
                if not ground_op.preconditions.issubset(atoms):
                    continue
                # Compute the next state under the operator.
                successor_atoms = atoms.copy()
                for atom in ground_op.add_effects:
                    successor_atoms.add(atom)
                for atom in ground_op.delete_effects:
                    successor_atoms.discard(atom)
                # Compute the heuristic for the successor atoms.
                heur = hadd_fn(utils.atoms_to_tuples(successor_atoms))
                ground_op_to_heur[ground_op] = heur
                # Check whether the successor atoms match the demonstration.
                match = (successor_atoms == next_atoms)
                ground_op_to_match[ground_op] = match
            if not any(ground_op_to_match.values()) or all(
                np.isinf(h) for h in ground_op_to_heur.values()):
                return float("inf")
            # Compute the probability that the correct next atoms would be
            # output under an energy-based policy.
            k = 5. # can adjust exponent here
            ground_op_to_neg_exp = {o: np.exp(-k*h) if not np.isinf(h) else 0.
                                    for o, h in ground_op_to_heur.items()}
            z = sum(ground_op_to_neg_exp.values())
            ground_op_to_prob = {o: ground_op_to_neg_exp[o] / z
                                 for o in ground_op_to_match}
            atom_score = sum(match * ground_op_to_prob[o]
                             for o, match in ground_op_to_match.items())

            # print("Real add effects:", next_atoms - atoms)
            # print("Real del effects:", atoms - next_atoms)
            # for op in ground_op_to_heur:
            #     print("Op:", op)
            #     print("Heur:", ground_op_to_heur[op])
            #     print("Match:", ground_op_to_match[op])
            #     print("Prob select:", ground_op_to_prob[op])
            # print("Score:", atom_score)
            # import ipdb; ipdb.set_trace()

            score -= atom_score

    # Also add a size penalty.
    op_size = _get_operators_size(strips_ops)
    # Also add a penalty based on predicate complexity.
    pred_complexity = sum(candidates[p] for p in s)
    # Lower is better.
    total_score = score + \
        CFG.grammar_search_size_weight * op_size + \
        CFG.grammar_search_pred_complexity_weight * pred_complexity

    print("total_score:", total_score)

    return total_score


def _select_predicates_to_keep(
        candidates: Dict[Predicate, float],
        heuristic: Callable[[FrozenSet[Predicate]], float]
        ) -> Set[Predicate]:
    """Perform a greedy search over predicate sets.
    """

    # There are no goal states for this search; run until exhausted.
    def _check_goal(s: FrozenSet[Predicate]) -> bool:
        del s  # unused
        return False

    # Successively consider larger predicate sets.
    def _get_successors(s: FrozenSet[Predicate]
            ) -> Iterator[Tuple[None, FrozenSet[Predicate], float]]:
        for predicate in sorted(set(candidates) - s):  # determinism
            # Actions not needed. Frozensets for hashing.
            # The cost of 1.0 is irrelevant because we're doing GBFS
            # and not A* (because we don't care about the path).
            yield (None, frozenset(s | {predicate}), 1.0)

    # Start the search with no candidates.
    init : FrozenSet[Predicate] = frozenset()

    # Greedy best first search.
    path, _ = utils.run_gbfs(
        init, _check_goal, _get_successors, heuristic,
        max_evals=CFG.grammar_search_max_evals)
    kept_predicates = path[-1]

    print(f"Selected {len(kept_predicates)} predicates out of "
          f"{len(candidates)} candidates:")
    for pred in kept_predicates:
        print(pred)

    return set(kept_predicates)


def _count_positives_for_ops(strips_ops: List[STRIPSOperator],
                             option_specs: List[OptionSpec],
                             segments: List[Segment],
                             ) -> Tuple[int, int,
                                        List[Set[int]], List[Set[int]]]:
    """Returns num true positives, num false positives, and for each strips op,
    lists of segment indices that contribute true or false positives.

    The lists of segment indices are useful only for debugging; they are
    otherwise redundant with num_true_positives/num_false_positives.
    """
    assert len(strips_ops) == len(option_specs)
    num_true_positives = 0
    num_false_positives = 0
    # The following two lists are just useful for debugging.
    true_positive_idxs : List[Set[int]] = [set() for _ in strips_ops]
    false_positive_idxs : List[Set[int]] = [set() for _ in strips_ops]
    for idx, segment in enumerate(segments):
        objects = set(segment.states[0])
        segment_option = segment.get_option()
        option_objects = segment_option.objects
        covered_by_some_op = False
        # Ground only the operators with a matching option spec.
        for op_idx, (op, option_spec) in enumerate(zip(strips_ops,
                                                       option_specs)):
            # If the parameterized options are different, not relevant.
            if option_spec[0] != segment_option.parent:
                continue
            option_vars = option_spec[1]
            assert len(option_vars) == len(option_objects)
            option_var_to_obj = dict(zip(option_vars, option_objects))
            # We want to get all ground operators whose corresponding
            # substitution is consistent with the option vars for this
            # segment. So, determine all of the operator variables
            # that are not in the option vars, and consider all
            # groundings of them.
            for ground_op in utils.all_ground_operators_given_partial(
                op, objects, option_var_to_obj):
                # Check the ground_op against the segment.
                if not ground_op.preconditions.issubset(
                    segment.init_atoms):
                    continue
                if ground_op.add_effects == segment.add_effects and \
                   ground_op.delete_effects == segment.delete_effects:
                    covered_by_some_op = True
                    true_positive_idxs[op_idx].add(idx)
                else:
                    false_positive_idxs[op_idx].add(idx)
                    num_false_positives += 1
        if covered_by_some_op:
            num_true_positives += 1
    return num_true_positives, num_false_positives, \
        true_positive_idxs, false_positive_idxs


def _get_operators_size(strips_ops: List[STRIPSOperator]) -> int:
    size = 0
    for op in strips_ops:
        size += len(op.parameters) + len(op.preconditions) + \
                len(op.add_effects) + len(op.delete_effects)
    return size
