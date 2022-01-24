"""Algorithms for task and motion planning.

Mainly, "SeSamE": SEarch-and-SAMple planning, then Execution.
"""

from __future__ import annotations
from collections import defaultdict
import heapq as hq
import time
from typing import Collection, List, Set, Optional, Tuple, Iterator, Sequence
from dataclasses import dataclass
import numpy as np
from predicators.src.approaches import ApproachFailure, ApproachTimeout
from predicators.src.structs import State, Task, NSRT, Predicate, \
    GroundAtom, _GroundNSRT, DummyOption, DefaultState, _Option, \
    Metrics, STRIPSOperator, OptionSpec, Object
from predicators.src import utils
from predicators.src.utils import _TaskPlanningHeuristic
from predicators.src.envs import EnvironmentFailure
from predicators.src.option_model import _OptionModel
from predicators.src.settings import CFG

_NOT_CAUSES_FAILURE = "NotCausesFailure"


@dataclass(repr=False, eq=False)
class _Node:
    """A node for the search over skeletons."""
    atoms: Collection[GroundAtom]
    skeleton: List[_GroundNSRT]
    atoms_sequence: List[Collection[GroundAtom]]  # expected state sequence
    parent: Optional[_Node]


def sesame_plan(
    task: Task,
    option_model: _OptionModel,
    nsrts: Set[NSRT],
    initial_predicates: Set[Predicate],
    timeout: float,
    seed: int,
    check_dr_reachable: bool = True,
) -> Tuple[List[_Option], Metrics]:
    """Run TAMP.

    Return a sequence of options, and a dictionary of metrics for this
    run of the planner. Uses the SeSamE strategy: SEarch-and-SAMple
    planning, then Execution.
    """
    nsrt_preds, _ = utils.extract_preds_and_types(nsrts)
    # Ensure that initial predicates are always included.
    predicates = initial_predicates | set(nsrt_preds.values())
    init_atoms = utils.abstract(task.init, predicates)
    objects = list(task.init)
    start_time = time.time()
    ground_nsrts = []
    for nsrt in sorted(nsrts):
        for ground_nsrt in utils.all_ground_nsrts(nsrt, objects):
            ground_nsrts.append(ground_nsrt)
            if time.time() - start_time > timeout:
                raise ApproachTimeout("Planning timed out in grounding!")
    # Keep restarting the A* search while we get new discovered failures.
    metrics: Metrics = defaultdict(float)
    while True:
        # There is no point in using NSRTs with empty effects, and they can
        # slow down search significantly, so we exclude them. Note however
        # that we need to do this inside the while True here, because an NSRT
        # that initially has empty effects may later have a _NOT_CAUSES_FAILURE.
        nonempty_ground_nsrts = [
            nsrt for nsrt in ground_nsrts
            if nsrt.add_effects | nsrt.delete_effects
        ]
        all_reachable_atoms = utils.get_reachable_atoms(
            nonempty_ground_nsrts, init_atoms)
        if check_dr_reachable and not task.goal.issubset(all_reachable_atoms):
            raise ApproachFailure(f"Goal {task.goal} not dr-reachable")
        reachable_nsrts = [
            nsrt for nsrt in nonempty_ground_nsrts
            if nsrt.preconditions.issubset(all_reachable_atoms)
        ]
        heuristic = utils.create_task_planning_heuristic(
            CFG.task_planning_heuristic, init_atoms, task.goal,
            reachable_nsrts, predicates, objects)
        try:
            new_seed = seed + int(metrics["num_failures_discovered"])
            for skeleton, atoms_sequence in _skeleton_generator(
                    task, reachable_nsrts, init_atoms, heuristic, new_seed,
                    timeout - (time.time() - start_time), metrics):
                plan = _run_low_level_search(
                    task, option_model, skeleton, atoms_sequence, new_seed,
                    timeout - (time.time() - start_time))
                if plan is not None:
                    print(
                        f"Planning succeeded! Found plan of length "
                        f"{len(plan)} after "
                        f"{int(metrics['num_skeletons_optimized'])} "
                        f"skeletons, discovering "
                        f"{int(metrics['num_failures_discovered'])} failures")
                    metrics["plan_length"] = len(plan)
                    return plan, metrics
        except _DiscoveredFailureException as e:
            metrics["num_failures_discovered"] += 1
            new_predicates, ground_nsrts = _update_nsrts_with_failure(
                e.discovered_failure, ground_nsrts)
            predicates |= new_predicates


