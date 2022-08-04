"""The core algorithm for learning a collection of NSRT data structures."""

from __future__ import annotations

import logging
from typing import Dict, List, Set, Tuple

from gym.spaces import Box
from scipy import rand

from predicators.src.nsrt_learning.option_learning import \
    KnownOptionsOptionLearner, _OptionLearnerBase, create_option_learner
from predicators.src.nsrt_learning.sampler_learning import learn_samplers
from predicators.src.nsrt_learning.segmentation import segment_trajectory
from predicators.src.nsrt_learning.strips_learning import \
    learn_strips_operators
from predicators.src.settings import CFG
from predicators.src.structs import NSRT, GroundAtomTrajectory, \
    LowLevelTrajectory, ParameterizedOption, PartialNSRTAndDatastore, \
    Predicate, Segment, Task


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
    #####
    # TODO (wbm3): Try data reshuffling (trajectories and ground_atom_dataset)
    def total_add_effects(traj):
            return sum([len(seg.add_effects) for seg in segment_trajectory(traj)])

    def max_add_effects(traj):
        return max([len(seg.add_effects) for seg in segment_trajectory(traj)])
    
    def min_length(traj):
        return len(traj[1])

    def weighted_add_effects(traj):
        return sum([(i + 1) * len(seg.add_effects) for i, seg in enumerate(segment_trajectory(traj))])
    
    import random

    rnd_seed = 37774 #int(random.random() * 1000000)
    if CFG.sort_data != "default":
        if CFG.sort_data == "sum":
            sorting_heuristic = total_add_effects
        elif CFG.sort_data == "max":
            sorting_heuristic = max_add_effects
        elif CFG.sort_data == "min_len":
            sorting_heuristic = min_length
        elif CFG.sort_data == "weighted_max":
            sorting_heuristic = weighted_add_effects
        elif CFG.sort_data == "random":
            random.seed(rnd_seed)
            sorting_heuristic = lambda traj: random.random()
        else:
            raise NotImplementedError(f"Not a vaild sorting heuristic {CFG.sort_data}")

        trajectories = [traj for _, traj in sorted(zip(ground_atom_dataset, trajectories), key=lambda pair: sorting_heuristic(pair[0]))]
        random.seed(rnd_seed)
        ground_atom_dataset.sort(key = sorting_heuristic)
    #####

    # STEP 1: Segment each trajectory in the dataset based on changes in
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

    # STEP 2: Learn STRIPS operators from the data, and use them to produce
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


def _learn_pnad_options(pnads: List[PartialNSRTAndDatastore],
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
        pnads: List[PartialNSRTAndDatastore],
        option_learner: _OptionLearnerBase) -> None:
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
