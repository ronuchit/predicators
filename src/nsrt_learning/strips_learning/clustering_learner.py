"""Algorithms for STRIPS learning that rely on clustering to obtain effects."""

import abc
import functools
import heapq as hq
import itertools
import logging
from typing import FrozenSet, Iterator, List, Set, Tuple, cast

from predicators.src import utils
from predicators.src.nsrt_learning.strips_learning import BaseSTRIPSLearner
from predicators.src.settings import CFG
from predicators.src.structs import Datastore, DummyOption, LiftedAtom, \
    PartialNSRTAndDatastore, Predicate, STRIPSOperator, VarToObjSub


class ClusteringSTRIPSLearner(BaseSTRIPSLearner):
    """Base class for a clustering-based STRIPS learner."""

    def _learn(self) -> List[PartialNSRTAndDatastore]:
        segments = [seg for segs in self._segmented_trajs for seg in segs]
        # Cluster the segments according to common option and effects.
        pnads: List[PartialNSRTAndDatastore] = []
        for segment in segments:
            if segment.has_option():
                segment_option = segment.get_option()
                segment_param_option = segment_option.parent
                segment_option_objs = tuple(segment_option.objects)
            else:
                segment_param_option = DummyOption.parent
                segment_option_objs = tuple()
            for pnad in pnads:
                # Try to unify this transition with existing effects.
                # Note that both add and delete effects must unify,
                # and also the objects that are arguments to the options.
                (pnad_param_option, pnad_option_vars) = pnad.option_spec
                suc, ent_to_ent_sub = utils.unify_preconds_effects_options(
                    frozenset(),  # no preconditions
                    frozenset(),  # no preconditions
                    frozenset(segment.add_effects),
                    frozenset(pnad.op.add_effects),
                    frozenset(segment.delete_effects),
                    frozenset(pnad.op.delete_effects),
                    segment_param_option,
                    pnad_param_option,
                    segment_option_objs,
                    tuple(pnad_option_vars))
                sub = cast(VarToObjSub,
                           {v: o
                            for o, v in ent_to_ent_sub.items()})
                if suc:
                    # Add to this PNAD.
                    assert set(sub.keys()) == set(pnad.op.parameters)
                    pnad.add_to_datastore((segment, sub))
                    break
            else:
                # Otherwise, create a new PNAD.
                objects = {o for atom in segment.add_effects |
                           segment.delete_effects for o in atom.objects} | \
                          set(segment_option_objs)
                objects_lst = sorted(objects)
                params = utils.create_new_variables(
                    [o.type for o in objects_lst])
                preconds: Set[LiftedAtom] = set()  # will be learned later
                obj_to_var = dict(zip(objects_lst, params))
                var_to_obj = dict(zip(params, objects_lst))
                add_effects = {
                    atom.lift(obj_to_var)
                    for atom in segment.add_effects
                }
                delete_effects = {
                    atom.lift(obj_to_var)
                    for atom in segment.delete_effects
                }
                side_predicates: Set[Predicate] = set(
                )  # will be learned later
                op = STRIPSOperator(f"Op{len(pnads)}", params, preconds,
                                    add_effects, delete_effects,
                                    side_predicates)
                datastore = [(segment, var_to_obj)]
                option_vars = [obj_to_var[o] for o in segment_option_objs]
                option_spec = (segment_param_option, option_vars)
                pnads.append(
                    PartialNSRTAndDatastore(op, datastore, option_spec))

        # Learn the preconditions of the operators in the PNADs. This part
        # is flexible; subclasses choose how to implement it.
        pnads = self._learn_pnad_preconditions(pnads)

        # Handle optional postprocessing to learn side predicates.
        pnads = self._postprocessing_learn_side_predicates(pnads)

        # Log and return the PNADs.
        if self._verbose:
            logging.info("Learned operators (before option learning):")
            for pnad in pnads:
                logging.info(pnad)
        return pnads

    @abc.abstractmethod
    def _learn_pnad_preconditions(
            self, pnads: List[PartialNSRTAndDatastore]
    ) -> List[PartialNSRTAndDatastore]:
        """Subclass-specific algorithm for learning PNAD preconditions.

        Returns a list of new PNADs. Should NOT modify the given PNADs.
        """
        raise NotImplementedError("Override me!")

    def _postprocessing_learn_side_predicates(
            self, pnads: List[PartialNSRTAndDatastore]
    ) -> List[PartialNSRTAndDatastore]:
        """Optionally postprocess to learn side predicates."""
        _ = self  # unused, but may be used in subclasses
        return pnads


