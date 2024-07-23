"""The core algorithm for learning a collection of NSRT data structures."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from gym.spaces import Box

from predicators.nsrt_learning.option_learning import \
    KnownOptionsOptionLearner, _OptionLearnerBase, create_option_learner
from predicators.nsrt_learning.sampler_learning import learn_samplers
from predicators.nsrt_learning.segmentation import segment_trajectory
from predicators.nsrt_learning.strips_learning import learn_strips_operators
from predicators.settings import CFG
from predicators.structs import NSRT, PNAD, GroundAtomTrajectory, \
    LowLevelTrajectory, ParameterizedOption, Predicate, Segment, Task


def learn_nsrts_from_data(
    trajectories: List[LowLevelTrajectory], train_tasks: List[Task],
    predicates: Set[Predicate], known_options: Set[ParameterizedOption],
    action_space: Box,
    ground_atom_dataset: Optional[List[GroundAtomTrajectory]],
    sampler_learner: str, annotations: Optional[List[Any]]
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
    for _ in range(CFG.data_orderings_to_search):
        # Step 0: Shuffle dataset to learn from.
        if CFG.data_orderings_to_search > 1:
            random_data_indices = sorted(
                [int(i) for i in range(len(trajectories))],
                key=lambda _: rng.random())
            trajectories = [trajectories[i] for i in random_data_indices]
            if ground_atom_dataset is not None:
                ground_atom_dataset = [
                    ground_atom_dataset[i] for i in random_data_indices
                ]
        # STEP 1: Segment each trajectory in the dataset based on changes in
        #         either predicates or options. If we are doing option learning,
        #         then the data will not contain options, so this segmenting
        #         procedure only uses the predicates.
        if ground_atom_dataset is None:
            segmented_trajs = [
                segment_trajectory(traj, predicates) for traj in trajectories
            ]
        else:
            segmented_trajs = [
                segment_trajectory(traj, predicates, atom_seq=atom_seq)
                for traj, atom_seq in ground_atom_dataset
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
            verbose=(CFG.option_learner != "no_learning"),
            annotations=annotations)

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
    # In the special case where all NSRT learning components are oracle, skip
    # this step, because there may be empty PNADs, and option learning assumes
    # in several places that the PNADs are not empty.
    if CFG.strips_learner != "oracle" or CFG.sampler_learner != "oracle" or \
       CFG.option_learner != "no_learning":
        # Updates the PNADs in-place.
        _learn_pnad_options(pnads, known_options, action_space)

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
