"""Oracle for STRIPS learning."""

from typing import List, Set

from predicators.envs import get_or_create_env
from predicators.ground_truth_models import get_gt_nsrts, get_gt_options
from predicators.nsrt_learning.strips_learning import BaseSTRIPSLearner
from predicators.settings import CFG
from predicators.structs import NSRT, PNAD, Datastore, DummyOption, \
    LiftedAtom, Predicate, Segment


class OracleSTRIPSLearner(BaseSTRIPSLearner):
    """Base class for a STRIPS learner that uses oracle operators but re-learns
    all the components via currently-implemented methods in the base class."""

    def _induce_add_effects_by_intersection(self,
                                            pnad: PNAD) -> Set[LiftedAtom]:
        """Given a PNAD with a nonempty datastore, compute the add effects for
        the PNAD's operator by intersecting all lifted add effects."""
        assert len(pnad.datastore) > 0
        for i, (segment, var_to_obj) in enumerate(pnad.datastore):
            objects = set(var_to_obj.values())
            obj_to_var = {o: v for v, o in var_to_obj.items()}
            atoms = {
                atom
                for atom in segment.add_effects
                if all(o in objects for o in atom.objects)
            }
            lifted_atoms = {atom.lift(obj_to_var) for atom in atoms}
            if i == 0:
                add_effects = lifted_atoms
            else:
                add_effects &= lifted_atoms
        return add_effects

    def _compute_datastores_given_nsrt(self, nsrt: NSRT) -> Datastore:
        datastore = []
        assert self._annotations is not None
        for seg_traj, ground_nsrt_list in zip(self._segmented_trajs,
                                              self._annotations):
            assert len(seg_traj) == len(ground_nsrt_list)
            for segment, ground_nsrt in zip(seg_traj, ground_nsrt_list):
                if ground_nsrt.parent == nsrt:
                    op_vars = nsrt.op.parameters
                    obj_sub = ground_nsrt.objects
                    var_to_obj_sub = dict(zip(op_vars, obj_sub))
                    datastore.append((segment, var_to_obj_sub))
        return datastore

    def _find_add_effect_intersection_preds(
            self, segments: List[Segment]) -> Set[Predicate]:
        unique_add_effect_preds: Set[Predicate] = set()
        for seg in segments:
            if len(unique_add_effect_preds) == 0:
                unique_add_effect_preds = set(atom.predicate
                                              for atom in seg.add_effects)
            else:
                unique_add_effect_preds &= set(atom.predicate
                                               for atom in seg.add_effects)
        return unique_add_effect_preds

    def _learn(self) -> List[PNAD]:
        # For this learner to work, we need annotations to be non-null
        # and the length of the annotations to match the length of
        # the trajectory set.
        assert self._annotations is not None
        assert len(self._annotations) == len(self._trajectories)
        assert CFG.offline_data_method == "demo+gt_operators"

        env = get_or_create_env(CFG.env)
        env_options = get_gt_options(env.get_name())
        gt_nsrts = get_gt_nsrts(env.get_name(), env.predicates, env_options)
        pnads: List[PNAD] = []
        for nsrt in gt_nsrts:
            # If options are unknown, use a dummy option spec.
            if CFG.option_learner == "no_learning":
                option_spec = (nsrt.option, list(nsrt.option_vars))
            else:
                option_spec = (DummyOption.parent, [])

            datastore = self._compute_datastores_given_nsrt(nsrt)
            pnad = PNAD(nsrt.op, datastore, option_spec)
            add_effects = self._induce_add_effects_by_intersection(pnad)
            preconditions = self._induce_preconditions_via_intersection(pnad)
            pnad = PNAD(
                pnad.op.copy_with(preconditions=preconditions,
                                  add_effects=add_effects), datastore,
                option_spec)
            self._compute_pnad_delete_effects(pnad)
            self._compute_pnad_ignore_effects(pnad)
            pnads.append(pnad)

        return pnads

    @classmethod
    def get_name(cls) -> str:
        return "oracle_clustering"