class ClusterAndIntersectSTRIPSLearner(ClusteringSTRIPSLearner):
    """A clustering STRIPS learner that learns preconditions via
    intersection."""

    def _learn_pnad_preconditions(
            self, pnads: List[PartialNSRTAndDatastore]
    ) -> List[PartialNSRTAndDatastore]:
        new_pnads = []
        for pnad in pnads:
            preconditions = self._induce_preconditions_via_intersection(pnad)
            # Since we are taking an intersection, we're guaranteed that the
            # datastore can't change, so we can safely use pnad.datastore here.
            new_pnads.append(
                PartialNSRTAndDatastore(
                    pnad.op.copy_with(preconditions=preconditions),
                    pnad.datastore, pnad.option_spec))
        return new_pnads

    @classmethod
    def get_name(cls) -> str:
        return "cluster_and_intersect"


class ClusterAndSearchSTRIPSLearner(ClusteringSTRIPSLearner):
    """A clustering STRIPS learner that learns preconditions via search,
    following the LOFT algorithm: https://arxiv.org/abs/2103.00589."""

    def _learn_pnad_preconditions(
            self, pnads: List[PartialNSRTAndDatastore]
    ) -> List[PartialNSRTAndDatastore]:
        new_pnads = []
        for i, pnad in enumerate(pnads):
            positive_data = pnad.datastore
            # Construct negative data by merging the datastores of all
            # other PNADs that have the same option.
            negative_data = []
            for j, other_pnad in enumerate(pnads):
                if i == j:
                    continue
                if pnad.option_spec[0] != other_pnad.option_spec[0]:
                    continue
                negative_data.extend(other_pnad.datastore)
            all_preconditions = self._run_search(pnad, positive_data,
                                                 negative_data)
            # The datastores of the new PNADs could need to be changed, so
            # we initialize them as empty, then recompute them at the end.
            for j, preconditions in enumerate(all_preconditions):
                new_pnads.append(
                    PartialNSRTAndDatastore(
                        pnad.op.copy_with(name=f"{pnad.op.name}-{j}",
                                          preconditions=preconditions), [],
                        pnad.option_spec))
        self._recompute_datastores_from_segments(new_pnads)
        return new_pnads

    def _run_search(self, pnad: PartialNSRTAndDatastore,
                    positive_data: Datastore,
                    negative_data: Datastore) -> Set[FrozenSet[LiftedAtom]]:
        """Run outer-level search to find a set of precondition sets.

        Each precondition set will produce one operator.
        """
        all_preconditions = set()
        # We'll remove positives as they get covered.
        remaining_positives = list(positive_data)
        while remaining_positives:
            new_preconditions = self._run_single_search(
                remaining_positives, negative_data)
            # Update the remaining positives.
            new_remaining_positives = []
            for seg, var_to_obj in remaining_positives:
                ground_pre = {a.ground(var_to_obj) for a in new_preconditions}
                if not ground_pre.issubset(seg.init_atoms):
                    # If the preconditions ground with this substitution don't
                    # hold in this segment's init_atoms, this segment has yet
                    # to be covered, so we keep it in the positives.
                    new_remaining_positives.append((seg, var_to_obj))
                else:
                    # Otherwise, we can move this segment to negative_data,
                    # for any future preconditions that get learned.
                    negative_data.append((seg, var_to_obj))
            remaining_positives = new_remaining_positives
            # Update the set to be returned.
            assert new_preconditions not in all_preconditions
            all_preconditions.add(new_preconditions)
        return all_preconditions

    def _run_single_search(self, positive_data: Datastore,
                           negative_data: Datastore) -> FrozenSet[LiftedAtom]:
        """Run inner-level search to find a single precondition set."""
        tiebreak = itertools.count()
        queue: List[Tuple[float, float, FrozenSet[LiftedAtom]]] = []
        best_preconditions = self._get_initial_preconditions(positive_data)
        best_score = self._score_preconditions(best_preconditions,
                                               positive_data, negative_data)
        hq.heappush(queue, (best_score, next(tiebreak), best_preconditions))
        visited = {best_preconditions}

        while queue:
            _, _, preconditions = hq.heappop(queue)
            for child in self._get_precondition_successors(preconditions):
                if child in visited:
                    continue
                child_score = self._score_preconditions(
                    child, positive_data, negative_data)
                if child_score < best_score:
                    best_score = child_score
                    best_preconditions = child
                hq.heappush(queue, (child_score, next(tiebreak), child))
                visited.add(child)
        print(f"Added preconditions {best_preconditions} with score {best_score}")
        return best_preconditions

    @staticmethod
    def _get_initial_preconditions(
            positive_data: Datastore) -> FrozenSet[LiftedAtom]:
        """The initial preconditions are a UNION over all lifted initial states
        in the data.

        We filter out atoms containing any object that doesn't have a
        binding to the PNAD parameters.
        """
        initial_preconditions = set()
        for seg, var_to_obj in positive_data:
            obj_to_var = {v: k for k, v in var_to_obj.items()}
            for atom in seg.init_atoms:
                if not all(obj in obj_to_var for obj in atom.objects):
                    continue
                initial_preconditions.add(atom.lift(obj_to_var))
        return frozenset(initial_preconditions)

    @staticmethod
    def _score_preconditions(preconditions: FrozenSet[LiftedAtom],
                             positive_data: Datastore,
                             negative_data: Datastore) -> float:
        # Count up the number of true positives and false positives.
        num_true_positives = 0
        num_false_positives = 0
        for seg, var_to_obj in positive_data:
            ground_pre = {a.ground(var_to_obj) for a in preconditions}
            if ground_pre.issubset(seg.init_atoms):
                num_true_positives += 1
        for seg, var_to_obj in negative_data:
            ground_pre = set()
            for atom in preconditions:
                if not all(var in var_to_obj for var in atom.variables):
                    continue
                ground_pre.add(atom.ground(var_to_obj))
            if ground_pre.issubset(seg.init_atoms):
                num_false_positives += 1
        tp_w = CFG.clustering_learner_true_pos_weight
        fp_w = CFG.clustering_learner_false_pos_weight
        score = fp_w * num_false_positives + tp_w * (-num_true_positives)
        # Penalize the number of variables in the preconditions.
        all_vars = {v for atom in preconditions for v in atom.variables}
        score += CFG.cluster_and_search_var_count_weight * len(all_vars)
        # Penalize the number of preconditions.
        score += CFG.cluster_and_search_precon_size_weight * len(preconditions)
        print(f"for {preconditions} got score {score} found {num_true_positives} tp and {num_false_positives} fp")
        return score

    @staticmethod
    def _get_precondition_successors(
            preconditions: FrozenSet[LiftedAtom]
    ) -> List[FrozenSet[LiftedAtom]]:
        """The successors remove each atom in the preconditions."""
        successors = []
        preconditions_sorted = sorted(preconditions)
        for i in range(len(preconditions_sorted)):
            successor = preconditions_sorted[:i] + preconditions_sorted[i + 1:]
            successors.append(frozenset(successor))
        return successors

    @classmethod
    def get_name(cls) -> str:
        return "cluster_and_search"


