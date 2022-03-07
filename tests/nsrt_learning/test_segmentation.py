"""Tests for low-level trajectory segmentation."""

from gym.spaces import Box
import numpy as np
import pytest
from predicators.src.nsrt_learning.segmentation import segment_trajectory
from predicators.src.structs import Type, Predicate, State, Action, \
    LowLevelTrajectory, ParameterizedOption
from predicators.src import utils


def test_segment_trajectory():
    """Tests for segment_trajectory()."""
    utils.reset_config({"segmenter": "option_changes"})
    cup_type = Type("cup_type", ["feat1"])
    cup0 = cup_type("cup0")
    cup1 = cup_type("cup1")
    cup2 = cup_type("cup2")
    pred0 = Predicate("Pred0", [cup_type], lambda s, o: s[o[0]][0] > 0.5)
    pred1 = Predicate("Pred1", [cup_type, cup_type],
                      lambda s, o: s[o[0]][0] > 0.5)
    pred2 = Predicate("Pred2", [cup_type], lambda s, o: s[o[0]][0] > 0.5)
    preds = {pred0, pred1, pred2}
    state0 = State({cup0: [0.4], cup1: [0.7], cup2: [0.1]})
    atoms0 = utils.abstract(state0, preds)
    state1 = State({cup0: [0.8], cup1: [0.3], cup2: [1.0]})
    atoms1 = utils.abstract(state1, preds)
    # Tests with known options.
    param_option = utils.SingletonParameterizedOption(
        "Dummy",
        lambda s, m, o, p: Action(p),
        types=[cup_type],
        params_space=Box(0.1, 1, (1, )),
    )
    option0 = param_option.ground([cup0], np.array([0.2]))
    assert option0.initiable(state0)
    action0 = option0.policy(state0)
    # The option changes, but the option spec stays the same. Want to segment.
    # Note that this is also a test for the case where the final option
    # terminates in the final state.
    option1 = param_option.ground([cup0], np.array([0.1]))
    assert option1.initiable(state0)
    action1 = option1.policy(state0)
    option2 = param_option.ground([cup1], np.array([0.1]))
    assert option2.initiable(state0)
    action2 = option2.policy(state0)
    trajectory = (LowLevelTrajectory([state0.copy() for _ in range(5)],
                                     [action0, action1, action2, action0]),
                  [atoms0, atoms0, atoms0, atoms0, atoms0])
    known_option_segments = segment_trajectory(trajectory)
    assert len(known_option_segments) == 4
    # Test case where the final option does not terminate in the final state.
    infinite_param_option = ParameterizedOption(
        "InfiniteDummy",
        types=[cup_type],
        params_space=Box(0.1, 1, (1, )),
        policy=lambda s, m, o, p: Action(p),
        initiable=lambda s, m, o, p: True,
        terminal=lambda s, m, o, p: False,
    )
    infinite_option = infinite_param_option.ground([cup0], np.array([0.2]))
    states = [state0.copy() for _ in range(5)]
    infinite_option.initiable(states[0])
    actions = [infinite_option.policy(s) for s in states[:-1]]
    trajectory = (LowLevelTrajectory(states, actions),
                  [atoms0, atoms0, atoms0, atoms0, atoms0])
    assert len(segment_trajectory(trajectory)) == 0
    # Tests without known options.
    action0 = option0.policy(state0)
    action0.unset_option()
    action1 = option0.policy(state0)
    action1.unset_option()
    action2 = option1.policy(state0)
    action2.unset_option()
    trajectory = (LowLevelTrajectory([state0.copy() for _ in range(5)],
                                     [action0, action1, action2, action0]),
                  [atoms0, atoms0, atoms0, atoms0, atoms0])
    # Should crash, because the option_changes segmenter assumes that options
    # are known.
    with pytest.raises(AssertionError):
        segment_trajectory(trajectory)
    # Segment with atoms changes instead.
    utils.reset_config({"segmenter": "atom_changes"})
    assert len(segment_trajectory(trajectory)) == 0
    trajectory = (LowLevelTrajectory(
        [state0.copy() for _ in range(5)] + [state1],
        [action0, action1, action2, action0, action1]),
                  [atoms0, atoms0, atoms0, atoms0, atoms0, atoms1])
    unknown_option_segments = segment_trajectory(trajectory)
    assert len(unknown_option_segments) == 1
    assert len(unknown_option_segments[0].actions) == 5
    # Test unknown segmenter.
    utils.reset_config({"segmenter": "not a real segmenter"})
    with pytest.raises(NotImplementedError):
        segment_trajectory(trajectory)
    # Return for use by test_strips_learning.
    return known_option_segments, unknown_option_segments
