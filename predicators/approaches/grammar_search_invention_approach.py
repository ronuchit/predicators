"""An approach that invents predicates by searching over candidate sets, with
the candidates proposed from a grammar."""

from __future__ import annotations

import abc
import itertools
import logging
from dataclasses import dataclass, field
from functools import cached_property
from operator import le
from typing import Callable, Dict, FrozenSet, Iterator, List, Sequence, Set, \
    Tuple

from gym.spaces import Box

from predicators import utils
from predicators.approaches.nsrt_learning_approach import NSRTLearningApproach
from predicators.nsrt_learning.segmentation import segment_trajectory
from predicators.nsrt_learning.strips_learning import learn_strips_operators
from predicators.predicate_search_score_functions import \
    _PredicateSearchScoreFunction, create_score_function
from predicators.settings import CFG
from predicators.structs import Dataset, GroundAtom, GroundAtomTrajectory, \
    Object, ParameterizedOption, Predicate, Segment, State, Task, Type

################################################################################
#                          Programmatic classifiers                            #
################################################################################


def _create_grammar(dataset: Dataset,
                    given_predicates: Set[Predicate]) -> _PredicateGrammar:
    # We start with considering various ways to split either single or
    # two feature values across our dataset.
    grammar: _PredicateGrammar = _SingleFeatureInequalitiesPredicateGrammar(
        dataset)
    if CFG.grammar_search_grammar_use_diff_features:
        diff_grammar = _FeatureDiffInequalitiesPredicateGrammar(dataset)
        grammar = _ChainPredicateGrammar([grammar, diff_grammar],
                                         alternate=True)
    # We next optionally add in the given predicates because we want to allow
    # negated and quantified versions of the given predicates, in
    # addition to negated and quantified versions of new predicates.
    # The chained grammar has the effect of enumerating first the
    # given predicates, then the single feature inequality ones.
    if CFG.grammar_search_grammar_includes_givens:
        given_grammar = _GivenPredicateGrammar(given_predicates)
        grammar = _ChainPredicateGrammar([given_grammar, grammar])
    # Now, the grammar will undergo a series of transformations.
    # For each predicate enumerated by the grammar, we also
    # enumerate the negation of that predicate.
    grammar = _NegationPredicateGrammarWrapper(grammar)
    # For each predicate enumerated, we also optionally enumerate foralls
    # for that predicate, along with appropriate negations.
    if CFG.grammar_search_grammar_includes_foralls:
        grammar = _ForallPredicateGrammarWrapper(grammar)
    # Prune proposed predicates by checking if they are equivalent to
    # any already-generated predicates with respect to the dataset.
    # Note that we want to do this before the skip grammar below,
    # because if any predicates are equivalent to the given predicates,
    # we would not want to generate them. Don't do this if we're using
    # DebugGrammar, because we don't want to prune things that are in there.
    if not CFG.grammar_search_use_handcoded_debug_grammar:
        grammar = _PrunedGrammar(dataset, grammar)
        # pass
    # We don't actually need to enumerate the given predicates
    # because we already have them in the initial predicate set,
    # so we just filter them out from actually being enumerated.
    # But remember that we do want to enumerate their negations
    # and foralls, which is why they're included originally.
    grammar = _SkipGrammar(grammar, given_predicates)
    # If we're using the DebugGrammar, filter out all other predicates.
    if CFG.grammar_search_use_handcoded_debug_grammar:
        grammar = _DebugGrammar(grammar)
    # We're done! Return the final grammar.
    return grammar


class _ProgrammaticClassifier(abc.ABC):
    """A classifier implemented as an arbitrary program."""

    @abc.abstractmethod
    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        """All programmatic classifiers are functions of state and objects.

        The objects are the predicate arguments.
        """
        raise NotImplementedError("Override me!")

    @abc.abstractmethod
    def __str__(self) -> str:
        raise NotImplementedError("Override me!")

    @abc.abstractmethod
    def pretty_str(self) -> Tuple[str, str]:
        """Display the classifier in a nice human-readable format.

        Returns a tuple of (variables string, body string).
        """
        raise NotImplementedError("Override me!")


class _NullaryClassifier(_ProgrammaticClassifier):
    """A classifier on zero objects."""

    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        assert len(o) == 0
        return self._classify_state(s)

    @abc.abstractmethod
    def _classify_state(self, s: State) -> bool:
        raise NotImplementedError("Override me!")


class _UnaryClassifier(_ProgrammaticClassifier):
    """A classifier on one object."""

    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        assert len(o) == 1
        return self._classify_object(s, o[0])

    @abc.abstractmethod
    def _classify_object(self, s: State, obj: Object) -> bool:
        raise NotImplementedError("Override me!")


class _BinaryClassifier(_ProgrammaticClassifier):
    """A classifier on two objects."""

    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        assert len(o) == 2
        o0, o1 = o
        return self._classify_object(s, o0, o1)

    @abc.abstractmethod
    def _classify_object(self, s: State, obj1: Object, obj2: Object) -> bool:
        raise NotImplementedError("Override me!")


@dataclass(frozen=True, eq=False, repr=False)
class _SingleAttributeCompareClassifier(_UnaryClassifier):
    """Compare a single feature value with a constant value."""
    object_index: int
    object_type: Type
    attribute_name: str
    constant: float
    constant_idx: int
    compare: Callable[[float, float], bool]
    compare_str: str

    def _classify_object(self, s: State, obj: Object) -> bool:
        assert obj.type == self.object_type
        return self.compare(s.get(obj, self.attribute_name), self.constant)

    def __str__(self) -> str:
        return (
            f"(({self.object_index}:{self.object_type.name})."
            f"{self.attribute_name}{self.compare_str}[idx {self.constant_idx}]"
            f"{self.constant:.3})")

    def pretty_str(self) -> Tuple[str, str]:
        name = CFG.grammar_search_classifier_pretty_str_names[
            self.object_index]
        vars_str = f"{name}:{self.object_type.name}"
        body_str = (f"({name}.{self.attribute_name} "
                    f"{self.compare_str} {self.constant:.3})")
        return vars_str, body_str


@dataclass(frozen=True, eq=False, repr=False)
class _AttributeDiffCompareClassifier(_BinaryClassifier):
    """Compare the difference between two feature values with a constant
    value."""
    object1_index: int
    object1_type: Type
    attribute1_name: str
    object2_index: int
    object2_type: Type
    attribute2_name: str
    constant: float
    constant_idx: int
    compare: Callable[[float, float], bool]
    compare_str: str

    def _classify_object(self, s: State, obj1: Object, obj2: Object) -> bool:
        assert obj1.type == self.object1_type
        assert obj2.type == self.object2_type
        return self.compare(
            abs(
                s.get(obj1, self.attribute1_name) -
                s.get(obj2, self.attribute2_name)), self.constant)

    def __str__(self) -> str:
        return (f"(|({self.object1_index}:{self.object1_type.name})."
                f"{self.attribute1_name} - ({self.object2_index}:"
                f"{self.object2_type.name}).{self.attribute2_name}|"
                f"{self.compare_str}[idx {self.constant_idx}]"
                f"{self.constant:.3})")

    def pretty_str(self) -> Tuple[str, str]:
        name1 = CFG.grammar_search_classifier_pretty_str_names[
            self.object1_index]
        name2 = CFG.grammar_search_classifier_pretty_str_names[
            self.object2_index]
        vars_str = (f"{name1}:{self.object1_type.name}, "
                    f"{name2}:{self.object2_type.name}")
        body_str = (f"(|{name1}.{self.attribute1_name} - "
                    f"{name2}.{self.attribute2_name}| "
                    f"{self.compare_str} {self.constant:.3})")
        return vars_str, body_str


@dataclass(frozen=True, eq=False, repr=False)
class _NegationClassifier(_ProgrammaticClassifier):
    """Negate a given classifier."""
    body: Predicate

    def __call__(self, s: State, o: Sequence[Object]) -> bool:
        return not self.body.holds(s, o)

    def __str__(self) -> str:
        return f"NOT-{self.body}"

    def pretty_str(self) -> Tuple[str, str]:
        vars_str, body_str = self.body.pretty_str()
        return vars_str, f"¬{body_str}"


@dataclass(frozen=True, eq=False, repr=False)
class _ForallClassifier(_NullaryClassifier):
    """Apply a predicate to all objects."""
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

    def pretty_str(self) -> Tuple[str, str]:
        types = self.body.types
        _, body_str = self.body.pretty_str()
        head = ", ".join(
            f"{CFG.grammar_search_classifier_pretty_str_names[i]}:{t.name}"
            for i, t in enumerate(types))
        vars_str = ""  # there are no variables
        return vars_str, f"(∀ {head} . {body_str})"


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
        return [
            t for i, t in enumerate(self.body.types)
            if i != self.free_variable_idx
        ]

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

    def pretty_str(self) -> Tuple[str, str]:
        types = self.body.types
        _, body_str = self.body.pretty_str()
        head = ", ".join(
            f"{CFG.grammar_search_classifier_pretty_str_names[i]}:{t.name}"
            for i, t in enumerate(types) if i != self.free_variable_idx)
        name = CFG.grammar_search_classifier_pretty_str_names[
            self.free_variable_idx]
        vars_str = f"{name}:{types[self.free_variable_idx].name}"
        return vars_str, f"(∀ {head} . {body_str})"


################################################################################
#                             Predicate grammars                               #
################################################################################


@dataclass(frozen=True, eq=False, repr=False)
class _PredicateGrammar(abc.ABC):
    """A grammar for generating predicate candidates."""

    def generate(self, max_num: int) -> Dict[Predicate, float]:
        """Generate candidate predicates from the grammar.

        The dict values are costs, e.g., negative log prior probability
        for the predicate in a PCFG.
        """
        candidates: Dict[Predicate, float] = {}
        if max_num == 0:
            return candidates
        assert max_num > 0
        for candidate, cost in self.enumerate():
            assert cost > 0
            if cost >= CFG.grammar_search_predicate_cost_upper_bound:
                break
            candidates[candidate] = cost
            if len(candidates) == max_num:
                break
        return candidates

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        """Iterate over candidate predicates from less to more cost."""
        raise NotImplementedError("Override me!")


