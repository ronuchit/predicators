"""The core algorithm for learning a collection of NSRT data structures."""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Sequence, Set, Tuple

import dill as pkl
import numpy as np
from gym.spaces import Box

from predicators import utils
from predicators.behavior_utils import behavior_utils
from predicators.envs import behavior, get_or_create_env
from predicators.nsrt_learning.option_learning import \
    KnownOptionsOptionLearner, _OptionLearnerBase, create_option_learner
from predicators.nsrt_learning.sampler_learning import learn_samplers
from predicators.nsrt_learning.segmentation import segment_trajectory
from predicators.nsrt_learning.strips_learning import learn_strips_operators
from predicators.settings import CFG
from predicators.structs import NSRT, PNAD, GroundAtom, GroundAtomTrajectory, \
    LowLevelTrajectory, ParameterizedOption, Predicate, Segment, Task

try:
    from igibson.envs.behavior_env import \
        BehaviorEnv  # pylint: disable=unused-import
except (ImportError, ModuleNotFoundError) as e:  # pragma: no cover
    pass


def learn_nsrts_from_data(
    trajectories: List[LowLevelTrajectory], train_tasks: List[Task],
    predicates: Set[Predicate], known_options: Set[ParameterizedOption],
    action_space: Box, ground_atom_dataset: List[GroundAtomTrajectory],
    sampler_learner: str
) -> Tuple[Set[NSRT], List[List[Segment]], Dict[Segment, NSRT]]:
    """Learn NSRTs from the given dataset of low-level transitions, using the
    given set of predicates.

    There are three return values: (1) The final set of NSRTs. (2) The
    segmented trajectories. These are returned because any options that
    were learned will be contained properly in these segments. (3) A
    mapping from segment to NSRT. This is returned because not all
    segments in return value (2) are necessarily covered by an NSRT, in
    the case that we are enforcing a min_data (see
    base_strips_learner.py).
    """
    logging.info(f"\nLearning NSRTs on {len(trajectories)} trajectories...")

    # Search over data orderings to find least complex PNAD set.
    # If the strips learner is not Backchaining then it will
    # only do one iteration, because all other approaches are
    # data order invariant.
    smallest_pnads = None
    smallest_pnad_complexity = float('inf')
    rng = np.random.default_rng(CFG.seed)
    for order_i in range(CFG.data_orderings_to_search):
        # Step 0: Shuffle dataset to learn from.
        if CFG.data_orderings_to_search > 1:
            random_data_indices = sorted(
                [int(i) for i in range(len(trajectories))],
                key=lambda _: rng.random())
            trajectories = [trajectories[i] for i in random_data_indices]
            ground_atom_dataset = [
                ground_atom_dataset[i] for i in random_data_indices
            ]
            logging.info(f"Learning NSRTs on Ordering {order_i}/"
                         f"{CFG.data_orderings_to_search}")
        # STEP 1: Segment each trajectory in the dataset based on changes in
        #         either predicates or options. If we are doing option learning,
        #         then the data will not contain options, so this segmenting
        #         procedure only uses the predicates.
        segmented_trajs = [
            segment_trajectory(traj) for traj in ground_atom_dataset
        ]
        # If performing goal-conditioned sampler learning, we need to attach the
        # goals to the segments.
        if CFG.sampler_learning_use_goals:
            for segment_traj, ll_traj in zip(segmented_trajs, trajectories):
                # If the trajectory is not a demonstration, it does not have a
                # known goal (e.g., it was generated by random replay data),
                # so we won't attach a goal to the segment.
                if ll_traj.is_demo:
                    goal = train_tasks[ll_traj.train_task_idx].goal
                    for segment in segment_traj:
                        segment.set_goal(goal)

        # STEP 2: Learn STRIPS operators from the data, and use them to
        #         produce PNAD objects. Each PNAD
        #         contains a STRIPSOperator, Datastore, and OptionSpec. The
        #         samplers will be filled in on a later step.
        pnads = learn_strips_operators(
            trajectories,
            train_tasks,
            predicates,
            segmented_trajs,
            verify_harmlessness=True,
            verbose=(CFG.option_learner != "no_learning"))

        # Save least complex learned PNAD set across data orderings.
        pnads_complexity = sum(pnad.op.get_complexity() for pnad in pnads)
        if pnads_complexity < smallest_pnad_complexity:
            smallest_pnad_complexity = pnads_complexity
            smallest_pnads = pnads
        assert smallest_pnads is not None  # smallest pnads should be set here

        if CFG.strips_learner != 'backchaining':
            break

    assert smallest_pnads is not None
    pnads = smallest_pnads

    # We delete ground_atom_dataset because it's prone to causing bugs --
    # we should rarely care about the low-level ground atoms sequence after
    # segmentation.
    del ground_atom_dataset

    # STEP 3: Learn options (option_learning.py) and update PNADs.
    _learn_pnad_options(pnads, known_options, action_space)  # in-place update

    # STEP 4: Learn samplers (sampler_learning.py) and update PNADs.
    _learn_pnad_samplers(pnads, sampler_learner)  # in-place update

    # STEP 5: Make, log, and return the NSRTs.
    nsrts = []
    seg_to_nsrt = {}
    for pnad in pnads:
        nsrt = pnad.make_nsrt()
        nsrts.append(nsrt)
        for (seg, _) in pnad.datastore:
            assert seg not in seg_to_nsrt
            seg_to_nsrt[seg] = nsrt
    logging.info("\nLearned NSRTs:")
    for nsrt in nsrts:
        logging.info(nsrt)
    logging.info("")

    return set(nsrts), segmented_trajs, seg_to_nsrt


