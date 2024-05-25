"""
Example command line:
    export OPENAI_API_KEY=<your API key>
"""
import re
import os
import ast
import json
import time
import base64
import dill
import logging
import textwrap
from typing import Set, List, Dict, Sequence, Tuple, Any, FrozenSet, Iterator,\
    Callable
import subprocess
import inspect
from inspect import getsource
from copy import deepcopy
import importlib.util
from collections import defaultdict, namedtuple
from pprint import pformat
from tqdm import tqdm

from gym.spaces import Box
import numpy as np
import imageio
from tabulate import tabulate

from predicators import utils
from predicators.settings import CFG
from predicators.ground_truth_models import get_gt_nsrts
from predicators.llm_interface import OpenAILLM, OpenAILLMNEW
from predicators.approaches import ApproachFailure, ApproachTimeout
from predicators.approaches.nsrt_learning_approach import NSRTLearningApproach
from predicators.predicate_search_score_functions import \
    _PredicateSearchScoreFunction, create_score_function
from predicators.structs import Dataset, LowLevelTrajectory, Predicate, \
    ParameterizedOption, Type, Task, Optional, GroundAtomTrajectory, \
    AnnotatedPredicate, State, Object, _TypedEntity, GroundOptionRecord, Action
from predicators.approaches.grammar_search_invention_approach import \
    create_score_function, _create_grammar 
from predicators.envs import BaseEnv
from predicators.utils import option_plan_to_policy, OptionExecutionFailure, \
    EnvironmentFailure
from predicators.predicate_search_score_functions import \
    _ClassificationErrorScoreFunction


import_str = """
from typing import Sequence
import numpy as np
from predicators.structs import State, Object, Predicate, Type
"""

PlanningResult = namedtuple("PlanningResult", 
                                ['succeeded', 
                                 'info'])

def print_confusion_matrix(tp: float, tn: float, fp: float, fn: float) -> None:
    precision = round(tp / (tp + fp), 2) if tp + fp > 0 else 0
    recall = round(tp / (tp + fn), 2) if tp + fn > 0 else 0
    specificity = round(tn / (tn + fp), 2) if tn + fp > 0 else 0
    accuracy = round((tp + tn) / (tp + tn + fp + fn), 
                     2) if tp + tn + fp + fn > 0 else 0
    f1_score = round(2 * (precision * recall) / (precision + recall), 
                     2) if precision + recall > 0 else 0

    table = [["", "Positive", "Negative", "Precision", "Recall", "Specificity",
              "Accuracy", "F1 Score", ],
             ["True", tp, tn, "", "", "", "", ""],
             ["False", fp, fn, "", "", "", "", ""],
             ["", "", "", precision, recall, specificity, accuracy, f1_score]]
    logging.info(tabulate(table, headers="firstrow", tablefmt="fancy_grid"))

# Function to encode the image
def encode_image(image_path: str) -> str:
  with open(image_path, "rb") as image_file:
    return base64.b64encode(image_file.read()).decode('utf-8')

def add_python_quote(text: str) -> str:
    return f"```python\n{text}\n```\n"

def d2s(dict_with_arrays: Dict) -> str:
    # Convert State data with numpy arrays to lists, and to string
    return str({k: [round(i, 2) for i in v.tolist()] for k, v in 
            dict_with_arrays.items()})

