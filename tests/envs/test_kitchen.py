"""Test cases for the Kitchen environment."""

import numpy as np
import pytest

from predicators import utils
from predicators.envs.kitchen import KitchenEnv
from predicators.ground_truth_models import get_gt_nsrts, get_gt_options
from predicators.perception.kitchen_perceiver import KitchenPerceiver

longrun = pytest.mark.skipif("not config.getoption('longrun')")


@longrun
def test_kitchen():
    """Tests for kitchen env.

    Since the gym environment can be slow to initialize, we group all
    tests together.
    """
    utils.reset_config({
        "env": "kitchen",
        "num_train_tasks": 1,
        "num_test_tasks": 2,
        "kitchen_use_perfect_samplers": True,
    })
    env = KitchenEnv()
    perceiver = KitchenPerceiver()
    assert env.get_name() == "kitchen"
    assert perceiver.get_name() == "kitchen"
    for env_task in env.get_train_tasks():
        task = perceiver.reset(env_task)
        for obj in task.init:
            assert len(obj.type.feature_names) == len(task.init[obj])
    for env_task in env.get_test_tasks():
        task = perceiver.reset(env_task)
        for obj in task.init:
            assert len(obj.type.feature_names) == len(task.init[obj])
    assert len(env.predicates) == 5
    At, NotOnTop, OnTop, TurnedOff, TurnedOn = sorted(env.predicates)
    assert At.name == "At"
    assert NotOnTop.name == "NotOnTop"
    assert OnTop.name == "OnTop"
    assert TurnedOff.name == "TurnedOff"
    assert TurnedOn.name == "TurnedOn"
    assert env.goal_predicates == {OnTop, TurnedOn}
    options = get_gt_options(env.get_name())
    assert len(options) == 3
    moveto_option, pushobjonobjforward_option, pushobjturnonright_option = \
        sorted(options)
    assert moveto_option.name == "MoveTo"
    assert pushobjonobjforward_option.name == "PushObjOnObjForward"
    assert pushobjturnonright_option.name == "PushObjTurnOnLeftRight"
    assert len(env.types) == 2
    gripper_type, object_type = sorted(env.types)
    assert gripper_type.name == "gripper"
    assert object_type.name == "obj"
    assert env.action_space.shape == (7, )
    nsrts = get_gt_nsrts(env.get_name(), env.predicates, options)
    assert len(nsrts) == 3
    env_train_tasks = env.get_train_tasks()
    assert len(env_train_tasks) == 1
    env_test_tasks = env.get_test_tasks()
    assert len(env_test_tasks) == 2
    env_task = env_test_tasks[1]
    obs = env.reset("test", 1)
    assert all(
        np.allclose(m1, m2)
        for m1, m2 in zip(obs['state_info'].values(),
                          env_task.init_obs['state_info'].values()))
    imgs = env.render()
    assert len(imgs) == 1
    task = perceiver.reset(env_task)

    # Cover not implemented methods.
    with pytest.raises(NotImplementedError) as e:
        env.render_state_plt(obs, task)
    assert "This env does not use Matplotlib" in str(e)
    with pytest.raises(NotImplementedError) as e:
        env.render_state(obs, task)
    assert "A gym environment cannot render arbitrary states." in str(e)
    with pytest.raises(NotImplementedError) as e:
        env.simulate(obs, env.action_space.sample())
    assert "Simulate not implemented for gym envs." in str(e)

    # Test NSRTs.
    MoveTo, PushObjOnObjForward, PushObjTurnOnLeftRight = sorted(nsrts)
    assert MoveTo.name == "MoveTo"
    assert PushObjOnObjForward.name == "PushObjOnObjForward"
    assert PushObjTurnOnLeftRight.name == "PushObjTurnOnLeftRight"

    obs = env_test_tasks[0].init_obs
    init_state = env.state_info_to_state(obs["state_info"])
    rng = np.random.default_rng(123)

    obj_name_to_obj = {o.name: o for o in init_state}
    gripper = obj_name_to_obj["gripper"]
    knob4 = obj_name_to_obj["knob4"]
    kettle = obj_name_to_obj["kettle"]
    burner4 = obj_name_to_obj["burner4"]
    light = obj_name_to_obj["light"]

    def _run_ground_nsrt(ground_nsrt, state):
        for atom in ground_nsrt.preconditions:
            assert atom.holds(state)
        option = ground_nsrt.sample_option(state, set(), rng)
        assert option.initiable(state)
        for _ in range(1000):
            act = option.policy(state)
            obs = env.step(act)
            state = env.state_info_to_state(obs["state_info"])
            if option.terminal(state):
                break
        for atom in ground_nsrt.add_effects:
            assert atom.holds(state)
        for atom in ground_nsrt.delete_effects:
            assert not atom.holds(state)
        return state

    # Set up all the NSRTs for the following tests.
    move_to_light_nsrt = MoveTo.ground([gripper, light])
    push_light_nsrt = PushObjTurnOnLeftRight.ground([gripper, light])
    move_to_knob4_nsrt = MoveTo.ground([gripper, knob4])
    push_knob4_nsrt = PushObjTurnOnLeftRight.ground([gripper, knob4])
    move_to_kettle_nsrt = MoveTo.ground([gripper, kettle])
    push_kettle_on_burner4_nsrt = PushObjOnObjForward.ground(
        [gripper, kettle, burner4])

    # Test moving to and turning on the light.
    obs = env.reset("test", 0)
    state = env.state_info_to_state(obs["state_info"])
    assert state.allclose(init_state)
    state = _run_ground_nsrt(move_to_light_nsrt, state)
    state = _run_ground_nsrt(push_light_nsrt, state)
    assert TurnedOn([light]).holds(state)

    # Test moving to and pushing knob4, then moving to and pushing the kettle.
    obs = env.reset("test", 0)
    state = env.state_info_to_state(obs["state_info"])
    assert state.allclose(init_state)
    state = _run_ground_nsrt(move_to_knob4_nsrt, state)
    state = _run_ground_nsrt(push_knob4_nsrt, state)
    state = _run_ground_nsrt(move_to_kettle_nsrt, state)
    state = _run_ground_nsrt(push_kettle_on_burner4_nsrt, state)
    assert OnTop([kettle, burner4]).holds(state)
    assert TurnedOn([knob4]).holds(state)

    # Test reverse order: moving to and pushing the kettle, then moving to and
    # pushing knob4.
    obs = env.reset("test", 0)
    state = env.state_info_to_state(obs["state_info"])
    assert state.allclose(init_state)
    state = _run_ground_nsrt(move_to_kettle_nsrt, state)
    state = _run_ground_nsrt(push_kettle_on_burner4_nsrt, state)
    state = _run_ground_nsrt(move_to_knob4_nsrt, state)
    state = _run_ground_nsrt(push_knob4_nsrt, state)
    assert OnTop([kettle, burner4]).holds(state)
    assert TurnedOn([knob4]).holds(state)

    # Test light, kettle, then knob.
    obs = env.reset("test", 0)
    state = env.state_info_to_state(obs["state_info"])
    assert state.allclose(init_state)
    state = _run_ground_nsrt(move_to_light_nsrt, state)
    state = _run_ground_nsrt(push_light_nsrt, state)
    state = _run_ground_nsrt(move_to_kettle_nsrt, state)
    state = _run_ground_nsrt(push_kettle_on_burner4_nsrt, state)
    state = _run_ground_nsrt(move_to_knob4_nsrt, state)
    state = _run_ground_nsrt(push_knob4_nsrt, state)
    assert OnTop([kettle, burner4]).holds(state)
    assert TurnedOn([knob4]).holds(state)
    assert TurnedOn([light]).holds(state)

    # Test knob, light, kettle.
    obs = env.reset("test", 0)
    state = env.state_info_to_state(obs["state_info"])
    assert state.allclose(init_state)
    state = _run_ground_nsrt(move_to_knob4_nsrt, state)
    state = _run_ground_nsrt(push_knob4_nsrt, state)
    state = _run_ground_nsrt(move_to_light_nsrt, state)
    state = _run_ground_nsrt(push_light_nsrt, state)
    state = _run_ground_nsrt(move_to_kettle_nsrt, state)
    state = _run_ground_nsrt(push_kettle_on_burner4_nsrt, state)
    assert OnTop([kettle, burner4]).holds(state)
    assert TurnedOn([knob4]).holds(state)
    assert TurnedOn([light]).holds(state)
