"""Tests for main.py."""
import os
import shutil
import sys
import tempfile
import time
from typing import Callable

import numpy as np
import pytest

import predicators.ground_truth_models
from predicators import utils
from predicators.approaches import ApproachFailure, ApproachTimeout, \
    BaseApproach, create_approach
from predicators.cogman import CogMan
from predicators.envs.cover import CoverEnv
from predicators.execution_monitoring import create_execution_monitor
from predicators.ground_truth_models import get_gt_options
from predicators.main import _run_episode, _run_testing, main
from predicators.perception import create_perceiver
from predicators.structs import Action, DefaultState, State, Task

_GROUND_TRUTH_MODULE_PATH = predicators.ground_truth_models.__name__


class _DummyFailureApproach(BaseApproach):
    """Dummy approach that raises ApproachFailure for testing."""

    @classmethod
    def get_name(cls) -> str:
        return "dummy_failure"

    @property
    def is_learning_based(self):
        return False

    def _solve(self, task: Task, timeout: int) -> Callable[[State], Action]:

        def _policy(s: State) -> Action:
            raise ApproachFailure("Option plan exhausted.")

        return _policy


class _DummySolveTimeoutApproach(BaseApproach):
    """Dummy approach that raises ApproachTimeout during planning for
    testing."""

    @classmethod
    def get_name(cls) -> str:
        return "dummy_solve_timeout"

    @property
    def is_learning_based(self):
        return False

    def _solve(self, task: Task, timeout: int) -> Callable[[State], Action]:
        raise ApproachTimeout("Planning timed out.")


class _DummyExecutionTimeoutApproach(BaseApproach):
    """Dummy approach that raises ApproachTimeout during execution for
    testing."""

    @classmethod
    def get_name(cls) -> str:
        return "dummy_execution_timeout"

    @property
    def is_learning_based(self):
        return False

    def _solve(self, task: Task, timeout: int) -> Callable[[State], Action]:

        def _policy(s: State) -> Action:
            raise ApproachTimeout("Policy timed out.")

        return _policy


class _DummyCoverEnv(CoverEnv):
    """Dummy cover environment that raises EnvironmentFailure for testing."""

    @classmethod
    def get_name(cls) -> str:
        return "dummy"

    def simulate(self, state, action):
        raise utils.EnvironmentFailure("", {"offending_objects": set()})


