"""Test cases for the active sampler learning approach."""
import pytest

from predicators import utils
from predicators.approaches.active_sampler_learning_approach import \
    ActiveSamplerLearningApproach
from predicators.approaches.base_approach import ApproachFailure
from predicators.datasets import create_dataset
from predicators.envs.cover import BumpyCoverEnv
from predicators.ground_truth_models import get_gt_options
from predicators.main import _generate_interaction_results
from predicators.settings import CFG
from predicators.structs import Dataset
from predicators.teacher import Teacher


@pytest.mark.parametrize("model_name,right_targets",
                         [("myopic_classifier", False),
                          ("myopic_classifier", True), ("fitted_q", False),
                          ("fitted_q", True)])
def test_active_sampler_learning_approach(model_name, right_targets):
    """Test for ActiveSamplerLearningApproach class, entire pipeline."""
    utils.reset_config({
        "env": "bumpy_cover",
        "approach": "active_sampler_learning",
        "active_sampler_learning_model": model_name,
        "timeout": 10,
        "strips_learner": "oracle",
        "sampler_learner": "oracle",
        "sampler_disable_classifier": True,
        "num_online_learning_cycles": 1,
        "max_num_steps_interaction_request": 4,
        "online_nsrt_learning_requests_per_cycle": 1,
        "sampler_mlp_classifier_max_itr": 10,
        "mlp_regressor_max_itr": 10,
        "num_train_tasks": 3,
        "num_test_tasks": 1,
        "explorer": "random_nsrts",
        "active_sampler_learning_num_samples": 5,
        "active_sampler_learning_fitted_q_iters": 2,
        "active_sampler_learning_num_next_option_samples": 2,
        "bumpy_cover_right_targets": right_targets,
    })
    env = BumpyCoverEnv()
    train_tasks = [t.task for t in env.get_train_tasks()]
    approach = ActiveSamplerLearningApproach(env.predicates,
                                             get_gt_options(env.get_name()),
                                             env.types, env.action_space,
                                             train_tasks)
    dataset = create_dataset(env, train_tasks, get_gt_options(env.get_name()))
    assert approach.is_learning_based
    # Learning with an empty dataset should not crash.
    approach.learn_from_offline_dataset(Dataset([]))
    # Learning with the actual dataset.
    approach.learn_from_offline_dataset(dataset)
    approach.load(online_learning_cycle=None)
    interaction_requests = approach.get_interaction_requests()
    teacher = Teacher(train_tasks)
    interaction_results, _ = _generate_interaction_results(
        env, teacher, interaction_requests)
    approach.learn_from_interaction_results(interaction_results)
    approach.load(online_learning_cycle=0)
    with pytest.raises(FileNotFoundError):
        approach.load(online_learning_cycle=1)
    for task in env.get_test_tasks():
        policy = approach.solve(task, timeout=CFG.timeout)
        # We won't fully check the policy here because we don't want
        # tests to have to train very good models, since that would
        # be slow. But we will test that the policy at least produces
        # an action.
        action = policy(task.init)
        assert env.action_space.contains(action.arr)