def task_plan_grounding(
    init_atoms: Set[GroundAtom],
    objects: Set[Object],
    strips_ops: Sequence[STRIPSOperator],
    option_specs: Sequence[OptionSpec],
) -> Tuple[List[_GroundNSRT], Set[GroundAtom]]:
    """Ground all operators for task planning into dummy _GroundNSRTs,
    filtering out ones that are unreachable or have empty effects.

    Also return the set of reachable atoms, which is used by task
    planning to quickly determine if a goal is unreachable.

    See the task_plan docstring for usage instructions.
    """
    nsrts = utils.ops_and_specs_to_dummy_nsrts(strips_ops, option_specs)
    ground_nsrts = []
    for nsrt in sorted(nsrts):
        for ground_nsrt in utils.all_ground_nsrts(nsrt, objects):
            ground_nsrts.append(ground_nsrt)
    nonempty_ground_nsrts = [
        nsrt for nsrt in ground_nsrts if nsrt.add_effects | nsrt.delete_effects
    ]
    reachable_atoms = utils.get_reachable_atoms(nonempty_ground_nsrts,
                                                init_atoms)
    reachable_nsrts = [
        nsrt for nsrt in nonempty_ground_nsrts
        if nsrt.preconditions.issubset(reachable_atoms)
    ]
    return reachable_nsrts, reachable_atoms


def task_plan(
    init_atoms: Set[GroundAtom],
    goal: Set[GroundAtom],
    ground_nsrts: List[_GroundNSRT],
    reachable_atoms: Set[GroundAtom],
    heuristic: _TaskPlanningHeuristic,
    seed: int,
    timeout: float,
) -> Iterator[Tuple[List[_GroundNSRT], List[Collection[GroundAtom]], Metrics]]:
    """Run only the task planning portion of SeSamE. A* search is run, and
    skeletons that achieve the goal symbolically are yielded. Specifically,
    yields a tuple of (skeleton, atoms sequence, metrics dictionary).

    This method is NOT used by SeSamE, but is instead provided as a
    convenient wrapper around _skeleton_generator below (which IS used
    by SeSamE) that takes in only the minimal necessary arguments.

    This method is tightly coupled with task_plan_grounding -- the reason they
    are separate methods is that it is sometimes possible to ground only once
    and then plan multiple times (e.g. from different initial states, or to
    different goals). To run task planning once, call task_plan_grounding to
    get ground_nsrts and reachable_atoms; then create a heuristic using
    utils.create_task_planning_heuristic; then call this method. See the tests
    in tests/test_planning for usage examples.
    """
    if not goal.issubset(reachable_atoms):
        raise ApproachFailure(f"Goal {goal} not dr-reachable")
    dummy_task = Task(State({}), goal)
    metrics: Metrics = defaultdict(float)
    for skeleton, atoms_sequence in _skeleton_generator(
            dummy_task, ground_nsrts, init_atoms, heuristic, seed, timeout,
            metrics):
        yield skeleton, atoms_sequence, metrics.copy()