class VlmInventionApproach(NSRTLearningApproach):
    """Predicate Invention with VLMs"""
    def __init__(self, initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task]) -> None:
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
        self._vlm = OpenAILLMNEW(CFG.vlm_model_name)
        self._type_dict = {type.name: type for type in self._types}

    @classmethod
    def get_name(cls) -> str:
        return "vlm_online_invention"

    @property
    def is_offline_learning_based(self) -> bool:
        return False

    def _get_current_predicates(self) -> Set[Predicate]:
        return self._initial_predicates | self._learned_predicates
    
    def load(self, online_learning_cycle: Optional[int]) -> None:
        super().load(online_learning_cycle)

        preds, _ = utils.extract_preds_and_types(self._nsrts)
        self._learned_predicates = (set(preds.values()) -
                                    self._initial_predicates)

    def _solve_tasks(self, env: BaseEnv, tasks: List[Task]) -> \
        Tuple[List, Dataset]:
        '''When return_trajctories is True, return the dataset of trajectories
        otherwise, return the results of solving the tasks (succeeded/failed 
        plans).
        '''
        results = []
        trajectories = []
        for idx, task in enumerate(tasks):
            logging.info(f"Solving Task {idx}")
            try:
                policy = self.solve(task, timeout=CFG.timeout) 
            except (ApproachTimeout, ApproachFailure) as e:
                logging.info(f"Planning failed: {str(e)}")
                result = PlanningResult(
                    succeeded=False,
                    info={
                        "metrics": e.info["metrics"],
                        "partial_refinements": e.info["partial_refinements"],
                        "error": str(e)}
                )
            else:
                # logging.info(f"--> Succeeded")
                policy = utils.option_plan_to_policy(self._last_plan)
                
                result = PlanningResult(
                    succeeded=True,
                    info={"option_plan": self._last_plan,
                        "nsrt_plan": self._last_nsrt_plan,
                        "metrics": self._last_metrics,
                        "partial_refinements": self._last_partial_refinements,
                        "policy": policy})
                # Collect trajectory
                traj, _ = utils.run_policy(policy,
                    env,
                    "train",
                    idx,
                    termination_function=lambda s: False,
                    max_num_steps=CFG.horizon,
                    exceptions_to_break_on={utils.OptionExecutionFailure,})
                traj = LowLevelTrajectory(traj.states,
                                        traj.actions,
                                        _is_demo=True,
                                        _train_task_idx=idx)
                trajectories.append(traj)
            results.append(result)
        dataset = Dataset(trajectories)
        return results, dataset

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        pass

    def learn_from_tasks(self, env: BaseEnv, tasks: List[Task]) -> None:
        '''Learn from interacting with the offline dataset
        '''
        self.env_name = env.get_name()
        num_tasks = len(tasks)
        propose_ite = 0
        max_invent_ite = 5
        invent_at_every_ite = True # Invent at every iterations
        base_candidates = set()
        manual_prompt = True
        regenerate_response = False
        load_llm_pred_invent_dataset = True
        save_llm_pred_invent_dataset = True
        solve_rate, prev_solve_rate = 0.0, np.inf # init to inf
        best_solve_rate, best_ite, clf_acc = 0.0, 0.0, 0.0
        clf_acc_at_best_solve_rate = 0.0
        best_nsrt, best_preds = deepcopy(self._nsrts), set()
        self._learned_predicates = set()
        self._init_nsrts = deepcopy(self._nsrts)
        no_improvement = False

        # init data collection
        ds_fname = utils.llm_pred_dataset_save_name(0)
        if load_llm_pred_invent_dataset and os.path.exists(ds_fname):
            with open(ds_fname, 'rb') as f:
                results, dataset = dill.load(f) 
            logging.info(f"Loaded dataset from {ds_fname}\n")
        else:
            # Ask it to solve the tasks
            results, dataset = self._solve_tasks(env, tasks)
            if save_llm_pred_invent_dataset:
                os.makedirs(os.path.dirname(ds_fname), exist_ok=True)
                with open(ds_fname, 'wb') as f:
                    dill.dump((results, dataset), f)
                logging.info(f"Saved dataset to {ds_fname}\n")

        num_solved = sum([r.succeeded for r in results])
        prev_solve_rate = num_solved / num_tasks
        logging.info(f"===ite {0}; "
                f"no invent solve rate {num_solved / num_tasks}\n")
        self.succ_optn_dict: Dict[str, GroundOptionRecord] =\
            defaultdict(GroundOptionRecord)
        self.fail_optn_dict: Dict[str, GroundOptionRecord] =\
            defaultdict(GroundOptionRecord)

        for ite in range(1, max_invent_ite+1):
            logging.info(f"===Starting iteration {ite}...")
            # Reset at every iteration
            # self.succ_optn_dict = defaultdict(lambda: defaultdict(list))
            # self.fail_optn_dict = defaultdict(lambda: defaultdict(list))
            self._process_interaction_result(env, results, tasks, ite,
                                            log_when_first_success=True)
            #### End of data collection

            # Invent when no improvement in solve rate
            self._prev_learned_predicates: Set[Predicate] =\
                self._learned_predicates
            if ite == 1 or no_improvement or invent_at_every_ite:
                # Invent only when there is no improvement in solve rate
                # Or when invent_at_every_ite is True
                #   Create prompt to inspect the execution
                if CFG.llm_predicator_oracle_base:
                    # If using the oracle predicates
                    new_candidates = env.predicates - self._initial_predicates
                else:
                    # Use the results to prompt the llm
                    prompt = self._create_prompt(env, ite)
                    response_file =\
                    f'./prompts/invent_{self.env_name}_1.response'
                    # f'./prompts/invent_{self.env_name}_{ite}.response'
                    breakpoint()
                    new_candidates = self._get_llm_predictions(prompt,
                                                    response_file, 
                                                    manual_prompt,
                                                    regenerate_response)
                logging.info(f"Done: created {len(new_candidates)} candidates:")

                if CFG.llm_predicator_oracle_learned:
                    self._learned_predicates = new_candidates
                else:
                    # Select a subset candidates by score optimization    
                    base_candidates |= new_candidates

                    ### Predicate Search
                    # Optionally add grammar to the candidates
                    all_candidates: Dict[Predicate, float] = {}
                    if CFG.llm_predicator_use_grammar:
                        grammar = _create_grammar(dataset=dataset, 
                            given_predicates=base_candidates |\
                                self._initial_predicates)
                        all_candidates.update(grammar.generate(
                            max_num=CFG.grammar_search_max_predicates))
                    else:
                        # Assign cost 0 for every candidate, for now.
                        all_candidates.update({p: 0 for p in base_candidates})
                    # Add a atomic states for succ_optn_dict and fail_optn_dict
                    logging.info("Applying predicates to data...")
                    for optn_dict in [self.succ_optn_dict, self.fail_optn_dict]:
                        for g_optn in optn_dict.keys():
                            atom_states = []
                            for state in optn_dict[g_optn].states:
                                atom_states.append(utils.abstract(
                                state, 
                                set(all_candidates) | self._initial_predicates))
                            optn_dict[g_optn].abstract_states = atom_states
                    # Apply the candidate predicates to the data.
                    atom_dataset: List[GroundAtomTrajectory] =\
                        utils.create_ground_atom_dataset(
                            dataset.trajectories, 
                            set(all_candidates) | self._initial_predicates)
                    logging.info("Done.")
                    score_function = _ClassificationErrorScoreFunction(
                        self._initial_predicates, atom_dataset, all_candidates,
                        self._train_tasks, self.succ_optn_dict, 
                        self.fail_optn_dict)

                    start_time = time.perf_counter()
                    self._learned_predicates = \
                        self._select_predicates_by_score_hillclimbing(
                            all_candidates, 
                            score_function, 
                            self._initial_predicates)
                    logging.info(
                    f"Total search time {time.perf_counter()-start_time:.2f} "
                    "seconds")
                propose_ite += 1

            # Finally, learn NSRTs via superclass, using all the kept predicates.
            annotations = None
            if dataset.has_annotations:
                annotations = dataset.annotations
            self._learn_nsrts(dataset.trajectories, online_learning_cycle=None,
                annotations=annotations)

            # Add init_nsrts whose option isn't in the current nsrts to 
            cur_options = [nsrt.option for nsrt in self._nsrts]
            for p_nsrts in self._init_nsrts:
                if not p_nsrts.option in cur_options:
                    self._nsrts.add(p_nsrts)
            print("All NSRTS after learning", pformat(self._nsrts))

            ### Collect Data again
            # Set up load/save filename for interaction dataset
            ds_fname = utils.llm_pred_dataset_save_name(ite)
            if load_llm_pred_invent_dataset and os.path.exists(ds_fname):
                # Load from dataset_fname
                with open(ds_fname, 'rb') as f:
                    results, dataset = dill.load(f) 
                logging.info(f"Loaded dataset from {ds_fname}\n")
            else:
                # Ask it to try to solve the tasks
                results, dataset = self._solve_tasks(env, tasks)
                if save_llm_pred_invent_dataset:
                    with open(ds_fname, 'wb') as f:
                        dill.dump((results, dataset), f)
                    logging.info(f"Saved dataset to {ds_fname}\n")

            num_solved = sum([r.succeeded for r in results])
            solve_rate= num_solved / num_tasks
            no_improvement = not(solve_rate > prev_solve_rate)

            # Print the new classification results with the new operators
            tp, tn, fp, fn, _ = utils.count_classification_result_for_ops(
                self._nsrts, self.succ_optn_dict, self.fail_optn_dict,
                return_str=False, initial_ite=False, print_cm=True)
            clf_acc = (tp + tn) / (tp + tn + fp + fn)
            logging.info(f"\n===ite {ite} finished. "
                            f"Solve rate {num_solved / num_tasks} "
                            f"Prev solve rate {prev_solve_rate} "
                            f"Clf accuracy: {clf_acc:.2f}\n")

            # Save the best model
            if solve_rate > best_solve_rate :
                best_solve_rate = solve_rate
                clf_acc_at_best_solve_rate = clf_acc
                best_ite = ite
                best_nsrt = self._nsrts
                best_preds = self._learned_predicates
            prev_solve_rate = solve_rate
            if solve_rate == 1:
                break

        logging.info("Invention finished.")
        logging.info(f"\nBest solve rate {best_solve_rate} first achieved at ite "
            f"{best_ite}; clf accuracy {clf_acc_at_best_solve_rate}")
        logging.info(f"Predicates learned {best_preds}")
        logging.info(f"NSRTs learned {pformat(best_nsrt)}")
        breakpoint()
        self._nsrts = best_nsrt
        self._learned_predicates = best_preds
        return
    
    def _get_llm_predictions(self, prompt: str, response_file: str,
                             manual_prompt: bool=False,
                             regenerate_response: bool=False) -> Set[Predicate]:
        if not os.path.exists(response_file) or regenerate_response:
            if manual_prompt:
                # create a empty file for pasting chatGPT response
                with open(response_file, 'w') as file:
                    pass
                logging.info(
                    f"## Please paste the response from the LLM "+
                    f"to {response_file}")
                input("Press Enter when you have pasted the "+
                        "response.")
            else:
                self._vlm.sample_completions(prompt,
                    temperature=CFG.llm_temperature,
                    seed=CFG.seed,
                    save_file=response_file)[0]
        new_candidates = self._parse_predicate_predictions(
            response_file)
        return new_candidates

    def _select_predicates_by_score_hillclimbing(
            self, candidates: Dict[Predicate, float],
            score_function: _PredicateSearchScoreFunction,
            initial_predicates: Set[Predicate]=set(),
            atom_dataset: List[GroundAtomTrajectory]=[],
            train_tasks: List[Task]=[]) -> Set[Predicate]:
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
            # for predicate in sorted(s):  # determinism
            #     # Actions not needed. Frozensets for hashing. The cost of
            #     # 1.0 is irrelevant because we're doing GBFS / hill
            #     # climbing and not A* (because we don't care about the
            #     # path).
            #     yield (None, frozenset(set(s) - {predicate}), 1.0)

        # Start the search with no candidates.
        init: FrozenSet[Predicate] = frozenset()
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

        logging.info(f"\nSelected {len(kept_predicates)} predicates out of "
                     f"{len(candidates)} candidates:")
        for pred in kept_predicates:
            logging.info(f"\t{pred}")
        score_function.evaluate(kept_predicates)  # log useful numbers

        return set(kept_predicates)        



    def _create_prompt(self, env: BaseEnv, ite: int) -> str:
        '''Compose a prompt for VLM for predicate invention
        '''
        # Read the template
        with open('./prompts/invent_.outline', 'r') as file:
            template = file.read()

        ##### Meta Environment
        # Structure classes
        with open('./prompts/class_definitions.py', 'r') as f:
            struct_str = f.read()
        template = template.replace('[STRUCT_DEFINITION]',
                                    add_python_quote(struct_str))

        ##### Environment
        self.env_source_code = getsource(env.__class__)
        # Type Instantiation
        type_instan_str = add_python_quote(
                            self._env_type_str(self.env_source_code))
        template = template.replace("[TYPES_IN_ENV]", type_instan_str)

        # Predicates
        pred_str_lst = []
        pred_str_lst.append(self._init_predicate_str(self.env_source_code))
        if ite > 1:
            pred_str_lst.append("The previously invented predicates are:")
            pred_str_lst.append(self._invented_predicate_str(ite))
        pred_str = '\n'.join(pred_str_lst)
        template = template.replace("[PREDICATES_IN_ENV]", pred_str)
        
        # Options
        options_str_set = set()
        for nsrt in self._nsrts:
            options_str_set.add(nsrt.option_str())
        options_str = '\n'.join(list(options_str_set))
        template = template.replace("[OPTIONS_IN_ENV]", options_str)
        
        # NSRTS
        nsrt_str = []
        for nsrt in self._nsrts:
            nsrt_str.append(str(nsrt).replace("NSRT-", ""))
        template = template.replace("[NSRTS_IN_ENV]", '\n'.join(nsrt_str))

        _, _, _, _, summary_str = utils.count_classification_result_for_ops(
                                        self._nsrts,
                                        self.succ_optn_dict,
                                        self.fail_optn_dict,
                                        return_str=True,
                                        initial_ite=(ite==0),
                                        print_cm=True)
        template = template.replace("[OPERATOR_PERFORMACE]", summary_str)

        # Save the text prompt
        with open(f'./prompts/invent_{self.env_name}_{ite}.prompt','w') as f:
            f.write(template)
        prompt = template

        # if CFG.rgb_observation:
        #     # Visual observation
        #     images = []
        #     for i, trajectory in enumerate(dataset.trajectories):
        #         # Get the init observation in the trajectory
        #         img_save_path = f'./prompts/init_obs_{i}.png'
        #         observation = trajectory.states[0].rendered_state['scene'][0]
        #         imageio.imwrite(img_save_path, observation)

        #         # Encode the image
        #         image_str = encode_image(img_save_path)
        #         # Add the image to the images list
        #         images.append(image_str)

        # ########### Make the prompt ###########
        # # Create the text entry
        # text_entry = {
        #     "type": "text",
        #     "text": text_prompt
        # }
        
        # prompt = [text_entry]
        # if CFG.rgb_observation:
        #     # Create the image entries
        #     image_entries = []
        #     for image_str in images:
        #         image_entry = {
        #             "type": "image_url",
        #             "image_url": {
        #                 "url": f"data:image/png;base64,{image_str}"
        #             }
        #         }
        #         image_entries.append(image_entry)

        #     # Combine the text entry and image entries and Create the final prompt
        #     prompt += image_entries

        # prompt = [{
        #         "role": "user",
        #         "content": prompt
        #     }]

        # # Convert the prompt to JSON string
        # prompt_json = json.dumps(prompt, indent=2)
        # with open('./prompts/invent_2_cover_final.prompt', 'w') as file:
        #     file.write(str(prompt_json))
        # # Can be loaded with:
        # # with open('./prompts/2_invention_cover_final.prompt', 'r') as file:
        # #     prompt = json.load(file)
        return prompt

    def _parse_predicate_predictions(self, prediction_file: str
                                     ) -> Set[Predicate]:
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
        
        candidates = set()
        context: Dict = {}
        type_init_str = self._env_type_str(self.env_source_code)
        constants_str = self._constants_str(self.env_source_code)
        for code_str in python_blocks:
            # Extract name from code block
            match = re.search(r'(\w+)\s*=\s*Predicate', code_str)
            if match is None:
                raise ValueError("No predicate name found in the code block")
            pred_name =  match.group(1)
            logging.info(f"Found definition for predicate {pred_name}")
            
            # # Type check the code
            # passed = False
            # while not passed:
            #     result, passed = self.type_check_proposed_predicates(pred_name, 
            #                                                          code_str)
            #     if not passed:
            #         # Ask the LLM or the User to fix the code
            #         pass
            #     else:
            #         break

            # Instantiate the predicate
            exec(import_str + '\n' + type_init_str + '\n' + constants_str +
                     code_str, context)
            candidates.add(context[pred_name])

        return candidates

    def type_check_proposed_predicates(self,
                                       predicate_name: str,
                                       code_block: str) -> Tuple[str, bool]:
        # Write the definition to a python file
        predicate_fname = f'./prompts/oi1_predicate_{predicate_name}.py'
        with open(predicate_fname, 'w') as f:
            f.write(import_str + '\n' + code_block)

        # Type check
        logging.info(f"Start type checking the predicate "+
                        f"{predicate_name}...")
        result = subprocess.run(["mypy", 
                                    "--strict-equality", 
                                    "--disallow-untyped-calls", 
                                    "--warn-unreachable",
                                    "--disallow-incomplete-defs",
                                    "--show-error-codes",
                                    "--show-column-numbers",
                                    "--show-error-context",
                                predicate_fname], 
                                capture_output=True, text=True)
        stdout = result.stdout
        passed = result.returncode == 0
        return stdout, passed

    def _env_type_str(self, source_code: str) -> str:  
        type_pattern = r"(    # Types.*?)(?=\n\s*\n|$)"        
        type_block = re.search(type_pattern, source_code, re.DOTALL)
        if type_block is not None:
            type_init_str = type_block.group()
            type_init_str = textwrap.dedent(type_init_str)
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


    def _init_predicate_str(self, source_code: str) -> str:
        '''Extract the initial predicates from the environment source code
        '''
        init_pred_str = []
        init_pred_str.append(str({p.pretty_str()[1] for p 
                                    in self._initial_predicates}) + "\n")
        
        # Print the variable definitions
        constants_str = self._constants_str(source_code)
        if constants_str:
            init_pred_str.append(
                "The environment defines the following constants that can be "+\
                "used in defining predicates:")
            init_pred_str.append(add_python_quote(constants_str))

        # Get the entire predicate instantiation code block.
        predicate_pattern = r"(# Predicates.*?)(?=\n\s*\n|$)"        
        predicate_block = re.search(predicate_pattern, source_code, re.DOTALL)
        if predicate_block is not None:
            pred_instantiation_str = predicate_block.group()

            for p in self._initial_predicates:
                p_name = p.name
                # Get the instatiation code for p from the code block
                p_instan_pattern = r"(self\._" + re.escape(p_name) +\
                                    r" = Predicate\(.*?\n.*?\))"
                block = re.search(p_instan_pattern, pred_instantiation_str, 
                                  re.DOTALL)
                if block is not None:
                    p_instan_str = block.group()
                    pred_str = "Predicate " + p.pretty_str()[1] +\
                                " is defined by\n" +\
                                add_python_quote(p.classifier_str() +\
                                p_instan_str)
                    init_pred_str.append(pred_str.replace("self.", ""))

        return '\n'.join(init_pred_str)
        
    def _invented_predicate_str(self, ite: int) -> str:
        '''Get the predicate definitions from the previous response file
        '''
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
                pred_name =  pred_match.group(2)
                pred = next((p for p in self._learned_predicates if p.name == 
                             pred_name), None)
                if pred:
                    new_predicate_str.append("Predicate " + 
                            pred.pretty_str()[1] + " is defined by\n" + 
                            add_python_quote(python_block))
        has_not_or_forall = [p.name.startswith("NOT") or 
                             p.name.startswith("Forall")  for p in 
                             self._learned_predicates]
        if has_not_or_forall:
            new_predicate_str.append("Predicates with names starting with "+
            "'NOT' or 'Forall' are defined by taking the negation or adding"+
            "universal quantifiers over other existing predicates.")
        return '\n'.join(new_predicate_str)

    def _process_interaction_result(self, env: BaseEnv,
                                results: Dict, 
                                tasks: List[Task],
                                ite: int,
                                log_when_first_success: bool,
                                add_intermediate_details: bool=False) -> None:    
        '''When add_intermediate_details == True, detailed interaction 
        trajectories are added to the return string
        '''

        # num_solved = sum([isinstance(r, tuple) for r in results])
        # num_attempted = len(results)
        # logging.info(f"The agent solved {num_solved} out of " +
        #                 f"{num_attempted} tasks.\n")
        logging.info("===Processing the interaction results...\n")
        num_tasks = len(tasks)
        if ite == 1:
            self.solve_log = [False] * num_tasks

        # Add a progress bar
        for i, _ in tqdm(enumerate(tasks), total=num_tasks, 
                         desc="Processing Interaction results"):
            # Planning Results
            result = results[i]
            try:
                num_skeletons_optimized = result.info['metrics'][
                    'num_skeletons_optimized']
                # When would this happen?
                # if num_skeletons_optimized == 0:
                #     continue
            except:
                logging.info(f"Task {i}: is not dr-reachable.")
                continue

            # if isinstance(result, tuple):
            if result.succeeded:
                # Found a successful plan
                # logging.info(f"Task {i}: planning succeeded.")
                # Only log
                nsrt_plan = result.info['nsrt_plan']
                option_plan = result.info['option_plan'].copy()
                if log_when_first_success:
                    if self.solve_log[i]:
                        continue
                    else:
                        self.solve_log[i] = True
                # Add the Plan Execution Trajectory
                init_state = env.reset(train_or_test='train', task_idx=i)
                _ = self._plan_to_str(init_state, env, 
                    nsrt_plan, option_plan, add_intermediate_details)

            # The failed refinement
            # This result is either a Result tuple or an exception
            # todo: Maybe should unify them in _solve_tasks?
            for p_ref in result.info['partial_refinements']:
                nsrt_plan = p_ref[0]
                # longest option refinement
                option_plan = p_ref[1].copy()
                failed_opt_idx = len(option_plan) - 1

                state = env.reset(train_or_test='train', task_idx=i)
                # Successful part
                if failed_opt_idx > 0:
                    state = self._plan_to_str(
                        state, env, nsrt_plan, option_plan[:-1], 
                        add_intermediate_details)
                # Failed part
                _ = self._plan_to_str(state, env, 
                        nsrt_plan[failed_opt_idx:], option_plan[-1:], 
                        add_intermediate_details, failed_opt=True)

        # Add the abstract states
        # maybe should be optimized?
        for optn_dict in [self.succ_optn_dict, self.fail_optn_dict]:
            for g_optn in optn_dict.keys():
                atom_states = []
                for state in optn_dict[g_optn].states:
                    atom_states.append(utils.abstract(
                        state, self._get_current_predicates()))
                optn_dict[g_optn].abstract_states = atom_states
    
    def _plan_to_str(self, init_state: State, env: BaseEnv, 
                             nsrt_plan: List, option_plan: List, 
                             add_intermediate_details: bool=False,
                             failed_opt: bool=False) -> State:

        # Executing the plan
        # task_str = []
        state = init_state
        policy: Optional[Callable[[State], Action]] = None
        nsrt_counter = 0
        for _ in range(CFG.horizon):
            try:
                if policy is None:
                    raise OptionExecutionFailure("placeholder policy")
                act = policy(state)
                state = env.step(act)
                # if add_intermediate_details:
                #     task_str.append("Action: " + str(act._arr) + "\n")
                #     task_str.append("State: " + d2s(state.data) + "\n")
            except OptionExecutionFailure as e:
                # When the one-option policy reaches terminal state
                # we're cetain the plan is successfully terminated 
                # because this is a successful plan.
                if str(e) == "placeholder policy" or\
                (str(e) == "Option plan exhausted!" and not failed_opt) or\
                (str(e) == "Encountered repeated state." and not failed_opt):
                    # if nsrt_counter > 0:
                    #     task_str.append(
                    #         "The option successfully terminated in state: \n" +
                    #         state.dict_str() + "\n")
                    try:
                        option = option_plan.pop(0)
                        policy = utils.option_plan_to_policy(
                            [option], raise_error_on_repeated_state=True)
                    except IndexError: break
                    else:
                        if not failed_opt:
                            g_nsrt = nsrt_plan[nsrt_counter]
                            gop_str = g_nsrt.ground_option_str()
                            if not self.succ_optn_dict[gop_str].has_states():
                                self.succ_optn_dict[gop_str].assign_values(
                                    g_nsrt.option_objs,
                                    g_nsrt.parent.option_vars,
                                    g_nsrt.option
                                )
                            self.succ_optn_dict[gop_str].append_state(
                                env.get_observation())
                            nsrt_counter += 1
                else:
                    g_nsrt = nsrt_plan[0]
                    gop_str = g_nsrt.ground_option_str()
                    if not self.fail_optn_dict[gop_str].has_states():
                        self.fail_optn_dict[gop_str].assign_values(
                            g_nsrt.option_objs,
                            g_nsrt.parent.option_vars,
                            g_nsrt.option,
                            error=e
                        )
                    self.fail_optn_dict[gop_str].append_state(
                        env.get_observation())
                    break

        return state
        # final_state = state
        # return final_state, task_str


    # def _create_interpretation_prompt(self, pred: AnnotatedPredicate, idx: int) -> str:
    #     with open('./prompts/interpret_0.prompt', 'r') as file:
    #         template = file.read()
    #     text_prompt = template.replace('[INSERT_QUERY_HERE]', pred.__str__())

    #     # Save the text prompt
    #     with open(f'./prompts/interpret_1_cover_{idx}_{pred.name}_text.prompt', 'w') \
    #         as file:
    #         file.write(text_prompt)
        
    #     text_entry = {
    #         "type": "text",
    #         "text": text_prompt
    #     }
    #     prompt = [{
    #         "role": "user",
    #         "content": text_entry
    #     }]

    #     # Convert the prompt to JSON string
    #     prompt_json = json.dumps(prompt, indent=2)
    #     with open(f'./prompts/interpret_2_cover_{idx}_{pred.name}.prompt', 'w') \
    #         as file:
    #         file.write(str(prompt_json))
    #     return prompt

    # def _parse_classifier_response(self, response: str) -> str:
    #     # Define the regex pattern to match Python code block
    #     pattern = r'```python(.*?)```'
        
    #     # Use regex to find the Python code block in the response
    #     match = re.search(pattern, response, re.DOTALL)
        
    #     # If a match is found, return the Python code block
    #     if match:
    #         return match.group(1).strip()
        
    #     # If no match is found, return an empty string
    #     return ''

    # def _parse_predicate_signature_predictions(self, response: str) -> Set[Predicate]:

    #     # Regular expression to match the predicate format
    #     pattern = r"`(.*?)` -- (.*?)\n"
    #     matches = re.findall(pattern, response)

    #     # Create a list of AnnotatedPredicate instances
    #     predicates = []
    #     for match in matches:
    #         pred_str = match[0]
    #         description = match[1]
    #         name = pred_str.split('(')[0]
    #         args = pred_str.split('(')[1].replace(')', '').split(', ')
    #         types = [self._type_dict[arg.split(':')[1]] for arg in args]
    #         predicate = AnnotatedPredicate(name=name, types=types, 
    #                                        description=description,
    #                                        _classifier=None)
    #         predicates.append(predicate)
    #     for pred in predicates: 
    #         logging.info(pred)
    #     return predicates

    # def option_positive_negative_states_str(self,
    #                       succ_option_dict: Dict, 
    #                       fail_option_dict: Dict,
    #                     ) -> str:
    #     # Set the print options
    #     np.set_printoptions(precision=1)

    #     # a "success" dictionary of NSRT: List[executable states]
    #     # a "failure" dictionary of NSRT, List[(non-executable state, error)]
    #     task_str = []
    #     for ground_opt, obj_states in succ_option_dict.items():
    #         # task_str.append(
    #         #     f"The precondition of {str(nsrt)} \nwas satisfied on the " +
    #         #     "following states and the corresponding option was " +
    #         #     "successfully executed until termination:\n")
    #         task_str.append(
    #             f"The ground option {ground_opt} was initilized successfully"+
    #             " by a ground NSRT on the following states and was executed "+
    #             "successfully until termination:\n")
    #         states = obj_states['states']
    #         for state in states:
    #             task_str.append(state.dict_str() + "\n")

    #     task_str.append("But some options failed to execute.")
    #     # task_str.append("The agent failed to solve the following NSRTs:")
    #     for ground_opt, obj_states in fail_option_dict.items():
    #         # task_str.append(
    #         #     f"The precondition of {str(nsrt)} \n was satisfied on the " +
    #         #     "following states but the corresponding option " +
    #         #     "failed to execute due to these errors: \n")
    #         task_str.append(
    #             f"The ground option {ground_opt} was initialized according "+
    #              "to the task plan on the following states but failed to "+
    #              "execute until successful termination due errors (listed "+
    #              "under the initial state: \n")
    #         states = obj_states['states']
    #         for state, error in states:
    #             task_str.append("The option was initialized on:\n"+
    #                             state.dict_str() + "\n" +
    #                             "Error Encountered: " + str(error) + "\n")

    #     return '\n'.join(task_str)

    # def _get_option_states_str(self, succ_optn_dict: Dict, fail_optn_dict: Dict,
    #                                     init: bool=False) -> str:
    #     result_str = []
    #     max_examples_num = 5
    #     sum_tp, sum_fn, sum_tn, sum_fp = 0, 0, 0, 0
    #     ground_options =\
    #         set(succ_optn_dict.keys()) | set(fail_optn_dict.keys())

    #     for g_optn in ground_options:
    #         succ_states = succ_optn_dict[g_optn]['states']
    #         fail_states = fail_optn_dict[g_optn]['states']
    #         n_succ_states, n_fail_states = len(succ_states), len(fail_states)
    #         n_tot = n_succ_states + n_fail_states

    #         # Get the tp, fn, tn, fp states for each ground_option
    #         tp_states, fn_states, tn_states, fp_states = [], [], [], []
    #         if init:
    #             # Initially, all the succ states are true positives by the 
    #             # classification of the preconditions; while all the fail states
    #             # are false positives.
    #             tp_states = succ_states
    #             fp_states = fail_states
    #         else:
    #             # Filter out the tp, fn states from succ_option_dict
    #             if succ_states:
    #                 optn = succ_optn_dict[g_optn]['option']
    #                 grounding = succ_optn_dict[g_optn]['grounding']
    #                 ground_nsrts = [utils.all_ground_nsrts(nsrt, grounding)
    #                                 for nsrt in self._nsrts if nsrt.option==optn]
    #                 ground_nsrts = [nsrt for nsrt_list in ground_nsrts 
    #                                 for nsrt in nsrt_list]
    #                 for state in succ_states:
    #                     atom_state = utils.abstract(state, 
    #                                                 self._get_current_predicates())
    #                     if any([nsrt.preconditions.issubset(atom_state) for nsrt in 
    #                                 ground_nsrts]):
    #                         tp_states.append(state)
    #                     else:
    #                         fn_states.append(state)
    #             # filter out the tn, fp states
    #             if fail_states:
    #                 optn = fail_optn_dict[g_optn]['option']
    #                 grounding = fail_optn_dict[g_optn]['grounding']
    #                 ground_nsrts = [utils.all_ground_nsrts(nsrt, grounding)
    #                                 for nsrt in self._nsrts if nsrt.option==optn]
    #                 ground_nsrts = [nsrt for nsrt_list in ground_nsrts
    #                                 for nsrt in nsrt_list]
    #                 for state in fail_states:
    #                     atom_state = utils.abstract(state,
    #                                                 self._get_current_predicates())
    #                     if any([nsrt.preconditions.issubset(atom_state) for nsrt in 
    #                                 ground_nsrts]):
    #                         fp_states.append(state)
    #                     else:
    #                         tn_states.append(state)
                
    #         # Convert the states to string
    #         n_tp, n_fn = len(tp_states), len(fn_states)
    #         n_tn, n_fp = len(tn_states), len(fp_states)
    #         sum_tp, sum_fn = sum_tp+n_tp, sum_fn+n_fn
    #         sum_tn, sum_fp = sum_tn+n_tn, sum_fp+n_fp
    #         tp_state_str = set([s.dict_str() for s in tp_states])
    #         fn_state_str = set([s.dict_str() for s in fn_states])
    #         tn_state_str = set([s.dict_str() for s in tn_states])
    #         fp_state_str = set([s.dict_str() for s in fp_states])
    #         uniq_n_tp, uniq_n_fn = len(tp_state_str), len(fn_state_str)
    #         uniq_n_tn, uniq_n_fp = len(tn_state_str), len(fp_state_str)

    #         # GT Positive
    #         if n_succ_states:
    #             result_str.append(
    #             f"Ground option {g_optn} was applied on {n_tot} states and "+
    #             f"*successfully* executed on {n_succ_states}/{n_tot} states "+
    #             "(ground truth positive states).")
    #             # True Positive
    #             if n_tp:
    #                 result_str.append(
    #                 f"Out of the {n_succ_states} GT positive states, "+
    #                 f"with the current predicates and operators, "+
    #                 f"{n_tp}/{n_succ_states} states *satisfy* at least one of its "+
    #                 "operators' precondition (true positives)"+
    #                 (f", to list {max_examples_num}:" if 
    #                     uniq_n_tp > max_examples_num else ":"))
    #                 for i, state_str in enumerate(tp_state_str): 
    #                     if i == max_examples_num: break
    #                     result_str.append(state_str+'\n')
    #                 # result_str.append("\n")

    #             # False Negative
    #             if n_fn:
    #                 result_str.append(
    #                 f"Out of the {n_succ_states} GT positive states, "+
    #                 f"with the current predicates and operators, "+
    #                 f"{n_fn}/{n_succ_states} states *no longer satisfy* any of its "+
    #                 "operators' precondition (false negatives)"+
    #                 (f", to list {max_examples_num}:" if 
    #                     uniq_n_fn > max_examples_num else ":"))
    #                 for i, state_str in enumerate(fn_state_str): 
    #                     if i == max_examples_num: break
    #                     result_str.append(state_str+'\n')
    #                 # result_str.append("\n")

    #         # GT Negative
    #         if n_fail_states:
    #             result_str.append(
    #             f"Ground option {g_optn} was applied on {n_tot} states and "+
    #             f"*failed* to executed on {n_fail_states}/{n_tot} states "+
    #             "(ground truth negative states).")
    #             if n_fp:
    #                 # False Positive
    #                 result_str.append(
    #                 f"Out of the {n_fail_states} GT negative states, "+
    #                 f"with the current predicates and operators, "+
    #                 f"{n_fp}/{n_fail_states} states *satisfy* at least one of its "+
    #                 "operators' precondition (false positives)"+
    #                 (f", to list {max_examples_num}:" if 
    #                     uniq_n_fp > max_examples_num else ":"))
    #                 for i, state_str in enumerate(fp_state_str):
    #                     if i == max_examples_num: break
    #                     result_str.append(state_str+'\n')
    #                 # result_str.append("\n")

    #             if n_tn:
    #                 # True Negative
    #                 result_str.append(
    #                 f"Out of the {n_fail_states} GT negative states, "+
    #                 f"with the current predicates and operators, "+
    #                 f"{n_tn}/{n_fail_states} states *no longer satisfy* any of its "+
    #                 "operators' precondition (true negatives)"+
    #                 (f", to list {max_examples_num}:" if 
    #                     uniq_n_tn > max_examples_num else ":"))
    #                 for i, state_str in enumerate(tn_state_str):
    #                     if i == max_examples_num: break
    #                     result_str.append(state_str+'\n')
    #                 # result_str.append("\n")

    #     print_confusion_matrix(sum_tp, sum_tn, sum_fp, sum_fn)
    #     return '\n'.join(result_str)

    # def _interaction_summary_str(self, 
    #                              succ_optn_dict, 
    #                              fail_optn_dict, 
    #                              include_traj_str: bool) -> str:
    #     result_str = []
    #     max_example_num = 5
    #     sum_tp, sum_fp = 0, 0
    #     ground_options =\
    #         set(succ_optn_dict.keys()) | set(fail_optn_dict.keys())

    #     for g_optn in ground_options:
    #         # Get the tp, fn, tn, fp states for each ground_option
    #         succ_states = succ_optn_dict[g_optn]['states']
    #         fail_states = fail_optn_dict[g_optn]['states']
    #         n_succ_states, n_fail_states = len(succ_states), len(fail_states)

    #         tp_states, fp_states = succ_states, fail_states
    #         n_tp, n_fp = n_succ_states, n_fail_states
            
    #         sum_tp += n_tp
    #         sum_fp += n_fp
    #         tp_state_str = set([s.dict_str() for s in tp_states])
    #         fp_state_str = set([s.dict_str() for s in fp_states])

    #         if not include_traj_str and tp_states:
    #             result_str.append(
    #                 f"Ground option {g_optn} successfully executed on "+
    #                 f"{n_succ_states} states (true positives)" +
    #                 f", to list {max_example_num}:" if n_tp > max_example_num 
    #                     else ":")
    #             for i, state_str in enumerate(tp_state_str):
    #                 if i == max_example_num: break
    #                 result_str.append(f"{state_str}\n")
    #         if fp_states:
    #             result_str.append(f"Ground option {g_optn} was initialized "+
    #                 f"but failed to execute on {n_fail_states} states (false positives)"+
    #                 f", to list {max_example_num}:" if n_fp > max_example_num 
    #                     else ":")
    #             for i, state_str in enumerate(fp_state_str):
    #                 if i == max_example_num: break
    #                 result_str.append(f"{state_str}\n")

    #     # Print a confusion matrix table based on sum_tp and sum_fp
    #     print_confusion_matrix(sum_tp, 0, sum_fp, 0)
    #     return '\n'.join(result_str)

    # def _create_refinement_prompt(self, env: BaseEnv,
    #                               ite: int,
    #                               ) -> str:
    #     # Read the template
    #     with open(f'./prompts/invent_1.outline', 'r') as file:
    #         template = file.read()

    #     #### Meta
    #     # Structure classes
    #     with open('./prompts/class_definitions.py', 'r') as f:
    #         struct_str = f.read()
    #     template = template.replace('[STRUCT_DEFINITION]',
    #                                 add_python_quote(struct_str))

    #     #### Environment
    #     # self.env_source_code = getsource(env.__class__)
    #     # Type Initialization
    #     type_init_str = add_python_quote(
    #         self._env_type_str(self.env_source_code))
    #     template = template.replace("[TYPES_IN_ENV]",  type_init_str)

    #     # Initial Predicates
    #     init_predicate_str = self._init_predicate_str(self.env_source_code)
    #     template = template.replace("[PREDICATES_IN_ENV]", init_predicate_str)

    #     # Previously Invented Predicates
    #     new_predicate_str = self._invented_predicate_str(ite)
    #     template = template.replace("[INVENTED_PREDICATES]", new_predicate_str)

    #     # Options
    #     options_str = set()
    #     for nsrt in self._nsrts:
    #         options_str.add(nsrt.option_str())
    #     options_str = '\n'.join(list(options_str))
    #     template = template.replace("[OPTIONS_IN_ENV]", options_str)

    #     # NSRTS
    #     nsrt_str = []
    #     for nsrt in self._nsrts:
    #         nsrt_str.append(str(nsrt).replace("NSRT-", ""))
    #     template = template.replace("[NSRTS_IN_ENV]", '\n'.join(nsrt_str))

    #     # Interaction result
    #     # Add a atomic states for succ_optn_dict and fail_optn_dict
    #     _, _, _, _, summary_str = utils.count_classification_result_for_ops(
    #                                     self._nsrts,
    #                                     self.succ_optn_dict,
    #                                     self.fail_optn_dict,
    #                                     return_str=True,
    #                                     print_cm=True,
    #                                 )
    #     template = template.replace("[OPERATOR_PERFORMACE]", summary_str)

    #     # Save the text prompt
    #     with open(f'./prompts/invent_{self.env_name}_{ite}.prompt', 'w') as\
    #         file:
    #         file.write(template)

    #     prompt = template
    #     return prompt