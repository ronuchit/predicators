import cProfile
import os
from copy import deepcopy
import dataclasses
from dataclasses import dataclass
from itertools import cycle, repeat, groupby
from types import SimpleNamespace
from experiments.envs.donuts.env import Donuts
from gym.spaces import Box
import logging
import numpy as np
from numpy import typing as npt
import pickle
import time
import torch
from tqdm import tqdm
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, cast
# from operator import itemgetter

import matplotlib
import matplotlib.pyplot as plt
matplotlib.use("tkagg")

# from experiments.envs.shelves2d.env import Shelves2DEnv
from experiments.search_pruning_approach.dataset import FeasibilityDataset
from experiments.search_pruning_approach.learning import ConstFeasibilityClassifier, FeasibilityClassifier, NeuralFeasibilityClassifier
from experiments.search_pruning_approach.low_level_planning import BacktrackingTree, run_backtracking_for_data_generation, run_low_level_search

from predicators import utils
from predicators.approaches.base_approach import ApproachFailure, ApproachTimeout
from predicators.approaches.nsrt_learning_approach import NSRTLearningApproach
from predicators.nsrt_learning.sampler_learning import _LearnedSampler
from predicators.option_model import _OptionModelBase
from predicators.planning import PlanningTimeout, task_plan, task_plan_grounding
from predicators.settings import CFG
from predicators.structs import NSRT, _GroundNSRT, _Option, Dataset, GroundAtom, Metrics, ParameterizedOption, Predicate, State, Task, Type


__all__ = ["SearchPruningApproach"]

@dataclass(frozen=True)
class InterleavedBacktrackingDatapoint():
    states: List[State]
    atoms_sequence: List[Set[GroundAtom]]
    horizons: npt.NDArray
    skeleton: List[_GroundNSRT]

    def __post_init__(self):
        assert len(self.states) == len(self.atoms_sequence)
        assert len(self.states) == len(self.skeleton) + 1
        assert len(self.skeleton) == len(self.horizons)

    def substitute_nsrts(self, nsrts_dict: Dict[str, NSRT]) -> 'InterleavedBacktrackingDatapoint':
        return dataclasses.replace(self, skeleton=[
            nsrts_dict[str(ground_nsrt.parent)].ground(ground_nsrt.objects)
            for ground_nsrt in self.skeleton
        ])

    def __iter__(self) -> Iterator[Any]:
        return iter((self.states, self.atoms_sequence, self.horizons, self.skeleton))

# def get_placement_coords(state: State, current_nsrt: _GroundNSRT, top_coords: bool) -> Tuple[float, float]:
#     assert current_nsrt.name == "InsertBox"

#     box, _1, _2, _3 = current_nsrt.objects
#     x, y, w, h = Shelves2DEnv.get_shape_data(state, box)

#     if top_coords:
#         return (x + w/2, y + h)
#     else:
#         return (x + w/2, y)

# def visualize_shelves2d_datapoint(
#     skeleton: List[_GroundNSRT],
#     previous_states: List[State],
#     goal: Set[GroundAtom],
#     option_model: _OptionModelBase,
#     seed: int,
#     atoms_sequence: List[Set[GroundAtom]],
#     feasibility_classifier: FeasibilityClassifier,
# ) -> matplotlib.figure.Figure:
#     assert previous_states
#     current_state = previous_states[-1]
#     nsrt = skeleton[len(previous_states) - 1]
#     rng_sampler = np.random.default_rng(seed)

#     datapoints: List[Tuple[State, bool]] = []

#     for idx in range(100):
#         option = nsrt.sample_option(current_state, goal, rng_sampler, skeleton[len(previous_states) - 1:])
#         next_state, _ = option_model.get_next_state_and_num_actions(current_state, option)

#         if not all(a.holds(next_state) for a in atoms_sequence[len(previous_states)]):
#             continue

#         feasible, _ = feasibility_classifier.classify(previous_states + [next_state], skeleton)
#         datapoints.append((next_state, feasible))

#     fig = Shelves2DEnv.render_state_plt(current_state, None)
#     ax, = fig.axes

#     assert skeleton[-1].name in {"MoveCoverToTop", "MoveCoverToBottom"}
#     top_coords = skeleton[-1].name == "MoveCoverToTop"

#     feasible_samples = [get_placement_coords(state, nsrt, top_coords) for (state, feasible) in datapoints if feasible]
#     infeasible_samples = [get_placement_coords(state, nsrt, top_coords) for (state, feasible) in datapoints if not feasible]