_DEBUG_PREDICATE_PREFIXES = {
    "tools": [
        "NOT-((0:robot).fingers<=[idx 0]0.5)",  # HandEmpty
        "NOT-((0:screw).is_held<=[idx 0]0.5)",  # HoldingScrew
        "NOT-((0:screwdriver).is_held<=[idx 0]0.5)",  # HoldingScrewdriver
        "NOT-((0:nail).is_held<=[idx 0]0.5)",  # HoldingNail
        "NOT-((0:hammer).is_held<=[idx 0]0.5)",  # HoldingHammer
        "NOT-((0:bolt).is_held<=[idx 0]0.5)",  # HoldingBolt
        "NOT-((0:wrench).is_held<=[idx 0]0.5)",  # HoldingWrench
        "((0:screwdriver).size<=[idx 0]",  # ScrewdriverGraspable
        "((0:hammer).size<=[idx 0]",  # HammerGraspable
    ],
    "painting": [
        "NOT-((0:robot).fingers<=[idx 0]0.5)",  # GripperOpen
        "((0:obj).pose_y<=[idx 2]",  # OnTable
        "NOT-((0:obj).grasp<=[idx 0]0.5)",  # HoldingTop
        "((0:obj).grasp<=[idx 1]0.25)",  # HoldingSide
        "NOT-((0:obj).held<=[idx 0]0.5)",  # Holding
        "NOT-((0:obj).wetness<=[idx 0]0.5)",  # IsWet
        "((0:obj).wetness<=[idx 0]0.5)",  # IsDry
        "NOT-((0:obj).dirtiness<=[idx 0]",  # IsDirty
        "((0:obj).dirtiness<=[idx 0]",  # IsClean
        "Forall[0:lid].[NOT-((0:lid).is_open<=[idx 0]0.5)(0)]",  # AllLidsOpen
        # "NOT-((0:lid).is_open<=[idx 0]0.5)",  # LidOpen (doesn't help)
    ],
    "cover": [
        "NOT-((0:block).grasp<=[idx 0]",  # Holding
        "Forall[0:block].[((0:block).grasp<=[idx 0]",  # HandEmpty
    ],
    "cover_regrasp": [
        "NOT-((0:block).grasp<=[idx 0]",  # Holding
        "Forall[0:block].[((0:block).grasp<=[idx 0]",  # HandEmpty
    ],
    "cover_multistep_options": [
        "NOT-((0:block).grasp<=[idx 0]",  # Holding
        "Forall[0:block].[((0:block).grasp<=[idx 0]",  # HandEmpty
    ],
    "blocks": [
        "NOT-((0:robot).fingers<=[idx 0]",  # GripperOpen
        "Forall[0:block].[NOT-On(0,1)]",  # Clear
        "NOT-((0:block).pose_z<=[idx 0]",  # Holding
    ],
    "repeated_nextto_single_option": [
        "(|(0:dot).x - (1:robot).x|<=[idx 7]6.25)",  # NextTo
    ],
    "unittest": [
        "((0:robot).hand<=[idx 0]0.65)", "((0:block).grasp<=[idx 0]0.0)",
        "NOT-Forall[0:block].[((0:block).width<=[idx 0]0.085)(0)]"
    ],
}


@dataclass(frozen=True, eq=False, repr=False)
class _DebugGrammar(_PredicateGrammar):
    """A grammar that generates only predicates starting with some string in
    _DEBUG_PREDICATE_PREFIXES[CFG.env]."""
    base_grammar: _PredicateGrammar

    def generate(self, max_num: int) -> Dict[Predicate, float]:
        del max_num
        env_name = (CFG.env if not CFG.env.startswith("pybullet") else
                    CFG.env[CFG.env.index("_") + 1:])
        expected_len = len(_DEBUG_PREDICATE_PREFIXES[env_name])
        result = super().generate(expected_len)
        assert len(result) == expected_len
        return result

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        env_name = (CFG.env if not CFG.env.startswith("pybullet") else
                    CFG.env[CFG.env.index("_") + 1:])
        for (predicate, cost) in self.base_grammar.enumerate():
            if any(
                    str(predicate).startswith(debug_str)
                    for debug_str in _DEBUG_PREDICATE_PREFIXES[env_name]):
                yield (predicate, cost)


@dataclass(frozen=True, eq=False, repr=False)
class _DataBasedPredicateGrammar(_PredicateGrammar):
    """A predicate grammar that uses a dataset."""
    dataset: Dataset

    @cached_property
    def types(self) -> Set[Type]:
        """Infer types from the dataset."""
        types: Set[Type] = set()
        for traj in self.dataset.trajectories:
            types.update(o.type for o in traj.states[0])
        return types

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        """Iterate over candidate predicates in an arbitrary order."""
        raise NotImplementedError("Override me!")


def _halving_constant_generator(
        lo: float,
        hi: float,
        cost: float = 1.0) -> Iterator[Tuple[float, float]]:
    """The second element of the tuple is a cost. For example, the first
    several tuples yielded will be:

    (0.5, 1.0), (0.25, 2.0), (0.75, 2.0), (0.125, 3.0), ...
    """
    mid = (hi + lo) / 2.
    yield (mid, cost)
    left_gen = _halving_constant_generator(lo, mid, cost + 1)
    right_gen = _halving_constant_generator(mid, hi, cost + 1)
    for l, r in zip(left_gen, right_gen):
        yield l
        yield r


@dataclass(frozen=True, eq=False, repr=False)
class _SingleFeatureInequalitiesPredicateGrammar(_DataBasedPredicateGrammar):
    """Generates features of the form "0.feature >= c" or "0.feature <= c"."""

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        # Get ranges of feature values from data.
        feature_ranges = self._get_feature_ranges()
        # Edge case: if there are no features at all, return immediately.
        if not any(r for r in feature_ranges.values()):
            return
        # 0.5, 0.25, 0.75, 0.125, 0.375, ...
        constant_generator = _halving_constant_generator(0.0, 1.0)
        for constant_idx, (constant, cost) in enumerate(constant_generator):
            for t in sorted(self.types):
                for f in t.feature_names:
                    lb, ub = feature_ranges[t][f]
                    # Optimization: if lb == ub, there is no variation
                    # among this feature, so there's no point in trying to
                    # learn a classifier with it. So, skip the feature.
                    if abs(lb - ub) < 1e-6:
                        continue
                    # Scale the constant by the feature range.
                    k = constant * (ub - lb) + lb
                    # Only need one of (ge, le) because we can use negations
                    # to get the other (modulo equality, which we shouldn't
                    # rely on anyway because of precision issues).
                    comp, comp_str = le, "<="
                    classifier = _SingleAttributeCompareClassifier(
                        0, t, f, k, constant_idx, comp, comp_str)
                    name = str(classifier)
                    types = [t]
                    pred = Predicate(name, types, classifier)
                    assert pred.arity == 1
                    yield (pred, 1 + cost)  # cost = arity + cost from constant

    def _get_feature_ranges(
            self) -> Dict[Type, Dict[str, Tuple[float, float]]]:
        feature_ranges: Dict[Type, Dict[str, Tuple[float, float]]] = {}
        for traj in self.dataset.trajectories:
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
                            feature_ranges[obj.type][f] = (min(mn,
                                                               v), max(mx, v))
        return feature_ranges


@dataclass(frozen=True, eq=False, repr=False)
class _FeatureDiffInequalitiesPredicateGrammar(
        _SingleFeatureInequalitiesPredicateGrammar):
    """Generates features of the form "|0.feature - 1.feature| <= c"."""

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        # Get ranges of feature values from data.
        feature_ranges = self._get_feature_ranges()
        # Edge case: if there are no features at all, return immediately.
        if not any(r for r in feature_ranges.values()):
            return
        # 0.5, 0.25, 0.75, 0.125, 0.375, ...
        constant_generator = _halving_constant_generator(0.0, 1.0)
        for constant_idx, (constant, cost) in enumerate(constant_generator):
            for (t1, t2) in itertools.combinations_with_replacement(
                    sorted(self.types), 2):
                for f1 in t1.feature_names:
                    for f2 in t2.feature_names:
                        # To create our classifier, we need to leverage the
                        # upper and lower bounds of its features.
                        # First, we extract these and move on if these
                        # bounds are relatively close together.
                        lb1, ub1 = feature_ranges[t1][f1]
                        if abs(lb1 - ub1) < 1e-6:
                            continue
                        lb2, ub2 = feature_ranges[t2][f2]
                        if abs(lb2 - ub2) < 1e-6:
                            continue
                        # Now, we must compute the upper and lower bounds of
                        # the expression |t1.f1 - t2.f2|. If the intervals
                        # [lb1, ub1] and [lb2, ub2] overlap, then the lower
                        # bound of the expression is just 0. Otherwise, if
                        # lb2 > ub1, the lower bound is |ub1 - lb2|, and if
                        # ub2 < lb1, the lower bound is |lb1 - ub2|.
                        if utils.f_range_intersection(lb1, ub1, lb2, ub2):
                            lb = 0.0
                        else:
                            lb = min(abs(lb2 - ub1), abs(lb1 - ub2))
                        # The upper bound for the expression can be
                        # computed in a similar fashion.
                        ub = max(abs(ub2 - lb1), abs(ub1 - lb2))

                        # Scale the constant by the correct range.
                        k = constant * (ub - lb) + lb
                        # Create classifier.
                        comp, comp_str = le, "<="
                        diff_classifier = _AttributeDiffCompareClassifier(
                            0, t1, f1, 1, t2, f2, k, constant_idx, comp,
                            comp_str)
                        name = str(diff_classifier)
                        types = [t1, t2]
                        pred = Predicate(name, types, diff_classifier)
                        assert pred.arity == 2
                        yield (pred, 2 + cost
                               )  # cost = arity + cost from constant


@dataclass(frozen=True, eq=False, repr=False)
class _GivenPredicateGrammar(_PredicateGrammar):
    """Enumerates a given set of predicates."""
    given_predicates: Set[Predicate]

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        for predicate in sorted(self.given_predicates):
            yield (predicate, predicate.arity + 1)


@dataclass(frozen=True, eq=False, repr=False)
class _ChainPredicateGrammar(_PredicateGrammar):
    """Chains together multiple predicate grammars in sequence."""
    base_grammars: Sequence[_PredicateGrammar]
    alternate: bool = False

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        if not self.alternate:
            return itertools.chain.from_iterable(g.enumerate()
                                                 for g in self.base_grammars)
        return utils.roundrobin([g.enumerate() for g in self.base_grammars])


@dataclass(frozen=True, eq=False, repr=False)
class _SkipGrammar(_PredicateGrammar):
    """A grammar that omits given predicates from being enumerated."""
    base_grammar: _PredicateGrammar
    omitted_predicates: Set[Predicate]

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        for (predicate, cost) in self.base_grammar.enumerate():
            if predicate in self.omitted_predicates:
                continue
            # No change to costs when skipping.
            yield (predicate, cost)


