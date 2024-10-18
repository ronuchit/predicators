"""Example command line: export OPENAI_API_KEY=<your API key>

Example run:     python scripts/run_interactive_yaml.py -c
vlm_predicate_cover.yaml
"""
import ast
import sys
import base64
import errno
import importlib.util
import inspect
import itertools
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import textwrap
import time
import traceback
from collections import defaultdict, namedtuple
from copy import deepcopy
from inspect import getsource
from pprint import pformat
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Sequence, \
    Set, Tuple

import dill
import imageio
import numpy as np
from gym.spaces import Box
from PIL import Image, ImageDraw, ImageFont
from tabulate import tabulate
from tqdm import tqdm

from predicators import utils
from predicators.approaches import ApproachFailure, ApproachTimeout
from predicators.approaches.grammar_search_invention_approach import \
    _create_grammar, _GivenPredicateGrammar
from predicators.approaches.nsrt_learning_approach import NSRTLearningApproach
from predicators.approaches.oracle_approach import OracleApproach
from predicators.cogman import CogMan
from predicators.envs import BaseEnv
from predicators.execution_monitoring import create_execution_monitor
from predicators.ground_truth_models import get_gt_nsrts
from predicators.perception import create_perceiver
from predicators.predicate_search_score_functions import \
    _ClassificationErrorScoreFunction, _PredicateSearchScoreFunction, \
    create_score_function
from predicators.pretrained_model_interface import VisionLanguageModel
from predicators.settings import CFG
from predicators.structs import NSRT, Action, AnnotatedPredicate, Dataset, \
    GroundAtomTrajectory, GroundOptionRecord, LowLevelTrajectory, Object, \
    Optional, ParameterizedOption, Predicate, State, Task, Type, _Option, \
    _TypedEntity, ConceptPredicate
from predicators.utils import EnvironmentFailure, OptionExecutionFailure, \
    get_value_from_tuple_key, has_key_in_tuple_key, option_plan_to_policy

import_str = """
from predicators.settings import CFG
import numpy as np
from typing import Sequence, Set
from predicators.structs import State, Object, Predicate, Type, \
    ConceptPredicate, GroundAtom
from predicators.utils import RawState, NSPredicate
"""
sys.setrecursionlimit(10000)  # Example value, adjust as needed

def handle_remove_error(func, path, exc_info):
    # Check if the error is a permission error
    if not os.access(path, os.W_OK):
        # Change the permissions of the directory or file
        os.chmod(path, stat.S_IWUSR)
        # Retry the operation
        func(path)
    else:
        raise


PlanningResult = namedtuple("PlanningResult", ['succeeded', 'info'])


def are_equal_by_obj(list1: List[_Option], list2: List[_Option]) -> bool:
    if len(list1) != len(list2):
        return False

    return all(
        option1.eq_by_obj(option2) for option1, option2 in zip(list1, list2))


def print_confusion_matrix(tp: float, tn: float, fp: float, fn: float) -> None:
    """Compate and print the confusion matrix."""
    precision = round(tp / (tp + fp), 2) if tp + fp > 0 else 0
    recall = round(tp / (tp + fn), 2) if tp + fn > 0 else 0
    specificity = round(tn / (tn + fp), 2) if tn + fp > 0 else 0
    accuracy = round(
        (tp + tn) / (tp + tn + fp + fn), 2) if tp + tn + fp + fn > 0 else 0
    f1_score = round(2 * (precision * recall) /
                     (precision + recall), 2) if precision + recall > 0 else 0

    table = [[
        "",
        "Positive",
        "Negative",
        "Precision",
        "Recall",
        "Specificity",
        "Accuracy",
        "F1 Score",
    ], ["True", tp, tn, "", "", "", "", ""],
             ["False", fp, fn, "", "", "", "", ""],
             ["", "", "", precision, recall, specificity, accuracy, f1_score]]
    logging.info(tabulate(table, headers="firstrow", tablefmt="fancy_grid"))


# Function to encode the image
def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')


def add_python_quote(text: str) -> str:
    return f"```python\n{text}\n```"


def d2s(dict_with_arrays: Dict) -> str:
    # Convert State data with numpy arrays to lists, and to string
    return str({
        k: [round(i, 2) for i in v.tolist()]
        for k, v in dict_with_arrays.items()
    })