class ClusterAndIntersectSidelineSTRIPSLearner(ClusterAndIntersectSTRIPSLearner
                                               ):
    """Base class for a clustering-based STRIPS learner that does sidelining
    via hill climbing, after operator learning."""

    def _postprocessing_learn_side_predicates(
            self, pnads: List[PartialNSRTAndDatastore]
    ) -> List[PartialNSRTAndDatastore]:
        # Run hill climbing search, starting from original PNADs.
        path, _, _ = utils.run_hill_climbing(
            tuple(pnads), self._check_goal, self._get_sidelining_successors,
            functools.partial(self._evaluate, pnads))
        # The last state in the search holds the final PNADs.
        pnads = list(path[-1])
        # Because the PNADs have been modified, recompute the datastores.
        self._recompute_datastores_from_segments(pnads)
        return pnads

    @abc.abstractmethod
    def _evaluate(self, initial_pnads: List[PartialNSRTAndDatastore],
                  s: Tuple[PartialNSRTAndDatastore, ...]) -> float:
        """Abstract evaluation/score function for search.

        Lower is better.
        """
        raise NotImplementedError("Override me!")

    @staticmethod
    def _check_goal(s: Tuple[PartialNSRTAndDatastore, ...]) -> bool:
        del s  # unused
        # There are no goal states for this search; run until exhausted.
        return False

    @staticmethod
    def _get_sidelining_successors(
        s: Tuple[PartialNSRTAndDatastore, ...],
    ) -> Iterator[Tuple[None, Tuple[PartialNSRTAndDatastore, ...], float]]:
        # For each PNAD/operator...
        for i in range(len(s)):
            pnad = s[i]
            _, option_vars = pnad.option_spec
            # ...consider changing each of its add effects to a side predicate.
            for effect in pnad.op.add_effects:
                if len(pnad.op.add_effects) > 1:
                    # We don't want sidelining to result in a no-op.
                    new_pnad = PartialNSRTAndDatastore(
                        pnad.op.effect_to_side_predicate(
                            effect, option_vars, "add"), pnad.datastore,
                        pnad.option_spec)
                    sprime = list(s)
                    sprime[i] = new_pnad
                    yield (None, tuple(sprime), 1.0)

            # ...consider removing it.
            sprime = list(s)
            del sprime[i]
            yield (None, tuple(sprime), 1.0)