def _skeleton_generator(
    task: Task, ground_nsrts: List[_GroundNSRT], init_atoms: Set[GroundAtom],
    heuristic: _TaskPlanningHeuristic, seed: int, timeout: float,
    metrics: Metrics
) -> Iterator[Tuple[List[_GroundNSRT], List[Collection[GroundAtom]]]]:
    """A* search over skeletons (sequences of ground NSRTs).
    Iterates over pairs of (skeleton, atoms sequence).
    """
    start_time = time.time()
    queue: List[Tuple[float, float, _Node]] = []
    root_node = _Node(atoms=init_atoms,
                      skeleton=[],
                      atoms_sequence=[init_atoms],
                      parent=None)
    rng_prio = np.random.default_rng(seed)
    hq.heappush(queue,
                (heuristic(root_node.atoms), rng_prio.uniform(), root_node))
    # Start search.
    while queue and (time.time() - start_time < timeout):
        if (int(metrics["num_skeletons_optimized"]) ==
                CFG.max_skeletons_optimized):
            raise ApproachFailure("Planning reached max_skeletons_optimized!")
        _, _, node = hq.heappop(queue)
        # Good debug point #1: print out the skeleton here to see what
        # the high-level search is doing. You can accomplish this via:
        # for act in node.skeleton:
        #     print(act.name, act.objects)
        # print()
        if task.goal.issubset(node.atoms):
            # If this skeleton satisfies the goal, yield it.
            metrics["num_skeletons_optimized"] += 1
            yield node.skeleton, node.atoms_sequence
        else:
            # Generate successors.
            metrics["num_nodes_expanded"] += 1
            for nsrt in utils.get_applicable_operators(ground_nsrts,
                                                       node.atoms):
                child_atoms = utils.apply_operator(nsrt, set(node.atoms))
                child_node = _Node(atoms=child_atoms,
                                   skeleton=node.skeleton + [nsrt],
                                   atoms_sequence=node.atoms_sequence +
                                   [child_atoms],
                                   parent=node)
                # priority is g [plan length] plus h [heuristic]
                priority = (len(child_node.skeleton) +
                            heuristic(child_node.atoms))
                hq.heappush(queue, (priority, rng_prio.uniform(), child_node))
    if not queue:
        raise ApproachFailure("Planning ran out of skeletons!")
    assert time.time() - start_time >= timeout
    raise ApproachTimeout("Planning timed out in skeleton search!")


def _run_low_level_search(task: Task, option_model: _OptionModel,
                          skeleton: List[_GroundNSRT],
                          atoms_sequence: List[Collection[GroundAtom]],
                          seed: int,
                          timeout: float) -> Optional[List[_Option]]:
    """Backtracking search over continuous values."""
    start_time = time.time()
    rng_sampler = np.random.default_rng(seed)
    assert CFG.sesame_propagate_failures in \
        {"after_exhaust", "immediately", "never"}
    cur_idx = 0
    num_tries = [0 for _ in skeleton]
    plan: List[_Option] = [DummyOption for _ in skeleton]
    traj: List[State] = [task.init] + [DefaultState for _ in skeleton]
    # We'll use a maximum of one discovered failure per step, since
    # resampling can render old discovered failures obsolete.
    discovered_failures: List[Optional[_DiscoveredFailure]] = [
        None for _ in skeleton
    ]
    while cur_idx < len(skeleton):
        if time.time() - start_time > timeout:
            raise ApproachTimeout("Planning timed out in backtracking!")
        assert num_tries[cur_idx] < CFG.max_samples_per_step
        # Good debug point #2: if you have a skeleton that you think is
        # reasonable, but sampling isn't working, print num_tries here to
        # see at what step the backtracking search is getting stuck.
        num_tries[cur_idx] += 1
        state = traj[cur_idx]
        nsrt = skeleton[cur_idx]
        # Ground the NSRT's ParameterizedOption into an _Option.
        # This invokes the NSRT's sampler.
        option = nsrt.sample_option(state, rng_sampler)
        plan[cur_idx] = option
        if option.initiable(state):
            try:
                next_state = option_model.get_next_state(state, option)
                discovered_failures[cur_idx] = None  # no failure occurred
            except EnvironmentFailure as e:
                can_continue_on = False
                failure = _DiscoveredFailure(e, nsrt)
                # Remember only the most recent failure.
                discovered_failures[cur_idx] = failure
                # If we're immediately propagating failures, raise it now.
                if CFG.sesame_propagate_failures == "immediately":
                    raise _DiscoveredFailureException("Discovered a failure",
                                                      failure)
            if not discovered_failures[cur_idx]:
                traj[cur_idx + 1] = next_state
                cur_idx += 1
                # Check atoms against expected atoms_sequence constraint.
                assert len(traj) == len(atoms_sequence)
                # The expected atoms are ones that we definitely expect to be
                # true at this point in the plan. They are not *all* the atoms
                # that could be true.
                expected_atoms = {
                    atom
                    for atom in atoms_sequence[cur_idx]
                    if atom.predicate.name != _NOT_CAUSES_FAILURE
                }
                # This is equivalent to, but faster than, checking whether
                # expected_atoms is a subset of utils.abstract(traj[cur_idx],
                # predicates).
                if all(atom.holds(traj[cur_idx]) for atom in expected_atoms):
                    can_continue_on = True
                    if cur_idx == len(skeleton):  # success!
                        result = plan
                        return result
                else:
                    can_continue_on = False
            else:
                cur_idx += 1  # it's about to be decremented again
        else:
            # If the option is not initiable, need to resample / backtrack.
            can_continue_on = False
            cur_idx += 1  # it's about to be decremented again
        if not can_continue_on:
            # Go back to re-do the step we just did. If necessary, backtrack.
            cur_idx -= 1
            assert cur_idx >= 0
            while num_tries[cur_idx] == CFG.max_samples_per_step:
                num_tries[cur_idx] = 0
                plan[cur_idx] = DummyOption
                traj[cur_idx + 1] = DefaultState
                cur_idx -= 1
                if cur_idx < 0:
                    # Backtracking exhausted. If we're only propagating failures
                    # after exhaustion, and if there are any failures,
                    # propagate up the EARLIEST one so that search restarts.
                    # Otherwise, return None so that search continues.
                    for earliest_failure in discovered_failures:
                        if (CFG.sesame_propagate_failures == "after_exhaust"
                                and earliest_failure is not None):
                            raise _DiscoveredFailureException(
                                "Discovered a failure", earliest_failure)
                    return None
    # Should only get here if the skeleton was empty
    assert not skeleton
    return []