def test_main():
    """Tests for main.py."""
    utils.reset_config()
    sys.argv = [
        "dummy", "--env", "my_env", "--approach", "my_approach", "--seed",
        "123", "--num_test_tasks", "3"
    ]
    with pytest.raises(NotImplementedError):
        main()  # invalid env
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "my_approach", "--seed",
        "123", "--num_test_tasks", "3"
    ]
    with pytest.raises(NotImplementedError):
        main()  # invalid approach
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "random_actions", "--seed",
        "123", "--not-a-real-flag", "0"
    ]
    with pytest.raises(ValueError):
        main()  # invalid flag
    parent_dir = os.path.dirname(__file__)
    video_dir = os.path.join(parent_dir, "_fake_videos")
    results_dir = os.path.join(parent_dir, "_fake_results")
    eval_traj_dir = os.path.join(parent_dir, "_fake_trajs")
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "oracle", "--seed", "123",
        "--make_test_videos", "--num_test_tasks", "1", "--video_dir",
        video_dir, "--results_dir", results_dir, "--eval_trajectories_dir",
        eval_traj_dir
    ]
    main()
    # Test making videos of failures and local logging.
    temp_log_file = tempfile.NamedTemporaryFile(delete=False).name
    sys.argv = [
        "dummy", "--env", "painting", "--approach", "oracle", "--seed", "123",
        "--num_test_tasks", "1", "--video_dir", video_dir, "--results_dir",
        results_dir, "--eval_trajectories_dir", eval_traj_dir,
        "--sesame_max_skeletons_optimized", "1", "--painting_lid_open_prob",
        "0.0", "--make_failure_videos", "--log_file", temp_log_file
    ]
    main()
    shutil.rmtree(video_dir)
    shutil.rmtree(results_dir)
    shutil.rmtree(eval_traj_dir)
    # Run NSRT learning, but without sampler learning.
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "nsrt_learning", "--seed",
        "123", "--sampler_learner", "random", "--cover_initial_holding_prob",
        "0.0", "--num_train_tasks", "1", "--num_test_tasks", "1",
        "--experiment_id", "foobar"
    ]
    main()
    # Try loading approaches and data.
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "nsrt_learning", "--seed",
        "123", "--load_approach", "--load_data",
        "--cover_initial_holding_prob", "0.0", "--experiment_id", "foobar"
    ]
    main()
    # Try loading with a bad experiment id.
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "nsrt_learning", "--seed",
        "123", "--load_approach", "--cover_initial_holding_prob", "0.0",
        "--experiment_id", "baz"
    ]
    with pytest.raises(FileNotFoundError):
        main()
    # Try loading with load experiment id.
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "nsrt_learning", "--seed",
        "123", "--load_approach", "--cover_initial_holding_prob", "0.0",
        "--load_experiment_id", "foobar", "--experiment_id", "baz"
    ]
    main()
    # Run NSRT learning with option learning.
    sys.argv = [
        "dummy", "--env", "blocks", "--approach", "nsrt_learning", "--seed",
        "123", "--sampler_learner", "random", "--num_train_tasks", "1",
        "--num_test_tasks", "1", "--option_learner", "direct_bc",
        "--segmenter", "atom_changes", "--mlp_regressor_max_itr", "1"
    ]
    main()
    # Try running interactive approach with no online learning, to make sure
    # it doesn't crash. This is also an important test of the full pipeline
    # in the case where a goal predicate is excluded. No online learning occurs
    # because max number of transitions is set.
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "interactive_learning",
        "--seed", "123", "--num_online_learning_cycles", "1",
        "--online_learning_max_transitions", "0", "--excluded_predicates",
        "Covers", "--interactive_num_ensemble_members", "1",
        "--num_train_tasks", "3", "--num_test_tasks", "3",
        "--predicate_mlp_classifier_max_itr", "lambda n: n * 50"
    ]
    main()
    # Tests for --crash_on_failure flag.
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "oracle", "--seed", "123",
        "--num_test_tasks", "3", "--timeout", "0", "--crash_on_failure"
    ]
    with pytest.raises(ApproachTimeout) as e:
        main()  # should time out
    assert "Planning timed out in grounding!" in str(e)
    sys.argv = [
        "dummy", "--env", "cover", "--approach", "random_actions", "--seed",
        "123", "--num_test_tasks", "3", "--crash_on_failure"
    ]
    with pytest.raises(RuntimeError) as e:
        main()  # should fail to solve the task
    assert "Policy failed to reach goal" in str(e)


def test_bilevel_planning_approach_failure_and_timeout():
    """Test coverage for ApproachFailure and ApproachTimeout in
    run_testing()."""
    utils.reset_config({
        "env": "cover",
        "approach": "nsrt_learning",
        "timeout": 10,
        "make_test_videos": False,
        "num_test_tasks": 1,
    })
    env = CoverEnv()
    train_tasks = [t.task for t in env.get_train_tasks()]
    approach = _DummyFailureApproach(env.predicates,
                                     get_gt_options(env.get_name()), env.types,
                                     env.action_space, train_tasks)
    assert not approach.is_learning_based
    perceiver = create_perceiver("trivial")
    exec_monitor = create_execution_monitor("trivial")
    cogman = CogMan(approach, perceiver, exec_monitor)
    _run_testing(env, cogman)

    approach = _DummySolveTimeoutApproach(env.predicates,
                                          get_gt_options(env.get_name()),
                                          env.types, env.action_space,
                                          train_tasks)
    assert not approach.is_learning_based
    perceiver = create_perceiver("trivial")
    exec_monitor = create_execution_monitor("trivial")
    cogman = CogMan(approach, perceiver, exec_monitor)
    _run_testing(env, cogman)

    approach = _DummyExecutionTimeoutApproach(env.predicates,
                                              get_gt_options(env.get_name()),
                                              env.types, env.action_space,
                                              train_tasks)
    assert not approach.is_learning_based
    perceiver = create_perceiver("trivial")
    exec_monitor = create_execution_monitor("trivial")
    cogman = CogMan(approach, perceiver, exec_monitor)
    _run_testing(env, cogman)


def test_env_failure():
    """Test coverage for EnvironmentFailure in run_testing()."""
    utils.reset_config({
        "env": "cover",
        "approach": "random_actions",
        "timeout": 10,
        "make_test_videos": False,
        "cover_initial_holding_prob": 0.0,
        "num_test_tasks": 1,
    })
    cover_options = get_gt_options("cover")
    env = _DummyCoverEnv()
    train_tasks = [t.task for t in env.get_train_tasks()]
    approach = create_approach("random_actions", env.predicates, cover_options,
                               env.types, env.action_space, train_tasks)
    assert not approach.is_learning_based
    task = train_tasks[0]
    approach.solve(task, timeout=500)
    perceiver = create_perceiver("trivial")
    exec_monitor = create_execution_monitor("trivial")
    cogman = CogMan(approach, perceiver, exec_monitor)
    _run_testing(env, cogman)