class ClusterAndIntersectSidelinePredictionErrorSTRIPSLearner(
        ClusterAndIntersectSidelineSTRIPSLearner):
    """A STRIPS learner that uses hill climbing with a prediction error score
    function for side predicate learning."""

    @classmethod
    def get_name(cls) -> str:
        return "cluster_and_intersect_sideline_prederror"

    def _evaluate(self, initial_pnads: List[PartialNSRTAndDatastore],
                  s: Tuple[PartialNSRTAndDatastore, ...]) -> float:
        segments = [seg for traj in self._segmented_trajs for seg in traj]
        strips_ops = [pnad.op for pnad in s]
        option_specs = [pnad.option_spec for pnad in s]
        num_true_positives, num_false_positives, _, _ = \
            utils.count_positives_for_ops(strips_ops, option_specs, segments)
        # Note: lower is better! We want more true positives and fewer
        # false positives.
        tp_w = CFG.clustering_learner_true_pos_weight
        fp_w = CFG.clustering_learner_false_pos_weight
        return fp_w * num_false_positives + tp_w * (-num_true_positives)


class ClusterAndIntersectSidelineHarmlessnessSTRIPSLearner(
        ClusterAndIntersectSidelineSTRIPSLearner):
    """A STRIPS learner that uses hill climbing with a harmlessness score
    function for side predicate learning."""

    @classmethod
    def get_name(cls) -> str:
        return "cluster_and_intersect_sideline_harmlessness"

    def _evaluate(self, initial_pnads: List[PartialNSRTAndDatastore],
                  s: Tuple[PartialNSRTAndDatastore, ...]) -> float:
        preserves_harmlessness = self._check_harmlessness(list(s))
        if preserves_harmlessness:
            # If harmlessness is preserved, the score is the number of
            # operators that we have, minus the number of side predicates.
            # This means we prefer fewer operators and more side predicates.
            score = 2 * len(s)
            for pnad in s:
                score -= len(pnad.op.side_predicates)
        else:
            # If harmlessness is not preserved, the score is an arbitrary
            # constant bigger than the total number of operators at the
            # start of the search. This is guaranteed to be worse (higher)
            # than any score that occurs if harmlessness is preserved.
            score = 10 * len(initial_pnads)
        return score
