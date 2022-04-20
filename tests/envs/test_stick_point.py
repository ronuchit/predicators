"""Test cases for the stick point environment."""

import numpy as np

from predicators.src import utils
from predicators.src.envs.stick_point import StickPointEnv
from predicators.src.structs import Action, Task


def test_stick_point():
    """Tests for StickPointEnv()."""
    utils.reset_config({
        "env": "stick_point",
        "stick_point_num_points_train": [2],
    })
    env = StickPointEnv()
    for task in env.get_train_tasks():
        for obj in task.init:
            assert len(obj.type.feature_names) == len(task.init[obj])
    for task in env.get_test_tasks():
        for obj in task.init:
            assert len(obj.type.feature_names) == len(task.init[obj])
    assert len(env.predicates) == 6
    assert len(env.goal_predicates) == 1
    assert {pred.name for pred in env.goal_predicates} == {"Touched"}
    assert len(env.options) == 3
    assert len(env.types) == 3
    point_type, robot_type, stick_type = sorted(env.types)
    assert point_type.name == "point"
    assert robot_type.name == "robot"
    assert stick_type.name == "stick"
    assert env.action_space.shape == (4, )
    # Create a custom initial state, with the robot in the middle, one point
    # reachable on the left, one point out of the reachable zone in the middle,
    # and the stick on the right at a 45 degree angle.
    state = env.get_train_tasks()[0].init.copy()
    robot, = state.get_objects(robot_type)
    stick, = state.get_objects(stick_type)
    points = state.get_objects(point_type)
    assert len(points) == 2
    robot_x = (env.rz_x_ub + env.rz_x_lb) / 2
    state.set(robot, "x", robot_x)
    state.set(robot, "y", (env.rz_y_ub + env.rz_y_lb) / 2)
    state.set(robot, "theta", np.pi / 2)
    reachable_point, unreachable_point = points
    reachable_x = (env.rz_x_ub + env.rz_x_lb) / 4
    state.set(reachable_point, "x", reachable_x)
    state.set(reachable_point, "y", (env.rz_y_ub + env.rz_y_lb) / 2)
    unreachable_x = robot_x
    state.set(unreachable_point, "x", unreachable_x)
    unreachable_y = 0.75 * env.y_ub
    assert not env.rz_y_lb <= unreachable_y <= env.rz_y_ub
    state.set(unreachable_point, "y", unreachable_y)
    stick_x = 3 * (env.rz_x_ub + env.rz_x_lb) / 4
    state.set(stick, "x", stick_x)
    state.set(stick, "y", (env.rz_y_ub + env.rz_y_lb) / 4)
    state.set(stick, "theta", np.pi / 4)
    task = Task(state, task.goal)
    env.render_state(state, task)

    ## Test simulate ##

    # Test for going to touch the reachable point.
    num_steps_to_left = int(np.ceil((robot_x - reachable_x) / env.max_speed))
    action_arrs = [
        np.array([-1.0, 0.0, 0.0, 1.0], dtype=np.float32)
        for _ in range(num_steps_to_left)
    ]

    policy = utils.action_arrs_to_policy(action_arrs)
    traj = utils.run_policy_with_simulator(policy,
                                           env.simulate,
                                           task.init,
                                           lambda _: False,
                                           max_num_steps=len(action_arrs))
    assert traj.states[-2].get(reachable_point, "touched") < 0.5
    assert traj.states[-1].get(reachable_point, "touched") > 0.5

    # Test for going to pick up the stick.
    pick_x = stick_x + 0.2  # figuring out this constant generally is a pain
    num_steps_to_right = int(np.ceil((pick_x - reachable_x) / env.max_speed))
    action_arrs.extend([
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        for _ in range(num_steps_to_right)
    ])

    # The stick should now be held.
    policy = utils.action_arrs_to_policy(action_arrs)
    traj = utils.run_policy_with_simulator(policy,
                                           env.simulate,
                                           task.init,
                                           lambda _: False,
                                           max_num_steps=len(action_arrs))
    assert traj.states[-2].get(stick, "held") < 0.5
    assert traj.states[-1].get(stick, "held") > 0.5

    # Test for rotating the stick.
    assert env.max_angular_speed >= np.pi / 4
    action_arrs.append(np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32))

    # The stick should now be rotated. The actual amount is complicated.
    policy = utils.action_arrs_to_policy(action_arrs)
    traj = utils.run_policy_with_simulator(policy,
                                           env.simulate,
                                           task.init,
                                           lambda _: False,
                                           max_num_steps=len(action_arrs))
    assert abs(traj.states[-2].get(stick, "theta") - np.pi / 4) < 1e-6
    assert traj.states[-1].get(stick, "theta") > np.pi / 4 + 1e-6

    # Test for moving and pressing the unreachable point with the stick.
    robot_x = traj.states[-1].get(robot, "x")
    num_steps_to_left = int(np.ceil((robot_x - unreachable_x) / env.max_speed))
    action_arrs.extend([
        np.array([-1.0, 0.0, 0.0, 1.0], dtype=np.float32)
        for _ in range(num_steps_to_left)
    ])
    # Move up and slightly left while pressing.
    action_arrs.extend([
        np.array([-0.1, 1.0, 0.0, 1.0], dtype=np.float32),
        np.array([0.0, 1.0, 0.0, 1.0], dtype=np.float32),
    ])

    # The unreachable point should now be touched.
    policy = utils.action_arrs_to_policy(action_arrs)
    traj = utils.run_policy_with_simulator(policy,
                                           env.simulate,
                                           task.init,
                                           lambda _: False,
                                           max_num_steps=len(action_arrs))
    assert traj.states[-2].get(unreachable_point, "touched") < 0.5
    assert traj.states[-1].get(unreachable_point, "touched") > 0.5

    # Uncomment for debugging.
    # policy = utils.action_arrs_to_policy(action_arrs)
    # monitor = utils.SimulateVideoMonitor(task, env.render_state)
    # traj = utils.run_policy_with_simulator(policy,
    #                                        env.simulate,
    #                                        task.init,
    #                                        lambda _: False,
    #                                        max_num_steps=len(action_arrs),
    #                                        monitor=monitor)
    # video = monitor.get_video()
    # outfile = "hardcoded_actions_stick_point.mp4"
    # utils.save_video(outfile, video)

    ## Test options ##

    PickStick, RobotTouchPoint, StickTouchPoint = sorted(env.options)
    assert PickStick.name == "PickStick"
    assert RobotTouchPoint.name == "RobotTouchPoint"
    assert StickTouchPoint.name == "StickTouchPoint"

    # Test RobotTouchPoint.
    option = RobotTouchPoint.ground([robot, reachable_point], [])

    policy = utils.option_plan_to_policy([option])
    traj = utils.run_policy_with_simulator(policy,
                                           env.simulate,
                                           task.init,
                                           lambda _: False,
                                           max_num_steps=1000,
                                           exceptions_to_break_on={utils.OptionExecutionFailure})
    assert traj.states[-2].get(reachable_point, "touched") < 0.5
    assert traj.states[-1].get(reachable_point, "touched") > 0.5

    # Uncomment for debugging.
    policy = utils.option_plan_to_policy([option])
    monitor = utils.SimulateVideoMonitor(task, env.render_state)
    traj = utils.run_policy_with_simulator(policy,
                                           env.simulate,
                                           task.init,
                                           lambda _: False,
                                           
                                           exceptions_to_break_on={utils.OptionExecutionFailure},
                                           monitor=monitor)
    video = monitor.get_video()
    outfile = "hardcoded_options_stick_point.mp4"
    utils.save_video(outfile, video)
