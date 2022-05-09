"""The core algorithm for learning a collection of NSRT data structures."""

from __future__ import annotations

import logging
from typing import Dict, List, Set, Tuple

from gym.spaces import Box

from predicators.src import utils
from predicators.src.nsrt_learning.option_learning import create_option_learner
from predicators.src.nsrt_learning.sampler_learning import learn_samplers
from predicators.src.nsrt_learning.segmentation import segment_trajectory
from predicators.src.nsrt_learning.strips_learning import \
    learn_strips_operators
from predicators.src.settings import CFG
from predicators.src.structs import NSRT, LowLevelTrajectory, \
    PartialNSRTAndDatastore, Predicate, Segment, Task


def learn_nsrts_from_data(
    trajectories: List[LowLevelTrajectory], train_tasks: List[Task],
    predicates: Set[Predicate], action_space: Box, sampler_learner: str
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

    # STEP 1: Apply predicates to data, producing a dataset of abstract states.
    ground_atom_dataset = utils.create_ground_atom_dataset(
        trajectories, predicates)

    # STEP 2: Segment each trajectory in the dataset based on changes in
    #         either predicates or options. If we are doing option learning,
    #         then the data will not contain options, so this segmenting
    #         procedure only uses the predicates.
    segmented_trajs = [
        segment_trajectory(traj) for traj in ground_atom_dataset
    ]
    # We delete ground_atom_dataset because it's prone to causing bugs --
    # we should rarely care about the low-level ground atoms sequence after
    # segmentation.
    del ground_atom_dataset

    # If performing goal-conditioned sampler learning, we need to attach the
    # goals to the segments.
    if CFG.sampler_learning_use_goals:
        for segment_traj, ll_traj in zip(segmented_trajs, trajectories):
            # If the trajectory is not a demonstration, it does not have a
            # known goal (e.g., it was generated by random replay data), so we
            # won't attach a goal to the segment.
            if ll_traj.is_demo:
                goal = train_tasks[ll_traj.train_task_idx].goal
                for segment in segment_traj:
                    segment.set_goal(goal)

    # STEP 3: Learn STRIPS operators from the data, and use them to produce
    #         PartialNSRTAndDatastore (PNAD) objects. Each PNAD contains a
    #         STRIPSOperator, Datastore, and OptionSpec. The samplers will be
    #         filled in on a later step.
    pnads = learn_strips_operators(
        trajectories,
        train_tasks,
        predicates,
        segmented_trajs,
        verify_harmlessness=True,
        verbose=(CFG.option_learner != "no_learning"))

    # STEP 4: Learn options (option_learning.py) and update PNADs.
    _learn_pnad_options(pnads, action_space)  # in-place update

    # STEP 5: Learn samplers (sampler_learning.py) and update PNADs.
    _learn_pnad_samplers(pnads, sampler_learner)  # in-place update

    # STEP 6: Make, log, and return the NSRTs.
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


def _learn_pnad_options(pnads: List[PartialNSRTAndDatastore],
                        action_space: Box) -> None:
    logging.info("\nDoing option learning...")
    option_learner = create_option_learner(action_space)
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
            # Sanity check: if an action in the segment has a known option,
            # then its parent should be the parameterized option in the spec.
            # Furthermore, either all of the actions in the segment should have
            # a known option, or none of them should.
            expecting_known_option = segment.actions[0].has_option()
            for action in segment.actions:
                if expecting_known_option:
                    assert action.has_option()
                    assert action.get_option().parent == spec[0]
                else:
                    assert not action.has_option()
            # Modify the segment in-place.
            option_learner.update_segment_from_option_spec(segment, spec)
    logging.info("\nLearned operators with option specs:")
    for pnad in pnads:
        logging.info(pnad)


def _learn_pnad_samplers(pnads: List[PartialNSRTAndDatastore],
                         sampler_learner: str) -> None:
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