def test_run_episode():
    """Tests for _run_episode()."""
    utils.reset_config({"env": "cover"})
    env = CoverEnv()
    train_tasks = [t.task for t in env.get_train_tasks()]
    cover_options = get_gt_options(env.get_name())
    task = env.get_task("test", 0)
    approach = create_approach("random_actions", env.predicates, cover_options,
                               env.types, env.action_space, train_tasks)
    perceiver = create_perceiver("trivial")
    exec_monitor = create_execution_monitor("trivial")
    cogman = CogMan(approach, perceiver, exec_monitor)
    cogman.reset(task)
    (states, actions), solved, metrics = _run_episode(cogman,
                                                      env,
                                                      "test",
                                                      0,
                                                      max_num_steps=5)
    assert not solved
    assert len(states) == 6
    assert len(actions) == 5
    assert "policy_call_time" in metrics
    assert metrics["policy_call_time"] > 0.0

    # Test exceptions_to_break_on.
    def _value_error_policy(_):
        raise ValueError("mock error")

    class _MockApproach:

        def __init__(self, policy):
            self._policy = policy

        def solve(self, task, timeout):
            """Just use the given policy."""
            del task, timeout  # unused
            return self._policy

    class _CountingMonitor(utils.LoggingMonitor):

        def __init__(self):
            self.num_observations = 0

        def observe(self, obs, action):
            self.num_observations += 1

    approach = _MockApproach(_value_error_policy)
    cogman = CogMan(approach, perceiver, exec_monitor)
    cogman.reset(task)

    with pytest.raises(ValueError) as e:
        _, _, _ = _run_episode(cogman, env, "test", 0, max_num_steps=5)
    assert "mock error" in str(e)

    monitor = _CountingMonitor()
    (states, _), _, _ = _run_episode(cogman,
                                     env,
                                     "test",
                                     0,
                                     max_num_steps=5,
                                     exceptions_to_break_on={ValueError},
                                     monitor=monitor)

    assert len(states) == 1
    assert monitor.num_observations == 1

    class _MockEnv:

        @staticmethod
        def reset(train_or_test, task_idx):
            """Reset the mock environment."""
            del train_or_test, task_idx  # unused
            return DefaultState

        @staticmethod
        def step(action):
            """Step the mock environment."""
            del action  # unused
            raise utils.EnvironmentFailure("mock failure")

        def get_observation(self):
            """Gets currrent observation in mock environment."""
            return DefaultState

        def goal_reached(self):
            """Goal never reached."""
            return False

    mock_env = _MockEnv()
    ones_policy = lambda _: Action(np.zeros(1, dtype=np.float32))
    approach = _MockApproach(ones_policy)
    cogman = CogMan(approach, perceiver, exec_monitor)
    cogman.reset(task)
    monitor = _CountingMonitor()
    (states, actions), _, _ = _run_episode(
        cogman,
        mock_env,
        "test",
        0,
        max_num_steps=5,
        exceptions_to_break_on={utils.EnvironmentFailure},
        monitor=monitor)
    assert len(states) == 1
    assert len(actions) == 0
    assert monitor.num_observations == 1

    # Test policy call time.
    def _policy(_):
        time.sleep(0.1)
        return Action(env.action_space.sample())

    approach = _MockApproach(_policy)
    cogman = CogMan(approach, perceiver, exec_monitor)
    cogman.reset(task)

    _, _, metrics = _run_episode(cogman, env, "test", 0, max_num_steps=3)
    assert metrics["policy_call_time"] >= 3 * 0.1

    # Test with monitor in case where an uncaught exception is raised.

    def _policy(_):
        raise ValueError("mock error")

    monitor = _CountingMonitor()
    approach = _MockApproach(_policy)
    cogman = CogMan(approach, perceiver, exec_monitor)
    cogman.reset(task)

    try:
        _run_episode(cogman,
                     mock_env,
                     "test",
                     0,
                     max_num_steps=3,
                     monitor=monitor)
    except ValueError:
        pass
    assert monitor.num_observations == 1