@dataclass(frozen=True, eq=False, repr=False)
class _PrunedGrammar(_DataBasedPredicateGrammar):
    """A grammar that prunes redundant predicates."""
    base_grammar: _PredicateGrammar
    _state_sequences: List[List[State]] = field(init=False,
                                                default_factory=list)

    def __post_init__(self) -> None:
        if CFG.segmenter != "atom_changes":
            # If the segmenter doesn't depend on atoms, we can be very
            # efficient during pruning by pre-computing the segments.
            # Then, we only need to care about the initial and final
            # states in each segment, which we store into
            # self._state_sequence.
            for traj in self.dataset.trajectories:
                dummy_atoms_seq: List[Set[GroundAtom]] = [
                    set() for _ in range(len(traj.states))
                ]
                seg_traj = segment_trajectory((traj, dummy_atoms_seq))
                state_seq = utils.segment_trajectory_to_state_sequence(
                    seg_traj)
                self._state_sequences.append(state_seq)

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        # Predicates are identified based on their evaluation across
        # all states in the dataset.
        seen: Dict[FrozenSet[Tuple[int, int, FrozenSet[Tuple[Object, ...]]]],
                   Predicate] = {}  # keys are from _get_predicate_identifier()
        for (predicate, cost) in self.base_grammar.enumerate():
            # if predicate.name == "((0:block).held<=[idx 0]0.5)":
            #     yield (predicate, cost)
            if cost >= CFG.grammar_search_predicate_cost_upper_bound:
                return
            pred_id = self._get_predicate_identifier(predicate)
            if pred_id in seen:
                logging.debug(f"Pruning {predicate} b/c equal to "
                              f"{seen[pred_id]}")
                logging.info(f"Pruning {predicate} b/c equal to "
                              f"{seen[pred_id]}")
                continue
            # Found a new predicate.
            seen[pred_id] = predicate
            yield (predicate, cost)

    def _get_predicate_identifier(
        self, predicate: Predicate
    ) -> FrozenSet[Tuple[int, int, FrozenSet[Tuple[Object, ...]]]]:
        """Returns frozenset identifiers for each data point."""
        raw_identifiers = set()
        if CFG.segmenter == "atom_changes":
            # Get atoms for this predicate alone on the dataset, and then
            # go through the entire dataset.
            atom_dataset = utils.create_ground_atom_dataset(
                self.dataset.trajectories, {predicate})
            for traj_idx, (_, atom_traj) in enumerate(atom_dataset):
                for t, atoms in enumerate(atom_traj):
                    atom_args = frozenset(tuple(a.objects) for a in atoms)
                    raw_identifiers.add((traj_idx, t, atom_args))
        else:
            # This list may expand in the future if we add other segmentation
            # methods, but leaving this assertion in as a safeguard anyway.
            assert CFG.segmenter in ("option_changes", "contacts")
            # Make use of the pre-computed segment-level state sequences.
            for traj_idx, state_seq in enumerate(self._state_sequences):
                for t, state in enumerate(state_seq):
                    atoms = utils.abstract(state, {predicate})
                    atom_args = frozenset(tuple(a.objects) for a in atoms)
                    raw_identifiers.add((traj_idx, t, atom_args))
        return frozenset(raw_identifiers)


@dataclass(frozen=True, eq=False, repr=False)
class _NegationPredicateGrammarWrapper(_PredicateGrammar):
    """For each x generated by the base grammar, also generates not(x)."""
    base_grammar: _PredicateGrammar

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        for (predicate, cost) in self.base_grammar.enumerate():
            yield (predicate, cost)
            classifier = _NegationClassifier(predicate)
            negated_predicate = Predicate(str(classifier), predicate.types,
                                          classifier)
            # No change to costs when negating.
            yield (negated_predicate, cost)


@dataclass(frozen=True, eq=False, repr=False)
class _ForallPredicateGrammarWrapper(_PredicateGrammar):
    """For each x generated by the base grammar, also generates forall(x) and
    the negation not-forall(x).

    If x has arity at least 2, also generates UnaryFreeForallClassifiers
    over x, along with negations.
    """
    base_grammar: _PredicateGrammar

    def enumerate(self) -> Iterator[Tuple[Predicate, float]]:
        for (predicate, cost) in self.base_grammar.enumerate():
            yield (predicate, cost)
            if predicate.arity == 0:
                continue
            # Generate Forall(x)
            forall_classifier = _ForallClassifier(predicate)
            forall_predicate = Predicate(str(forall_classifier), [],
                                         forall_classifier)
            assert forall_predicate.arity == 0
            yield (forall_predicate, cost + 1)  # add arity + 1 to cost
            # Generate NOT-Forall(x)
            notforall_classifier = _NegationClassifier(forall_predicate)
            notforall_predicate = Predicate(str(notforall_classifier),
                                            forall_predicate.types,
                                            notforall_classifier)
            assert notforall_predicate.arity == 0
            yield (notforall_predicate, cost + 1)  # add arity + 1 to cost
            # Generate UFFs
            if predicate.arity >= 2:
                for idx in range(predicate.arity):
                    # Positive UFF
                    uff_classifier = _UnaryFreeForallClassifier(predicate, idx)
                    uff_predicate = Predicate(str(uff_classifier),
                                              [predicate.types[idx]],
                                              uff_classifier)
                    assert uff_predicate.arity == 1
                    yield (uff_predicate, cost + 2)  # add arity + 1 to cost
                    # Negated UFF
                    notuff_classifier = _NegationClassifier(uff_predicate)
                    notuff_predicate = Predicate(str(notuff_classifier),
                                                 uff_predicate.types,
                                                 notuff_classifier)
                    assert notuff_predicate.arity == 1
                    yield (notuff_predicate, cost + 2)  # add arity + 1 to cost


################################################################################
#                                 Approach                                     #
################################################################################