def get_ground_atoms_dataset(
        trajectories: Sequence[LowLevelTrajectory], predicates: Set[Predicate],
        online_learning_cycle: Optional[int]) -> List[GroundAtomTrajectory]:
    """Either tries to load a saved ground atom dataset, or creates a new one
    depending on the CFG.load_atoms flag.

    If creating a new one, then save a ground atoms dataset file.
    """
    dataset_fname, _ = utils.create_dataset_filename_str(
        saving_ground_atoms=True, online_learning_cycle=online_learning_cycle)
    # If CFG.load_atoms is set, then try to create a GroundAtomTrajectory
    # by loading sets of GroundAtoms directly from a saved file.
    if CFG.load_atoms:
        os.makedirs(CFG.data_dir, exist_ok=True)
        # Check that the dataset file was previously saved.
        if os.path.exists(dataset_fname):
            # Load the ground atoms dataset.
            with open(dataset_fname, "rb") as f:
                ground_atom_dataset_atoms = pkl.load(f)
            assert len(trajectories) == len(ground_atom_dataset_atoms)
            logging.info("\n\nLOADED GROUND ATOM DATASET")

            if CFG.env == "behavior":  # pragma: no cover
                pred_name_to_pred = {pred.name: pred for pred in predicates}
                new_ground_atom_dataset_atoms = []
                # Since we save ground atoms for behavior with dummy
                # classifiers, we need to restore the correct classifers.
                for ground_atom_seq in ground_atom_dataset_atoms:
                    new_ground_atom_seq = []
                    for ground_atom_set in ground_atom_seq:
                        new_ground_atom_set = {
                            GroundAtom(pred_name_to_pred[atom.predicate.name],
                                       atom.entities)
                            for atom in ground_atom_set
                        }
                        new_ground_atom_seq.append(new_ground_atom_set)
                    new_ground_atom_dataset_atoms.append(new_ground_atom_seq)

            # The saved ground atom dataset consists only of sequences
            # of sets of GroundAtoms, we need to recombine this with
            # the LowLevelTrajectories to create a GroundAtomTrajectory.
            ground_atom_dataset = []
            for i, traj in enumerate(trajectories):
                if CFG.env == "behavior":
                    ground_atom_seq = new_ground_atom_dataset_atoms[
                        i]  # pragma: no cover
                else:
                    ground_atom_seq = ground_atom_dataset_atoms[i]
                ground_atom_dataset.append(
                    (traj, [set(atoms) for atoms in ground_atom_seq]))
        else:
            raise ValueError(f"Cannot load ground atoms: {dataset_fname}")
    else:
        # Apply predicates to data, producing a dataset of abstract states.
        if CFG.env == "behavior":  # pragma: no cover
            env = get_or_create_env("behavior")
            assert isinstance(env, behavior.BehaviorEnv)
            ground_atom_dataset = \
                behavior_utils.create_ground_atom_dataset_behavior(
                    trajectories, predicates, env)
        else:
            ground_atom_dataset = utils.create_ground_atom_dataset(
                trajectories, predicates)
        # Save ground atoms dataset to file. Note that a
        # GroundAtomTrajectory contains a normal LowLevelTrajectory and a
        # list of sets of GroundAtoms, so we only save the list of
        # GroundAtoms (the LowLevelTrajectories are saved separately).
        ground_atom_dataset_to_pkl = []
        for gt_traj in ground_atom_dataset:
            trajectory = []
            for ground_atom_set in gt_traj[1]:
                if CFG.env == "behavior":  # pragma: no cover
                    # In the case of behavior, we cannot directly pickle
                    # the ground atoms dataset because the classifiers are
                    # linked to the simulator, which cannot be pickled.
                    # Thus, we must strip away the classifiers and replace
                    # them with dummies.
                    trajectory.append({
                        GroundAtom(
                            Predicate(atom.predicate.name,
                                      atom.predicate.types,
                                      lambda s, o: False), atom.entities)
                        for atom in ground_atom_set
                    })
                else:
                    trajectory.append(ground_atom_set)
            ground_atom_dataset_to_pkl.append(trajectory)
        with open(dataset_fname, "wb") as f:
            pkl.dump(ground_atom_dataset_to_pkl, f)
        # If we're only interested in creating a training dataset, then
        # terminate the program here and return how many demos were
        # collected.
        if CFG.create_training_dataset:  # pragma: no cover
            if CFG.num_train_tasks != len(trajectories):
                raise AssertionError(
                    "ERROR!: Collected only" +
                    f"{len(trajectories)} trajectories, but needed" +
                    f"{CFG.num_train_tasks}.")
            raise AssertionError("SUCCESS!: Created training dataset" +
                                 f"with {len(trajectories)} trajectories.")
    return ground_atom_dataset


