"""Tests for methods in the BaseSTRIPSLearner class.
"""

from predicators.src.nsrt_learning.strips_learning.base_strips_learner import \
    BaseSTRIPSLearner
from predicators.src.structs import LowLevelTrajectory, Type, Task, \
    PartialNSRTAndDatastore, Predicate, Segment, State, STRIPSOperator
from predicators.src.utils import SingletonParameterizedOption


class MockBaseSTRIPSLearner(BaseSTRIPSLearner):
    """Mock class that exposes private methods for testing."""

    def recompute_datastores_from_segments(self, pnads):
        """Exposed for testing."""
        return self._recompute_datastores_from_segments(pnads)

    def _learn(self):
        raise Exception("Can't use this")

    @classmethod
    def get_name(cls) -> str:
        raise Exception("Can't use this")


def test_recompute_datastores_from_segments():
    """Tests for recompute_datastores_from_segments().
    """
    obj_type = Type("obj_type", ["feat"])
    Pred = Predicate("Pred", [obj_type], lambda s, o: s[o[0]][0] > 0.5)
    opt_name_to_opt = {"Act": SingletonParameterizedOption(
        "Act", lambda s, m, o, p: None)}
    obj = obj_type("obj")
    var = obj_type("?obj")
    state = State({obj: [1.0]})
    act = opt_name_to_opt["Act"].ground([], [])
    op1 = STRIPSOperator("Op1", [var], set(), {Pred([var])}, set(), set())
    pnad1 = PartialNSRTAndDatastore(op1, [], (act.parent, []))
    op2 = STRIPSOperator("Op2", [], set(), set(), set(), set())
    pnad2 = PartialNSRTAndDatastore(op2, [], (act.parent, []))
    traj = LowLevelTrajectory([state, state], [act], True, 0)
    task = Task(state, set())
    segment = Segment(traj, {Pred([obj])}, {Pred([obj])}, act)
    learner = MockBaseSTRIPSLearner([traj], [task], {Pred}, [[segment]])
    learner.recompute_datastores_from_segments([pnad1, pnad2])
    assert len(pnad1.datastore) == 0
    assert len(pnad1.datastore) == 0