class GrammarSearchInventionApproach(NSRTLearningApproach):
    """An approach that invents predicates by searching over candidate sets,
    with the candidates proposed from a grammar."""

    def __init__(self, initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task]) -> None:
        super().__init__(initial_predicates, initial_options, types,
                         action_space, train_tasks)
        self._learned_predicates: Set[Predicate] = set()
        self._num_inventions = 0

    @classmethod
    def get_name(cls) -> str:
        return "grammar_search_invention"

    def _get_current_predicates(self) -> Set[Predicate]:
        return self._initial_predicates | self._learned_predicates

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        # Generate a candidate set of predicates.
        logging.info("Generating candidate predicates...")
        grammar = _create_grammar(dataset, self._initial_predicates)
        candidates = grammar.generate(
            max_num=CFG.grammar_search_max_predicates)
        logging.info(f"Done: created {len(candidates)} candidates:")
        for predicate, cost in candidates.items():
            logging.info(f"{predicate} {cost}")
        # Apply the candidate predicates to the data.
        logging.info("Applying predicates to data...")
        atom_dataset = utils.create_ground_atom_dataset(
            dataset.trajectories,
            set(candidates) | self._initial_predicates)
        logging.info("Done.")
        # Create the score function that will be used to guide search.
        score_function = create_score_function(
            CFG.grammar_search_score_function, self._initial_predicates,
            atom_dataset, candidates, self._train_tasks)
        # Select a subset of the candidates to keep.
        logging.info("Selecting a subset...")
        if CFG.grammar_search_pred_selection_approach == "score_optimization":
            self._learned_predicates = \
                self._select_predicates_by_score_hillclimbing(
                candidates, score_function, self._initial_predicates,
                atom_dataset, self._train_tasks)
        elif CFG.grammar_search_pred_selection_approach == "clustering":
            self._learned_predicates = self._select_predicates_by_clustering(
                candidates, self._initial_predicates, dataset, atom_dataset, self._train_tasks)
        logging.info("Done.")

        # Finally, learn NSRTs via superclass, using all the kept predicates.
        self._learn_nsrts2(dataset.trajectories,
                          online_learning_cycle=None,
                          annotations=dataset.annotations)
        print("NUM NSRTS: ", len(self._nsrts))

    # def learn_from_offline_dataset(self, dataset: Dataset) -> None:
    #     # Generate a candidate set of predicates.
    #     logging.info("Generating candidate predicates...")
    #     grammar = _create_grammar(dataset, self._initial_predicates)
    #     candidates = grammar.generate(
    #         max_num=CFG.grammar_search_max_predicates)
    #     logging.info(f"Done: created {len(candidates)} candidates:")
    #     for predicate, cost in candidates.items():
    #         logging.info(f"{predicate} {cost}")
    #     # Apply the candidate predicates to the data.
    #     logging.info("Applying predicates to data...")
    #     atom_dataset = utils.create_ground_atom_dataset(
    #         dataset.trajectories,
    #         set(candidates) | self._initial_predicates)
    #     logging.info("Done.")
    #     # Create the score function that will be used to guide search.
    #     score_function = create_score_function(
    #         CFG.grammar_search_score_function, self._initial_predicates,
    #         atom_dataset, candidates, self._train_tasks)
    #     # Select a subset of the candidates to keep.
    #     logging.info("Selecting a subset...")
    #     if CFG.grammar_search_pred_selection_approach == "score_optimization":
    #         self._learned_predicates = \
    #             self._select_predicates_by_score_hillclimbing(
    #             candidates, score_function, self._initial_predicates,
    #             atom_dataset, self._train_tasks)
    #     elif CFG.grammar_search_pred_selection_approach == "clustering":
    #         self._learned_predicates = self._select_predicates_by_clustering(
    #             candidates, self._initial_predicates, dataset, atom_dataset)
    #     logging.info("Done.")
    #     # Finally, learn NSRTs via superclass, using all the kept predicates.
    #     self._learn_nsrts(dataset.trajectories,
    #                       online_learning_cycle=None,
    #                       annotations=dataset.annotations)
    #     print("NUM NSRTS: ", len(self._nsrts))

    def _select_predicates_by_score_hillclimbing(
            self, candidates: Dict[Predicate, float],
            score_function: _PredicateSearchScoreFunction,
            initial_predicates: Set[Predicate],
            atom_dataset: List[GroundAtomTrajectory],
            train_tasks: List[Task]) -> Set[Predicate]:
        """Perform a greedy search over predicate sets."""

        # There are no goal states for this search; run until exhausted.
        def _check_goal(s: FrozenSet[Predicate]) -> bool:
            del s  # unused
            return False

        # Successively consider larger predicate sets.
        def _get_successors(
            s: FrozenSet[Predicate]
        ) -> Iterator[Tuple[None, FrozenSet[Predicate], float]]:
            for predicate in sorted(set(candidates) - s):  # determinism
                # Actions not needed. Frozensets for hashing. The cost of
                # 1.0 is irrelevant because we're doing GBFS / hill
                # climbing and not A* (because we don't care about the
                # path).
                yield (None, frozenset(s | {predicate}), 1.0)

        # Start the search with no candidates.
        init: FrozenSet[Predicate] = frozenset()

        # Greedy local hill climbing search.
        if CFG.grammar_search_search_algorithm == "hill_climbing":
            path, _, heuristics = utils.run_hill_climbing(
                init,
                _check_goal,
                _get_successors,
                score_function.evaluate,
                enforced_depth=CFG.grammar_search_hill_climbing_depth,
                parallelize=CFG.grammar_search_parallelize_hill_climbing)
            logging.info("\nHill climbing summary:")
            for i in range(1, len(path)):
                new_additions = path[i] - path[i - 1]
                assert len(new_additions) == 1
                new_addition = next(iter(new_additions))
                h = heuristics[i]
                prev_h = heuristics[i - 1]
                logging.info(f"\tOn step {i}, added {new_addition}, with "
                             f"heuristic {h:.3f} (an improvement of "
                             f"{prev_h - h:.3f} over the previous step)")
        elif CFG.grammar_search_search_algorithm == "gbfs":
            path, _ = utils.run_gbfs(
                init,
                _check_goal,
                _get_successors,
                score_function.evaluate,
                max_evals=CFG.grammar_search_gbfs_num_evals)
        else:
            raise NotImplementedError(
                "Unrecognized grammar_search_search_algorithm: "
                f"{CFG.grammar_search_search_algorithm}.")
        kept_predicates = path[-1]
        # The total number of predicate sets evaluated is just the
        # ((number of candidates selected) + 1) * total number of candidates.
        # However, since 'path' always has length one more than the
        # number of selected candidates (since it evaluates the empty
        # predicate set first), we can just compute it as below.
        assert self._metrics.get("total_num_predicate_evaluations") is None
        self._metrics["total_num_predicate_evaluations"] = len(path) * len(
            candidates)

        # Filter out predicates that don't appear in some operator
        # preconditions.
        logging.info("\nFiltering out predicates that don't appear in "
                     "preconditions...")
        pruned_atom_data = utils.prune_ground_atom_dataset(
            atom_dataset, kept_predicates | initial_predicates)
        segmented_trajs = [
            segment_trajectory(traj) for traj in pruned_atom_data
        ]
        low_level_trajs = [ll_traj for ll_traj, _ in pruned_atom_data]
        preds_in_preconds = set()
        for pnad in learn_strips_operators(low_level_trajs,
                                           train_tasks,
                                           set(kept_predicates
                                               | initial_predicates),
                                           segmented_trajs,
                                           verify_harmlessness=False,
                                           annotations=None,
                                           verbose=False):
            for atom in pnad.op.preconditions:
                preds_in_preconds.add(atom.predicate)
        kept_predicates &= preds_in_preconds

        logging.info(f"\nSelected {len(kept_predicates)} predicates out of "
                     f"{len(candidates)} candidates:")
        for pred in kept_predicates:
            logging.info(f"\t{pred}")
        score_function.evaluate(kept_predicates)  # log useful numbers

        return set(kept_predicates)

    def _select_predicates_by_clustering(
            self, candidates: Dict[Predicate, float],
            initial_predicates: Set[Predicate], dataset: Dataset,
            atom_dataset: List[GroundAtomTrajectory], train_tasks: List[Task]) -> Set[Predicate]:
        """Cluster segments from the atom_dataset into clusters corresponding
        to operators and use this to select predicates."""

        # given_clear = None
        # learned_clear = None
        # for p in initial_predicates:
        #     if p.name == "Clear":
        #         given_clear = p
        # for p in candidates.keys():
        #     if p.name == "Forall[0:block].[NOT-On(0,1)]":
        #         learned_clear = p
        # import pdb; pdb.set_trace()

        # predicates_to_keep = set()
        # for c in candidates.keys():
        #     if c.name == "(|(0:dot).x - (1:robot).x|<=[idx 7]6.24)":
        #         predicates_to_keep.add(c)
        #         print("Adding in to debug: ", c.name)
        # predicates_to_keep -= initial_predicates
        # logging.info(
        #     f"\nSelected {len(predicates_to_keep)} predicates out of "
        #     f"{len(candidates)} candidates:")
        # for pred in predicates_to_keep:
        #     logging.info(f"\t{pred}")
        # return predicates_to_keep


        if CFG.grammar_search_pred_clusterer == "option-type-number-sample":

            # first, look at every operator in the last frontier.
            # from the potential add effects, initialize the goal atoms as definitely add effects.



            # Algorithm:
            # Step 1: cluster segments according to which option was executed
            # Step 2: in each of clusters from the previous step, further cluster
            # segments according to the unique set of object types involved in
            # the segment's add effects
            # Step 3: in each of the clusters from the previous step, further
            # cluster segments according to the (maximum) number of unique objects
            # involved in the segment's add effects
            # Step 4: in each of the clusters from the previous step, further
            # cluster segments by assignment from a gaussian mixture model fit
            # to samples associated with the options of all the segments in the
            # cluster, but only if a sample exists
            # Step 5: remove predicates that are not consistent
            # Step 6: get the final set of predicates via a pure intersection of
            # add effect predicates across segments in each cluster

            assert CFG.segmenter == "option_changes"
            segmented_trajs = [segment_trajectory(traj) for traj in atom_dataset]

            # for seg_traj in segmented_trajs:
            #     l = [seg.get_option().name for seg in seg_traj]
            #     b = [seg.get_option() for seg in seg_traj]
            #     if "Pick" in l:
            #         import pdb; pdb.set_trace()


            from functools import reduce
            flattened_segmented_trajs = reduce(lambda a, b: a+b, segmented_trajs)

            # import pdb; pdb.set_trace()

            # Step 1:
            option_to_segments = {}
            for s in flattened_segmented_trajs:
                n = s.get_option().name
                if n in option_to_segments:
                    option_to_segments[n].append(s)
                else:
                    option_to_segments[n] = [s]
            logging.info(f"STEP 1: generated {len(option_to_segments.values())} option-based clusters.")

            # Step 2:
            all_clusters = []
            for j, pair in enumerate(option_to_segments.items()):
                option, segments = pair
                clusters = {}
                for seg in segments:
                    all_types = [set(a.predicate.types) for a in seg.add_effects]
                    if len(all_types) == 0 or len(set.union(*all_types)) == 0:
                        # Either there are no add effects, or the object
                        # arguments for all add effects are empty (which would
                        # happen e.g. if the add effects only involved Forall
                        # predicates with no object arguments). The former
                        # happens in repeated_nextto.
                        continue
                    types = tuple(sorted(list(set.union(*all_types))))
                    if types in clusters:
                        clusters[types].append(seg)
                    else:
                        clusters[types] = [seg]
                        print("New unique type: ", types)
                logging.info(f"STEP 2: generated {len(clusters.values())} type-based clusters for for {j+1}th cluster from STEP 1 involving option {option}.")
                for _, c in clusters.items():
                    all_clusters.append(c)

            # Step 3:
            next_clusters = []
            for j, cluster in enumerate(all_clusters):
                clusters = {}
                for seg in cluster:

                    # debugs = list(seg.add_effects)
                    # import pdb; pdb.set_trace()

                    max_num_unique_objs = max(len(eff.objects) for eff in seg.add_effects)
                    if max_num_unique_objs in clusters:
                        clusters[max_num_unique_objs].append(seg)
                    else:
                        clusters[max_num_unique_objs] = [seg]
                for c in clusters.values():
                    next_clusters.append(c)
                logging.info(f"STEP 3: generated {len(clusters.values())} num-object-based clusters for the {j+1}th cluster from STEP 2 involving option {seg.get_option().name}.")
            # final_clusters = next_clusters
            all_clusters = next_clusters

            # Step 4:
            final_clusters = []
            for j, cluster in enumerate(all_clusters):
                example_segment = cluster[0]
                option_name = example_segment.get_option().name
                # if len(example_segment.get_option().params) == 0 or option_name in ["Paint", "Place", "Pick"]:
                if len(example_segment.get_option().params) == 0:
                    final_clusters.append(cluster)
                    logging.info(f"STEP 4: generated no further sample-based clusters (no parameter!) for the {j+1}th cluster from STEP 3 involving option {option_name}.")
                else:
                    # Do model selection between
                    # a uniform distribution and a gaussian mixture?
                    import numpy as np
                    from sklearn.mixture import GaussianMixture as GMM
                    from scipy.stats import kstest
                    data = np.array([seg.get_option().params for seg in cluster])
                    # If parameter is uniformly distributed, don't cluster
                    # further. Should do a multi-dimensional test but won't do
                    # that for now. TODO: do that later.
                    all_uniform = True
                    # TODO: explain that this is doing it per dimension, but we should be looking at the joint distribution.
                    for i in range(data.shape[1]):
                        col = data[:,i]
                        minimum = col.min()
                        maximum = col.max()
                        null_hypothesis = np.random.uniform(minimum, maximum, len(col))
                        p_value = kstest(col, null_hypothesis).pvalue
                        # p_value = kstest(col, "uniform").pvalue

                        # Significance value of 0.05
                        if p_value < 0.05:
                            all_uniform = False
                            break
                    # if option_name == "Paint":
                    #     import matplotlib.pyplot as plt
                    #     x = data[:,0]
                    #     plt.hist(x)
                    #     plt.savefig("painting_samples.png")
                    #     plt.clf()
                    #     import pdb; pdb.set_trace()
                        # arr = data[:,0]
                        # import matplotlib.pyplot as plt
                        # plt.hist(arr)
                        # plt.savefig("temp.png")
                        # plt.clf()
                    if all_uniform or option_name == "PutOnTable":
                        final_clusters.append(cluster)
                        logging.info(f"STEP 4: generated no further sample-based clusters (uniformly distributed parameter!) for the {j+1}th cluster from STEP 3 involving option {option_name}.")
                    else:
                        max_components = min(len(data), len(np.unique(data)), CFG.grammar_search_clustering_gmm_num_components)
                        n_components = np.arange(1, max_components+1)
                        models = [GMM(n, covariance_type="full", random_state=0).fit(data)
                            for n in n_components]
                        bic = [m.bic(data) for m in models]
                        # TODO: add some penalty based on how it gets less data in each cluster.

                        best = models[np.argmin(bic)]
                        assignments = best.predict(data)

                        sub_clusters = {}
                        for i, assignment in enumerate(assignments):
                            if assignment in sub_clusters:
                                sub_clusters[assignment].append(cluster[i])
                            else:
                                sub_clusters[assignment] = [cluster[i]]


                        logging.info(f"STEP 4: generated {len(sub_clusters.values())} sample-based clusters for the {j+1}th cluster from STEP 3 involving option {option_name}.")
                        for c in sub_clusters.values():
                            final_clusters.append(c)

            logging.info(f"Total {len(final_clusters)} final clusters.")

            ####

            ###
            # Stuff from oracle learning to test if the stuff is working.
            ###
            assert CFG.offline_data_method == "demo+gt_operators"
            assert dataset.annotations is not None and len(
                dataset.annotations) == len(dataset.trajectories)
            assert CFG.segmenter == "option_changes"
            segmented_trajs = [
                segment_trajectory(traj) for traj in atom_dataset
            ]
            assert len(segmented_trajs) == len(dataset.annotations)
            # First, get the set of all ground truth operator names.
            all_gt_op_names = set(ground_nsrt.parent.name
                                  for anno_list in dataset.annotations
                                  for ground_nsrt in anno_list)
            # Next, make a dictionary mapping operator name to segments
            # where that operator was used.
            gt_op_to_segments: Dict[str, List[Segment]] = {
                op_name: []
                for op_name in all_gt_op_names
            }
            for op_list, seg_list in zip(dataset.annotations, segmented_trajs):
                assert len(seg_list) == len(op_list)
                for ground_nsrt, segment in zip(op_list, seg_list):
                    gt_op_to_segments[ground_nsrt.parent.name].append(segment)
            final_clusters = list(gt_op_to_segments.values())
            ###

            # operator to preconditions, and add effects
            # filter out an operator that barely ever appears
            ddd = {}
            for i, c in enumerate(final_clusters):
                op_name = "Op"+str(i)+"-"+str(c[0].get_option().name)
                # preconditions, add effects, delete effects, segments
                ddd[op_name] = [set(), set(), set(), c]

            # For clusters that appear at the end,
            # we can narrow down their add effects as:
            # they DEFINITELY have an add effect that whatever goal atoms
            # were not true in the pre-image.
            # But beyond that, they may have other effects?
            #


            ####

            add_effects_per_cluster = []
            all_add_effects = set()
            for j, c in enumerate(final_clusters):

                # add effects
                add_effects_per_segment = [s.add_effects for s in c]
                ungrounded_add_effects_per_segment = []
                for add_effects in add_effects_per_segment:
                    ungrounded_add_effects_per_segment.append(set(a.predicate for a in add_effects))
                add_effects = set.intersection(*ungrounded_add_effects_per_segment)
                add_effects_per_cluster.append(add_effects)

                # delete effects
                del_effects_per_segment = [s.delete_effects for s in c]
                ungrounded_del_effects_per_segment = []
                for del_effects in del_effects_per_segment:
                    ungrounded_del_effects_per_segment.append(set(a.predicate for a in del_effects))
                del_effects = set.intersection(*ungrounded_del_effects_per_segment)

                # preconditions
                init_atoms_per_segment = [s.init_atoms for s in c]
                ungrounded_init_atoms_per_segment = []
                for init_atoms in init_atoms_per_segment:
                    ungrounded_init_atoms_per_segment.append(set(a.predicate for a in init_atoms))
                init_atoms = set.intersection(*ungrounded_init_atoms_per_segment)

                op_name = "Op"+str(j)+"-"+str(c[0].get_option().name)
                ddd[op_name][0] = init_atoms
                ddd[op_name][1] = add_effects
                ddd[op_name][2] = del_effects

                print(f"Cluster {j} with option {c[0].get_option().name}, predicates:")
                for a in add_effects:
                    print(a)
                print()
                all_add_effects |= add_effects
                # import pdb; pdb.set_trace()

                #
                # ex = c[0]
                # print(f"Cluster {j} with option {ex.get_option().name}:")
                # for a in add_effects:
                #     print(a)
                #

            predicates_to_keep = all_add_effects

            ##########
            # Remove inconsistent predicates.
            inconsistent_preds = set()

            # Old way to remove inconsistent predicates
            predicates_to_keep: Set[Predicate] = set()
            for pred in all_add_effects:
                keep_pred = True
                for j, seg_list in enumerate(final_clusters):
                    seg_0 = seg_list[0]
                    pred_in_add_effs_0 = pred in [
                        atom.predicate for atom in seg_0.add_effects
                    ]
                    pred_in_del_effs_0 = pred in [
                        atom.predicate for atom in seg_0.delete_effects
                    ]
                    for seg in seg_list[1:]:
                        pred_in_curr_add_effs = pred in [
                            atom.predicate for atom in seg.add_effects
                        ]
                        pred_in_curr_del_effs = pred in [
                            atom.predicate for atom in seg.delete_effects
                        ]
                        A = pred_in_add_effs_0 != pred_in_curr_add_effs
                        B = pred_in_del_effs_0 != pred_in_curr_del_effs
                        if A or B:
                        # if not ((pred_in_add_effs_0 == pred_in_curr_add_effs)
                        #         and
                        #         (pred_in_del_effs_0 == pred_in_curr_del_effs)):
                            keep_pred = False
                            print("INCONSISTENT: ", pred.name)

                            inconsistent_preds.add(pred)
                            # if pred.name == "NOT-((0:obj).grasp<=[idx 0]0.5)" or pred.name == "((0:obj).grasp<=[idx 1]0.25)":
                            #     import pdb; pdb.set_trace()
                            break
                    if not keep_pred:
                        break
                if keep_pred:
                    predicates_to_keep.add(pred)
                else:
                    inconsistent_preds.add(pred)

            # Re-add inconsistent predicates that were necessary to disambiguate two clusters.
            # That is, if any two clusters's predicates look the same after removing inconsistent predicates from both,
            # keep all those predicates.
            new_add_effects_per_cluster = []
            for effs_cluster in add_effects_per_cluster:
                new_add_effects_per_cluster.append(effs_cluster - inconsistent_preds)
            # Check all pairs of clusters, if they are the same, add their original cluster's predicates back.
            num_clusters = len(final_clusters)
            for i in range(num_clusters):
                for j in range(num_clusters):
                    if i == j:
                        continue
                    else:
                        if new_add_effects_per_cluster[i] == new_add_effects_per_cluster[j]:
                            print(f"MATCH! : {i}, {j}")
                            predicates_to_keep = predicates_to_keep.union(add_effects_per_cluster[i])
                            predicates_to_keep = predicates_to_keep.union(add_effects_per_cluster[j])
            ####################

            # import pdb; pdb.set_trace()
            #
            # import pdb; pdb.set_trace()
            # print("inconsistent preds: ", inconsistent_preds)

            # # add back in to debug
            # predicates_to_keep = set()
            # add_back = [
            #     # "NOT-((0:obj).grasp<=[idx 1]0.25)",
            #     # "Forall[0:obj].[NOT-((0:obj).grasp<=[idx 1]0.25)(0)]",
            #     # "((0:obj).grasp<=[idx 0]0.5)",
            #     # "Forall[0:obj].[((0:obj).grasp<=[idx 0]0.5)(0)]",
            #     # "((0:block).held<=[idx 0]0.5)"
            #
            #
            #
            #     # for repeated next to
            #     "(|(0:dot).x - (1:robot).x|<=[idx 7]6.24)"
            # ]
            # for c in candidates.keys():
            #     print(c.name)
            #     # if c.name in add_back:
            #     if c.name == "(|(0:dot).x - (1:robot).x|<=[idx 7]6.24)":
            #         predicates_to_keep.add(c)
            #         print("Adding in to debug: ", c.name)
            #
            # import pdb; pdb.set_trace()
            # remove = [
            #     # "NOT-((0:block).pose_z<=[idx 3]0.282)",
            #     # "NOT-Forall[0:block].[((0:block).pose_z<=[idx 1]0.342)(0)]",
            #     # "NOT-((0:block).pose_z<=[idx 1]0.342)",
            #     "((0:block).pose_z<=[idx 1]0.342)",
            #     "((0:block).pose_z<=[idx 3]0.282)",
            #     "Forall[0:block].[((0:block).pose_z<=[idx 1]0.342)(0)]"
            # ]
            #
            # for c in candidates.keys():
            #     if c.name in remove:
            #         predicates_to_keep.remove(c)
            #         print("Removing: ", c)

            # import pdb; pdb.set_trace()

            #
            # new_candidates = {}
            # for c in candidates.keys():
            #     if c in predicates_to_keep:
            #         new_candidates[c] = candidates[c]
            # score_function = create_score_function(
            #     CFG.grammar_search_score_function, self._initial_predicates,
            #     atom_dataset, new_candidates, self._train_tasks)
            #
            # logging.info(f"Sending {len(new_candidates)} predicates to hill climbing approach.")
            # return self._select_predicates_by_score_hillclimbing(
            #     new_candidates,
            #     score_function,
            #     initial_predicates,
            #     atom_dataset,
            #     self._train_tasks
            # )
            #

            # Remove the initial predicates.
            # predicates_to_keep -= initial_predicates

            # remove predicates we aren't keeping
            for i, c in enumerate(final_clusters):
                if len(c) < 10:
                    ddd.pop("Op"+str(i))
            print()
            print()
            for op, stuff in ddd.items():
                print()
                ddd[op][0] = ddd[op][0].intersection(predicates_to_keep)
                ddd[op][1] = ddd[op][1].intersection(predicates_to_keep)
                ddd[op][2] = ddd[op][2].intersection(predicates_to_keep)

                preconditions, add_effects, del_effects, _ = stuff
                print(op + ": ")
                print("preconditions: ")
                for p in preconditions:
                    print(p)
                print()
                print("add effects: ")
                for p in add_effects:
                    print(p)
                print()
                print("delete effects: ")
                for p in del_effects:
                    print(p)
                print()

            logging.info("Performing backchaining to decide operator definitions.")

            #####################
            # backchaining
            #####################
            def seg_to_op(segment, clusters):
                for i, c in enumerate(clusters):
                    if segment in c:
                        # return f"Op{i}-{c[0].get_option().name} with objects ({c[0].get_option().objects})"
                        return f"Op{i}-{c[0].get_option().name}"
            # Go through demos
            temp = []
            for a, segmented_traj in enumerate(segmented_trajs):
                if a == 10:
                    break
                traj = []
                for seg in segmented_traj:
                    traj.append(seg_to_op(seg, final_clusters))
                temp.append(traj)
                print(traj)
            # import pdb; pdb.set_trace()

            ttt = list(predicates_to_keep)
            for p in ttt:
                print(p)

            def print_demo2(segmented_traj, filename):

                max_length = 0
                with open(filename, 'w') as file:
                    # preconditions
                    preconds = []
                    for seg in segmented_traj:
                        op_name = seg_to_op(seg, final_clusters)
                        ps = sorted([p.name for p in ddd[op_name][0]])
                        init_atoms = set(p for p in seg.init_atoms if p.predicate.name in ps)
                        # we assume we only have to look at objects involved in the operator,
                        # so we can ignore all other ground atoms.
                        # can figure out which objects change in terms of predicates.
                        objects = set(o for a in seg.add_effects for o in a.objects) |set(o for a in seg.delete_effects for o in a.objects)
                        relevant_init_atoms = set(p for p in init_atoms if set(p.objects).issubset(objects))
                        relevant_init_atoms_str = sorted([str(p) for p in relevant_init_atoms])
                        for p in relevant_init_atoms_str:
                            max_length = max(max_length, len(p))
                        to_print = [op_name, '----', 'preconditions', '===='] + relevant_init_atoms_str
                        preconds.append(to_print)

                    # add effects
                    add_effs = []
                    for seg in segmented_traj:
                        op_name = seg_to_op(seg, final_clusters)
                        adds = sorted([str(p) for p in seg.add_effects if p.predicate in ddd[op_name][1]])
                        for p in adds:
                            max_length = max(max_length, len(p))
                        adds = ['add effects', '===='] + adds
                        add_effs.append(adds)

                    # delete effects
                    del_effs = []
                    for seg in segmented_traj:
                        op_name = seg_to_op(seg, final_clusters)
                        dels = sorted([str(p) for p in seg.delete_effects if p.predicate in ddd[op_name][2]])
                        for p in dels:
                            max_length = max(max_length, len(p))
                        dels = ['delete effects', '===='] + dels
                        del_effs.append(dels)

                    max_length += 3

                    max_lst_len = max(len(lst) for lst in preconds)
                    for i in range(max_lst_len):
                        s = " ".join(f"{lst[i]:<{max_length}}" if i < len(lst) else " " * max_length for lst in preconds)
                        file.write(s + '\n')
                    file.write('\n')
                    max_lst_len = max(len(lst) for lst in add_effs)
                    for i in range(max_lst_len):
                        s = " ".join(f"{lst[i]:<{max_length}}" if i < len(lst) else " " * max_length for lst in add_effs)
                        file.write(s + '\n')
                    file.write('\n')
                    max_lst_len = max(len(lst) for lst in del_effs)
                    for i in range(max_lst_len):
                        s = " ".join(f"{lst[i]:<{max_length}}" if i < len(lst) else " " * max_length for lst in del_effs)
                        file.write(s + '\n')
                    file.write('\n')

            # print_demo2(segmented_trajs[8][0:3], "demo8_part1.txt")
            # print_demo2(segmented_trajs[8][3:], "demo8_part12.txt")

            print_demo2(segmented_trajs[7], "demo7.txt")
            import pdb; pdb.set_trace()

            ###################
            # ALGORITHM
            #

            def get_story(segmented_traj):
                story = []
                    # each entry is a list of lists
                for seg in segmented_traj:
                    entry = []
                    op_name = seg_to_op(seg, final_clusters)
                    init_atoms = set(p for p in seg.init_atoms if p.predicate in ddd[op_name][0])
                    objects = (set(o for a in seg.add_effects for o in a.objects) | set(o for a in seg.delete_effects for o in a.objects))
                    preconditions = set(p for p in init_atoms if set(p.objects).issubset(objects))
                    add_effects = set(p for p in seg.add_effects if p.predicate in ddd[op_name][1])
                    delete_effects = set(p for p in seg.delete_effects if p.predicate in ddd[op_name][2])

                    relevant_objects = (set(o for a in add_effects for o in a.objects) | set(o for a in delete_effects for o in a.objects))

                    entry = [
                        op_name,
                        preconditions,
                        add_effects,
                        delete_effects,
                        relevant_objects
                    ]
                    story.append(entry)
                return story

            import pdb; pdb.set_trace()

            def process_adds(potential_ops, remaining_story, goal):
                curr_entry = remaining_story[0]
                name = curr_entry[0]
                to_add = []
                for ground_atom in curr_entry[2]: # add effects
                    # can we find this atom in the preconditions of any future segment
                    for entry in remaining_story[1:]:
                        if ground_atom in entry[1]: # preconditions
                            # then we add it!
                            to_add.append(ground_atom) # for debugging
                            potential_ops[name]["add"].add(ground_atom.predicate)
                            if "pre" not in potential_ops[entry[0]].keys():
                                import pdb; pdb.set_trace()
                            potential_ops[entry[0]]["pre"].add(ground_atom.predicate)
                    if ground_atom in goal:
                        # then we add it!
                        to_add.append(ground_atom) # for debugging
                        potential_ops[name]["add"].add(ground_atom.predicate)

            def process_dels(potential_ops, remaining_story):
                curr_entry = remaining_story[0]
                name = curr_entry[0]
                to_del = []
                for ground_atom in curr_entry[3]: # delete effects
                    # is this atom every added in the future?
                    # *** do we need to do this with an updated potential_ops after process_adds runs on all the trajectories?
                    for entry in remaining_story[1:]:
                        if ground_atom in entry[2]: # add effects
                            to_del.append(ground_atom) # for debugging
                            potential_ops[name]["del"].add(ground_atom.predicate)
                            potential_ops[entry[0]]["add"].add(ground_atom.predicate)

            all_potential_ops = []
            # TODO: change it so that you fill up a unique potential_ops at each iteration, and then take the union for the final one
            # potential_ops = {}
            # for op_name in ddd.keys():
            #     potential_ops[op_name] = {
            #         "pre": set(),
            #         "add": set(),
            #         "del": set()
            #     }

            for j, traj in enumerate(segmented_trajs):
                potential_ops = {}
                for op_name in ddd.keys():
                    potential_ops[op_name] = {
                        "pre": set(),
                        "add": set(),
                        "del": set()
                    }

                story = get_story(traj)
                reversed_story = list(reversed(story))
                for i, entry in enumerate(reversed_story):
                    remaining_story = story[len(story)-i-1:]
                    name, preconds, add_effs, del_effs, relevant_objs = entry
                    # TODO: do some more thinking about what is a relevant object
                    if i == 0:
                        # For the last segment (in the non-reversed story), we can only evaluate add_effects.
                        # The initial predicates are the goal predicates.
                        for p in add_effs:
                            if p in train_tasks[j].goal:
                                potential_ops[name]["add"].add(p.predicate)
                    else:
                        process_adds(potential_ops, remaining_story, train_tasks[j].goal)
                        process_dels(potential_ops, remaining_story)

                all_potential_ops.append(potential_ops)

            import pdb; pdb.set_trace()

            final_potential_ops = {
                op_name: {
                    "pre": set(),
                    "add": set(),
                    "del": set()
                } for op_name in all_potential_ops[0].keys()
            }

            # for op_name in final_potential_ops.keys():
            #     # get all preconditions that aren't empty
            #
            #     # if op_name == "Op0-Pick":
            #     #     temp = []
            #     #     for h, e in enumerate(all_potential_ops):
            #     #         s = e[op_name]["pre"]
            #     #         if len(s) > 0:
            #     #             temp.append(s)
            #     #         name_ps = [p.name for p in s]
            #     #         if "OnTable" not in name_ps:
            #     #             import pdb; pdb.set_trace()
            #
            #     # temp = [e[op_name]["pre"] for e in all_potential_ops if len(e[op_name]["pre"]) > 0]
            #
            #     final_potential_ops[op_name]["pre"] = set.intersection(*[e[op_name]["pre"] for e in all_potential_ops if len(e[op_name]["pre"]) > 0])
            #     final_potential_ops[op_name]["add"] = set.intersection(*[e[op_name]["add"] for e in all_potential_ops if len(e[op_name]["add"]) > 0])
            #     final_potential_ops[op_name]["del"] = set.intersection(*[e[op_name]["del"] for e in all_potential_ops if len(e[op_name]["del"]) > 0])

            import pdb; pdb.set_trace()

            for k, po in enumerate(all_potential_ops):
                for op_name in po.keys():
                    final_potential_ops[op_name]["pre"] = final_potential_ops[op_name]["pre"].union(po[op_name]["pre"])
                    final_potential_ops[op_name]["add"] = final_potential_ops[op_name]["add"].union(po[op_name]["add"])
                    final_potential_ops[op_name]["del"] = final_potential_ops[op_name]["del"].union(po[op_name]["del"])
                    # if len(po[op_name]["pre"]) > 0:
                    #     final_potential_ops[op_name]["pre"] = final_potential_ops[op_name]["pre"].intersection(po[op_name]["pre"])
                    # if len(po[op_name]["add"]) > 0:
                    #     final_potential_ops[op_name]["add"] = final_potential_ops[op_name]["add"].intersection(po[op_name]["add"])
                    # if len(po[op_name]["del"]) > 0:
                    #     final_potential_ops[op_name]["del"] = final_potential_ops[op_name]["del"].intersection(po[op_name]["del"])


            # print the operators nicely to see what we missed
            for k, v in final_potential_ops.items():
                print(k)
                print("====")
                print("preconditions:")
                for p in sorted(list(v["pre"])):
                    print(p)
                print("====")
                print("add effects:")
                for p in sorted(list(v["add"])):
                    print(p)
                print("====")
                print("delete effects:")
                for p in sorted(list(v["del"])):
                    print(p)


            import pdb; pdb.set_trace()

            fff = {}
            for op in ddd.keys():
                fff[op] = []
                fff[op].append(
                    set(p for p in ddd[op][0] if p in final_potential_ops[op]["pre"])
                )

                if op == "Op0-Pick":
                    pred_to_manually_add = [p for p in ddd[op][0] if p.name == "OnTable"][0]
                    print("MANUALLY ADDING: ", pred_to_manually_add)
                    fff[op][-1].add(pred_to_manually_add)

                fff[op].append(
                    set(p for p in ddd[op][1] if p in final_potential_ops[op]["add"])
                )

                fff[op].append(
                    set(p for p in ddd[op][2] if p in final_potential_ops[op]["del"])
                )
                if op == "Op2-Stack":
                    pred_to_manually_add = [p for p in ddd[op][2] if p.name == "Forall[0:block].[NOT-On(0,1)]"][0]
                    print("MANUALLY ADDING: ", pred_to_manually_add)
                    fff[op][-1].add(pred_to_manually_add)

                fff[op].append(ddd[op][3])

            import pdb; pdb.set_trace()
            self._clusters = fff
            import pdb; pdb.set_trace()
            return predicates_to_keep

            ###################


            def print_demo(demo, filename):
                # Note, demo is a list of strings (operator names).
                # First determine the longest predicate, to help decide the
                # column width.
                max_length = max(len(p.name) for p in predicates_to_keep) + 5

                with open(filename, 'w') as file:

                    # Print the preconditions of each operator.
                    preconds = []
                    for n in demo:
                        ps = sorted([p.name for p in ddd[n][0]])
                        ps = [n, '----', 'preconditions', '===='] + ps
                        preconds.append(ps)
                    max_lst_len = max(len(lst) for lst in preconds)
                    for i in range(max_lst_len):
                        s = " ".join(f"{lst[i]:<{max_length}}" if i < len(lst) else " " * max_length for lst in preconds)
                        file.write(s + '\n')
                    file.write('\n')
                    add_effs = []

                    for n in demo:
                        ads = sorted([p.name for p in ddd[n][1]])
                        ads = ['add effects', '===='] + ads
                        add_effs.append(ads)
                    max_lst_len = max(len(lst) for lst in add_effs)
                    for i in range(max_lst_len):
                        s = " ".join(f"{lst[i]:<{max_length}}" if i < len(lst) else " " * max_length for lst in add_effs)
                        file.write(s + '\n')
                    file.write('\n')
                    del_effs = []

                    for n in demo:
                        dels = sorted([p.name for p in ddd[n][2]])
                        dels = ['delete effects', '===='] + dels
                        del_effs.append(dels)
                    max_lst_len = max(len(lst) for lst in del_effs)
                    for i in range(max_lst_len):
                        s = " ".join(f"{lst[i]:<{max_length}}" if i < len(lst) else " " * max_length for lst in del_effs)
                        file.write(s + '\n')

            print_demo(temp[8][0:3], "demo_8_part1.txt")
            print_demo(temp[8][3:], "demo_8_part2.txt")

            # self._clusters = ddd
            # return predicates_to_keep

            import pdb; pdb.set_trace()

            # fff = {n: [set(), set(), set()] for n in ddd.keys()}

            mmm = {}
            for n in ddd.keys():
                mmm[n] = {
                    "pre": [],
                    "add": [],
                    "del": []
                }
            # import pdb; pdb.set_trace()

            mmm["Op0-Pick"]["pre"] = [
                "((0:block).pose_z<=[idx 0]0.461)",
                "Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-((0:robot).fingers<=[idx 0]0.5)",
                "OnTable",
                "Forall[0:block].[NOT-On(0,1)]"
            ]
            mmm["Op0-Pick"]["add"] = [
                "((0:robot).fingers<=[idx 0]0.5)",
                "NOT-((0:block).pose_z<=[idx 0]0.461)",
                "NOT-Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-OnTable"
            ]
            mmm["Op0-Pick"]["del"] = [
                "((0:block).pose_z<=[idx 0]0.461)",
                "Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-((0:robot).fingers<=[idx 0]0.5)",
                "OnTable"
            ]

            mmm["Op1-PutOnTable"]["pre"] = [
                "((0:robot).fingers<=[idx 0]0.5)",
                "Forall[0:block].[NOT-On(0,1)]",
                "Forall[1:block].[NOT-On(0,1)]",
                "NOT-((0:block).pose_z<=[idx 0]0.461)",
                "NOT-Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]"
            ]
            mmm["Op1-PutOnTable"]["add"] = [
                "((0:block).pose_z<=[idx 0]0.461)",
                "Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-((0:robot).fingers<=[idx 0]0.5)",
                "OnTable"
            ]
            mmm["Op1-PutOnTable"]["del"] = [
                "((0:robot).fingers<=[idx 0]0.5)",
                "NOT-((0:block).pose_z<=[idx 0]0.461)",
                "NOT-Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-OnTable"
            ]

            mmm["Op2-Stack"]["pre"] = [
                "((0:robot).fingers<=[idx 0]0.5)",
                "NOT-((0:block).pose_z<=[idx 0]0.461)",
                "NOT-Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-OnTable",
                "Forall[0:block].[NOT-On(0,1)]"
            ]
            mmm["Op2-Stack"]["add"] = [
                "((0:block).pose_z<=[idx 0]0.461)",
                "Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-((0:robot).fingers<=[idx 0]0.5)",
                "On"
            ]
            mmm["Op2-Stack"]["del"] = [
                "((0:robot).fingers<=[idx 0]0.5)",
                "NOT-((0:block).pose_z<=[idx 0]0.461)",
                "NOT-Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "Forall[0:block].[NOT-On(0,1)]"
            ]

            mmm["Op3-Pick"]["pre"] = [
                "((0:block).pose_z<=[idx 0]0.461)",
                "Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-((0:robot).fingers<=[idx 0]0.5)",
                "OnTable",
                "Forall[0:block].[NOT-On(0,1)]",
                "On"
            ]
            mmm["Op3-Pick"]["add"] = [
                "((0:robot).fingers<=[idx 0]0.5)",
                "Forall[0:block].[NOT-On(0,1)]",
                "Forall[1:block].[NOT-On(0,1)]",
                "NOT-((0:block).pose_z<=[idx 0]0.461)",
                "NOT-Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                # NOT-On?
            ]
            mmm["Op3-Pick"]["del"] = [
                "((0:block).pose_z<=[idx 0]0.461)",
                "Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
                "NOT-((0:robot).fingers<=[idx 0]0.5)",
                "On"
            ]

            fff = {}
            for op in ddd.keys():
                fff[op] = []
                fff[op].append(
                    set(p for p in ddd[op][0] if p.name in mmm[op]["pre"])
                )
                fff[op].append(
                    set(p for p in ddd[op][1] if p.name in mmm[op]["add"])
                )
                fff[op].append(
                    set(p for p in ddd[op][2] if p.name in mmm[op]["del"])
                )
                fff[op].append(ddd[op][3])

            import pdb; pdb.set_trace()
            self._clusters = fff
            import pdb; pdb.set_trace()
            return predicates_to_keep


            # # hard-coded test
            # op1_pre = [
            #     "Forall[0:block].[((0:block).grasp<=[idx 0]-0.486)(0)]",
            #     # "((0:block).grasp<=[idx 0]-0.486)"
            # ]
            # op1_add = [
            #     "NOT-((0:block).grasp<=[idx 0]-0.486)",
            #     "NOT-Forall[0:block].[((0:block).grasp<=[idx 0]-0.486)(0)]"
            # ]
            # op1_del = [
            #     "Forall[0:block].[((0:block).grasp<=[idx 0]-0.486)(0)]",
            #     # "((0:block).grasp<=[idx 0]-0.486)"
            # ]
            #
            # op0_pre = [
            #     "NOT-((0:block).grasp<=[idx 0]-0.486)",
            #     "NOT-Forall[0:block].[((0:block).grasp<=[idx 0]-0.486)(0)]",
            # ]
            # op0_add = [
            #     "Covers",
            #     "Forall[0:block].[((0:block).grasp<=[idx 0]-0.486)(0)]",
            #     # "((0:block).grasp<=[idx 0]-0.486)"
            # ]
            # op0_del = [
            #     "NOT-((0:block).grasp<=[idx 0]-0.486)",
            #     "NOT-Forall[0:block].[((0:block).grasp<=[idx 0]-0.486)(0)]"
            # ]
            #
            # # filter
            # fff = {}
            # fff['Op1-PickPlace'] = []
            # fff['Op0-PickPlace'] = []
            # fff['Op1-PickPlace'].append(
            #     set(p for p in ddd['Op1-PickPlace'][0] if p.name in op0_pre)
            #     )
            # fff['Op1-PickPlace'].append(
            #     set(p for p in ddd['Op1-PickPlace'][1] if p.name in op0_add)
            #     )
            # fff['Op1-PickPlace'].append(
            #     set(p for p in ddd['Op1-PickPlace'][2] if p.name in op0_del)
            #     )
            # fff['Op1-PickPlace'].append(ddd['Op1-PickPlace'][3])
            #
            # fff['Op0-PickPlace'].append(
            #     set(p for p in ddd['Op0-PickPlace'][0] if p.name in op1_pre)
            #     )
            # fff['Op0-PickPlace'].append(
            #     set(p for p in ddd['Op0-PickPlace'][1] if p.name in op1_add)
            #     )
            # fff['Op0-PickPlace'].append(
            #     set(p for p in ddd['Op0-PickPlace'][2] if p.name in op1_del)
            #     )
            # fff['Op0-PickPlace'].append(ddd['Op0-PickPlace'][3])
            # self._clusters = fff
            # # import pdb; pdb.set_trace()
            # return predicates_to_keep

            # ####
            # # 1.
            # potential_ops = {o: dict() for o in ddd.keys()}
            #
            # for i, segmented_traj in enumerate(segmented_trajs):
            #     print(f"Trajectory {i}: {temp[0]}")
            #
            #     # first
            #     goal = self._train_tasks[i].goal
            #     last_seg = segmented_traj[-1]
            #     name = seg_to_op(last_seg, final_clusters)
            #     grounded_relevant_add_effects = set(a for a in last_seg.add_effects if a.predicate in ddd[name][1])
            #     sure_add_effects = grounded_relevant_add_effects.intersection(goal)
            #     unfulfilled_goal = goal - sure_add_effects
            #     potential_ops[name]["add_effects"] = [a.predicate for a in sure_add_effects]
            #
            #     # second
            #     prev_seg = segmented_traj[-1]
            #     curr_seg = segmented_traj[-2]
            #     prev_name = seg_to_op(prev_seg, final_clusters)
            #     name = seg_to_op(curr_seg, final_clusters)
            #     grounded_relevant_add_effects = set(a for a in curr_seg.add_effects if a.predicate in ddd[name][1])
            #     sure_add_effects = grounded_relevant_add_effects.intersection(unfulfilled_goal)
            #     new_unfulfilled_goal = unfulfilled_goal - sure_add_effects
            #
            #     grounded_relevant_preconditions = set(p for p in prev_seg.init_atoms if p.predicate in ddd[prev_name][0])
            #     other_add_effects = grounded_relevant_add_effects.intersection(grounded_relevant_preconditions)
            #     unfulfilled_preconditions = grounded_relevant_preconditions - other_add_effects
            #     # TODO: need to keep track of unfulfilled preconditions for previous operators!
            #     # if no operator adds that atom, then maybe it's not part of the precondition!
            #     all_add_effects = sure_add_effects.union(other_add_effects)
            #     potential_ops[name]["add_effects"] = [a.predicate for a in sure_add_effects]
            #
            #     import pdb; pdb.set_trace()


            ####
            # task_to_ops = {i: dict() for i in range(len(self._train_tasks))}
            ddd2 = ddd.copy()
            def nice_print(s):
                for a in sorted(list(s)):
                    print(a)

            for i, segmented_traj in enumerate(segmented_trajs):
                # Fails if traj is: ['Op0-PickPlace', 'Op1-PickPlace'] => don't learn delete effects for Op1
                # or if it is ['Op1-PickPlace']
                # so still need to figure out some way to combine what we learn across multiple demonstrations

                traj = []
                for seg in segmented_traj:
                    traj.append(seg_to_op(seg, final_clusters))
                print("TRAJ: ", traj)

                seg_traj = list(reversed(segmented_traj))
                potential_ops = {o: {"preconditions": set(), "add_effects": set(), "delete_effects": set()} for o in ddd2.keys()}

                # [goal, last segment's preconditions, second to last segment's preconditions, ...]
                remaining_atoms = [] # goal and preconditions
                remaining_atoms.append(self._train_tasks[i].goal)
                for seg in seg_traj:
                    seg_name = seg_to_op(seg, final_clusters)
                    grounded_relevant_preconditions = set(p for p in seg.init_atoms if p.predicate in ddd2[seg_name][0])
                    grounded_relevant_add_effects = set(a for a in seg.add_effects if a.predicate in ddd2[seg_name][1])


                    all_objs = [set(a.objects) for a in grounded_relevant_add_effects]
                    objs = set.union(*all_objs)
                    filtered_preconditions = set()
                    for p in grounded_relevant_preconditions:
                        if len(p.objects) > 0 and len(set(p.objects)-objs) > 0:
                            continue
                        else:
                            filtered_preconditions.add(p)

                    remaining_atoms.append(filtered_preconditions)

                length = len(segmented_traj)
                for j in range(length):
                    # traverse backwards
                    curr_seg = seg_traj[j]
                    curr_name = seg_to_op(curr_seg, final_clusters)
                    grounded_relevant_add_effects = set(a for a in curr_seg.add_effects if a.predicate in ddd[curr_name][1])
                    grounded_relevant_del_effects = set(a for a in curr_seg.delete_effects if a.predicate in ddd[curr_name][2])
                    grounded_relevant_preconds = set(a for a in curr_seg.init_atoms if a.predicate in ddd[curr_name][0])

                    # delete effects
                    # include it if it gets added in the future
                    # only look one step ahead for now
                    if j > 0:
                        next_seg = seg_traj[j-1]
                        next_name = seg_to_op(next_seg, final_clusters)
                        next_add_effects = set(a for a in next_seg.add_effects if a.predicate in ddd[next_name][1])
                        to_del_at_step = grounded_relevant_del_effects.intersection(next_add_effects)
                        ungrounded_to_del_at_step = set(a.predicate for a in to_del_at_step)
                        potential_ops[curr_name]["delete_effects"] |= ungrounded_to_del_at_step
                        potential_ops[next_name]["add_effects"] |= ungrounded_to_del_at_step

                    # add effects
                    # include it if it fulfills a precondition in the future
                    # only look one step ahead for now
                    to_add_at_step = grounded_relevant_add_effects.intersection(remaining_atoms[j])
                    ungrounded_to_add_at_step = set(a.predicate for a in to_add_at_step)
                    potential_ops[curr_name]["add_effects"] |= ungrounded_to_add_at_step
                    if j > 0:
                        next_seg = seg_traj[j-1]
                        next_name = seg_to_op(next_seg, final_clusters)
                        potential_ops[next_name]["preconditions"] |= ungrounded_to_add_at_step

                break
                # import pdb; pdb.set_trace()

            for k, v in potential_ops.items():
                add = v["add_effects"]
                delete = v["delete_effects"]
                prec = v["preconditions"]
                ddd2[k][0] = ddd2[k][0].intersection(prec)
                ddd2[k][1] = ddd2[k][1].intersection(add)
                ddd2[k][2] = ddd2[k][2].intersection(delete)




            #         for k in range(length):
            #             if k <= j: # goal / preconditions that come AFTER this segment
            #
            #                 if len(remaining_atoms[k]) == 0:
            #                     continue
            #
            #                 # add effects
            #                 to_add_at_step = grounded_relevant_add_effects.intersection(remaining_atoms[k])
            #                 ungrounded_to_add_at_step = set(a.predicate for a in to_add_at_step)
            #                 potential_ops[curr_name][1] |= ungrounded_to_add_at_step
            #                 if k > 0:
            #                     seg_k = seg_traj
            #
            #                 # preconditions
            #                 #
            #                 potential_ops[curr_name][0] |= remaining_atoms[k+1]
            #
            #                 remaining_atoms[k] =
            #
            #                 # to_del_at_step = grounded_relevant_del_effects.intersection(remaining_atoms[k])
            #                 # to_del_at_step = set(d for d in grounded_relevant_del_effects if d not in remaining_atoms[k])
            #
            #                 # do we want to check that the NOT version of it IS EXPLICITLY in remaining_atoms? But, won't that always be the case?
            #                 # ungrounded_to_del_at_step = set(a.predicate for a in to_del_at_step)
            #                 # to_del |= ungrounded_to_del_at_step
            #
            #                 remaining_atoms[k] = remaining_atoms[k] - to_add_at_step
            #                 if k > 0:
            #                     # if k is 1, you want the last segment
            #                     # if k is 2, you want the second to last segment
            #                     # if k is length-1, you want the first segment
            #                     prev_seg = seg_traj[k-1]
            #                     prev_name = seg_to_op(prev_seg, final_clusters)
            #                     potential_ops[prev_name]["preconditions"] |= ungrounded_to_add_at_step
            #                     potential_ops[prev_name]["preconditions"] |= ungrounded_to_del_at_step
            #
            #         potential_ops[curr_name]["add_effects"] |= to_add
            #         potential_ops[curr_name]["delete_effects"] |= to_del
            #
            #         print("J: ", j)
            #         import pdb; pdb.set_trace()
            #
            #         if j == length - 1:
            #             # Now what's left is to get the preconditions of the first operator in the trajectory.
            #             first_seg = seg_traj[-1]
            #             first_name = seg_to_op(first_seg, final_clusters)
            #             if len(potential_ops[first_name]["preconditions"]) == 0:
            #                 grounded_relevant_preconditions = set(p for p in first_seg.init_atoms if p.predicate in ddd[first_name][0])
            #                 ungrounded = set(p.predicate for p in grounded_relevant_preconditions)
            #                 potential_ops[first_name]["preconditions"] = ungrounded
            #
            #     # check that every operator has add effects, delete effects, and preconditions
            #     import pdb; pdb.set_trace()
            #     for k, v in potential_ops.items():
            #         add = v["add_effects"]
            #         delete = v["delete_effects"]
            #         prec = v["preconditions"]
            #
            #         if len(add) == 0:
            #             import pdb; pdb.set_trace()
            #         if len(delete) == 0:
            #             import pdb; pdb.set_trace()
            #         if len(prec) == 0:
            #             import pdb; pdb.set_trace()
            #
            #         import pdb; pdb.set_trace()
            #         ddd2[k][0] = ddd2[k][0].intersection(prec)
            #         ddd2[k][1] = ddd2[k][1].intersection(add)
            #         ddd2[k][2] = ddd2[k][2].intersection(delete)
            #         import pdb; pdb.set_trace()
            #
            # import pdb; pdb.set_trace()


            #####################

            self._clusters = ddd2

            # import pdb; pdb.set_trace()

            logging.info(
                f"\nSelected {len(predicates_to_keep)} predicates out of "
                f"{len(candidates)} candidates:")
            for pred in sorted(predicates_to_keep):
                logging.info(f"{pred}")
            return predicates_to_keep

        if CFG.grammar_search_pred_clusterer == "oracle":
            assert CFG.offline_data_method == "demo+gt_operators"
            assert dataset.annotations is not None and len(
                dataset.annotations) == len(dataset.trajectories)
            assert CFG.segmenter == "option_changes"
            segmented_trajs = [
                segment_trajectory(traj) for traj in atom_dataset
            ]
            assert len(segmented_trajs) == len(dataset.annotations)
            # First, get the set of all ground truth operator names.
            all_gt_op_names = set(ground_nsrt.parent.name
                                  for anno_list in dataset.annotations
                                  for ground_nsrt in anno_list)
            # Next, make a dictionary mapping operator name to segments
            # where that operator was used.
            gt_op_to_segments: Dict[str, List[Segment]] = {
                op_name: []
                for op_name in all_gt_op_names
            }

            for op_list, seg_list in zip(dataset.annotations, segmented_trajs):
                assert len(seg_list) == len(op_list)
                for ground_nsrt, segment in zip(op_list, seg_list):
                    gt_op_to_segments[ground_nsrt.parent.name].append(segment)
            consistent_add_effs_preds: Set[Predicate] = set()
            # First, select predicates that change as add effects consistently
            # within clusters.
            for seg_list in gt_op_to_segments.values():
                unique_add_effect_preds: Set[Predicate] = set()
                for seg in seg_list:
                    if len(unique_add_effect_preds) == 0:
                        unique_add_effect_preds = set(
                            atom.predicate for atom in seg.add_effects)
                    else:
                        unique_add_effect_preds &= set(
                            atom.predicate for atom in seg.add_effects)
                print("predicates from this cluster: ")
                for p in unique_add_effect_preds:
                    print(p)
                consistent_add_effs_preds |= unique_add_effect_preds

            # Next, select predicates that are consistent (either, it is
            # an add effect, or a delete effect, or doesn't change)
            # within all demos.
            print("LOOKING FOR ANY INCONSISTENT PREDICATES.")
            predicates_to_keep: Set[Predicate] = set()
            for pred in consistent_add_effs_preds:
                keep_pred = True
                for op_name, seg_list in gt_op_to_segments.items():
                    print(f"CHECKING CONSISTENCY FOR OPERATOR {op_name}")
                    segment_0 = seg_list[0]
                    pred_in_add_effs_0 = pred in [
                        atom.predicate for atom in segment_0.add_effects
                    ]
                    pred_in_del_effs_0 = pred in [
                        atom.predicate for atom in segment_0.delete_effects
                    ]
                    for seg in seg_list[1:]:
                        pred_in_curr_add_effs = pred in [
                            atom.predicate for atom in seg.add_effects
                        ]
                        pred_in_curr_del_effs = pred in [
                            atom.predicate for atom in seg.delete_effects
                        ]
                        if not ((pred_in_add_effs_0 == pred_in_curr_add_effs)
                                and
                                (pred_in_del_effs_0 == pred_in_curr_del_effs)):
                            keep_pred = False
                            print("INCONSISTENT: ", pred.name, "option: ", seg.get_option())
                            # if pred.name == "NOT-((0:obj).grasp<=[idx 0]0.5)" or pred.name == "((0:obj).grasp<=[idx 1]0.25)":
                            #     import pdb; pdb.set_trace()
                            break
                    if not keep_pred:
                        break

                else:
                    predicates_to_keep.add(pred)
            print("DONE LOOKING FOR ANY INCONSISTENT PREDICATES.")

            # remove for a test
            temp = predicates_to_keep.copy()
            for p in temp:
                if p.name == "((0:obj).grasp<=[idx 0]0.5)" or p.name == "Forall[0:obj].[((0:obj).grasp<=[idx 0]0.5)(0)]":
                    predicates_to_keep.remove(p)
                    print("Removing for the purpose of test: ", p)

            # Remove all the initial predicates.
            predicates_to_keep -= initial_predicates
            logging.info(
                f"\nSelected {len(predicates_to_keep)} predicates out of "
                f"{len(candidates)} candidates:")
            for pred in sorted(predicates_to_keep):
                logging.info(f"{pred}")
            # for pred in sorted(predicates_to_keep):
            #     logging.info(f"\t{pred}")

            # print("Final # of predicates: ", len(predicates_to_keep))
            # import pdb; pdb.set_trace()

            # predicates to try removing
            # remove = [
            #     "NOT-OnTable",
            #     "NOT-On",
            #     "Forall[1:block].[NOT-On(0,1)]",
            #     "NOT-Forall[0:block].[NOT-On(0,1)]",
            #     "Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]",
            #     "NOT-Forall[0:block].[((0:block).pose_z<=[idx 0]0.461)(0)]"
            # ]
            # predicates_to_keep = set([pred for pred in predicates_to_keep if pred.name not in remove])
            # import pdb; pdb.set_trace()

            return predicates_to_keep

        raise NotImplementedError(
            "Unrecognized clusterer for predicate " +
            f"invention {CFG.grammar_search_pred_clusterer}.")