def _learn_pnad_options(pnads: List[PNAD],
                        known_options: Set[ParameterizedOption],
                        action_space: Box) -> None:
    logging.info("\nDoing option learning...")
    # Separate the PNADs into two groups: those with known options, and those
    # without. By assumption, for each PNAD, either all actions should have the
    # same known parameterized option, or all actions should have no option.
    known_option_pnads, unknown_option_pnads = [], []
    for pnad in pnads:
        assert pnad.datastore
        example_segment, _ = pnad.datastore[0]
        example_action = example_segment.actions[0]
        pnad_options_known = example_action.has_option()
        # Sanity check the assumption described above.
        if pnad_options_known:
            assert example_action.get_option().parent in known_options
        for (segment, _) in pnad.datastore:
            for action in segment.actions:
                if pnad_options_known:
                    assert action.has_option()
                    param_option = action.get_option().parent
                    assert param_option == example_action.get_option().parent
                else:
                    assert not action.has_option()
        if pnad_options_known:
            known_option_pnads.append(pnad)
        else:
            unknown_option_pnads.append(pnad)
    # Use a KnownOptionsOptionLearner on the known option pnads.
    known_option_learner = KnownOptionsOptionLearner()
    unknown_option_learner = create_option_learner(action_space)
    # "Learn" the known options.
    _learn_pnad_options_with_learner(known_option_pnads, known_option_learner)
    logging.info("\nOperators with known options:")
    for pnad in known_option_pnads:
        logging.info(pnad)
    # Learn the unknown options.
    _learn_pnad_options_with_learner(unknown_option_pnads,
                                     unknown_option_learner)
    logging.info("\nLearned operators with option specs:")
    for pnad in unknown_option_pnads:
        logging.info(pnad)


def _learn_pnad_options_with_learner(
        pnads: List[PNAD], option_learner: _OptionLearnerBase) -> None:
    """Helper for _learn_pnad_options()."""
    strips_ops = []
    datastores = []
    for pnad in pnads:
        strips_ops.append(pnad.op)
        datastores.append(pnad.datastore)
    option_specs = option_learner.learn_option_specs(strips_ops, datastores)
    assert len(option_specs) == len(pnads)
    # Replace the option_specs in the PNADs.
    for pnad, option_spec in zip(pnads, option_specs):
        pnad.option_spec = option_spec
    # Seed the new parameterized option parameter spaces.
    for parameterized_option, _ in option_specs:
        parameterized_option.params_space.seed(CFG.seed)
    # Update the segments to include which option is being executed.
    for datastore, spec in zip(datastores, option_specs):
        for (segment, _) in datastore:
            # Modifies segment in-place.
            option_learner.update_segment_from_option_spec(segment, spec)


def _learn_pnad_samplers(pnads: List[PNAD], sampler_learner: str) -> None:
    logging.info("\nDoing sampler learning...")
    strips_ops = []
    datastores = []
    option_specs = []
    for pnad in pnads:
        strips_ops.append(pnad.op)
        datastores.append(pnad.datastore)
        option_specs.append(pnad.option_spec)
    samplers = learn_samplers(strips_ops, datastores, option_specs,
                              sampler_learner)
    assert len(samplers) == len(strips_ops)
    # Replace the samplers in the PNADs.
    for pnad, sampler in zip(pnads, samplers):
        pnad.sampler = sampler