def _update_nsrts_with_failure(
    discovered_failure: _DiscoveredFailure, ground_nsrts: List[_GroundNSRT]
) -> Tuple[Set[Predicate], List[_GroundNSRT]]:
    """Update the given set of ground_nsrts based on the given
    DiscoveredFailure.

    Returns a new list of ground NSRTs to replace the input one, where
    all ground NSRTs that need modification are replaced with new ones
    (because _GroundNSRTs are frozen).
    """
    new_predicates = set()
    new_ground_nsrts = []
    for obj in discovered_failure.env_failure.offending_objects:
        pred = Predicate(_NOT_CAUSES_FAILURE, [obj.type],
                         _classifier=lambda s, o: False)
        new_predicates.add(pred)
        atom = GroundAtom(pred, [obj])
        for ground_nsrt in ground_nsrts:
            # Update the preconditions of the failing NSRT.
            if ground_nsrt == discovered_failure.failing_nsrt:
                new_ground_nsrt = ground_nsrt.copy_with(
                    preconditions=ground_nsrt.preconditions | {atom})
            # Update the effects of all NSRTs that use this object.
            # Note that this is an elif rather than an if, because it would
            # never be possible to use the failing NSRT's effects to set
            # the _NOT_CAUSES_FAILURE precondition.
            elif obj in ground_nsrt.objects:
                new_ground_nsrt = ground_nsrt.copy_with(
                    add_effects=ground_nsrt.add_effects | {atom})
            else:
                new_ground_nsrt = ground_nsrt
            new_ground_nsrts.append(new_ground_nsrt)
    return new_predicates, new_ground_nsrts


@dataclass(frozen=True, eq=False)
class _DiscoveredFailure:
    """Container class for holding information related to a low-level discovery
    of a failure which must be propagated up to the main search function, in
    order to restart A* search with new NSRTs."""
    env_failure: EnvironmentFailure
    failing_nsrt: _GroundNSRT


class _DiscoveredFailureException(Exception):
    """Exception class for DiscoveredFailure propagation."""

    def __init__(self, message: str, discovered_failure: _DiscoveredFailure):
        super().__init__(message)
        self.discovered_failure = discovered_failure