#     ax.scatter([x for x, _ in feasible_samples], [y for _, y in feasible_samples], s=1, c='black', alpha=0.3)
#     ax.scatter([x for x, _ in infeasible_samples], [y for _, y in infeasible_samples], s=1, c='red', marker='x', alpha=0.3)

#     return fig

# def run_visualization_saving(
#     search_datapoints: List[InterleavedBacktrackingDatapoint],
#     option_model: _OptionModelBase,
#     seed: int,
#     prefix_length: int,
#     visualization_directory: str,
#     feasibility_classifier: FeasibilityClassifier,
# ) -> None:
#     logging_level = deepcopy(logging.getLogger().level)
#     os.makedirs(visualization_directory, exist_ok=True)
#     for idx, search_datapoint in zip(range(10), search_datapoints):
#         states, atoms_sequence, horizons, skeleton = search_datapoint

#         logging.getLogger().setLevel(logging.WARNING)
#         fig = visualize_shelves2d_datapoint(
#             skeleton = skeleton,
#             previous_states = states[:prefix_length],
#             goal = atoms_sequence[-1],
#             option_model = option_model,
#             seed = seed,
#             atoms_sequence = atoms_sequence,
#             feasibility_classifier = feasibility_classifier,
#         )
#         logging.getLogger().setLevel(logging_level)

#         filepath = os.path.join(visualization_directory, f"{idx}.pdf")
#         logging.info(f"Saving visualization to file {filepath} with "
#                      f"skeleton {[(nsrt.name, nsrt.objects) for nsrt in skeleton]}")
#         fig.savefig(filepath)

# def shelves2d_ground_truth_classifier(states: Sequence[State], skeleton: Sequence[_GroundNSRT]) -> bool:
#         current_nsrt = skeleton[len(states) - 2]
#         final_nsrt = skeleton[-1]

#         assert final_nsrt.name in {"MoveCoverToBottom", "MoveCoverToTop"}
#         if current_nsrt.name != "InsertBox":
#             logging.info("GROUND TRUTH CLASSIFIER - CURRENT NSRT INVALID")
#             return True

#         box, shelf, _, cover = current_nsrt.objects
#         box_x, box_y, box_w, box_h = Shelves2DEnv.get_shape_data(states[-1], box)
#         shelf_x, shelf_y, shelf_w, shelf_h = Shelves2DEnv.get_shape_data(states[-1], shelf)
#         distance_thresh = Shelves2DEnv.cover_max_distance

#         if final_nsrt.name == "MoveCoverToTop":
#             return (box_y + box_h <= shelf_y + shelf_h + distance_thresh and box_x >= shelf_x and box_x + box_w <= shelf_x + shelf_w)
#         else:
#             return (box_y >= shelf_y - distance_thresh and box_x >= shelf_x and box_x + box_w <= shelf_x + shelf_w)

# def analyze_datapoints(
#     positive_datapoints: List[Tuple[List[_GroundNSRT], List[State]]],
#     negative_datapoints: List[Tuple[List[_GroundNSRT], List[State]]],
# ) -> str:
#     num_correct_positive = sum(1 for skeleton, states in positive_datapoints if shelves2d_ground_truth_classifier(states, skeleton))
#     num_correct_negative = sum(1 for skeleton, states in negative_datapoints if not shelves2d_ground_truth_classifier(states, skeleton))

#     key_fun = lambda datapoint: (datapoint[0][-1].name, len(datapoint[0]))
#     num_positive_datapoints_per_class = [(c, len(list(d))) for c, d in groupby(sorted(positive_datapoints, key=key_fun), key=key_fun)]
#     num_negative_datapoints_per_class = [(c, len(list(d))) for c, d in groupby(sorted(negative_datapoints, key=key_fun), key=key_fun)]

#     positive_report = (f"Positive datapoint purity: {num_correct_positive/len(positive_datapoints):.2%}; "
#     f"Positive datapoints per class: {num_positive_datapoints_per_class}") if positive_datapoints else "No positive datapoints collected"
#     negative_report = (f"negative datapoint purity: {num_correct_negative/len(negative_datapoints):.2%}; "
#     f"negative datapoints per class: {num_negative_datapoints_per_class}") if negative_datapoints else "No negative datapoints collected"
#     return positive_report + '\n' + negative_report