class VlmInventionApproach(NSRTLearningApproach):
    """Predicate Invention with VLMs."""

    def __init__(self, initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task],
                 ) -> None:
        super().__init__(initial_predicates, initial_options, types,
                         action_space, train_tasks)
        # Initial Predicates
        nsrts = get_gt_nsrts(CFG.env, self._initial_predicates,
                             self._initial_options)
        self._nsrts = nsrts

        self._learned_predicates: Set[Predicate] = set()
        # self._candidates: Set[Predicate] = set()
        self._num_inventions = 0
        # Set up the VLM
        self._gpt4o = utils.create_vlm_by_name("gpt-4o",
                                               system_instruction=\
"""You are interacting with a PhD student in AI who is passionate about learning new things and aims to change the world in significant ways to make it a better place. Respond in a manner that is insightful, critical, precise, and concise.""")
        self._vlm = utils.create_vlm_by_name(CFG.vlm_model_name)
        self._type_dict = {type.name: type for type in self._types}

    @classmethod
    def get_name(cls) -> str:
        return "vlm_online_invention"

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        if len(dataset.trajectories) > 0:
            # TODO: add data to the approach's dataset
            pass
        else:
            pass

    def _get_current_predicates(self) -> Set[Predicate]:
        """Get the current set of primitive predicates.
        """
        return self._initial_predicates | self._learned_predicates

    def _get_current_primitive_predicates(self) -> Set[Predicate]:
        """Get the current set of primitive predicates.
        """
        return self._get_current_predicates() -\
            self._get_current_concept_predicates()

    def load(self, online_learning_cycle: Optional[int]) -> None:
        super().load(online_learning_cycle)

        preds, _ = utils.extract_preds_and_types(self._nsrts)
        self._learned_predicates = (set(preds.values()) -
                                    self._initial_predicates)

    def _solve_tasks(self, env: BaseEnv, tasks: List[Task], ite: int) -> \
        List[PlanningResult]:
        """When return_trajctories is True, return the dataset of trajectories
        otherwise, return the results of solving the tasks (succeeded/failed
        plans)."""
        results = []
        trajectories = []
        for idx, task in enumerate(tasks):
            logging.debug(f"Ite {ite}. Solving Task {idx}")
            # logging.debug(f"Init: {init_atoms} \nGoals: {task.goal}")
            # task.init.labeled_image.save(f"images/trn_{idx}_init.png")
            try:
                policy = self.solve(task, timeout=CFG.timeout)
            except (ApproachTimeout, ApproachFailure) as e:
                logging.debug(f"Planning failed: {str(e)}")
                if "metrics" not in e.info:
                    # In the case of not dr-reachable
                    metrics, p_ref = None, []
                else:
                    metrics = e.info["metrics"],
                    p_ref = e.info["partial_refinements"]
                result = PlanningResult(succeeded=False,
                                        info={
                                            "metrics": metrics,
                                            "partial_refinements": p_ref,
                                            "error": str(e)
                                        })
            else:
                # logging.info(f"--> Succeeded")
                # This is essential, otherwise would cause errors
                # policy = utils.option_plan_to_policy(self._last_plan)

                result = PlanningResult(
                    succeeded=True,
                    info={
                        "option_plan": self._last_plan,
                        "nsrt_plan": self._last_nsrt_plan,
                        "metrics": self._last_metrics,
                        "partial_refinements": self._last_partial_refinements,
                        # "policy": policy
                    })
                # Collect trajectory
                # try:
                traj, _ = utils.run_policy(
                    policy,
                    env,
                    "train",
                    idx,
                    termination_function=lambda s: False,
                    max_num_steps=CFG.horizon,
                    exceptions_to_break_on={
                        utils.OptionExecutionFailure, ApproachFailure
                    })
                # except:
                #     breakpoint()
                self.task_to_latest_traj[idx] = LowLevelTrajectory(
                    traj.states,
                    traj.actions,
                    _is_demo=True,
                    _train_task_idx=idx)
                # trajectories.append(traj)
            results.append(result)
        # dataset = Dataset(trajectories)
        return results

    def learn_from_tasks(self, env: BaseEnv, tasks: List[Task]) -> None:
        """Learn from interacting with the offline dataset."""
        # for i, task in enumerate(tasks):
        #     img_dir = os.path.join(CFG.log_file, "images")
        #     os.makedirs(img_dir, exist_ok=True)
        #     # task.init.state_image.save(
        #     #     os.path.join(img_dir, f"init_unlab{i}.png"))
        #     task.init.labeled_image.save(
        #         os.path.join(img_dir, f"init_label{i}.png"))
        for i, task in enumerate(tasks):
            logging.debug(f"Task {i}:\n{task.init.pretty_str()}")
        self.env = env
        self.env_name = env.get_name()
        num_tasks = len(tasks)
        propose_ite = 1
        max_invent_ite = CFG.vlm_invention_max_invent_ite
        self.manual_prompt = False
        self.regenerate_response = True
        # solve_rate, prev_solve_rate = 0.0, np.inf  # init to inf
        best_solve_rate, best_ite, clf_acc = -np.inf, 0.0, 0.0
        clf_acc_at_best_solve_rate = 0.0
        num_failed_plans_at_best_solve_rate = np.inf
        best_nsrt, best_preds = deepcopy(self._nsrts), set()
        self._learned_predicates = set()
        # Keep a copy in case it doesn't learn it from data.
        self._init_nsrts = deepcopy(self._nsrts)
        no_improvement = False
        self.state_cache: Dict[int, RawState] = {}
        self.base_prim_candidates: Set[Predicate] =\
            self._initial_predicates.copy()
        self.cnpt_pred_candidates: Set[ConceptPredicate] = set()
        self.env_source_code = getsource(env.__class__)

        # Init data collection
        logging.debug(f"Initial predicates: {self._get_current_predicates()}")
        logging.debug(f"Initial operators: {pformat(self._init_nsrts)}")

        # For storing the results found at every iteration
        self.task_to_latest_traj: Dict[int, LowLevelTrajectory] = dict()
        # For help checking if a new plan is unique, to control data collection
        self.task_to_plans: Dict[int, List[_Option]] = defaultdict(list)
        # Organize a dataset for operator learning. This becomes the operator
        # learning dataset when the trajectories are put together.
        self.task_to_trajs: Dict[int, List[LowLevelTrajectory]] = \
            defaultdict(list)
        # Storing the prefix of partial trajectories
        self.task_to_partial_trajs: Dict[int, List[LowLevelTrajectory]] = \
            defaultdict(list)

        # Return the results and populate self.task_to_latest_traj
        num_init_nsrts = len(self._nsrts)
        self._nsrts = utils.reduce_nsrts(self._nsrts)
        num_reduced_nsrts = num_init_nsrts - len(self._nsrts)
        self._reduced_nsrts = deepcopy(self._nsrts)
        self._previous_nsrts = deepcopy(self._nsrts)
        logging.debug(f"Initial operators after pruning {num_reduced_nsrts}:\n"
                      f"{pformat(self._nsrts)}")
        results = self.collect_dataset(0, env, tasks)
        num_solved = sum([r.succeeded for r in results])
        num_failed_plans = prev_num_failed_plans =\
            num_failed_plans_at_best_solve_rate = sum(
                [len(r.info['partial_refinements']) for r in results])
        solve_rate = prev_solve_rate = best_solve_rate = num_solved / num_tasks
        logging.info(f"===ite 0; no invent solve rate {solve_rate}; "
                     f"num skeletons failed {num_failed_plans}\n")
        self.succ_optn_dict: Dict[str, GroundOptionRecord] =\
            defaultdict(GroundOptionRecord)
        self.fail_optn_dict: Dict[str, GroundOptionRecord] =\
            defaultdict(GroundOptionRecord)

        for ite in range(1, max_invent_ite + 1):
            logging.info(f"===Starting iteration {ite}...")
            # Reset at every iteration
            if CFG.reset_optn_state_dict_at_every_ite:
                self.succ_optn_dict = defaultdict(GroundOptionRecord)
                self.fail_optn_dict = defaultdict(GroundOptionRecord)
            # This will update self.task_to_tasjs
            if CFG.vlm_predicator_oracle_explore:
                if ite == 1:
                    self._collect_oracle_data(env, tasks)
            else:
                self._process_interaction_result(env,
                                                 results,
                                                 tasks,
                                                 ite,
                                                 use_only_first_solution=False)
            if ite == 1:
                n_tp = sum(
                    [len(v.states) for v in self.succ_optn_dict.values()])
                n_fp = sum(
                    [len(v.states) for v in self.fail_optn_dict.values()])
                prev_clf_acc = n_tp / (n_tp + n_fp)
            #### End of data collection

            # Invent when no improvement in solve rate
            self._prev_learned_predicates: Set[Predicate] =\
                self._learned_predicates

            all_trajs = []
            # needs to be improved
            if CFG.use_partial_plans_prefix_as_demo and num_solved == 0 and\
                not CFG.vlm_predicator_oracle_explore:
                # When oracle explore, there are full demo strajectories
                logging.info(f"Learning from only failed plans")
                iterator = self.task_to_partial_trajs.items()
            else:
                logging.info(f"Learning from only full solution trajectories.")
                iterator = self.task_to_trajs.items()
            for _, trajs in iterator:
                for traj in trajs:
                    all_trajs.append(traj)
            logging.info(f"Learning from {len(all_trajs)} trajectories.")

            if ite == 1 or no_improvement:  # or add_new_proposal_at_every_ite:
                if CFG.vlm_invention_alternate_between_p_ad:
                    if CFG.env in ["pybullet_balance"]:
                        CFG.vlm_invention_propose_nl_properties =\
                            propose_ite % 2 == 1
                    else:
                        CFG.vlm_invention_propose_nl_properties =\
                            propose_ite % 2 == 0
                logging.info("Proposing predicates mainly based on effect: "
                             f"{not CFG.vlm_invention_propose_nl_properties}")
                logging.info("Accquiring new predicates...")
                # Invent only when there is no improvement in solve rate
                # Or when add_new_proposal_at_every_ite is True
                prim_pred_proposals, cnpt_pred_proposals =\
                      self._get_predicate_proposals(env, tasks, ite)
                logging.info(
                    f"Done: created "
                    f"{len(prim_pred_proposals | cnpt_pred_proposals)} "
                    f"candidates:\n{prim_pred_proposals | cnpt_pred_proposals}")
                propose_ite += 1

            # Select the predicates to keep
            self._learned_predicates = self._select_proposed_predicates(
                                            all_trajs,
                                            best_solve_rate,
                                            ite,
                                            prim_pred_proposals, 
                                            cnpt_pred_proposals,
                                            )

            # Finally, learn NSRTs using all the selected predicates
            # When there is successful trajectories, maybe also use the positive
            # data to learn the operators?
            logging.debug(f"has negative states for "
                          f"{list(self.fail_optn_dict.keys())}")
            # The classification accuracy for the current nsrts.
            score_dict, _, _ = utils.count_classification_result_for_ops(
                                self._nsrts,
                                self.succ_optn_dict,
                                self.fail_optn_dict,
                                return_str=False,
                                initial_ite=False,
                                print_cm=True)

            self._learn_nsrts(all_trajs,
                              online_learning_cycle=None,
                              annotations=None,
                              fail_optn_dict=self.fail_optn_dict,
                              score_dict=score_dict)

            # Use the old NSRTs for an option if it had accuracy 1.0 in 
            # score_dict
            # TODO: maybe change to only use the old ones if the new ones are 
            # worse by some metrics (e.g., accuracy).
            if CFG.use_old_nsrt_if_new_is_worse:
                new_nsrts = set()
                for nsrt_candidate in self._nsrts:
                    # prev_nsrts_w_same_optn = {nsrt for nsrt in self._previous_nsrts
                    #                         if nsrt.option == nsrt_candidate.option}
                    # prev_empty_precon = all(n.preconditions == set() for n in 
                    #                         prev_nsrts_w_same_optn)
                    if score_dict[str(nsrt_candidate.option)]['acc'] == 1.0:
                        # Use the old one if it helps with planning
                        # logging.debug(f"Old NSRTs have empty precon {prev_empty_precon}")
                        logging.debug(f"Using old nsrt for {nsrt_candidate.option}")
                        for old_nsrt in self._previous_nsrts:
                            if old_nsrt.option == nsrt_candidate.option:
                                new_nsrts.add(old_nsrt)
                        # new_nsrts = prev_nsrts_w_same_optn
                    else:
                        new_nsrts.add(nsrt_candidate)
                self._nsrts = new_nsrts

            # How about instead we loop through the previous ones and only use
            # the new ones if the old ones doesn't have accuracy 1?

            # Add init_nsrts whose option isn't in the current nsrts to
            # Is this sufficient? Or should I add back all the operators?
            # Because if it only learned move to one then can it use it to do
            # move to two?
            cur_options = [nsrt.option for nsrt in self._nsrts]
            # When starting to use complete trajectory to learn operators,
            # add the previous nsrts whose option is no longer in the current
            # nsrt, e.g. (twist when the tasks with non-twist jug is solved).
            for p_nsrt in self._previous_nsrts:
                if not p_nsrt.option in cur_options:
                    logging.debug(f"Adding back nsrt: {pformat(p_nsrt)}")
                    # self._nsrts.add(p_nsrt)
                    self._nsrts.add(
                        NSRT(f"Op{len(self._nsrts)}", p_nsrt.parameters,
                             p_nsrt.preconditions, p_nsrt.add_effects,
                             p_nsrt.delete_effects, p_nsrt.ignore_effects,
                             p_nsrt.option, p_nsrt.option_vars,
                             p_nsrt._sampler))

            # Add the initial nsrts back to the nsrts
            # for p_nsrts in self._init_nsrts:
            #     if not p_nsrts.option in cur_options:
            #         self._nsrts.add(p_nsrts)
            # self._nsrts |= self._reduced_nsrts
            logging.info("\nAll NSRTs after learning:")
            for nsrt in self._nsrts:
                logging.info(nsrt)
            logging.info("")

            # Set the predicates to be the ones that are used in operators

            # Collect Data again
            # Set up load/save filename for interaction dataset
            # Add the auxiliary concepts to self._learned_predicates here?
            results = self.collect_dataset(ite, env, tasks)
            num_solved = sum([r.succeeded for r in results])
            num_failed_plans = sum(
                [len(r.info['partial_refinements']) for r in results])
            solve_rate = num_solved / num_tasks

            # Print the new classification results with the new operators
            score_dict, _, _ = utils.count_classification_result_for_ops(
                self._nsrts,
                self.succ_optn_dict,
                self.fail_optn_dict,
                return_str=False,
                initial_ite=False,
                print_cm=True)
            clf_acc = score_dict['overall']['acc']

            no_improvement = solve_rate <= prev_solve_rate
            # no_improvement &= clf_acc <= prev_clf_acc
            if solve_rate == prev_solve_rate:
                no_improvement &= num_failed_plans >= prev_num_failed_plans
            logging.info(f"\n===ite {ite} finished. "
                         f"No improvement={no_improvement}\n"
                         f"Solve rate {num_solved / num_tasks} "
                         f"Prev solve rate {prev_solve_rate}\n"
                         f"Num skeletons failed {num_failed_plans} "
                         f"Prev num skeletons failed {prev_num_failed_plans}\n"
                         f"Clf accuracy: {clf_acc:.2f}. "
                         f"Prev clf accuracy: {prev_clf_acc:.2f}\n")

            # breakpoint()
            # Save the best model
            if solve_rate > best_solve_rate or\
               (solve_rate == best_solve_rate and\
                num_failed_plans < num_failed_plans_at_best_solve_rate):
                best_solve_rate = solve_rate
                clf_acc_at_best_solve_rate = clf_acc
                best_ite = ite
                best_nsrt = self._nsrts
                best_preds = self._learned_predicates
                num_failed_plans_at_best_solve_rate = num_failed_plans
            prev_solve_rate = solve_rate
            prev_clf_acc = clf_acc
            prev_num_failed_plans = num_failed_plans
            self._previous_nsrts = deepcopy(self._nsrts)
            if solve_rate == 1 or (num_failed_plans == 0
                                   and solve_rate == best_solve_rate):
                # if solve_rate == 1:
                if CFG.env in [
                        # "pybullet_coffee"
                        ]:
                    # these are harder
                    if num_failed_plans / num_tasks < 1:
                        break
                else:
                    # if CFG.env in ["pybullet_cover_typed_options"]:
                    if num_failed_plans == 0 and solve_rate == best_solve_rate:
                        break
            time.sleep(5)

        logging.info("Invention finished.")
        logging.info(
            f"\nBest solve rate {best_solve_rate} and num_failed_plan "
            f"{num_failed_plans_at_best_solve_rate} first achieved at ite "
            f"{best_ite}; clf accuracy {clf_acc_at_best_solve_rate}")
        logging.info(f"Predicates learned {best_preds}")
        logging.info(f"NSRTs learned {pformat(best_nsrt)}")
        # breakpoint()
        self._nsrts = best_nsrt
        self._learned_predicates = best_preds
        return

    def _get_predicate_proposals(
        self,
        env: BaseEnv,
        tasks: List[Task],
        ite: int,
        # all_trajs: List[LowLevelTrajectory],
    ) -> Tuple[Set[Predicate], Set[ConceptPredicate]]:
        """Get predicate proposals either by using oracle or VLM.
        """

        if CFG.vlm_predicator_oracle_base_grammar:
            # Get proposals from oracle
            if CFG.neu_sym_predicate:
                # If using the oracle predicates
                # With NSP, we only want the GT NSPs besides the initial
                # predicates
                # Want to remove the predicates of the same name
                # Currently assume this is correct
                primitive_preds = env.ns_predicates - self._initial_predicates 
            else:
                primitive_preds = env.oracle_proposed_predicates -\
                                    self._initial_predicates
            concept_preds = env.concept_predicates -\
                    self._initial_concept_predicates
        else:
            # Get proposals from VLM
            primitive_preds, concept_preds = self._get_proposals_from_vlm(
                env, ite, tasks)
        return primitive_preds, concept_preds

    def _load_images_from_directory(self, directory: str):
        images = []
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)
            if filename.lower().endswith(('.png', '.jpg')):
                img = Image.open(file_path)
                images.append(img)
        return images

    def _collect_oracle_data(self, env: BaseEnv, tasks: List[Task]) -> None:
        """Collect oracle dataset by first finding oracle plans, and use the gt
        operators to identify negative states. And add the success trajectories
        to self.task_to_trajs.

        This is just used for the oracle explore model.
        """
        logging.info("Generating oracle explore data...")
        # Get the success nsrt and option plan
        options = self._initial_options
        oracle_approach = OracleApproach(
            env.predicates,
            options,
            env.types,
            env.action_space,
            tasks,
            task_planning_heuristic=CFG.offline_data_task_planning_heuristic,
            max_skeletons_optimized=CFG.offline_data_max_skeletons_optimized,
            bilevel_plan_without_sim=CFG.offline_data_bilevel_plan_without_sim)
        perceiver = create_perceiver(CFG.perceiver)
        execution_monitor = create_execution_monitor(CFG.execution_monitor)
        cogman = CogMan(oracle_approach, perceiver, execution_monitor)

        # Get the traj and positive states
        results = []
        for idx, task in enumerate(tasks):
            env_task = env.get_train_tasks()[idx]
            cogman.reset(env_task)
            nsrt_plan = cogman._approach._last_nsrt_plan
            option_plan = cogman._approach._last_plan
            result = PlanningResult(succeeded=True,
                                    info={
                                        "option_plan": option_plan,
                                        "nsrt_plan": nsrt_plan,
                                        "partial_refinements": []
                                    })
            results.append(result)
        # use process_interaction_results to save the positive states?
        self._process_interaction_result(env, results, tasks, 1, False)

        # Use GT operators to get some negative states
        # For each successful ground option, get a couple of negative states
        # trajs: self.task_to_trajs
        # succ options: self.succ_optn_dict
        gt_nsrt = cogman._approach._nsrts
        gt_preds = env.predicates
        all_objects = set.union(*[set(t.init) for t in tasks])
        max_neg_states = 2
        for option_str in list(self.succ_optn_dict.keys()):
            all_gnsrts = itertools.chain.from_iterable(
                utils.all_ground_nsrts(nsrt, all_objects) for nsrt in gt_nsrt)
            # get the gt nsrts with the same option
            num_neg_states = 0
            option = self.succ_optn_dict[option_str].option
            optn_objs = self.succ_optn_dict[option_str].optn_objs
            optn_vars = self.succ_optn_dict[option_str].optn_vars
            consistent_gnsrts = [
                gnsrts for gnsrts in all_gnsrts
                if gnsrts.option.name == option.name
                and gnsrts.option_objs == optn_objs
            ]
            break_outer = False
            for _, rec in self.succ_optn_dict.items():
                for state in rec.states:
                    # if it's not satisfied by any gnsrts, add it to the fail
                    # dict
                    atom_state = utils.abstract(state, gt_preds)
                    # logging.debug(f"atom state: {atom_state}")
                    # logging.debug(f"consistent gnsrts: {consistent_gnsrts}")
                    if not any(
                            gnsrt.preconditions.issubset(atom_state)
                            for gnsrt in consistent_gnsrts):
                        # logging.debug(f"Found a neg state for {option_str}")
                        state.labeled_image.save(
                            os.path.join(
                                CFG.log_file,
                                f"images/{option_str}_neg{num_neg_states}.png")
                        )
                        neg_state = state.copy()
                        neg_state.next_state = None
                        self.fail_optn_dict[option_str].append_state(
                            neg_state,
                            utils.abstract(state,
                                           self._get_current_predicates()),
                            optn_objs, optn_vars, option)
                        num_neg_states += 1
                    if num_neg_states >= max_neg_states:
                        break_outer = True
                        break
                if break_outer:
                    break

    def _process_interaction_result(self, env: BaseEnv,
                                    results: List[PlanningResult],
                                    tasks: List[Task], ite: int,
                                    use_only_first_solution: bool) -> None:
        """Process the data obtained in solving the tasks into ground truth
        positive and negative states for the ground options.

        Deprecated:
        When add_intermediate_details == True, detailed interaction
        trajectories are added to the return string
        """
        logging.info("===Processing the interaction results...\n")
        num_tasks = len(tasks)
        if ite == 1:
            self.solve_log = [False] * num_tasks

        # Add a progress bar
        for i, _ in tqdm(enumerate(tasks),
                         total=num_tasks,
                         desc="Processing Interaction results"):
            result = results[i]

            if result.succeeded:
                # Found a successful plan
                nsrt_plan = result.info['nsrt_plan']
                option_plan = result.info['option_plan'].copy()
                logging.debug(
                    f"[ite {ite} task {i}] Processing succeeded " +
                    f"plan {[op.name + str(op.objects) for op in option_plan]}"
                )

                # Check before processing for some efficiency gain
                if use_only_first_solution:
                    if self.solve_log[i]:
                        # If the task has previously been solved
                        continue  # continue to logging the next task
                    else:
                        # Otherwise, update the log
                        self.solve_log[i] = True

                # Check if the current plan is novel; only log the
                # plan for predicate/operator learning if it's novel
                novel_plan = (i not in self.task_to_plans) or (not any(
                            are_equal_by_obj(option_plan, plan) for plan in
                            self.task_to_plans[i]))

                if novel_plan:
                    logging.warning(f"Found a novel plan for task {i}")
                    self.task_to_plans[i].append(option_plan.copy())

                    states, actions = self._execute_succ_plan_and_track_state(
                        env.reset(train_or_test='train', task_idx=i), env,
                        nsrt_plan, option_plan)

                    if i in self.task_to_trajs:
                        if len(states) < len(self.task_to_trajs[i][0].states):
                            logging.info("Replacing the previous plan with a "
                                        f"shorter one: {option_plan}")
                            self.task_to_trajs[i] = [
                                LowLevelTrajectory(states,
                                                actions,
                                                _is_demo=True,
                                                _train_task_idx=i)
                            ]
                        elif len(states) ==\
                            len(self.task_to_trajs[i][0].states):
                            logging.info("Adding an alternative plan of"
                                        f"the same length: {option_plan}")
                            self.task_to_trajs[i].append(
                                LowLevelTrajectory(states,
                                                actions,
                                                _is_demo=True,
                                                _train_task_idx=i))
                        else:
                            logging.info(f"Found a new plan {option_plan} but "
                                        "its longer than the previous solution")
                    else:
                        logging.info(f"Found the first plan {option_plan}")
                        self.task_to_trajs[i] = [
                            LowLevelTrajectory(states,
                                            actions,
                                            _is_demo=True,
                                            _train_task_idx=i)
                        ]

            # The failed refinements (negative samples)
            for p_idx, p_ref in enumerate(result.info['partial_refinements']):
                # failed partial plans
                option_plan = p_ref[1].copy()
                nsrt_plan = p_ref[0]
                logging.debug(f"[ite {ite} task {i}] Processing failed plan "\
                        f"{p_idx} of len {len(option_plan)}: "\
                        f"{[op.name + str(op.objects) for op in option_plan]}")
                failed_opt_idx = len(option_plan) - 1

                # As above, check if the p-plan is novel
                novel_pplan = (i not in self.task_to_plans) or\
                              (not any(are_equal_by_obj(option_plan, plan)
                                            for plan in self.task_to_plans[i]))
                if novel_pplan:
                    self.task_to_plans[i].append(option_plan.copy())
                else:
                    continue

                state = env.reset(train_or_test='train', task_idx=i)
                prev_state = None
                # Successful part
                if failed_opt_idx > 0:
                    states, actions = self._execute_succ_plan_and_track_state(
                        state,
                        env,
                        nsrt_plan,
                        option_plan[:-1],
                        ite=ite,
                        task=i,
                        p_idx=p_idx,
                    )
                    state = states[-1]
                    try:
                        prev_state = states[-2]
                    except:
                        breakpoint()
                    # Take the prefix of the pplan and use it to learn operators
                    if len(states) <= len(actions):
                        logging.warning("states is not 1 more than actions")

                    if CFG.use_partial_plans_prefix_as_demo:
                        self.task_to_partial_trajs[i].append(
                                            LowLevelTrajectory(states,
                                                            actions,
                                                            _is_demo=True,
                                                            _train_task_idx=i))

                # Failed part
                ppp = [o.simple_str() for o in option_plan[:-1]]
                _, _ = self._execute_succ_plan_and_track_state(
                    state,
                    env,
                    nsrt_plan[failed_opt_idx:],
                    option_plan[-1:],
                    failed_opt=True,
                    partial_plan_prefix=ppp,
                    prev_state=prev_state)
        logging.debug("Collected Positive states for "
                      f"{list(self.succ_optn_dict.keys())}")
        logging.debug("Collected Negative states for "
                      f"{list(self.fail_optn_dict.keys())}")

    def _execute_succ_plan_and_track_state(
        self,
        init_state: State,
        env: BaseEnv,
        nsrt_plan: List,
        option_plan: List,
        failed_opt: bool = False,
        ite: Optional[int] = None,
        task: Optional[int] = None,
        p_idx: Optional[int] = None,
        partial_plan_prefix: Optional[List[str]] = None,
        prev_state: Optional[State] = None
    ) -> Tuple[List[State], List[Action]]:
        """Similar to _execute_plan_and_track_state but only run in successful
        policy because we only need the initial state for the failed option.

        Return:
        -------
        states: List[State]
            The states before executing each option in the option plan and last
            state before returning.
        actions: List[Action]
            The first action from each option in the option plan. The length of
            this will be 1 less than the number of states.
        partial_plan_prefix: Optional[str] = None,
            For failed options, the list of option successfully executed before
            them.
        """
        state = init_state

        def policy(_: State) -> Action:
            raise OptionExecutionFailure("placeholder policy")

        steps = 0
        nsrt_counter = 0
        env_step_counter = 0
        states, actions = [], []
        first_option_action = False
        if failed_opt:
            state_hash = state.__hash__()
            if state_hash in self.state_cache:
                option_start_state = self.state_cache[state_hash].copy()
            else:
                option_start_state = env.get_observation(
                    render=CFG.vlm_predicator_render_option_state)
                if CFG.env_include_bbox_features:
                    option_start_state.add_bbox_features()
                option_start_state.option_history = partial_plan_prefix
                option_start_state.prev_state = prev_state
                self.state_cache[state_hash] = option_start_state.copy()
            g_nsrt = nsrt_plan[0]
            gop_str = g_nsrt.ground_option_str(
                use_object_id=CFG.vlm_predicator_render_option_state)
            # logging.debug(f"found neg states for {gop_str}")
            # logging.debug(f"have neg state for {self.fail_optn_dict.keys()}")
            self.fail_optn_dict[gop_str].append_state(
                option_start_state,
                utils.abstract(option_start_state,
                               self._get_current_predicates()),
                g_nsrt.option_objs, g_nsrt.parent.option_vars, g_nsrt.option)
        else:
            temp_optn_state_lst = []
            for steps in range(CFG.horizon):
                try:
                    act = policy(state)
                except OptionExecutionFailure as e:
                    # When the one-option policy reaches terminal state
                    # we're cetain the plan is successfully terminated
                    # because this is a successful plan.
                    if str(e) == "placeholder policy" or\
                    (str(e) == "Option plan exhausted!") or\
                    (str(e) == "Encountered repeated state."):
                        try:
                            option = option_plan.pop(0)
                            # logging.debug(f"Executing {option}")
                        except IndexError:
                            # When the option_plan is exhausted
                            # Rendering the final state for success traj
                            state_hash = state.__hash__()
                            if state_hash in self.state_cache:
                                option_start_state = self.state_cache[
                                    state_hash].copy()
                            else:
                                option_start_state = env.get_observation(
                                    render=CFG.
                                    vlm_predicator_render_option_state)
                                if CFG.env_include_bbox_features:
                                    option_start_state.add_bbox_features()
                                # add plan prefix
                                option_start_state.option_history = [
                                    n.ground_option_str(
                                        use_object_id=CFG.
                                        vlm_predicator_render_option_state)
                                    for n in nsrt_plan[:nsrt_counter]
                                ]
                                option_start_state.prev_state = states[-1] if\
                                    len(states) > 0 else None
                                # option_start_state.option_history = [
                                #     n.option.parameterized_annotation(
                                #     n.option_objs)
                                #     for n in nsrt_plan[:nsrt_counter]]
                                self.state_cache[
                                    state_hash] = option_start_state.copy()
                                temp_optn_state_lst.append(
                                    (option_start_state, None, None))
                            # For debugging incomplete options
                            states.append(option_start_state)
                            break
                        else:
                            # raise_error_on_repeated_state is set to true in simple
                            # environments, but causes the option to not finish in
                            # the pybullet environment, hence are disabled in
                            # testing neu-sym-predicates.
                            # We are okay with this because the failure options
                            # have been handled above.
                            policy = utils.option_plan_to_policy(
                                [option], raise_error_on_repeated_state=False)
                            # [option], raise_error_on_repeated_state=True)
                            state_hash = state.__hash__()
                            if state_hash in self.state_cache:
                                option_start_state = self.state_cache[
                                    state_hash].copy()
                            else:
                                option_start_state = env.get_observation(
                                    render=CFG.
                                    vlm_predicator_render_option_state)
                                # add plan prefix
                                option_start_state.option_history = [
                                    n.ground_option_str(
                                        use_object_id=CFG.
                                        vlm_predicator_render_option_state)
                                    for n in nsrt_plan[:nsrt_counter]
                                ]
                                option_start_state.prev_state = states[-1] if\
                                    len(states) > 0 else None
                                # option_start_state.option_history = [
                                #     n.option.parameterized_annotation(
                                #     n.option_objs)
                                #     for n in nsrt_plan[:nsrt_counter]]
                                if CFG.env_include_bbox_features:
                                    option_start_state.add_bbox_features()
                                self.state_cache[
                                    state_hash] = option_start_state.copy()
                            # option_start_state = env.get_observation(
                            #     render=CFG.vlm_predicator_render_option_state)
                            # logging.info("Start new option at step "+
                            #                 f"{env_step_counter}")
                            g_nsrt = nsrt_plan[nsrt_counter]
                            gop_str = g_nsrt.ground_option_str(
                                use_object_id=CFG.
                                vlm_predicator_render_option_state)
                            states.append(option_start_state)
                            first_option_action = True
                            # Save to a temp list to add next_state
                            temp_optn_state_lst.append(
                                (option_start_state, g_nsrt, gop_str))
                            # self.succ_optn_dict[gop_str].append_state(
                            #     option_start_state,
                            #     utils.abstract(option_start_state,
                            #                    self._get_current_predicates()),
                            #     g_nsrt.option_objs, g_nsrt.parent.option_vars,
                            #     g_nsrt.option)
                            nsrt_counter += 1
                    else:
                        break
                else:
                    state = env.step(act)
                    if first_option_action:
                        actions.append(act)
                        first_option_action = False
                    env_step_counter += 1

            # Add next state
            for i in range(len(temp_optn_state_lst) - 1):
                temp_optn_state_lst[i][0].next_state =\
                    temp_optn_state_lst[i+1][0]

            # Add the states to succ_optn_dict
            for state, g_nsrt, gop_str in temp_optn_state_lst[:-1]:
                self.succ_optn_dict[gop_str].append_state(
                    state, utils.abstract(state,
                                          self._get_current_predicates()),
                    g_nsrt.option_objs, g_nsrt.parent.option_vars,
                    g_nsrt.option)

            if steps == CFG.horizon - 1:
                logging.warning("Processing stopped as steps reach the max.")

        # logging.debug(f"Finish executing after {steps} steps in the loop.")
        return states, actions

    def collect_dataset(self, ite: int, env: BaseEnv,
                        tasks: List[Task]) -> List[PlanningResult]:

        ds_fname = utils.llm_pred_dataset_save_name(ite)
        if CFG.load_vlm_pred_invent_dataset and os.path.exists(ds_fname):
            with open(ds_fname, 'rb') as f:
                results = dill.load(f)
            logging.info(f"Loaded dataset from {ds_fname}\n")
        else:
            # Ask it to solve the tasks
            results = self._solve_tasks(env, tasks, ite)
            if CFG.save_vlm_pred_invent_dataset:
                os.makedirs(os.path.dirname(ds_fname), exist_ok=True)
                with open(ds_fname, 'wb') as f:
                    dill.dump(results, f)
                logging.info(f"Saved dataset to {ds_fname}\n")
        return results

    def _select_proposed_predicates(self, 
                            all_trajs: List[LowLevelTrajectory],
                            num_solved: int,
                            ite: int,
                            prim_pred_proposals: Set[Predicate], 
                            cnpt_pred_proposals: Optional[Set[
                                ConceptPredicate]]=None,
                                ) -> Set[Predicate]:
        """Select the predicates to keep from the proposed predicates.
        """
        if CFG.vlm_predicator_oracle_learned:
            selected_preds = prim_pred_proposals | cnpt_pred_proposals
        else:
            # Select a subset candidates by score optimization
            self.base_prim_candidates |= prim_pred_proposals

            ## Predicate Search
            # Optionally add grammar to the candidates
            all_candidates: Dict[Predicate, float] = {}
            if CFG.vlm_predicator_use_grammar:
                grammar = _create_grammar(dataset=Dataset(all_trajs),
                                            given_predicates=\
                            self.base_prim_candidates|self._initial_predicates)
            else:
                grammar = _GivenPredicateGrammar(
                    self.base_prim_candidates | self._initial_predicates)
            all_candidates.update(
                grammar.generate(
                    max_num=CFG.grammar_search_max_predicates))

            # Add concept predicates
            concept_preds_candidates = _GivenPredicateGrammar(
                cnpt_pred_proposals).generate(
                    max_num=CFG.grammar_search_max_predicates)
            all_candidates.update(concept_preds_candidates)

            # logging.debug(f"all candidates {pformat(all_candidates)}")
            # breakpoint()
            # Add a atomic states for succ_optn_dict and fail_optn_dict
            logging.info("[Start] Applying predicates to data...")
            if num_solved == 0:
                score_func_name = "operator_classification_error"
            else:
                score_func_name = "expected_nodes_created"
                # score_function = CFG.grammar_search_score_function

            # if score_func_name == "operator_classification_error":
            # Abstract here because it's used in the score function
            # Evaluate the newly proposed predicates; the values for
            # previous proposed should have been cached by the previous
            # abstract calls.
            # this is also used in cluster_intersect_and_search pre learner
            num_states = len(
                set(state for optn_dict in
                    [self.succ_optn_dict, self.fail_optn_dict]
                    for g_optn in optn_dict.keys()
                    for state in optn_dict[g_optn].states))
            logging.debug(f"There are {num_states} distinct states.")
            logging.debug(f"all candidates before filtering through abstract "
                          f"{all_candidates}")
            for optn_dict in [self.succ_optn_dict, self.fail_optn_dict]:
                for g_optn in optn_dict.keys():
                    atom_states = []
                    for state in optn_dict[g_optn].states:
                        # TODO: remove the candidates if we get an error
                        #   this can replace the error check in get proposals
                        atoms, valid_preds = utils.abstract(
                                                    state, 
                                                    set(all_candidates), 
                                                    return_valid_preds=True)
                        all_candidates = {k: v for k, v in 
                                    all_candidates.items() if k in valid_preds}
                        atom_states.append(atoms)
                    optn_dict[g_optn].abstract_states = atom_states
            logging.debug(f"all candidates after filtering through abstract "
                          f"{all_candidates}")

            # This step should only make VLM calls on the end state
            # becuaes it would have labled all the other success states
            # from the previous step.
            atom_dataset: List[GroundAtomTrajectory] =\
                        utils.create_ground_atom_dataset(all_trajs,
                                                        set(all_candidates))
            # breakpoint()
            logging.info("[Finish] Applying predicates to data....")

            # logging.info(f"[ite {ite}] compare abstract accuracy of "
            #              f"{self.base_prim_candidates}")
            # utils.compare_abstract_accuracy(
            #     [s for traj in all_trajs for s in traj.states],
            #     sorted(self.base_prim_candidates - self._initial_predicates),
            #     env.ns_to_sym_predicates)
            # logging.info(f"Abstract accuracy of for the failed states")
            # utils.compare_abstract_accuracy(
            #     list(
            #         set(state for optn_dict in [self.fail_optn_dict]
            #             for g_optn in optn_dict.keys()
            #             for state in optn_dict[g_optn].states)),
            #     sorted(self.base_prim_candidates - self._initial_predicates),
            #     env.ns_to_sym_predicates)

            if CFG.skip_selection_if_no_solve and num_solved == 0:
                logging.info("No successful trajectories and not using the"
                                "accuracy-based objective. Skip selection.")
                selected_preds = set(all_candidates)
            else:
                logging.info("[Start] Predicate search from " +
                                f"{self._initial_predicates}...")
                score_function = create_score_function(
                    score_func_name, self._initial_predicates,
                    atom_dataset, all_candidates, self._train_tasks,
                    self.succ_optn_dict, self.fail_optn_dict)
                start_time = time.perf_counter()
                selected_preds = \
                    self._select_predicates_by_score_optimization(
                        ite,
                        all_candidates,
                        score_function,
                        initial_predicates = self._initial_predicates)
                logging.info("[Finish] Predicate search.")
                logging.info(
                    "Total search time "
                    f"{time.perf_counter() - start_time:.2f} seconds")
        
        return selected_preds

    def _get_proposals_from_vlm(
        self,
        env: BaseEnv,
        # prompt: str,
        # images: List[Image.Image],
        ite: int,
        tasks: List[Task],
        # state_list_str: str = "",
    ) -> Tuple[Set[Predicate], Set[ConceptPredicate]]:

        # Phase 1: invent concept predicates from the existing predicates.
        #   only do it in ite 1 for convenience
        if CFG.vlm_invention_initial_concept_invention and ite == 1:
            helper_cnpt_preds = self._invent_initial_concept_predicates(ite, 
                                                                        env, 
                                                                        tasks)
        else:
            helper_cnpt_preds = set()

        # The intially proposed concept predicates are immediately added to the 
        #   base candidates
        self.cnpt_pred_candidates |= helper_cnpt_preds

        # Phase 2: invent other predicates building on existing ones.
        primitive_preds, concept_preds = self._invent_predicates_from_data(ite, 
                                                                        env, 
                                                                        tasks)
        # all_concept_preds = self.cnpt_pred_candidates | concept_preds
        self.cnpt_pred_candidates |= concept_preds 
        return primitive_preds, self.cnpt_pred_candidates

    def _invent_predicates_from_data(self, 
                                ite: int, 
                                env: BaseEnv, 
                                tasks: List[Task]
                            ) -> Tuple[Set[Predicate], Set[ConceptPredicate]]:
        phase_n = "from_data"
        # Create the first prompt.
        max_attempts = 10
        max_num_groundings, max_num_examples = 1, 1
        min_imgs, max_imgs = 6, 10
        show_when_acc_is_one = False

        obs_dir = os.path.join(CFG.log_file, f"ite{ite}_obs")
        if os.path.exists(obs_dir):
            shutil.rmtree(obs_dir, handle_remove_error)

        for attempt in range(max_attempts):
            logging.debug(f"Prompt creation attempt {attempt}")
            prompt, state_str = self._create_invention_prompt(
                                    env,
                                    ite,
                                    max_num_options=10,
                                    max_num_groundings=max_num_groundings,
                                    max_num_examples=max_num_examples,
                                    show_when_acc_is_one=show_when_acc_is_one,
                                    categories_to_show=['tp', 'fp'],
                                    phase_n=phase_n)

            # Load the images accompanying the prompt
            try:
                images = self._load_images_from_directory(obs_dir)
            except Exception as e:
                images = []
                if attempt == max_attempts - 1:
                    raise e


            logging.debug(f"Created {len(images)} images")
            if (min_imgs <= len(images) <= max_imgs) or\
               (attempt == max_attempts - 1): break

            if len(images) > max_imgs:
                if os.path.exists(obs_dir):
                    shutil.rmtree(obs_dir, handle_remove_error)
                if attempt % 2 == 0:
                    max_num_examples = max(1, max_num_examples - 1)
                else:
                    max_num_groundings = max(1, max_num_groundings - 1)
            else:
                # Adjust parameters for the next attempt
                if attempt % 2 == 0:
                    max_num_groundings += 1
                else:
                    max_num_examples += 1
                
                if attempt > 5 or (CFG.vlm_invention_propose_nl_properties and 
                    len(images) == 0):
                    logging.debug("Including options with acc 1.")
                    show_when_acc_is_one = True
        
        
        # Stage 0 (optional): get predicate proposals in natural language
        # from gpt4o
        if CFG.vlm_invention_propose_nl_properties:
            nl_proposal_f = CFG.log_file + f"ite{ite}_{phase_n}_s0.response"
            response = self._get_vlm_response(nl_proposal_f,
                                              self._gpt4o if CFG.env in [
                                                "pybullet_balance",
                                                "pybullet_cover_weighted"
                                              ] else self._vlm,
                                              prompt,
                                              images,
                                              cache_chat_session=True,
                                              temperature=0.5,
                                              seed=CFG.seed * (ite+1))
            # Prepare the chat history for Gemini
            self._vlm.chat_history = [{
                "role": "user",
                "parts": [prompt] + images
            }, {
                "role": "model",
                "parts": [response]
            }]

            # if self.env.get_name() == "pybullet_balance":
            #     response = response.split("\n")[:3]
            #     response = "\n".join(response)
            # Convert the NL proposals to formal predicate specs.
            template_f = "prompts/invent_0_nl_2_pred_spec.outline"
            with open(template_f, "r") as f:
                template = f.read()
            type_names = str(set(t.name for t in env.types))
            prompt = template.format(CONCEPT_PROPOSALS=response,
                                     TYPES_IN_ENV=type_names)
            # Save the text prompt
            with open(CFG.log_file + f"ite{ite}_{phase_n}_s1.prompt", 'w') as f:
                f.write(prompt)

        if CFG.vlm_invent_predicates_in_stages:
            save_file = CFG.log_file + f"ite{ite}_{phase_n}_s1.response"
        else:
            save_file = CFG.log_file + f"ite{ite}.response"

        # Stage 1: 
        #   Either convert the NL proposals to formal predicate specs;
        #   Or     prompt the VLM to implement predicates directly.
        response = self._get_vlm_response(
            save_file, 
            # self._vlm, 
            self._gpt4o if CFG.env in [
            "pybullet_balance",
            "pybullet_cover_weighted"
            ] else self._vlm,
            prompt,
            [] if CFG.vlm_invention_propose_nl_properties else images,
            temperature=0.3)

        # if CFG.vlm_invent_predicates_in_stages:
        predicate_specs = response

        # Stage 2: Implement the predicates
        # Either implement all the predicates at once,
        save_file = CFG.log_file + f"ite{ite}_{phase_n}_s2.response"
        
        if CFG.implement_predicates_at_once:
            s2_prompt = self._create_implementation_prompt(
                env, ite, state_str, predicate_specs, 
                save_fn=f"{phase_n}_s2")
            response = self._get_vlm_response(
                save_file,
                # self._vlm,
                self._gpt4o if CFG.env in [
                "pybullet_balance",
                "pybullet_cover_weighted"
                ] else self._vlm,
                s2_prompt,
                images)
        # Or implement N at a time.
        else:
            # 1. Parse the specs
            breakpoint()
            specs = response.split("\n")[2:-1]
            # 2. Implement N at a time
            # 3. Write the implementation in the save file


        # Stage 3: Parse and load the predicates
        primitive_preds, concept_preds = self._parse_predicate_predictions(
            save_file, tasks, ite, env.types, translate_fn=f"{phase_n}_s3")
        # breakpoint()
        return primitive_preds, concept_preds

    def _invent_initial_concept_predicates(self, ite: int,
            env: BaseEnv, tasks: List[Task]
            ) -> Tuple[Set[Predicate], Set[ConceptPredicate]]:
        template_f = "prompts/invent_0_initial_concept_invent.outline"
        with open(template_f, "r") as f:
            template = f.read()

        # Stage 1: Propose the predicates
        logging.info("Stage 1: Querying VLM for concept predicate proposal.")
        # Get existing predicates
        pred_str = self._create_pred_str(env,
                                        self.env_source_code,
                                        show_predicate_assertion=True,
                            include_all_candidates=True,
                            include_selected_predicates=False)
        # Get existing types
        # with open(f"./prompts/types_{self.env_name}.py", 'r') as f:
        #     type_instan_str = f.read()
        # type_instan_str = add_python_quote(type_instan_str)
        type_names = str(set(t.name for t in env.types))
        
        prompt = template.format(PREDICATES_IN_ENV=pred_str,
                                 TYPES_IN_ENV=type_names)
        
        # Log the prompt
        prompt_f = CFG.log_file + f"ite{ite}_init_cnpt_s1.prompt"
        with open(prompt_f, "w") as f:
            f.write(prompt)
        
        # Get the response -- Predicate specification
        save_f = CFG.log_file + f"ite{ite}_init_cnpt_s1.response"
        response = self._get_vlm_response(save_f, 
                                              self._gpt4o if CFG.env in [
                                                "pybullet_balance",
                                                # "pybullet_cover_weighted"
                                              ] else self._vlm,
                                            #   self._vlm, 
                                          prompt, [])

        # Stage 2: Implement the predicates
        # Get the implementation
        imp_prompt = self._create_implementation_prompt(env, ite, "", 
                                            response, 
                                            save_fn="init_cnpt_s2")
        save_file = CFG.log_file + f"ite{ite}_init_cnpt_s2.response"

        logging.info("Stage 2: Querying VLM for predicate implementation.")
        response = self._get_vlm_response(save_file,
                                          self._vlm,
                                          imp_prompt,
                                        )

        # Stage 3: Parse and load the predicates
        logging.info("Stage 3: Transforming concept predicates.")
        _, concept_preds =\
            self._parse_predicate_predictions(
            save_file, tasks, ite, env.types, translate_fn="init_cnpt_s3")
        return concept_preds

    def _select_predicates_by_score_optimization(
        self,
        ite: int,
        candidates: Dict[Predicate, float],
        score_function: _PredicateSearchScoreFunction,
        initial_predicates: Set[Predicate] = set(),
        atom_dataset: List[GroundAtomTrajectory] = [],
        train_tasks: List[Task] = [],
    ) -> Set[Predicate]:
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
                # pre_str = [p.name for p in (s | {predicate})]
                # if sorted(pre_str) == \
                #     sorted(["Clear", "Holding", "On", "OnTable"]):
                #     breakpoint()
                yield (None, frozenset(s | {predicate}), 1.0)
            # for predicate in sorted(s):  # determinism
            #     # Actions not needed. Frozensets for hashing. The cost of
            #     # 1.0 is irrelevant because we're doing GBFS / hill
            #     # climbing and not A* (because we don't care about the
            #     # path).
            #     yield (None, frozenset(set(s) - {predicate}), 1.0)

        # Start the search with no candidates.
        init: FrozenSet[Predicate] = frozenset(initial_predicates)
        # init: FrozenSet[Predicate] = frozenset(candidates.keys())

        # calculate the number of total combinations of all sizes
        num_combinations = 2**len(set(candidates))

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
                max_evals=CFG.grammar_search_gbfs_num_evals,
                full_search_tree_size=num_combinations,
            )
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
        # assert self._metrics.get("total_num_predicate_evaluations") is None
        self._metrics["total_num_predicate_evaluations"] = len(path) * len(
            candidates)

        # # Filter out predicates that don't appear in some operator
        # # preconditions.
        # logging.info("\nFiltering out predicates that don't appear in "
        #              "preconditions...")
        # preds = kept_predicates | initial_predicates
        # pruned_atom_data = utils.prune_ground_atom_dataset(atom_dataset, preds)
        # segmented_trajs = [
        #     segment_trajectory(ll_traj, set(preds), atom_seq=atom_seq)
        #     for (ll_traj, atom_seq) in pruned_atom_data
        # ]
        # low_level_trajs = [ll_traj for ll_traj, _ in pruned_atom_data]
        # preds_in_preconds = set()
        # for pnad in learn_strips_operators(low_level_trajs,
        #                                    train_tasks,
        #                                    set(kept_predicates
        #                                        | initial_predicates),
        #                                    segmented_trajs,
        #                                    verify_harmlessness=False,
        #                                    annotations=None,
        #                                    verbose=False):
        #     for atom in pnad.op.preconditions:
        #         preds_in_preconds.add(atom.predicate)
        # kept_predicates &= preds_in_preconds

        logging.info(
            f"\n[ite {ite}] Selected {len(kept_predicates)} predicates"
            f" out of {len(candidates)} candidates:")
        for pred in kept_predicates:
            logging.info(f"\t{pred}")
        score_function.evaluate(kept_predicates)  # log useful numbers
        # logging.info(f"\nSelected {len(kept_predicates)} predicates out of "
        #              f"{len(candidates)} candidates:")
        # for pred in kept_predicates:
        #     logging.info(f"\t{pred}")

        return set(kept_predicates)

    # def _create_invention_from_traj_prompt(
    #     self,
    #     env: BaseEnv,
    #     ite: int,
    #     trajs: List[LowLevelTrajectory],
    # ) -> str:
    #     """Invent predicates from state action trajectories; no negative
    #     states."""
    #     obs_dir = CFG.log_file + f"ite{ite}_obs/"
    #     os.makedirs(obs_dir, exist_ok=True)
    #     with open(f"prompts/invent_0_prog_free_from_traj.outline",
    #               'r') as file:
    #         template = file.read()

    #     # Predicates
    #     # If NSP, provide the GT goal NSPs, although they are never used.
    #     self.env_source_code = getsource(env.__class__)
    #     pred_str_lst = []
    #     pred_str_lst.append(
    #         self._create_pred_str(
    #             env,
    #             self.env_source_code,
    #             include_definition=False,
    #             show_predicate_assertion=True,
    #         ))
    #     # if ite > 1:
    #     #     pred_str_lst.append("The previously invented predicates are:")
    #     #     pred_str_lst.append(self._invented_predicate_str(ite))
    #     pred_str = '\n'.join(pred_str_lst)
    #     template = template.replace("[PREDICATES_IN_ENV]", pred_str)

    #     # Pick the longest trajectory
    #     # Filter trajectories where no consecutive actions are the same
    #     # def has_consecutive_equal_actions(traj):
    #     #     for i in range(len(traj.actions) - 1):
    #     #         if traj.actions[i]._option.eq_by_obj(traj.actions[i + 1]._option):
    #     #             return True
    #     #     return False
    #     # filtered_trajs = [traj for traj in trajs if
    #     #                     not has_consecutive_equal_actions(traj)]

    #     # Filter trajectories where the first two actions are not the same
    #     filtered_trajs = [
    #         traj for traj in trajs if len(traj.actions) > 1
    #         and not traj.actions[0]._option.eq_by_obj(traj.actions[1]._option)
    #     ]

    #     # Pick the longest trajectory from the filtered list
    #     if filtered_trajs:
    #         traj = max(filtered_trajs, key=lambda traj: len(traj.states))
    #     else:
    #         traj = max(trajs, key=lambda traj: len(traj.states))
    #     logging.debug("Selected action traj: "
    #                   f"{[a._option.name for a in traj.actions]}")

    #     # Prepare the traj_str for the prompt
    #     traj_str = []
    #     state_str_lst = []
    #     for i, a in enumerate(traj.actions):
    #         # Append the state string
    #         s_name = "state_" + str(i)
    #         s = traj.states[i]
    #         traj_str.append(s_name + " with additional info:")
    #         state_str_lst.append(s_name + " with additional info:")
    #         state_dict_str = s.dict_str(
    #             indent=2,
    #             object_features=not CFG.vlm_predicator_render_option_state,
    #             use_object_id=CFG.vlm_predicator_render_option_state,
    #             position_proprio_features=True)
    #         traj_str.append(state_dict_str + "\n")
    #         state_str_lst.append(state_dict_str)
    #         traj_str.append(f"action_{i}: " +
    #                         a._option.simple_str(use_object_id=True) + "\n")

    #         # Write state name to the image for easy identification and save
    #         utils.save_image_with_label(s.labeled_image.copy(), s_name,
    #                                     obs_dir)

    #     # The final state
    #     s_name = "state_" + str(i + 1)
    #     s = traj.states[i + 1]
    #     traj_str.append(s_name + " with additional info:")
    #     state_str_lst.append(s_name + " with additional info:")
    #     state_dict_str = s.dict_str(
    #         indent=2,
    #         object_features=not CFG.vlm_predicator_render_option_state,
    #         use_object_id=CFG.vlm_predicator_render_option_state,
    #         position_proprio_features=True)
    #     traj_str.append(state_dict_str)
    #     state_str_lst.append(state_dict_str)
    #     utils.save_image_with_label(s.labeled_image.copy(), s_name, obs_dir)

    #     traj_str = "\n".join(traj_str)
    #     template = template.replace("[TRAJECTORY_STR]", traj_str)

    #     # Save the text prompt
    #     with open(f"{CFG.log_file}/ite{ite}_stage1_traj.prompt", 'w') as f:
    #         f.write(template)
    #     prompt = template
    #     return prompt, "\n".join(state_str_lst)

    def _create_implementation_prompt(
        self,
        env: BaseEnv,
        ite: int,
        state_list_str: str,
        predicate_specs: str,
        save_fn: str,
    ) -> str:

        # Structure classes
        if CFG.neu_sym_predicate:
            if save_fn == "init_cnpt_s2":
                # use the simplified version
                template_file = "invent_0_prog_syn_nesy_simp.outline"
                state_api_file = "api_raw_state_simp.py"
            else:
                template_file = "invent_0_prog_syn_nesy.outline"
                state_api_file = "api_raw_state.py"
            pred_api_file = "api_nesy_predicate.py"
        else:
            template_file = "invent_0_prog_syn_sym.outline"
            state_api_file = "api_oo_state.py"
            pred_api_file = "api_sym_predicate.py"

        with open(f"./prompts/{template_file}", 'r') as f:
            template = f.read()

        with open(f'./prompts/{state_api_file}', 'r') as f:
            state_str = f.read()
        with open(f'./prompts/{pred_api_file}', 'r') as f:
            pred_str = f.read()
        template = template.replace(
            '[STRUCT_DEFINITION]',
            add_python_quote(state_str + '\n\n' + pred_str))

        # Type Instances
        # if save_fn == "init_cnpt_s2":
        #     type_str = str(set(t.name for t in env.types))
        # else:
        with open(f"./prompts/types_{self.env_name}.py", 'r') as f:
            type_str = f.read()
        type_str = add_python_quote(type_str)
        template = template.replace("[TYPES_IN_ENV]", type_str)

        # Predicates
        # If NSP, provide the GT goal NSPs, although they are never used.
        # pred_str_lst = []
        # pred_str_lst.append()
        # pred_str = '\n'.join(pred_str_lst)
        template = template.replace("[PREDICATES_IN_ENV]", 
                                    self._create_pred_str(
                                        env,
                                        self.env_source_code,
                                        show_predicate_assertion=True,
                                        include_all_candidates=True,
                                        include_selected_predicates=False))
        if state_list_str != "":
            state_list_str =\
                "The states the predicates have been evaluated on are:\n" +\
                state_list_str
        template = template.replace("[LISTED_STATES]", state_list_str)
        template = template.replace("[PREDICATE_SPECS]", predicate_specs)

        with open(f"{CFG.log_file}/ite{ite}_{save_fn}.prompt", 'w') as f:
            f.write(template)
        prompt = template
        return prompt

    def _create_invention_prompt(
        self,
        env: BaseEnv,
        ite: int,
        max_num_options: int = 10,  # Number of options to show
        max_num_groundings: int = 2,  # Number of ground options per option.
        max_num_examples: int = 2,  # Number of examples per ground option.
        show_when_acc_is_one: bool = False,
        categories_to_show: List[str] = ['tp', 'fp'],
        seperate_prompt_per_option: bool = False,
        phase_n: str = "from_data"
    ) -> str:
        if CFG.vlm_invention_propose_nl_properties:
            template_f = "prompts/invent_0_prog_free_p_nl.outline"
        else:
            template_f = "prompts/invent_0_prog_free_pad_simp.outline"

        with open(template_f, 'r') as file:
            template = file.read()

        # Predicates
        # In proposing nl properties, the existing predicates are not listed
        pred_str_lst = []
        pred_str_lst.append(
            self._create_pred_str(
                env,
                self.env_source_code,
                include_definition=False,
                show_predicate_assertion=True,
                include_all_candidates=False,
                include_selected_predicates=True
            ))
        # if ite > 1:
        #     pred_str_lst.append("The previously invented predicates are:")
        #     pred_str_lst.append(self._invented_predicate_str(ite))
        pred_str = '\n'.join(pred_str_lst)
        template = template.replace("[PREDICATES_IN_ENV]", pred_str)

        type_names = str(set(t.name for t in env.types))
        template = template.replace("[TYPES_IN_ENV]", type_names)

        _, summary_str, state_str_set =\
            utils.count_classification_result_for_ops(
                self._nsrts,
                self.succ_optn_dict,
                self.fail_optn_dict,
                return_str=True,
                initial_ite=(ite == 0),
                print_cm=True,
                max_num_options=max_num_options,
                max_num_groundings=max_num_groundings,
                max_num_examples=max_num_examples,
                categories_to_show=categories_to_show,
                ite=ite,
                show_when_acc_is_one=show_when_acc_is_one,
            )
        template = template.replace("[OPERATOR_PERFORMACE]", summary_str)

        # Save the text prompt
        if CFG.vlm_invention_propose_nl_properties:
            with open(f"{CFG.log_file}/ite{ite}_{phase_n}_s0.prompt", 'w') as f:
                f.write(template)
        else:
            with open(f"{CFG.log_file}/ite{ite}_{phase_n}_s1.prompt", 'w') as f:
                f.write(template)
        prompt = template

        return prompt, "\n".join(sorted(state_str_set))

    # def _create_one_step_program_invention_prompt(
    #     self,
    #     env: BaseEnv,
    #     ite: int,
    #     max_num_options: int = 10,  # Number of options to show
    #     max_num_groundings: int = 2,  # Number of ground options per option.3
    #     max_num_examples: int = 2,  # Number of examples per ground option.
    #     categories_to_show: List[str] = ['tp', 'fp'],
    #     seperate_prompt_per_option: bool = False,
    # ) -> str:
    #     """Compose a prompt for VLM for predicate invention."""
    #     # Read the shared template
    #     with open(f'./prompts/invent_0_simple.outline', 'r') as file:
    #         template = file.read()
    #     # Get the different parts of the prompt
    #     if CFG.neu_sym_predicate:
    #         instr_fn = "raw"
    #     else:
    #         instr_fn = "oo"
    #     with open(f'./prompts/invent_0_{instr_fn}_state_simple.outline',
    #               'r') as f:
    #         instruction = f.read()
    #     template += instruction

    #     ##### Meta Environment
    #     # Structure classes

    #     # with open('./prompts/class_definitions.py', 'r') as f:
    #     #     struct_str = f.read()
    #     if CFG.neu_sym_predicate:
    #         with open('./prompts/api_raw_state.py', 'r') as f:
    #             state_str = f.read()
    #         with open('./prompts/api_nesy_predicate.py', 'r') as f:
    #             pred_str = f.read()
    #     else:
    #         with open('./prompts/api_oo_state.py', 'r') as f:
    #             state_str = f.read()
    #         with open('./prompts/api_sym_predicate.py', 'r') as f:
    #             pred_str = f.read()

    #     template = template.replace(
    #         '[STRUCT_DEFINITION]',
    #         add_python_quote(state_str + '\n\n' + pred_str))

    #     ##### Environment
    #     self.env_source_code = getsource(env.__class__)
    #     # Type Instances
    #     if CFG.neu_sym_predicate:
    #         # New version: just read from a file
    #         with open(f"./prompts/types_{self.env_name}.py", 'r') as f:
    #             type_instan_str = f.read()
    #     else:
    #         # Old version: extract directly from the source code
    #         type_instan_str = self._env_type_str(self.env_source_code)
    #     type_instan_str = add_python_quote(type_instan_str)
    #     template = template.replace("[TYPES_IN_ENV]", type_instan_str)

    #     # Predicates
    #     # If NSP, provide the GT goal NSPs, although they are never used.
    #     pred_str_lst = []
    #     pred_str_lst.append(self._create_pred_str(env,
    #                                                  self.env_source_code))
    #     # if ite > 1:
    #     #     pred_str_lst.append("The previously invented predicates are:")
    #     #     pred_str_lst.append(self._invented_predicate_str(ite))
    #     pred_str = '\n'.join(pred_str_lst)
    #     template = template.replace("[PREDICATES_IN_ENV]", pred_str)

    #     # Options
    #     '''Template: The set of options the robot has are:
    #     [OPTIONS_IN_ENV]'''
    #     options_str_set = set()
    #     for nsrt in self._nsrts:
    #         options_str_set.add(nsrt.option_str_annotated())
    #     options_str = '\n'.join(list(options_str_set))
    #     template = template.replace("[OPTIONS_IN_ENV]", options_str)

    #     # NSRTS
    #     nsrt_str = []
    #     for nsrt in self._nsrts:
    #         nsrt_str.append(str(nsrt).replace("NSRT-", "Operator-"))
    #     template = template.replace("[NSRTS_IN_ENV]", '\n'.join(nsrt_str))

    #     _, summary_str, _ =\
    #         utils.count_classification_result_for_ops(
    #             self._nsrts,
    #             self.succ_optn_dict,
    #             self.fail_optn_dict,
    #             return_str=True,
    #             initial_ite=(ite == 0),
    #             print_cm=True,
    #             max_num_options=max_num_options,
    #             max_num_groundings=max_num_groundings,
    #             max_num_examples=max_num_examples,
    #             categories_to_show=categories_to_show,
    #             ite=ite,
    #         )
    #     template = template.replace("[OPERATOR_PERFORMACE]", summary_str)

    #     # Save the text prompt
    #     with open(f"{CFG.log_file}/ite{ite}.prompt", 'w') as f:
    #         # with open(f'./prompts/invent_{self.env_name}_{ite}.prompt', 'w') as f:
    #         f.write(template)
    #     prompt = template

    #     return prompt, None

    def _parse_predicate_predictions(
        self,
        prediction_file: str,
        tasks: List[Task],
        ite: int,
        valid_types: Set[Type],
        translate_fn: str,
    ) -> Tuple[Set[Predicate], Set[ConceptPredicate]]:

        # Read the prediction file
        with open(prediction_file, 'r') as file:
            response = file.read()

        # Regular expression to match Python code blocks
        pattern = re.compile(r'```python(.*?)```', re.DOTALL)
        python_blocks = []
        # Find all Python code blocks in the text
        for match in pattern.finditer(response):
            # Extract the Python code block and add it to the list
            python_blocks.append(match.group(1).strip())

        primitive_preds = set()
        context: Dict = {}
        untranslated_concept_pred_str = []
        # Add the existing predicates and their classifiers to `context` for
        # potential reuse
        for p in self._initial_predicates:
            context[f"_{p.name}_NSP_holds"] = p._classifier

        for p in self.base_prim_candidates | self.cnpt_pred_candidates:
            context[f"{p.name}"] = p

        for p in self.cnpt_pred_candidates:
            context[f"_{p.name}_CP_holds"] = p._classifier


        type_init_str = self._env_type_str(self.env_source_code)
        
        # Load the imports and types
        exec(import_str, context)
        exec(type_init_str, context)

        for code_str in python_blocks:
            # Extract name from code block
            match = re.search(r'(\w+)\s*=\s*(NS)?Predicate', code_str)
            if match is None:
                logging.warning("No predicate name found in the code block")
                continue
            pred_name = match.group(1)
            logging.info(f"Found definition for predicate {pred_name}")
            if CFG.vlm_invention_use_concept_predicates:
                is_concept_predicate = self.check_is_concept_predicate(code_str)
                logging.info(f"\t it's a concept predicate: "
                             f"{is_concept_predicate}")
            else:
                is_concept_predicate = False
                logging.info(f"\t concept predicate disabled")

            # Recognize that it's a concept predicate
            if is_concept_predicate:
                untranslated_concept_pred_str.append(add_python_quote(
                    code_str))
            else:
                # Type check the code
                # passed = False
                # while not passed:
                #     result, passed = self.type_check_proposed_predicates(
                #                                                     pred_name,
                #                                                     code_str)
                #     if not passed:
                #         # Ask the LLM or the User to fix the code
                #         pass
                #     else:
                #         break

                # Instantiate the primitive predicates
                if CFG.vlm_invent_try_to_use_gt_predicates:
                    if has_key_in_tuple_key(self.env.ns_to_sym_predicates,
                                            pred_name.strip("_")):
                        primitive_preds.add(
                            get_value_from_tuple_key(
                                        self.env.ns_to_sym_predicates,
                                        pred_name.strip("_")))
                    else:
                        logging.warning(
                            f"{pred_name} isn't in the "
                            "ns_to_sym_predicates dict, please consider adding it."
                        )
                else:
                    # check if it's roughly runable, and add it to list if it is.
                    try:
                        exec(code_str, context)
                        logging.debug(f"Testing predicate {pred_name}")
                        # Check1: Make sure it uses types present in the environment
                        proposed_pred = context[pred_name]
                        for t in proposed_pred.types:
                            if t not in valid_types:
                                logging.warning(f"Type {t} not in the environment")
                                raise Exception(f"Type {t} not in the environment")
                        utils.abstract(tasks[0].init, [context[pred_name]])
                    except Exception as e:
                        error_trace = traceback.format_exc()
                        logging.warning(f"Proposed predicate {pred_name} not "
                                        f"executable: {e}\n{error_trace}")
                        continue
                    else:
                        primitive_preds.add(context[pred_name])
        
        concept_preds = set()
        # Translate the potential concept predicates to concept predicates
        if untranslated_concept_pred_str:
            logging.info("\nTransforming the potential concept predicates...")
            translated_concept_pred_str = self.translate_concept_predicate(
                                                ite,
                                                untranslated_concept_pred_str,
                                                translate_fn=translate_fn)
            cn_pred_python_blocks = []
            # Find all Python code blocks in the text
            for match in pattern.finditer(translated_concept_pred_str):
                # Extract the Python code block and add it to the list
                cn_pred_python_blocks.append(match.group(1).strip())

            # Instantiate the transformed concept predicates
            for code_str in cn_pred_python_blocks:
                # Extract name from code block
                match = re.search(r'(\w+)\s*=\s*(Concept)?Predicate', code_str)
                if match is None:
                    logging.warning("No predicate name found in the code block")
                    continue
                pred_name = match.group(1)
                logging.info(f"Found definition for Concept Predicate "
                             f"{pred_name}")

                # Instantiate the predicate
                try:
                    exec(code_str, context)
                    logging.debug(f"Testing Concept Predicate {pred_name}")
                    # Check1: Make sure it uses types present in the environment
                    proposed_pred = context[pred_name]
                    for t in proposed_pred.types:
                        if t not in valid_types:
                            logging.warning(f"Type {t} not in the environment")
                            raise Exception(f"Type {t} not in the environment")

                    # Check2: Make sure it's executable
                    utils.abstract(tasks[0].init, 
                                    self._get_current_predicates() |\
                                        set([context[pred_name]]))
                except Exception as e:
                    error_trace = traceback.format_exc()
                    logging.warning(f"We encountered the following error when "
                    f"testing predicate {pred_name}:\n{e}\n{error_trace}")
                    continue
                else:
                    logging.debug(f"Added {pred_name}")
                    concept_pred = context[pred_name]
                    # Get all the auxiliary concepts
                    # Make sure the if only matches if it doesn't match with
                    # leading or trailing letter.
                    aux_concepts = {p for p in 
                                concept_preds | self.cnpt_pred_candidates if
                    re.search(rf'(?<![a-zA-Z]){re.escape(p.name)}(?![a-zA-Z])', 
                    code_str)}
                    # either " {p.name} " or ' "{p.name}" ' or " '{p.name}' "
                    # or " {p.name}}"
                    logging.debug(f"Found auxiliary concepts: {aux_concepts}")
                    # Update the concept predicate
                    concept_pred = concept_pred.update_auxiliary_concepts(
                                                                aux_concepts)
                    concept_preds.add(concept_pred)

        return primitive_preds, concept_preds

    @staticmethod
    def check_is_concept_predicate(code_str: str) -> bool:
        """Check if the predicate is a concept predicate by looking for
        `get` or `evaluate_simple` in the code block.
        """
        if "state.get(" in code_str or\
           "state.evaluate_simple_assertion" in code_str:
            return False
        return True

    def translate_concept_predicate(self, ite: int, code_str: List[str],
                                    translate_fn: str) -> str:
        """Call GPT to transform the predicate str
        """
        template_f = f"prompts/classifier_transform.outline"
        with open(template_f, "r") as f:
            template = f.read()
        
        # Existing primitive predicates
        primitive_pred_str = self._create_pred_str(
                                include_definition=False,
                                include_primitive_preds=True,
                                include_concept_preds=False,
                                include_all_candidates=True
                                )
        # Existing concept predicates
        concept_pred_str = self._create_pred_str(
                                include_definition=False,
                                include_primitive_preds=False,
                                include_concept_preds=True,
                                include_all_candidates=True
                                )
        if concept_pred_str == "":
            concept_pred_str = "None"

        prompt = template.format(PRIMITIVE_PREDICATES=primitive_pred_str,
                                 CONCEPT_PREDICATES=concept_pred_str,
                                 UNTRANSFORMED_PREDICATES='\n'.join(code_str))
        prompt_f = CFG.log_file + f"ite{ite}_{translate_fn}_clf_trfm.prompt"
        with open(prompt_f, 'w') as f:
            f.write(prompt)

        response_f = CFG.log_file + f"ite{ite}_{translate_fn}_clf_trfm.response"
        response = self._get_vlm_response(response_f, self._vlm, prompt, [])

        return response

    def type_check_proposed_predicates(self, predicate_name: str,
                                       code_block: str) -> Tuple[str, bool]:
        # Write the definition to a python file
        predicate_fname = f'./prompts/oi1_predicate_{predicate_name}.py'
        with open(predicate_fname, 'w') as f:
            f.write(import_str + '\n' + code_block)

        # Type check
        logging.info(f"Start type checking the predicate " +
                     f"{predicate_name}...")
        result = subprocess.run([
            "mypy", "--strict-equality", "--disallow-untyped-calls",
            "--warn-unreachable", "--disallow-incomplete-defs",
            "--show-error-codes", "--show-column-numbers",
            "--show-error-context", predicate_fname
        ],
                                capture_output=True,
                                text=True)
        stdout = result.stdout
        passed = result.returncode == 0
        return stdout, passed

    def _env_type_str(self, source_code: str) -> str:
        type_pattern = r"(        # Types.*?)(?=\n\s*\n|$)"
        type_block = re.search(type_pattern, source_code, re.DOTALL)
        if type_block is not None:
            type_init_str = type_block.group()
            type_init_str = textwrap.dedent(type_init_str)
            type_init_str = type_init_str.replace("self.", "")
            # type_init_str = add_python_quote(type_init_str)
            return type_init_str
        else:
            raise Exception("No type definitions found in the environment.")

    def _constants_str(self, source_code: str) -> str:
        # Some constants, if any, defined in the environment are
        constants_str = ''
        pattern = r"(    # Constants present in goal predicates.*?)(?=\n\s*\n|$)"
        match = re.search(pattern, source_code, re.DOTALL)
        if match:
            constants_str = match.group(1)
            constants_str = textwrap.dedent(constants_str)
        return constants_str

    def _create_pred_str(
        self,
        env: Optional[BaseEnv] = None,
        source_code: str = "",
        include_definition: bool = True,
        show_predicate_assertion: bool = False,
        include_primitive_preds: bool = True,
        include_concept_preds: bool = True,
        include_all_candidates: bool = False,
        include_selected_predicates: bool = True,
    ) -> str:
        """Extract the initial predicates from the environment source code If
        NSP, provide the GT goal NSPs, although they are never used."""
        init_pred_str = []

        if include_all_candidates:
            predicates_shown = self.base_prim_candidates |\
                               self.cnpt_pred_candidates
        elif include_selected_predicates:
            predicates_shown = self._get_current_predicates()
        else:
            predicates_shown = self._initial_predicates

        init_pred_str = []
        for p in predicates_shown:
            if include_primitive_preds and not isinstance(p, ConceptPredicate):
                init_pred_str.append(p.pretty_str_with_assertion())
            elif include_concept_preds and isinstance(p, ConceptPredicate):
                init_pred_str.append(p.pretty_str_with_assertion())
        logging.debug(init_pred_str)
        init_pred_str = sorted(init_pred_str)

        if include_definition:
            assert source_code != "", "Source code must be provided."
            assert env is not None, "Environment must be provided."
            # Print the variable definitions
            init_pred_str.append("\n")
            constants_str = self._constants_str(source_code)
            if constants_str:
                init_pred_str.append(
                    "The environment defines the following constants that can be "+\
                    "used in defining predicates:")
                init_pred_str.append(add_python_quote(constants_str))

            # Get the entire predicate instantiation code block.
            predicate_pattern = r"(# Predicates.*?)(?=\n\s*\n|$)"
            predicate_block = re.search(predicate_pattern, source_code,
                                        re.DOTALL)
            if predicate_block is not None:
                pred_instantiation_str = predicate_block.group()

                if CFG.neu_sym_predicate:
                    init_pred = [
                        p for p in env.ns_predicates
                        if p in self._initial_predicates
                    ]
                else:
                    init_pred = self._initial_predicates

                for p in init_pred:

                    p_name = p.name
                    # Get the instatiation code for p from the code block
                    if CFG.neu_sym_predicate:
                        p_instan_pattern = r"(self\._" + re.escape(p_name) +\
                                        r"_NSP = NSPredicate\(.*?\n.*?\))"
                    else:
                        p_instan_pattern = r"(self\._" + re.escape(p_name) +\
                                        r" = Predicate\(.*?\n.*?\))"
                    block = re.search(p_instan_pattern, pred_instantiation_str,
                                      re.DOTALL)
                    if block is not None:
                        p_instan_str = block.group()

                        # remove the parameterized assertion part
                        # remove_pattern = r",\s*parameterized_assertion=.*?(\))"
                        # p_instan_str = re.sub(remove_pattern, r"\1",
                        #                         p_instan_strs)

                        pred_str = "Predicate " + p.pretty_str()[1] +\
                                    " is defined by:\n" +\
                                    add_python_quote(p.classifier_str() +\
                                    p_instan_str)
                        init_pred_str.append(pred_str.replace("self.", ""))

        return '\n'.join(init_pred_str)

    def _invented_predicate_str(self, ite: int) -> str:
        """Get the predicate definitions from the previous response file."""
        new_predicate_str = []
        new_predicate_str.append(str(self._learned_predicates) + '\n')
        prediction_file = f'./prompts/invent_{self.env_name}_{ite-1}'+\
            ".response"
        with open(prediction_file, 'r') as file:
            response = file.read()

        # Regular expression to match Python code blocks
        code_pattern = re.compile(r'```python(.*?)```', re.DOTALL)
        for match in code_pattern.finditer(response):
            python_block = match.group(1).strip()
            pred_match = re.search(r'name\s*(:\s*str)?\s*= "([^"]*)"',
                                   python_block)
            if pred_match is not None:
                pred_name = pred_match.group(2)
                pred = next(
                    (p
                     for p in self._learned_predicates if p.name == pred_name),
                    None)
                if pred:
                    new_predicate_str.append("Predicate " +
                                             pred.pretty_str()[1] +
                                             " is defined by\n" +
                                             add_python_quote(python_block))
        has_not_or_forall = [
            p.name.startswith("NOT") or p.name.startswith("Forall")
            for p in self._learned_predicates
        ]
        if has_not_or_forall:
            new_predicate_str.append(
                "Predicates with names starting with " +
                "'NOT' or 'Forall' are defined by taking the negation or adding"
                + "universal quantifiers over other existing predicates.")
        return '\n'.join(new_predicate_str)

    def _parse_pad_labels_to_truth_values(self, resp: str) -> str:
        """Parse the predicate_specs to include the next state predicates as
        positive and negative examples."""
        # breakpoint()
        proposals, evals = resp.split("# Action Preconditions and Effects")

        state_dict = defaultdict(lambda: (set(), set()))

        # Define regex patterns
        action_pattern = re.compile(r'\* Action:.*?\n(.*?)(?=\* Action:|\Z)',
                                    re.DOTALL)
        preconditions_pattern = re.compile(
            r'\* preconditions \(state_(\d+)\):'
            r'\n(.*?)(?=\* (add effect|delete effect)|$)', re.DOTALL)
        add_effect_pattern = re.compile(
            r'\* add effect \(state_(\d+)\):'
            f'\n(.*?)(?=\* (preconditions|delete effect)|$)', re.DOTALL)
        delete_effect_pattern = re.compile(
            r'\* delete effect \(state_(\d+)\):'
            f'\n(.*?)(?=\* (preconditions|add effect)|$)', re.DOTALL)

        state_dict = defaultdict(lambda: (set(), set()))

        for action_block in action_pattern.finditer(evals):
            block_text = action_block.group(1)

            for match in preconditions_pattern.finditer(block_text):
                state = match.group(1)
                predicates = match.group(2)
                for predicate in predicates.strip().split('\n'):
                    cleaned_predicate = predicate.strip()
                    if cleaned_predicate:
                        state_dict[f'state_{state}'][0].add(cleaned_predicate)

            for match in add_effect_pattern.finditer(block_text):
                state = match.group(1)
                predicates = match.group(2)
                for predicate in predicates.strip().split('\n'):
                    cleaned_predicate = predicate.strip()
                    if cleaned_predicate and cleaned_predicate != 'None':
                        state_dict[f'state_{state}'][0].add(cleaned_predicate)

            for match in delete_effect_pattern.finditer(block_text):
                state = match.group(1)
                predicates = match.group(2)
                for predicate in predicates.strip().split('\n'):
                    cleaned_predicate = predicate.strip()
                    if cleaned_predicate and cleaned_predicate != 'None':
                        state_dict[f'state_{state}'][1].add(cleaned_predicate)
        # print(state_dict)

        predicate_format = re.compile(r"\* ([A-Za-z0-9_]+)\(([^)]+)\): (.+)")

        def filter_predicates(predicates_set):
            return {
                predicate
                for predicate in predicates_set
                if predicate_format.match(predicate)
            }

        filtered_state_dict = defaultdict(
            lambda: (set(), set()), {
                state:
                (filter_predicates(preconditions), filter_predicates(effects))
                for state, (preconditions, effects) in state_dict.items()
            })

        # pprint(filtered_state_dict)

        # Iterate over the state_dict and modify predicates
        state_str = []
        for state in sorted(filtered_state_dict.keys()):
            add_set, del_set = filtered_state_dict[state]
            if not add_set and not del_set:
                continue
            state_str.append(state)
            if add_set:
                pos = "\n".join([f"{pred}: True" for pred in add_set])
                state_str.append(pos)
            if del_set:
                neg = "\n".join([f"{pred}: False" for pred in del_set])
                state_str.append(neg)

        state_str = "\n".join(state_str)

        spec = proposals + "\n#Predicate Evaluation\n" + state_str

        return spec

    def _get_vlm_response(self,
                          response_file: str,
                          vlm: VisionLanguageModel,
                          prompt: str,
                          images: List[Image.Image] = [],
                          cache_chat_session: bool = False,
                          temperature: float = 0.0,
                          seed: Optional[int] = None) -> str:

        if seed is None:
            seed = CFG.seed
        if not os.path.exists(response_file) or self.regenerate_response:
            if self.manual_prompt:
                # create a empty file for pasting chatGPT response
                with open(response_file, 'w') as file:
                    pass
                logging.info(f"## Please paste the response from the VLM " +
                             f"to {response_file}")
                input("Press Enter when you have pasted the " + "response.")
            else:
                # vlm.reset_chat_session()
                response = vlm.sample_completions(
                    prompt,
                    images,
                    temperature=temperature,
                    seed=seed,
                    num_completions=1,
                    cache_chat_session=cache_chat_session)[0]
                with open(response_file, 'w') as f:
                    f.write(response)
        with open(response_file, 'r') as file:
            response = file.read()
        return response
