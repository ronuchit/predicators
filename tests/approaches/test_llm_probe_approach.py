"""Test cases for the LLM probe approach."""

import shutil

import pytest

from predicators.src import utils
from predicators.src.approaches import ApproachFailure
from predicators.src.approaches.llm_probe_approach import LLMProbeApproach
from predicators.src.approaches.oracle_approach import OracleApproach
from predicators.src.datasets import create_dataset
from predicators.src.envs import create_new_env
from predicators.src.llm_interface import LargeLanguageModel


def test_llm_probe_approach():
    """Tests for LLMProbeApproach()."""
    env_name = "pddl_easy_delivery_procedural_tasks"
    cache_dir = "_fake_llm_cache_dir"
    utils.reset_config({
        "env": env_name,
        "llm_prompt_cache_dir": cache_dir,
        "approach": "llm_probe",
        "num_train_tasks": 1,
        "num_test_tasks": 1,
        "strips_learner": "oracle",
        "sesame_task_planning_heuristic": "hff"
    })
    env = create_new_env(env_name)
    train_tasks = env.get_train_tasks()
    approach = LLMProbeApproach(env.predicates, env.options, env.types,
                                   env.action_space, train_tasks)
    assert approach.get_name() == "llm_probe"
    # Test "learning", i.e., constructing the prompt prefix.
    dataset = create_dataset(env, train_tasks, env.options)
    assert not approach._prompt_prefix  # pylint: disable=protected-access
    approach.learn_from_offline_dataset(dataset)
    assert approach._prompt_prefix  # pylint: disable=protected-access

    # Create a mock LLM so that we can control the outputs.

    class _MockLLM(LargeLanguageModel):

        def __init__(self):
            self.response = None

        def get_id(self):
            return f"dummy-{hash(self.response)}"

        def _sample_completions(self,
                                prompt,
                                temperature,
                                seed,
                                num_completions=1):
            del prompt, temperature, seed, num_completions  # unused
            return [self.response]

    llm = _MockLLM()
    approach._llm = llm  # pylint: disable=protected-access

    # Test successful usage, where the LLM output corresponds to a plan.
    task_idx = 0
    task = train_tasks[task_idx]
    oracle = OracleApproach(env.predicates, env.options, env.types,
                            env.action_space, train_tasks)
    oracle.solve(task, timeout=500)
    last_plan = oracle.get_last_plan()
    option_to_str = approach._option_to_str  # pylint: disable=protected-access
    # Options and NSRTs are 1:1 for this test / environment.
    ideal_response = "\n".join(map(option_to_str, last_plan))
    # Add an empty line to the ideal response, should be no problem.
    ideal_response = "\n" + ideal_response
    llm.response = ideal_response
    # Run the approach.
    policy = approach.solve(task, timeout=500)
    traj, _ = utils.run_policy(policy,
                               env,
                               "train",
                               task_idx,
                               task.goal_holds,
                               max_num_steps=1000)
    assert task.goal_holds(traj.states[-1])
    ideal_metrics = approach.metrics
    approach.reset_metrics()

    # If the LLM response is garbage, we should still find a plan that achieves
    # the goal, because we will just fall back to regular planning.
    llm.response = "garbage"
    policy = approach.solve(task, timeout=500)
    traj, _ = utils.run_policy(policy,
                               env,
                               "train",
                               task_idx,
                               task.goal_holds,
                               max_num_steps=1000)
    assert task.goal_holds(traj.states[-1])
    worst_case_metrics = approach.metrics
    approach.reset_metrics()

    # If the LLM response is almost perfect, it should be very helpful for
    # planning guidance.
    llm.response = "\n".join(ideal_response.split("\n")[:-1])
    policy = approach.solve(task, timeout=500)
    traj, _ = utils.run_policy(policy,
                               env,
                               "train",
                               task_idx,
                               task.goal_holds,
                               max_num_steps=1000)
    assert task.goal_holds(traj.states[-1])
    almost_ideal_metrics = approach.metrics
    worst_case_nodes = worst_case_metrics["total_num_nodes_created"]
    almost_ideal_nodes = almost_ideal_metrics["total_num_nodes_created"]
    ideal_nodes = ideal_metrics["total_num_nodes_created"]
    assert worst_case_nodes > almost_ideal_nodes
    assert almost_ideal_nodes > ideal_nodes

    shutil.rmtree(cache_dir)