class SearchPruningApproach(NSRTLearningApproach):
    def __init__(self, initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task]) -> None:
        super().__init__(initial_predicates, initial_options, types, action_space, train_tasks)
        self._train_feasibility_dataset: Optional[FeasibilityDataset] = None
        self._validation_feasibility_dataset: Optional[FeasibilityDataset] = None
        self._feasibility_classifier = ConstFeasibilityClassifier()
        # self._test_tasks_ran = 0
        assert CFG.feasibility_debug_directory[-1] == '/'

    @classmethod
    def get_name(cls) -> str:
        return "search_pruning"

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        # Setting up the logger properly
        logging.getLogger().setLevel(logging.DEBUG)

        # Generate the base NSRTs
        super().learn_from_offline_dataset(dataset)

        # Make sure we have direct access to the regressors
        assert all(type(nsrt.sampler) == _LearnedSampler for nsrt in self._nsrts)

        # Make sure the trajectories are entirely covered by the learned NSRTs (we can easily generate skeletons)
        assert all(
            segment in self._seg_to_ground_nsrt
            for segmented_traj in self._segmented_trajs
            for segment in segmented_traj
        )

        # Preparing data collection and training
        self._train_feasibility_dataset = FeasibilityDataset(self._nsrts, CFG.feasibility_batch_size)
        self._validation_feasibility_dataset = FeasibilityDataset(self._nsrts, CFG.feasibility_batch_size)
        dataset_path: str = utils.create_dataset_filename_str(["feasibility_dataset"])[0]

        # Running data collection and training
        seed = self._seed + 100000
        assert CFG.feasibility_learning_strategy in {"backtracking", "load_data", "load_model"}

        if CFG.feasibility_learning_strategy == "load_model":
            assert CFG.feasibility_load_path
            self._feasibility_classifier = torch.load(CFG.feasibility_load_path)
            self._feasibility_classifier._optimizer = None
        elif CFG.feasibility_learning_strategy == "load_data":
            self._load_data(CFG.feasibility_load_path if CFG.feasibility_load_path else dataset_path, self._nsrts)
            snapshot_dir = os.path.join(CFG.feasibility_debug_directory, 'loaded-data-training-snapshots')
            os.makedirs(snapshot_dir, exist_ok=True)
            self._learn_neural_feasibility_classifier(1, snapshot_dir)
        else:
            self._collect_data_interleaved_backtracking(seed)
            self._save_data(dataset_path)

    def _load_data(
        self, path: str, nsrts: Iterable[NSRT]
    ) -> None:
        """Loads the feasibility datasets saved under a path.
        """
        train_data, validation_data = pickle.load(open(path, 'rb'))
        self._train_feasibility_dataset.loads(train_data, nsrts)
        self._validation_feasibility_dataset.loads(validation_data, nsrts)

    def _save_data(self, path: str) -> None:
        """Saves the feasibility datasets under a path.
        """
        pickle.dump((
            self._train_feasibility_dataset.dumps(),
            self._validation_feasibility_dataset.dumps()
        ), open(path, "wb"))
        logging.info(f"saved generated feasibility datasets to {path}")

    def _collect_data_interleaved_backtracking(self, seed: int) -> None:
        """Collects data by interleaving data collection for a certain suffix length and learning on that data
        """
        # TODO: add an option to limit the number of tasks explored and handling around that
        assert CFG.feasibility_search_device in {'cpu', 'cuda'}

        # To make sure we are able to use the Pool without having to copy the entire model we reset the classifier
        self._feasibility_classifier = ConstFeasibilityClassifier()

        # Precomputing the datapoints for interleaved backtracking
        search_datapoints: List[InterleavedBacktrackingDatapoint] = [
            InterleavedBacktrackingDatapoint(
                states = [segment.states[0] for segment in segmented_traj] + [segmented_traj[-1].states[-1]],
                atoms_sequence = [segment.init_atoms for segment in segmented_traj] + [segmented_traj[-1].final_atoms],
                horizons = CFG.horizon - np.cumsum([len(segment.actions) for segment in segmented_traj]),
                skeleton = [self._seg_to_ground_nsrt[segment] for segment in segmented_traj]
            ) for segmented_traj in self._segmented_trajs
        ]
        self._rng.shuffle(search_datapoints)
        validation_cutoff = round(len(search_datapoints) * CFG.feasibility_validation_fraction)
        training_search_datapoints = search_datapoints[:validation_cutoff]
        validation_search_datapoints = search_datapoints[validation_cutoff:]

        num_validation_datapoints_per_iter = round(CFG.feasibility_num_datapoints_per_iter * (1 - CFG.feasibility_validation_fraction))
        num_training_datapoints_per_iter = CFG.feasibility_num_datapoints_per_iter - num_validation_datapoints_per_iter

        # Precomputing the nsrts on different devices
        if CFG.feasibility_search_device == 'cpu':
            nsrts_dicts: List[Dict[str, NSRT]] = [{str(nsrt): nsrt for nsrt in self._nsrts}]
            for nsrt in self._nsrts:
                nsrt.sampler.to('cpu').share_memory()
        else:
            nsrts_dicts: List[Dict[str, NSRT]] = [{str(nsrt): nsrt for nsrt in self._nsrts}] + \
                [{str(nsrt): deepcopy(nsrt) for nsrt in self._nsrts} for _ in range(1, torch.cuda.device_count())]
            for id, nsrts_dict in enumerate(nsrts_dicts):
                device_name = f'cuda:{id}'
                for nsrt in nsrts_dict.values():
                    nsrt.sampler.to(device_name)

        # Main data generation loop
        logging.info(f"Generating data with interleaved learning from {len(search_datapoints)} datapoints "
                     f"({len(training_search_datapoints)} for training and {len(validation_search_datapoints)} for validation)...")

        torch.multiprocessing.set_start_method('forkserver')
        cfg = SimpleNamespace(
            sesame_max_samples_per_step = CFG.sesame_max_samples_per_step,
            sesame_propagate_failures = CFG.sesame_propagate_failures,
            sesame_check_expected_atoms = True,
            sesame_check_static_object_changes = CFG.sesame_check_static_object_changes,
            sesame_static_object_change_tol = CFG.sesame_static_object_change_tol,
            sampler_disable_classifier = CFG.sampler_disable_classifier,
            max_num_steps_option_rollout = CFG.max_num_steps_option_rollout,
            option_model_terminate_on_repeat = CFG.option_model_terminate_on_repeat,
            feasibility_num_data_collection_threads = CFG.feasibility_num_data_collection_threads,
        )

        max_skeleton_length = max(map(len, self._segmented_trajs))
        with torch.multiprocessing.Pool(CFG.feasibility_num_data_collection_threads) as pool:
            # for prefix_length in list(reversed(range(1, max_skeleton_length))) + [1]:
            for prefix_length in reversed(range(1, max_skeleton_length)):
                logging.info(f"Collecting data for prefix length {prefix_length} ...")

                # Creating a directory to store logs for the prefix
                prefix_directory = os.path.join(CFG.feasibility_debug_directory, f"prefix-{prefix_length}")
                os.makedirs(prefix_directory, exist_ok=True)

                # Moving the feasibility classifier to devices
                if isinstance(self._feasibility_classifier, torch.nn.Module) and CFG.feasibility_search_device == 'cuda':
                    feasibility_classifiers = [deepcopy(self._feasibility_classifier) for _ in range(torch.cuda.device_count())]
                    if len(feasibility_classifiers) == 1:
                        feasibility_classifiers[0].to('cuda')
                    else:
                        for id, feasibility_classifier in enumerate(feasibility_classifiers):
                            feasibility_classifier.to(f'cuda:{id}')
                elif isinstance(self._feasibility_classifier, torch.nn.Module):
                    feasibility_classifiers = [deepcopy(self._feasibility_classifier)]
                    feasibility_classifiers[0].to('cpu')
                else:
                    assert prefix_length == max_skeleton_length - 1
                    feasibility_classifiers = [self._feasibility_classifier]

                # Creating the datapoints to search over and moving them to different devices
                training_viable_datapoints = [d for d in training_search_datapoints if len(d.skeleton) > prefix_length]
                training_indices = self._rng.choice(
                    len(training_viable_datapoints), min(len(training_viable_datapoints), num_training_datapoints_per_iter), replace=False
                )
                chosen_training_search_datapoints = [
                    training_viable_datapoints[idx].substitute_nsrts(nsrts_dict)
                    for idx, nsrts_dict in zip(training_indices, cycle(nsrts_dicts))
                ]

                validation_viable_datapoints = [d for d in validation_search_datapoints if len(d.skeleton) > prefix_length]
                validation_indices = self._rng.choice(
                    len(validation_viable_datapoints), min(len(validation_viable_datapoints), num_validation_datapoints_per_iter), replace=False
                )
                chosen_validation_search_datapoints = [
                    validation_viable_datapoints[idx].substitute_nsrts(nsrts_dict)
                    for idx, nsrts_dict in zip(validation_indices, cycle(nsrts_dicts))
                ]

                # Collecting data samples
                duration, train_positive_datapoints, train_negative_datapoints = \
                    SearchPruningApproach._backtracking_fill_dataset(
                    self._train_feasibility_dataset, pool, prefix_length, self._option_model,
                    feasibility_classifiers, seed, chosen_training_search_datapoints, cfg,
                    os.path.join(prefix_directory, f'train-data-gathering'),
                )
                logging.info(f"Took {duration} seconds to gather training data")
                # logging.info("Statistics; " + analyze_datapoints(
                #     train_positive_datapoints, train_negative_datapoints
                # ))

                duration, validation_positive_datapoints, validation_negative_datapoints = \
                    SearchPruningApproach._backtracking_fill_dataset(
                    self._validation_feasibility_dataset, pool, prefix_length, self._option_model,
                    feasibility_classifiers, seed + 50000, chosen_validation_search_datapoints, cfg,
                    os.path.join(prefix_directory, f'validation-data-gathering'),
                )
                logging.info(f"Took {duration} seconds to gather validation data")
                # logging.info("Statistics; " + analyze_datapoints(
                #     validation_positive_datapoints, validation_negative_datapoints
                # ))

                # if prefix_length > 1:
                    # self._learn_neural_feasibility_classifier(prefix_length)
                self._learn_neural_feasibility_classifier(prefix_length)
                # run_visualization_saving(
                #     chosen_validation_search_datapoints, self._option_model, seed,
                #     prefix_length, os.path.join(prefix_directory, f"visualization"),
                #     self._feasibility_classifier
                # )
                self._save_data(os.path.join(
                    prefix_directory,
                    f'feasibility-classifier-data-snapshot.data'
                ))
                torch.save(
                    self._feasibility_classifier,
                    os.path.join(prefix_directory,f'feasibility-classifier-model.pt')
                )
                seed += max(CFG.feasibility_num_datapoints_per_iter, 100000)

        logging.info(
            "Generated interleaving-based feasibility dataset of "
            f"{self._train_feasibility_dataset.num_positive_datapoints} positive and "
            f"{self._train_feasibility_dataset.num_negative_datapoints} negative datapoints"
        )

    @staticmethod
    def _backtracking_fill_dataset(
        dataset: FeasibilityDataset,
        pool: torch.multiprocessing.Pool,
        prefix_length: int,
        option_model: _OptionModelBase,
        feasibility_classifiers: List[FeasibilityClassifier],
        seed: int,
        search_datapoints: List[InterleavedBacktrackingDatapoint],
        cfg: SimpleNamespace,
        debug_dir: str,
    ) -> Tuple[float, List[Tuple[List[_GroundNSRT], List[State]]], List[Tuple[List[_GroundNSRT], List[State]]]]:
        os.makedirs(debug_dir, exist_ok=True)
        start = time.perf_counter()
        loop_data = zip(pool.map(
            SearchPruningApproach._backtracking_iteration
            , zip(
                repeat(prefix_length),
                repeat(option_model),
                cycle(feasibility_classifiers),
                range(seed, seed + len(search_datapoints)),
                search_datapoints,
                repeat(cfg),
                repeat(debug_dir),
            )
        ), search_datapoints)
        positive_datapoints = []
        negative_datapoints = []
        for (positive_paths, negative_paths, augmentation_paths), (_1, _2, _3, skeleton) in loop_data:
            for positive_path in positive_paths:
                positive_datapoints.append((skeleton, positive_path))
                dataset.add_positive_datapoint(skeleton, positive_path)
            for negative_path in negative_paths:
                negative_datapoints.append((skeleton, negative_path))
                dataset.add_negative_datapoint(skeleton, negative_path)
            for augmentation_path in augmentation_paths:
                dataset.add_augmentation_datapoint(skeleton, augmentation_path)
        return time.perf_counter() - start, positive_datapoints, negative_datapoints

    @staticmethod
    def _backtracking_iteration(
        args: Tuple[int, _OptionModelBase, FeasibilityClassifier, int, InterleavedBacktrackingDatapoint, SimpleNamespace, str]
    ) -> Tuple[List[List[State]], List[List[State]], List[List[State]]]:
        """ Running data collection for a single suffix length and task
        """
        global CFG
        # Extracting args
        prefix_length, option_model, feasibility_classifier, seed, (states, atoms_sequence, horizons, skeleton), cfg, debug_dir = args
        assert len(skeleton) > prefix_length
        CFG.__dict__.update(cfg.__dict__)
        CFG.seed = seed
        torch.set_num_threads(1) # Bug in pytorch with shared_memory being slow to use with more than one thread
        utils.set_global_seed(seed)

        logging.basicConfig(filename=os.path.join(debug_dir, f"{seed}.log"), force=True, level=logging.DEBUG)
        logging.info("Started negative data collection")
        logging.info(f"Skeleton: {[nsrt.name for nsrt in skeleton]}")
        logging.info(f"Starting Depth {prefix_length}")

        # Running backtracking
        def search_stop_condition(current_depth: int, tree: BacktrackingTree) -> bool:
            if current_depth < prefix_length:
                if tree.num_tries >= CFG.sesame_max_samples_per_step or tree.is_successful or \
                    [mb_subtree for _, mb_subtree in tree.failed_tries if mb_subtree is not None]:
                    logging.info(f"Finishing search on highest depth {current_depth}, {tree.num_tries} "
                                f"tries, {CFG.sesame_max_samples_per_step} max samples")
                return tree.num_tries >= CFG.sesame_max_samples_per_step or tree.is_successful or \
                    [mb_subtree for _, mb_subtree in tree.failed_tries if mb_subtree is not None]
            if tree.num_tries >= CFG.sesame_max_samples_per_step or tree.is_successful:
                logging.info(f"Finishing search on depth {current_depth}, {tree.num_tries} "
                             f"tries, {CFG.sesame_max_samples_per_step} max samples")
            return tree.num_tries >= CFG.sesame_max_samples_per_step or tree.is_successful
        backtracking, _ = run_backtracking_for_data_generation(
            previous_states = states[:prefix_length],
            goal = atoms_sequence[-1],
            option_model = option_model,
            skeleton = skeleton,
            feasibility_classifier = feasibility_classifier,
            atoms_sequence = atoms_sequence,
            search_stop_condition = search_stop_condition,
            seed = seed,
            timeout = float('inf'),
            metrics = {},
            max_horizon = horizons[prefix_length],
        )
        next_success_states = [
            subtree.state
            for _, subtree, _ in backtracking.successful_tries
        ]
        next_failed_states = [
            mb_subtree.state
            for _, mb_subtree in backtracking.failed_tries if mb_subtree is not None
        ]
        option_params = [
            option.params
            for option, mb_subtree in backtracking.failed_tries if mb_subtree is not None
        ]
        if backtracking.is_successful:
            successful_states, _ = backtracking.successful_trajectory
            augmentation_datapoints = [states[:prefix_length] + successful_states[1:]]
        else:
            augmentation_datapoints = []

        positive_datapoints = [
            states[:prefix_length] + [next_state] for next_state in next_success_states
        ]
        negative_datapoints = [
            states[:prefix_length] + [next_state] for next_state in next_failed_states
        ]
        # if next_success_states + next_failed_states:
        #     fig = Shelves2DEnv.render_state_plt((next_success_states + next_failed_states)[0], None)
        #     fig.savefig(os.path.join(debug_dir, f"{seed}.pdf"))
        #     plt.close()
        # logging.info(f"Negative datapoint classification - {[shelves2d_ground_truth_classifier(path, skeleton) for path in negative_datapoints]}")

        logging.info(f"Finished negative data collection - {next_failed_states} samples found")
        logging.info(f"Option params: {option_params}")
        return positive_datapoints, negative_datapoints, augmentation_datapoints

    def _run_sesame_plan(
        self,
        task: Task,
        nsrts: Set[NSRT],
        preds: Set[Predicate],
        timeout: float,
        seed: int
    ) -> Tuple[List[_Option], List[_GroundNSRT], Metrics]:
        end = time.perf_counter() + timeout

        # self._test_tasks_ran += 1
        # test_debug_dir = os.path.join(CFG.feasibility_debug_directory, "test-visualizations", f"test-{self._test_tasks_ran}")
        # os.makedirs(test_debug_dir, exist_ok=True)

        init_atoms = utils.abstract(task.init, preds)
        objects = set(task.init)

        ground_nsrts, reachable_atoms = task_plan_grounding(init_atoms, objects, nsrts)
        heuristic = utils.create_task_planning_heuristic(
            heuristic_name = CFG.sesame_task_planning_heuristic,
            init_atoms = init_atoms,
            goal = task.goal,
            ground_ops = ground_nsrts,
            predicates = preds,
            objects = objects,
        )
        generator = task_plan(
            init_atoms = utils.abstract(task.init, preds),
            goal = task.goal,
            ground_nsrts = ground_nsrts,
            reachable_atoms = reachable_atoms,
            heuristic = heuristic,
            seed = seed,
            timeout = timeout,
            max_skeletons_optimized = CFG.horizon,
        )
        partial_refinements = []
        for _ in range(CFG.sesame_max_skeletons_optimized):
            skeleton, backtracking, timed_out = None, None, False
            try:
                skeleton, atoms_seq, metrics = next(generator)
                backtracking, is_success = run_low_level_search(
                    task = task,
                    option_model =self._option_model,
                    skeleton = skeleton,
                    feasibility_classifier = self._feasibility_classifier,
                    atoms_sequence = atoms_seq,
                    seed = seed, timeout = end - time.perf_counter(),
                    metrics = metrics,
                    max_horizon = CFG.horizon
                )
                # if not is_success:
                #     traj, _ = backtracking.longest_failuire
                #     run_visualization_saving(
                #         [InterleavedBacktrackingDatapoint(
                #             states = traj,
                #             atoms_sequence = atoms_seq,
                #             horizons = [100000000000] * (len(skeleton) + 2),
                #             skeleton = skeleton,
                #         )], self._option_model, seed, len(traj) - 1, test_debug_dir, self._feasibility_classifier
                #     )
                if is_success:
                    _, options = backtracking.successful_trajectory
                    return options, skeleton, metrics
            except StopIteration:
                break
            except PlanningTimeout as e:
                backtracking = e.info.get('backtracking_tree')
                timed_out = True
            if skeleton is not None and backtracking is not None:
                _, plan = backtracking.longest_failuire
                partial_refinements.append((skeleton, plan))
            if timed_out:
                raise ApproachTimeout(
                    "Planning timed out!",
                    info = {"partial_refinements": partial_refinements}
                )
        raise ApproachFailure("Failed to find a successful backtracking")

    def _learn_neural_feasibility_classifier(
        self, min_inference_prefix: int, training_snapshot_directory: str = "",
    ) -> None:
        """Running training on a fresh classifier

        Params:
            max_inference_prefix - what is the shortest prefix (number of decoder NSRTs)
                which the classifier will attempt to classify
            shared_memory - whether to make the classifier movable between processes
                (using the torch.multiprocessing library)
        """
        neural_feasibility_classifier = NeuralFeasibilityClassifier(
            nsrts = self._nsrts,
            featurizer_sizes = CFG.feasibility_featurizer_sizes,
            positional_embedding_size = CFG.feasibility_embedding_size,
            positional_embedding_concat = CFG.feasibility_embedding_concat,
            mark_failing_nsrt = CFG.feasibility_mark_failing_nsrt,
            token_size = CFG.feasibility_token_size,
            transformer_num_heads = CFG.feasibility_num_heads,
            transformer_encoder_num_layers = CFG.feasibility_enc_num_layers,
            transformer_decoder_num_layers = CFG.feasibility_dec_num_layers,
            transformer_ffn_hidden_size = CFG.feasibility_ffn_hid_size,
            cls_style = CFG.feasibility_cls_style,
            embedding_horizon = CFG.feasibility_embedding_max_idx,
            max_train_iters = CFG.feasibility_max_itr,
            general_lr = CFG.feasibility_general_lr,
            transformer_lr = CFG.feasibility_transformer_lr,
            min_inference_prefix = min_inference_prefix,
            threshold_recalibration_percentile = CFG.feasibility_threshold_recalibration_percentile,
            use_torch_gpu = CFG.use_torch_gpu,
            optimizer_name = CFG.feasibility_optim,
            l1_penalty = CFG.feasibility_l1_penalty,
            l2_penalty = CFG.feasibility_l2_penalty,
        )
        neural_feasibility_classifier.fit(self._train_feasibility_dataset, self._validation_feasibility_dataset, training_snapshot_directory)
        self._feasibility_classifier = neural_feasibility_classifier